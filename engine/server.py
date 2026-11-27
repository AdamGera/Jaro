import argparse
import asyncio
import gc
import json
import hmac
import socket
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from collections import deque
from contextlib import AsyncExitStack, aclosing, asynccontextmanager, nullcontext, suppress
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_hex
from threading import Thread
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import meeting_brief
import meeting_edits
import session_images
import annotation_retention
import annotation_storage
import meeting_exports
from session_store import SHUTDOWN_INTERRUPTION, MissingSession, SessionStore, open_library
from annotation_assets import MAX_TIMELINE_SECONDS, annotation_movie_info, read_staged_annotation, staged_annotation_paths
from diagnostics import PROCESS_LOG, log_directory, maintain_logs, open_log
from native_recordings import capture_active, is_meeting_stage, list_drafts, meeting_title_path, normalize_meeting_title, portable_meeting_drafts, read_meeting_title, read_wav_header, recording_marker, recording_metadata, recover_wav, recover_tracks, source_tracks
from managed_preferences import (ManagedFeatureError, annotate_managed, meetings_managed, opencode_managed,
                                 require_annotation, require_meetings, require_opencode)
from dictation_output import (MAX_PROFILE_CORRECTIONS, MAX_PROFILE_DOCUMENT, MAX_REWRITE_DEADLINE, MAX_VOCABULARY, correction_rules,
                              parse_portable_profiles, profile_for, rewrite_deadline, rewrite_locally, validate_dictation_vocabulary,
                              vocabulary_terms)
from meeting_rolling import MIXED_SPEAKERS, SOURCE_LABELS, append_rolling, read_rolling, rolling_path
from meeting_retention import readable_source
from meeting_transcript import FRAME as MEETING_FRAME, RATE as MEETING_RATE, read_window, merge_sources
from saved_dictations import shared_vocabulary
from saved_meetings import export_name, get_meeting, latest_transcript, meeting_detail, read_portable_live_meeting, render_export, saved_meeting_clipboard
from sdk_discovery import SDK_MAX_REQUESTS, SDK_MAX_UPLOAD, SDK_VERSION, publish_sdk, remove_sdk, sdk_proof

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect

from audio_file import (
    DURATION_ERROR, FILE_ERROR, MAX_AUDIO_SECONDS, PAUSE_AWARE_CHUNKING, audio_blocks, audio_duration,
    codec_binary, decimate_pcm16_to_16k_f32, dictation_has_signal, make_playable, open_audio_pcm,
)
from live_preview import (
    CommitAction, LiveWindowPlanner, SkipAction, TentativeAction, finish_live_preview, read_wav_i16_slice, trim_boundary_overlap,
)
from rust_speech import RustSpeechClient, start_bundled_speech, stop_speech_process
from insights import Insights
from local_qwen import writing_model_installed
from dictation_input import CleanupFailed, MIC_GAIN, StuPASECleanup, boost_microphone, replace_mixed_microphone
from portable_audio_control import AudioBehavior
from portable_dictation_sounds import SoundStyle
from model_installation import install_workspace_models, model_status
from writing_api import router as writing_routes
import windows_dictation_commands
import workspace_drafts
from rumble_model import (
    DIARIZATION_ID, DIARIZATION_REVISION, REVISION, LANGUAGES, MAX_SAMPLES, MODEL_ID, SAMPLE_RATE,
    RumbleModel, diarization_installed, prepare_model,
)

from engine_lifecycle import (
    HOST, PORT, URL, API_VERSION, ClientLeases, begin_draining, desktop_control_path, engine_proof, install_token, library_id, library_path,
    open_browser_workspace, running_engine, startup_lock,
)
from engine_paths import WORKSPACE_ROOT

DIST = WORKSPACE_ROOT / "dist"
MAX_BYTES = MAX_SAMPLES * 4
PRIVATE_MEDIA_HEADERS = {
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}
LANGUAGE_OPTIONS = [
    {"code": code, "name": name}
    for code, name in sorted(LANGUAGES.items(), key=lambda language: language[1])
]
IDLE_CACHE_TRIM_SECONDS = 15.0
IDLE_DIARIZER_UNLOAD_SECONDS = 300.0
logger = logging.getLogger("uvicorn.error")


class SpeechInference:
    """Serialize access to Rumble and let an already queued dictation go first."""

    def __init__(self):
        self.condition = asyncio.Condition()
        self.active = False
        self.waiting_dictation = 0

    async def acquire(self, *, dictation=False):
        async with self.condition:
            self.waiting_dictation += int(dictation)
            try:
                await self.condition.wait_for(
                    lambda: not self.active and (dictation or not self.waiting_dictation)
                )
                self.active = True
            finally:
                self.waiting_dictation -= int(dictation)
                if dictation and not self.active:
                    self.condition.notify_all()

    async def release(self):
        async with self.condition:
            self.active = False
            self.condition.notify_all()

    @asynccontextmanager
    async def turn(self, *, dictation=False):
        await self.acquire(dictation=dictation)
        try:
            yield
        finally:
            await self.release()


async def load_model(app: FastAPI):
    try:
        app.state.model = await asyncio.to_thread(RumbleModel)
    except Exception:
        logger.exception("Could not load Rumble.")
        app.state.status = "error"
    else:
        app.state.diarizer = None
        await load_speaker_tools(app.state)
        app.state.status = "ready"


def diarization_ready(state):
    return state.diarizer is not None or bool(getattr(state, "diarizer_lazy", False))


async def ensure_diarizer(state):
    if state.diarizer is not None:
        state.diarizer_last_used = time.monotonic()
        return state.diarizer
    if not getattr(state, "diarizer_lazy", False) or getattr(state, "model", None) is None:
        return None
    lock = getattr(state, "diarizer_lock", None)
    if lock is None:
        lock = state.diarizer_lock = asyncio.Lock()
    async with lock:
        if state.diarizer is not None:
            state.diarizer_last_used = time.monotonic()
            return state.diarizer
        if not getattr(state, "diarizer_lazy", False) or getattr(state, "model", None) is None:
            return None
        try:
            from diarization import Diarizer

            state.diarizer = await run_in_threadpool(Diarizer, state.model.device)
        except Exception:
            state.diarizer_lazy = False
            logger.exception("Speaker labeling could not start. Run Setup Jaro to install its models.")
            return None
        state.diarizer_last_used = time.monotonic()
        return state.diarizer


async def load_speaker_tools(state):
    if state.diarizer is None:
        state.diarizer_lazy = diarization_installed()
        if not state.diarizer_lazy:
            logger.info("Speaker labeling models are not installed yet.")
    if state.model.aligner is None:
        try:
            await run_in_threadpool(state.model.enable_alignment)
        except Exception:
            logger.exception("Word timing could not start. Run Setup Jaro to install alignment weights.")


def additional_models_ready(state):
    return diarization_ready(state) and state.model.aligner is not None and writing_model_installed()


def _trim_device_cache(state):
    state.cache_trim_handle = None
    if getattr(state, "draining", False):
        return
    inference = getattr(state, "inference", None)
    if inference is not None and (inference.active or inference.waiting_dictation):
        return
    model = getattr(state, "model", None)
    release = getattr(model, "release_device_cache", None)
    if callable(release):
        try:
            release()
        except Exception:
            logger.debug("Could not release idle device cache.", exc_info=True)


def schedule_cache_trim(state):
    if getattr(state, "draining", False):
        return
    model = getattr(state, "model", None)
    if not callable(getattr(model, "release_device_cache", None)):
        return
    if handle := getattr(state, "cache_trim_handle", None):
        handle.cancel()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    state.cache_trim_handle = loop.call_later(IDLE_CACHE_TRIM_SECONDS, _trim_device_cache, state)


async def maybe_unload_idle_diarizer(state):
    if state.diarizer is None or not getattr(state, "diarizer_lazy", False) or getattr(state, "draining", False):
        return
    if (getattr(state, "jobs", None) or getattr(state, "submitting", None) or getattr(state, "live_speakers", None)
            or getattr(state, "portable_recording", None)):
        return
    inference = getattr(state, "inference", None)
    if inference is not None and (inference.active or inference.waiting_dictation):
        return
    if time.monotonic() - getattr(state, "diarizer_last_used", 0.0) < IDLE_DIARIZER_UNLOAD_SECONDS:
        return
    lock = getattr(state, "diarizer_lock", None)
    if lock is None or lock.locked():
        return
    async with lock:
        if state.diarizer is None or getattr(state, "live_speakers", None) or getattr(state, "jobs", None):
            return
        state.diarizer = None
        gc.collect()
        release = getattr(getattr(state, "model", None), "release_device_cache", None)
        if callable(release):
            release()


async def _run_prewarm(state):
    try:
        model = getattr(state, "model", None)
        prewarm = getattr(model, "prewarm", None)
        if not callable(prewarm):
            return
        is_warm = getattr(model, "is_warm", None)
        if callable(is_warm) and is_warm():
            return
        async with state.inference.turn(dictation=False):
            if getattr(state, "draining", False) or state.inference.waiting_dictation:
                return
            if callable(is_warm) and is_warm():
                return
            started = time.monotonic()
            warmed = await run_in_threadpool(prewarm)
            if warmed:
                logger.info("Prewarmed speech model in %d ms.", round((time.monotonic() - started) * 1000))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Background speech model prewarm failed.", exc_info=True)
    finally:
        if getattr(state, "prewarm_task", None) is asyncio.current_task():
            state.prewarm_task = None


def rust_speech_client(state) -> RustSpeechClient | None:
    client = getattr(state, "rust_speech", None)
    if client is None and not getattr(state, "rust_speech_checked", False):
        state.rust_speech_checked = True
        client = RustSpeechClient.from_env()
        state.rust_speech = client
    return client


def schedule_prewarm(state) -> bool:
    if getattr(state, "draining", False):
        return False
    rust = rust_speech_client(state)
    if rust is not None:
        current = getattr(state, "prewarm_task", None)
        if current is not None and not current.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        state.prewarm_task = loop.create_task(rust.prewarm())
        return True
    if getattr(state, "status", None) != "ready":
        return False
    model = getattr(state, "model", None)
    if not callable(getattr(model, "prewarm", None)):
        return False
    is_warm = getattr(model, "is_warm", None)
    if callable(is_warm) and is_warm():
        return False
    inference = getattr(state, "inference", None)
    if inference is None or inference.active or inference.waiting_dictation:
        return False
    current = getattr(state, "prewarm_task", None)
    if current is not None and not current.done():
        return True
    state.prewarm_task = asyncio.create_task(_run_prewarm(state))
    return True


async def load_speech(app):
    app.state.status = "loading"
    async with app.state.insights.model.audio_priority():
        await load_model(app)


async def start_workspace_speech(app):
    state = app.state
    await load_speech(app)
    speech_ready = state.status == "ready"
    if speech_ready:
        if ready := getattr(state, "portable_shortcut_ready", None):
            ready.set()
        if additional_models_ready(state):
            state.model_setup = None
            return
    try:
        installed = await asyncio.to_thread(model_status, all_models=True)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        logger.exception("Could not check the desktop model installer.")
        state.model_setup = {"phase": "unavailable"}
    else:
        state.model_setup = {"phase": "load-error" if installed["ready"] else "needed", "total": installed["bytes"]}
    if speech_ready:
        state.model_setup["additional"] = True


async def setup_workspace_speech(app, *, repair=False):
    state = app.state

    def report(completed, total):
        if type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0:
            state.model_setup.update(completed=completed, total=total)

    try:
        await install_workspace_models(report, repair=repair)
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError):
        logger.exception("Could not install the desktop model set.")
        state.model_setup["phase"] = "download-error"
        return
    state.model_setup["phase"] = "starting"
    if state.model_setup.get("additional"):
        async with state.inference.turn():
            await load_speaker_tools(state)
        if additional_models_ready(state):
            state.model_setup = None
        else:
            state.model_setup["phase"] = "load-error"
        return
    await load_speech(app)
    if state.status == "ready":
        if additional_models_ready(state):
            state.model_setup = None
        else:
            state.model_setup.update(phase="load-error", additional=True)
        if ready := getattr(state, "portable_shortcut_ready", None):
            ready.set()
    else:
        state.model_setup["phase"] = "load-error"


async def open_portable_dictation(state, root):
    try:
        if sys.platform == "win32":
            from windows_dictation import WindowsDictation as Dictation, helper_path as supported
        elif os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            from wayland_dictation import WaylandDictation as Dictation, supported
        else:
            from x11_dictation import X11Dictation as Dictation, supported
    except ImportError:
        logger.info("The system dictation adapter is unavailable.", exc_info=True)
        return None
    if not supported():
        return None
    from portable_dictation_history import PortableDictationHistory

    history = PortableDictationHistory(root)
    try:
        await run_in_threadpool(history.initialize)
    except (OSError, ValueError, sqlite3.Error):
        logger.exception("Portable dictation history could not open.")
    dictation = Dictation(state, transcribe_portable_dictation, history)
    if dictation.noise_cleanup:
        stupase_cleanup(state).prepare()
    return dictation


async def enable_portable_shortcut(dictation, *, restoring=False):
    if dictation.platform == "windows":
        await dictation.enable()
    elif dictation.platform == "wayland":
        dictation.enable(restoring=restoring)
    else:
        dictation.enable()


async def restore_portable_shortcut(state):
    from portable_capture import CaptureError

    await state.portable_shortcut_ready.wait()
    dictation = state.portable_dictation
    async with dictation.control:
        if state.status != "ready" or state.draining:
            return
        if dictation.platform == "wayland" and dictation.command_mode:
            dictation.restore_paste()
        elif dictation.shortcut_requested and not dictation.enabled and not dictation.authorizing:
            try:
                await enable_portable_shortcut(dictation, restoring=True)
            except CaptureError as failure:
                dictation.feedback = str(failure)


async def load_bundled_speech(state):
    if rust_speech_client(state) is not None:
        return
    try:
        bundled = await start_bundled_speech(Path(__file__).resolve().parents[1], state.library.root)
        if bundled:
            state.rust_speech, state.rust_speech_process = bundled
            logger.info("Bundled Rust dictation ready with Rumble2 Flash.")
    except (OSError, ValueError, RuntimeError, TimeoutError):
        logger.warning("Bundled Rust speech could not start. Dictation will use the local model.", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.status = "loading"
    app.state.inference = SpeechInference()
    app.state.diarizer = None
    app.state.diarizer_lazy = False
    app.state.diarizer_lock = asyncio.Lock()
    app.state.diarizer_last_used = 0.0
    app.state.prewarm_task = None
    app.state.schedule_prewarm = lambda: schedule_prewarm(app.state)
    app.state.cache_trim_handle = None
    app.state.stupase_cleanup = None
    app.state.portable_screencast = None
    app.state.portable_annotation_shortcut = None
    app.state.desktop_annotation = None
    app.state.windows_dictation_control = None
    app.state.draining = False
    app.state.screenshot_pending = False
    app.state.live_speakers = {}
    app.state.meeting_subscriber = None
    app.state.sessions_revision = token_hex(16)
    root = library_path()
    app.state.jobs = set()
    portable = getattr(app.state, "portable_workspace", False)
    app.state.model_setup = {"phase": "checking"} if portable else None
    app.state.portable_shortcut_ready = asyncio.Event() if portable else None
    linux_audio = portable and sys.platform == "linux"
    windows_audio = portable and sys.platform == "win32"
    windows_screen = False
    recorder = None
    meeting_recorder = None
    browser_pages = None
    if linux_audio:
        from linux_capture import microphone_users, playback_users
        from linux_call_pages import browser_call_pages
        from linux_meeting_audio import LinuxMeetingRecording, linux_recording

        recorder = linux_recording
        meeting_recorder = LinuxMeetingRecording
        browser_pages = browser_call_pages
    elif windows_audio:
        from windows_capture import WindowsMeetingRecording, WindowsRecording, browser_call_pages
        from windows_capture import helper_path, microphone_users, playback_users
        from windows_screencast import helper_path as windows_screen_helper

        if helper_path() is not None:
            recorder = WindowsRecording
            meeting_recorder = WindowsMeetingRecording
            browser_pages = browser_call_pages
            windows_screen = windows_screen_helper() is not None
    with open_library(root) as library:
        app.state.library = library
        app.state.screen_draft_dir = None
        app.state.portable_recorder = recorder
        app.state.portable_meeting_recorder = meeting_recorder
        app.state.portable_recording = None
        app.state.portable_dictation = None
        app.state.portable_onboarding = None
        app.state.portable_sdk_capture = None
        if portable:
            app.state.portable_recording_lock = asyncio.Lock()
        if recorder is not None:
            from portable_sdk_capture import PortableSDKCapture

            app.state.portable_recording_dir = root / "microphone-drafts"
            app.state.portable_recording_dir.mkdir(mode=0o700, exist_ok=True)
            app.state.portable_dictation = await open_portable_dictation(app.state, root)
            if app.state.portable_dictation is not None:
                from portable_onboarding import PortableOnboarding

                app.state.portable_onboarding = PortableOnboarding(root)
            app.state.portable_sdk_capture = PortableSDKCapture(
                root, recorder, lambda path, languages: transcribe_sdk_microphone(app.state, path, languages))
        app.state.native_token = os.urandom(32).hex() if portable else install_token(root)
        app.state.staging = root / "staging"
        app.state.staging.mkdir(mode=0o700, exist_ok=True)
        if linux_audio or windows_screen:
            app.state.screen_draft_dir = root / "screen-drafts"
            app.state.screen_draft_dir.mkdir(mode=0o700, exist_ok=True)
        annotation_platform = getattr(app.state.portable_dictation, "platform", None)
        if annotation_platform and not annotate_managed():
            from portable_annotation_shortcut import AnnotationShortcut, notice_helper

            if linux_audio:
                from linux_screencast import supported

                annotation_ready = supported() and (annotation_platform == "wayland" or notice_helper())
            else:
                annotation_ready = windows_screen and notice_helper()
            if annotation_ready:
                async def start_annotation(cancelled):
                    if cancelled is not None:
                        await discard_screen_take(app.state, cancelled.draft.key)
                    await start_desktop_annotation(app.state, "display", require_shortcut=True)

                async def finish_annotation(take):
                    await finish_screen_take(app.state, take.draft.key, ())

                app.state.portable_annotation_shortcut = AnnotationShortcut(app.state, start_annotation, finish_annotation)
                if annotation_platform == "x11":
                    from x11_annotation import X11AnnotationBinding

                    app.state.desktop_annotation = X11AnnotationBinding(app.state.portable_annotation_shortcut,
                                                                        app.state.portable_dictation.history)
                elif annotation_platform == "windows":
                    from windows_annotation import WindowsAnnotationBinding

                    app.state.desktop_annotation = WindowsAnnotationBinding(app.state.portable_annotation_shortcut,
                                                                            app.state.portable_dictation)
                if app.state.desktop_annotation:
                    await app.state.desktop_annotation.restore()
        app.state.submitting = set()
        app.state.annotation_expiry_due = time.monotonic() + 90
        app.state.annotation_expiry = None
        app.state.sdk_pending = 0
        if not portable:
            app.state.sdk = None
        app.state.leases = ClientLeases()
        managed_meetings = meetings_managed()
        app.state.insights = Insights(library, meetings_managed=managed_meetings)
        if not portable:
            start_job(app.state, publish_meeting_exports(app.state))
        loading = asyncio.create_task(start_workspace_speech(app) if portable else load_speech(app))
        state = app.state
        state.startup_recovery = False
        state.portable_calls = None
        state.annotation_panel = None
        background = []
        call_tasks = []

        if portable and state.screen_draft_dir and not annotate_managed() and not opencode_managed():
            from portable_annotation_panel import PortableAnnotationPanel, annotation_panel_helper

            async def load_annotation(key):
                return await run_in_threadpool(library.get, key)

            async def deliver_annotation(key, target, service, authorize):
                from annotation_routing import RouteUnavailable, send_annotation

                require_annotation()
                active = getattr(state, "annotation_handoff_job", None)
                if state.draining or (active is not None and not active.done()):
                    raise RouteUnavailable("Another annotation operation is in progress.")
                state.annotation_handoff_job = start_job(state, send_annotation(root, key, target, service, authorize=authorize))
                return await state.annotation_handoff_job

            async def undo_automatic_delivery(delivery):
                return await run_portable_annotation_handoff(state, PortableAnnotationHandoff(action="undo", id=delivery))

            if helper := annotation_panel_helper():
                state.annotation_panel = PortableAnnotationPanel(helper, root, load_annotation, deliver_annotation, undo_automatic_delivery)
                background.append(asyncio.create_task(resume_annotation_routes(state.annotation_panel)))

        async def finish_call(key):
            await finish_portable_capture(state, key, (), diarization=True)

        if meeting_recorder and not managed_meetings:
            from portable_calls import PROVIDERS, CallWatcher, recording_label
            from portable_call_surface import watch_desktop_calls

            async def start_call(languages, call, *, title=""):
                source = PROVIDERS.get(call.source)
                binaries = source.binaries if windows_audio and source else ()
                reply = await begin_portable_recording(state, "both", True, languages,
                                                       provider=recording_label(call), call_binaries=binaries, title=title)
                return reply["id"]

            state.portable_calls = CallWatcher(state, microphone_users, playback_users, start_call, finish_call,
                                              lambda: state.status == "ready" and not portable_recording_busy(state),
                                              browser_pages=browser_pages)
            call_tasks = [asyncio.create_task(state.portable_calls.run()),
                          asyncio.create_task(watch_desktop_calls(state.portable_calls))]
        if portable:
            tray_directory = Path(os.environ.get("JARO_DESKTOP_TRAY", ""))
            if tray_directory.is_absolute() and tray_directory.is_dir():
                background.append(asyncio.create_task(watch_closed_portable_window(state, tray_directory)))
            if (tray_directory.is_absolute() and tray_directory.is_dir()
                    and (state.portable_calls is not None or getattr(state, "portable_updater", None) is not None)):
                from portable_tray import MeetingTray

                async def restart_from_tray(updater):
                    await restart_from_portable_tray(state, updater, (tray_directory / "window-pid").is_file)

                tray = MeetingTray(state, tray_directory, lambda: portable_recording_busy(state),
                                   lambda: portable_has_live_transcript(state) or latest_portable_meeting(library),
                                   lambda: copy_latest_portable_meeting(state),
                                   restart=restart_from_tray, meetings=state.portable_calls is not None,
                                   shutdown_busy=lambda: engine_work_in_progress(state) or portable_work_in_progress(state))
                call_tasks.append(asyncio.create_task(tray.run()))
        background.append(asyncio.create_task(watch_retention(app)))
        if not portable:
            background.append(asyncio.create_task(watch_idle(app)))
        if updater := getattr(state, "portable_updater", None):
            background.append(asyncio.create_task(updater.watch()))
        if state.portable_dictation is not None:
            background.append(asyncio.create_task(expire_portable_dictations(state.portable_dictation.history)))
        if not portable or recorder is not None:
            # Snapshotted at launch so automatic recovery never imports a new take.
            drafts = state.portable_recording_dir if portable else state.staging
            state.startup_stranded = {marker.stem for marker in drafts.glob("*.recording")} if not managed_meetings else set()
            state.startup_recovery = bool(state.startup_stranded or
                                          library.has_pending_recordings(include_meetings=not managed_meetings))
            background.append(asyncio.create_task(resume_recordings(state)))
        command_listener = None
        speech_startup = None
        shortcut_restore = None
        sdk_discovery = None
        try:
            if portable:
                try:
                    sdk_discovery = publish_sdk(root, state.sdk["port"], token=state.sdk["token"])
                    state.sdk.update(sdk_discovery)
                except OSError:
                    logger.warning("Could not publish the desktop SDK endpoint.", exc_info=True)
            elif getattr(state, "sdk_port", None):
                state.sdk = sdk_discovery = publish_sdk(root, state.sdk_port)
            if windows_audio:
                try:
                    state.windows_dictation_control = windows_dictation_commands.publish(root, state.sdk)
                except OSError:
                    logger.warning("Could not publish the Windows desktop command endpoint.", exc_info=True)
            if linux_audio:
                from wayland_dictation_commands import command_prefix, listen

                try:
                    dictation = state.portable_dictation
                    command_listener = await listen(root, dictation.run_command if dictation else None,
                                                    trash_meeting=lambda key: trash_meeting_from_linux_command(state, key),
                                                    meeting_command=lambda payload: run_linux_meeting_command(state, payload),
                                                    annotation_command=lambda payload: run_linux_annotation_command(state, payload),
                                                    onboarding=lambda: run_linux_onboarding_command(state))
                    if dictation and dictation.platform == "wayland":
                        await dictation.restore_command_preference(command_prefix(root), root)
                except OSError:
                    logger.warning("Could not start the Linux desktop command listener.", exc_info=True)
            if state.portable_dictation is not None:
                shortcut_restore = asyncio.create_task(restore_portable_shortcut(state))
            speech_startup = asyncio.create_task(load_bundled_speech(state))
            yield
        finally:
            if command_record := state.windows_dictation_control:
                state.windows_dictation_control = None
                with suppress(OSError):
                    windows_dictation_commands.remove(command_record)
            for task in call_tasks:
                task.cancel()
            await asyncio.gather(*call_tasks, return_exceptions=True)
            if shortcut_restore is not None:
                shortcut_restore.cancel()
                await asyncio.gather(shortcut_restore, return_exceptions=True)
            if desktop_annotation := state.desktop_annotation:
                await desktop_annotation.close()
            if annotation := state.portable_annotation_shortcut:
                await annotation.close()
            if screencast := state.portable_screencast:
                try:
                    await screencast.shutdown()
                except OSError:
                    logger.warning("Could not finalize the interrupted screen recording.", exc_info=True)
            if command_listener is not None:
                command_listener.close()
            if app.state.portable_dictation is not None:
                await app.state.portable_dictation.close()
            if state.portable_sdk_capture is not None:
                await state.portable_sdk_capture.close()
            if command_listener is not None:
                await command_listener.wait_closed()
            if capture := state.portable_recording:
                try:
                    with suppress(OSError):
                        await capture.stop()
                finally:
                    if claim := getattr(capture, "meeting_claim", None):
                        claim.close()
            if sdk_discovery is not None:
                remove_sdk(root, sdk_discovery)
            state.sdk = None
            if handle := getattr(state, "cache_trim_handle", None):
                handle.cancel()
                state.cache_trim_handle = None
            if prewarm := getattr(state, "prewarm_task", None):
                prewarm.cancel()
                await asyncio.gather(prewarm, return_exceptions=True)
                state.prewarm_task = None
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            if state.annotation_panel:
                await state.annotation_panel.close()
            jobs = list(app.state.jobs)
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            if portable:
                from annotation_agents import close

                await close()
            if speech_startup is not None:
                if not speech_startup.done():
                    speech_startup.cancel()
                await asyncio.gather(speech_startup, return_exceptions=True)
            if speech := getattr(state, "rust_speech", None):
                await speech.close()
            if process := getattr(state, "rust_speech_process", None):
                await stop_speech_process(process)
            if cleanup := getattr(state, "stupase_cleanup", None):
                await cleanup.close()
            await app.state.insights.close()
            await loading


async def resume_annotation_routes(panel):
    try:
        await panel.automation.resume()
    except (OSError, sqlite3.Error):
        logger.warning("Could not resume automatic annotation deliveries.", exc_info=True)


async def watch_retention(app):
    next_expiry = 0
    while True:
        await asyncio.sleep(5)
        state = app.state
        now = time.monotonic()
        if now >= next_expiry and not state.draining:
            next_expiry = now + 3600
            start_job(state, sweep_meeting_retention(state))
        if (now >= state.annotation_expiry_due and not state.draining and not annotate_managed()
                and (state.annotation_expiry is None or state.annotation_expiry.done())):
            state.annotation_expiry_due = now + 3600
            state.annotation_expiry = start_job(state, sweep_annotation_retention(state))
        await maybe_unload_idle_diarizer(state)


async def watch_idle(app):
    while True:
        await asyncio.sleep(5)
        state = app.state
        busy = (state.jobs or state.submitting or state.sdk_pending or getattr(state, "screenshot_pending", False)
                or state.insights.active_job or state.insights.chat_count
                or state.insights.model.audio_users or state.insights.wake.is_set())
        if state.leases.idle(busy) and getattr(state, "request_shutdown", None):
            state.request_shutdown()
            return


async def watch_closed_portable_window(state, directory):
    closed, tray_ready = directory / "workspace-closed", directory / "ready"
    while not state.draining:
        await asyncio.sleep(.25)
        if (closed.is_file() and not tray_ready.is_file() and not state.leases.streams
                and not engine_work_in_progress(state) and not portable_work_in_progress(state)):
            begin_draining(state)
            state.request_shutdown()
            return


async def sweep_meeting_retention(state):
    try:
        await publish_meeting_exports(state)
        failures = await run_in_threadpool(state.library.expire_meetings)
        if failures:
            logger.warning("%d meeting archives are still waiting for the Trash.", len(failures))
        audio_waiting = await run_in_threadpool(state.library.expire_meeting_audio)
        if audio_waiting:
            logger.warning("%d meeting recordings are still waiting for the Trash.", len(audio_waiting))
    except (OSError, sqlite3.Error):
        logger.exception("Could not apply meeting retention.")


async def sweep_annotation_retention(state):
    try:
        _, failures = await run_in_threadpool(annotation_retention.sweep, state.library)
        if failures:
            logger.warning("%d annotations are still waiting for the Trash.", len(failures))
    except (OSError, sqlite3.Error):
        logger.exception("Could not apply annotation retention.")


async def publish_meeting_exports(state, *, key=None):
    try:
        await meeting_exports.flush_async(state.library, key=key)
    except (OSError, sqlite3.Error):
        logger.exception("Could not process managed meeting exports.")


async def resume_recordings(state):
    if not state.startup_recovery:
        return
    portable = getattr(state, "portable_workspace", False)

    def available():
        capture_lock = getattr(state, "portable_recording_lock", None)
        return (state.status == "ready" and not state.draining and not state.jobs and not state.submitting
                and not (portable and portable_capture_busy(state)) and not (capture_lock and capture_lock.locked())
                and not state.insights.model.audio_users and not state.inference.waiting_dictation
                and not getattr(state, "is_shutting_down", lambda: False)())

    try:
        while True:
            await asyncio.sleep(2)
            if state.status == "error" and not portable:
                return
            if not available():
                continue
            try:
                if ((not portable and await run_in_threadpool(capture_active, state.staging))
                        or not available()):
                    continue
                if await asyncio.shield(start_job(state, recover_pending_recording(state, state.startup_stranded))):
                    continue
                # A disconnected drive may leave a deferred claim. Check for its
                # return without holding the update guard open between checks.
                state.startup_recovery = False
                if not await run_in_threadpool(state.library.has_pending_recordings, include_meetings=not meetings_managed()):
                    return
            except (OSError, sqlite3.Error, RuntimeError):
                logger.exception("Could not resume pending recording finalization.")
    finally:
        state.startup_recovery = False


async def recover_pending_recording(state, stranded):
    if getattr(state, "portable_workspace", False) and portable_capture_busy(state):
        return True
    # The SQLite claim is synchronous and short so cancellation cannot consume
    # an attempt before the worker has taken responsibility for it.
    include_meetings = not meetings_managed()
    pending = state.library.claim_recording_finalization(include_meetings=include_meetings)
    if pending is None:
        return include_meetings and await recover_stranded_meeting(state, stranded)
    state.startup_recovery = True
    key, options = pending["id"], pending["options"]
    source = pending["source"]
    tracks = ({name: source.parent / f"{name}.wav" for name in ("mic", "system")}
              if options.get("sourceSpeakerTurns") else None)
    await process_saved_audio(state, key, source, options, pending["duration"], tracks=tracks,
                              checkpoint=pending["next_frame"], recovered=True)
    return True


async def recover_stranded_meeting(state, stranded):
    if not stranded:
        return False
    if getattr(state, "portable_workspace", False):
        drafts = await run_in_threadpool(lambda: list(portable_meeting_drafts(state.portable_recording_dir)))
        draft = min((draft for draft in drafts if draft["id"] in stranded),
                    key=lambda draft: (draft["modified"], draft["id"]), default=None)
        if draft is None:
            return False
        key = draft["id"]
        try:
            if await finish_portable_capture(state, UUID(key), (), diarization=True, live=True, recovering=True) is None:
                return True
        except (HTTPException, OSError):
            logger.warning("Could not import interrupted meeting %s. Its files remain available for recovery.", key)
        stranded.discard(key)
        return True

    import fcntl

    drafts = await run_in_threadpool(list_drafts, state.staging)
    candidates = [draft for draft in reversed(drafts) if draft["meeting"] and draft["id"] in stranded]
    if not candidates:
        # If a recorder acquired its claim while we scanned, its draft could still become available.
        return await run_in_threadpool(capture_active, state.staging)
    draft = candidates[0]
    key = draft["id"]
    try:
        with recording_marker(Path(draft["path"])).open("rb") as claim:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Validate the same stage and import it under the recorder's existing UUID.
            await submit_staged_audio(state, UUID(key), (), recording=True, diarization=True,
                                      name="Recovered meeting", mime="audio/wav", live=True, recover=True)
    except BlockingIOError:
        return True
    except FileNotFoundError:
        pass
    except (HTTPException, OSError):
        logger.warning("Could not import interrupted meeting %s. Its files remain available for recovery.", key)
    stranded.discard(key)
    return True


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
sdk_router = APIRouter(prefix="/api/sdk/v1")
app.include_router(writing_routes)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST])


@app.get("/api/status")
async def status(request: Request, challenge: str = Query("", max_length=64)):
    proof = engine_proof(request.app.state.native_token, challenge) if re.fullmatch(r"[0-9a-f]{32}", challenge) else None
    return {
        "libraryId": library_id(request.app.state.library.root), "proof": proof,
        "status": request.app.state.status, "model": MODEL_ID, "languages": LANGUAGE_OPTIONS, "apiVersion": API_VERSION,
        "diarization": diarization_ready(request.app.state),
        **({"modelSetup": setup} if (setup := getattr(request.app.state, "model_setup", None)) else {}),
    }


@app.post("/api/portable/models", status_code=202)
async def start_model_setup(request: Request):
    state = request.app.state
    if not getattr(state, "portable_workspace", False):
        raise HTTPException(404, "Desktop model setup is unavailable.")
    if request.headers.get("origin") != URL or not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Open Jaro’s desktop workspace to set up the models.")
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to close.")
    setup = state.model_setup
    if setup and setup["phase"] in ("needed", "download-error", "load-error"):
        repair = setup["phase"] == "load-error" or setup.get("repair", False)
        setup.update(phase="downloading", completed=0, repair=repair)
        start_job(state, setup_workspace_speech(request.app, repair=repair))
    return {"modelSetup": setup}


def check_client(request):
    if request.headers.get("origin", URL) != URL:
        place = "this computer" if getattr(request.app.state, "portable_workspace", False) else "this Mac"
        raise HTTPException(403, f"Open Jaro on {place}.")


@app.post("/api/clients/{key}", status_code=204)
async def renew_client(key: UUID, request: Request):
    check_client(request)
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    state.leases.renew(str(key))
    return Response(status_code=204)


@app.delete("/api/clients/{key}", status_code=204)
async def detach_client(key: UUID, request: Request):
    check_client(request)
    request.app.state.leases.detach(str(key))
    return Response(status_code=204)


@app.get("/api/clients/{key}/stream")
async def connect_browser(key: UUID, request: Request):
    check_client(request)
    state = request.app.state

    async def stream():
        # An empty stream lets EventSource reconnect after a committed restart.
        if state.draining:
            return
        updater = getattr(state, "portable_updater", None)
        previous = None
        next_ping = time.monotonic()
        desktop = getattr(getattr(request, "state", None), "portable_desktop", False)
        onboarding = getattr(state, "portable_onboarding", None) if desktop else None
        last_onboarding = object()
        with state.leases.browser_connection(str(key), desktop=desktop):
            while not state.draining:
                updated = updater.changed if updater else state.leases.closing
                status = portable_update_view(state, updater, str(key), desktop=desktop) if updater else None
                if desktop and (receipt := getattr(state, "workspace_startup_receipt", None)):
                    with suppress(OSError):
                        receipt.touch(mode=0o600)
                        state.workspace_startup_receipt = None
                onboarding_changed = onboarding.changed if onboarding else None
                if onboarding and onboarding.id != last_onboarding:
                    last_onboarding = onboarding.id
                    yield f"event: onboarding\ndata: {json.dumps({'id': last_onboarding})}\n\n"
                now = time.monotonic()
                if status != previous:
                    previous = status
                    next_ping = now + 20
                    yield f"event: update\ndata: {json.dumps(status)}\n\n"
                elif now >= next_ping:
                    next_ping = now + 20
                    yield ": Jaro connected\n\n"
                # While an update is ready, refresh the restart guard as the
                # recording, model, and library jobs finish independently.
                delay = min(.5 if status and status.get("staged") else 20, max(.01, next_ping - time.monotonic()))
                if onboarding_changed is None:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(updated.wait(), timeout=delay)
                else:
                    events = [asyncio.create_task(event.wait()) for event in (updated, onboarding_changed)]
                    try:
                        await asyncio.wait(events, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in events:
                            task.cancel()
                        await asyncio.gather(*events, return_exceptions=True)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/api/meetings/events")
async def meeting_events(request: Request):
    check_client(request)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    state = request.app.state
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), state.native_token.encode()):
        raise HTTPException(401, "Valid Jard credentials are required.", headers={"WWW-Authenticate": "Bearer"})
    if state.meeting_subscriber is not None:
        raise HTTPException(409, "subscriber-busy")
    events = asyncio.Queue(maxsize=32)
    state.meeting_subscriber = events

    async def stream():
        last = time.monotonic()
        try:
            while not getattr(state, "is_shutting_down", lambda: False)():
                try:
                    identifier = await asyncio.wait_for(events.get(), timeout=1)
                except TimeoutError:
                    if time.monotonic() - last < 15:
                        continue
                    yield ": keep-alive\n\n"
                else:
                    if identifier is None:
                        return
                    yield "data: " + json.dumps({"type": "meeting.finished", "id": identifier}, separators=(",", ":")) + "\n\n"
                last = time.monotonic()
        finally:
            if state.meeting_subscriber is events:
                state.meeting_subscriber = None

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.post("/api/quit")
async def quit_app(request: Request):
    if request.headers.get("origin") != URL or request.headers.get("x-jard-action") != "quit":
        raise HTTPException(403, "Quit from the local Jaro app.")
    state = request.app.state
    if getattr(state, "portable_workspace", False) and (
        engine_work_in_progress(state) or portable_work_in_progress(state)
    ):
        raise HTTPException(409, "Finish the current recording or local work before quitting Jaro.")
    shutdown = getattr(state, "request_shutdown", None)
    if shutdown is None:
        raise HTTPException(503, "The local engine could not be stopped.")
    begin_draining(state)
    return Response(status_code=202, background=BackgroundTask(shutdown))


@app.exception_handler(ManagedFeatureError)
async def managed_feature(request: Request, error: ManagedFeatureError):
    return JSONResponse({"detail": str(error), "code": "managed"}, status_code=403)


@app.exception_handler(MissingSession)
async def missing_session(request: Request, error: MissingSession):
    return JSONResponse({"detail": "Session no longer exists."}, status_code=404)


@app.exception_handler(annotation_storage.Unavailable)
async def unavailable_annotation_storage(request: Request, error: annotation_storage.Unavailable):
    return JSONResponse({"detail": str(error), "code": "annotation-storage-unavailable"}, status_code=503)


@app.get("/api/sessions")
def list_sessions(request: Request, q: str = "", meetings: bool = False):
    state = request.app.state
    headers = {"Cache-Control": "no-store"}
    if state.startup_recovery and state.status != "error":
        headers["X-Jard-Meeting-Recovery"] = "pending"
    rows = state.library.list(q.strip(), meetings_only=meetings)
    if meetings_managed():
        for row in rows:
            if row["meeting"]:
                row["meetingsManaged"] = True
    return JSONResponse(rows, headers=headers)


@app.get("/api/sessions/revision")
async def session_revision(request: Request):
    check_client(request)
    return JSONResponse({"revision": request.app.state.sessions_revision}, headers={"Cache-Control": "no-store"})


@app.get("/api/sessions/{key}")
async def get_session(request: Request, key: UUID):
    session = await run_in_threadpool(request.app.state.library.get, str(key), include_turns=True)
    if not session.get("meetingsManaged") and session["overview"] and session["overview"]["status"] == "queued":
        request.app.state.insights.wake.set()
    return JSONResponse(session, headers={"Cache-Control": "no-store"})


@app.get("/api/sessions/{key}/images")
def list_session_images(request: Request, key: UUID):
    library = request.app.state.library
    with library.connection() as db:
        return JSONResponse(session_images.list_images(db, str(key)), headers={"Cache-Control": "no-store"})


@app.post("/api/sessions/{key}/images", status_code=201)
async def attach_session_image(request: Request, key: UUID):
    if request.headers.get("origin") != URL or request.headers.get("content-type") != "image/jpeg":
        raise HTTPException(403, "Add images from the Jaro workspace.")
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    image = bytearray()
    async for chunk in request.stream():
        if len(image) + len(chunk) > session_images.MAX_BYTES:
            raise HTTPException(413, "This image exceeds the 512 KB size limit.")
        image.extend(chunk)

    def attach():
        with state.library.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return session_images.add(db, str(key), image)

    try:
        result = await run_in_threadpool(attach)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if result is None:
        raise MissingSession(str(key))
    return result


@app.get("/api/sessions/{key}/images/{image_id}")
def session_image(request: Request, key: UUID, image_id: UUID):
    with request.app.state.library.connection() as db:
        image = session_images.get(db, str(key), str(image_id))
    if image is None:
        raise MissingSession(str(key))
    return Response(image, media_type="image/jpeg", headers=PRIVATE_MEDIA_HEADERS)


@app.delete("/api/sessions/{key}/images/{image_id}", status_code=204)
def remove_session_image(request: Request, key: UUID, image_id: UUID):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Remove images from the Jaro workspace.")
    if request.app.state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    library = request.app.state.library
    with library.connection(write_lock=True) as db:
        row = db.execute("""SELECT s.options FROM sessions s JOIN session_images i ON i.session_id=s.id
                            WHERE s.id=? AND i.id=?""", (str(key), str(image_id))).fetchone()
        if row:
            folder = library.media_folder(db, str(key), json.loads(row["options"]))
            try:
                session_images.handoff_file(folder, str(image_id)).unlink(missing_ok=True)
            except OSError as error:
                raise HTTPException(503, "Could not remove the local image file. Try again.") from error
        db.execute("DELETE FROM session_images WHERE session_id=? AND id=?", (str(key), str(image_id)))
    return Response(status_code=204)


def linux_capture_workspace(request):
    state = request.app.state
    if sys.platform != "linux" or not getattr(state, "portable_workspace", False):
        raise HTTPException(404, "Desktop screen capture is unavailable.")
    return state


@app.get("/api/portable/screenshots")
async def screenshot_targets(request: Request):
    if sys.platform == "win32":
        screencast_workspace(request)
        from windows_screencast import sources

        return JSONResponse({"targets": await sources(), "picker": "workspace"}, headers={"Cache-Control": "no-store"})
    linux_capture_workspace(request)
    from linux_desktop_portal import CaptureError, DBusError, ScreenshotPortal

    portal = ScreenshotPortal()
    try:
        await portal.connect()
        targets = await portal.targets()
    except (CaptureError, DBusError, EOFError, OSError, TimeoutError, ValueError):
        targets = []
    finally:
        await portal.close()
    return JSONResponse({"targets": targets}, headers={"Cache-Control": "no-store"})


@app.post("/api/portable/screenshots")
async def portable_screenshot(request: Request, source: Literal["picker", "area", "window", "display"],
                              target: Annotated[str, Query(max_length=27)] = ""):
    state = screencast_workspace(request)
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Take screenshots from Jaro’s desktop workspace.")
    async with state.portable_recording_lock:
        if state.draining or getattr(state, "screenshot_pending", False) or state.portable_screencast:
            raise HTTPException(409, "Finish the current screen capture first.")
        state.screenshot_pending = True
    try:
        if sys.platform == "win32":
            if source not in ("window", "display"):
                raise HTTPException(422, "Choose a display or window in Jaro’s capture picker.")
            return await windows_screenshot(state, source, target)
        return await linux_screenshot(source)
    finally:
        state.screenshot_pending = False


async def linux_screenshot(source):
    from linux_desktop_portal import CaptureError, DBusError, PortalResponseError, ScreenshotPortal, read_screenshot

    portal = ScreenshotPortal()
    try:
        await portal.connect()
        path = await portal.screenshot(source)
        image, mime = await run_in_threadpool(read_screenshot, path)
    except PortalResponseError as error:
        if error.code == 1:
            return Response(status_code=204)
        raise HTTPException(503, str(error)) from error
    except CaptureError as error:
        raise HTTPException(422, str(error)) from error
    except TimeoutError as error:
        raise HTTPException(504, "The desktop screenshot request timed out.") from error
    except (DBusError, EOFError, OSError, ValueError) as error:
        raise HTTPException(503, "Could not get a screenshot from the desktop.") from error
    finally:
        await portal.close()
    return Response(image, media_type=mime, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


async def windows_screenshot(state, source, target):
    from portable_capture import CaptureError
    from windows_screencast import snapshot

    if not getattr(state, "screen_draft_dir", None):
        raise HTTPException(503, "The screen recorder is unavailable.")
    try:
        await require_windows_target(source, target)
        if source == "display":
            await asyncio.sleep(3)
        picture = await snapshot(source, target)
    except CaptureError as error:
        raise HTTPException(422, str(error)) from error
    except TimeoutError as error:
        raise HTTPException(504, "The desktop screenshot request timed out.") from error
    except OSError as error:
        raise HTTPException(503, "Could not get a screenshot from the desktop.") from error
    return Response(picture.jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/portable/screencast/sources")
async def screencast_sources(request: Request):
    if sys.platform == "win32":
        screencast_workspace(request)
        from windows_screencast import sources

        return JSONResponse({"sources": await sources(), "picker": "workspace"},
                            headers={"Cache-Control": "no-store"})
    linux_capture_workspace(request)
    from linux_desktop_portal import DBusError, ScreenCastPortal
    from linux_screencast import supported
    from linux_x11 import x11_session

    if not supported():
        return {"sources": []}
    sources = ["display", "area"] if x11_session() else []
    portal = ScreenCastPortal(lambda: None)
    try:
        await portal.connect()
        portal_sources = await portal.sources()
        sources.extend(portal_sources)
        if "display" in portal_sources:
            sources.append("area")
    except (DBusError, OSError, EOFError, TimeoutError, ValueError):
        pass
    finally:
        await portal.close()
    return {"sources": list(dict.fromkeys(sources))}


def screencast_workspace(request):
    state = request.app.state
    if sys.platform not in ("linux", "win32") or not getattr(state, "portable_workspace", False):
        raise HTTPException(404, "Desktop screen capture is unavailable.")
    return state


def screencast_action(request):
    state = screencast_workspace(request)
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Record the screen from Jaro’s desktop workspace.")
    return state


@app.get("/api/portable/screencast")
async def screencast_status(request: Request):
    state = screencast_workspace(request)
    take = state.portable_screencast
    status = take.status() if take else {"phase": "idle"}
    if not getattr(request.state, "portable_desktop", False):
        status.pop("drawingView", None)
    if shortcut := getattr(state, "portable_annotation_shortcut", None):
        status.update(shortcutError=shortcut.feedback, shortcutSaved=shortcut.saved)
        if binding := getattr(state, "desktop_annotation", None):
            options = binding.status()
            status.update(desktopShortcut=options, shortcut=options["shortcut"] if options["enabled"] else "",
                          shortcutPlatform=state.portable_dictation.platform)
        else:
            status.update(shortcut=state.portable_dictation.annotation_shortcut,
                          gnomeShortcut=state.portable_dictation.gnome_annotation)
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


@app.get("/api/portable/screencast/targets")
async def screencast_targets(request: Request, source: Literal["display", "window"]):
    state = screencast_workspace(request)
    if sys.platform != "win32" or not state.screen_draft_dir:
        raise HTTPException(404, "This desktop provides its own capture picker.")
    from portable_capture import CaptureError
    from windows_screencast import targets

    try:
        available = await targets(source)
    except CaptureError as error:
        raise HTTPException(503, str(error)) from error
    return JSONResponse({"targets": available}, headers={"Cache-Control": "no-store"})


async def screen_microphone_inputs():
    from portable_capture import CaptureError

    if sys.platform == "win32":
        from windows_capture import microphone_inputs
    else:
        from linux_capture import microphone_inputs

    try:
        return await microphone_inputs()
    except CaptureError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/portable/screencast/microphones")
async def screencast_microphones(request: Request):
    state = screencast_workspace(request)
    if not state.screen_draft_dir:
        raise HTTPException(404, "Screen recording microphone selection is unavailable.")
    return JSONResponse({"inputs": await screen_microphone_inputs()}, headers={"Cache-Control": "no-store"})


async def require_windows_target(source, target):
    from portable_capture import CaptureError
    from windows_screencast import validate_target

    try:
        await validate_target(source, target)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except CaptureError as error:
        raise HTTPException(503, str(error)) from error


@app.post("/api/portable/screencast", status_code=202)
async def begin_screencast(request: Request, source: Literal["display", "window", "area"],
                           microphone: bool = False, computer_audio: bool = False,
                           target: Annotated[str, Query(max_length=27)] = "",
                           microphone_device: Annotated[str, Query(max_length=1024)] = ""):
    state = screencast_action(request)
    if microphone_device and (not microphone or "\x00" in microphone_device):
        raise HTTPException(422, "Choose a microphone with microphone narration enabled.")
    if microphone or computer_audio:
        transcription_languages(request)
    return await start_screen_take(state, source, microphone, computer_audio, target, microphone_device=microphone_device)


async def start_screen_take(state, source, microphone, computer_audio, target, *, microphone_device="", notice=None,
                            require_shortcut=True, annotation_languages=None, draw_on_start=False, area=None,
                            show_area_picker=None, observe_origin=False):
    require_annotation()
    if not state.screen_draft_dir:
        raise HTTPException(503, "The screen recorder is unavailable.")
    async with state.portable_recording_lock:
        if require_shortcut and notice is not None:
            binding = getattr(state, "desktop_annotation", None)
            if binding is None or not binding.status()["enabled"]:
                raise HTTPException(409, "The Annotate shortcut is disabled.")
        if portable_recording_busy(state):
            raise HTTPException(409, "Finish the current recording first.")
        if getattr(state, "screenshot_pending", False):
            raise HTTPException(409, "Finish the current screenshot first.")
        if (microphone or computer_audio) and state.status != "ready":
            raise HTTPException(503, "The local model is unavailable.")
        if microphone_device and not any(device["id"] == microphone_device for device in await screen_microphone_inputs()):
            raise HTTPException(409, "The selected microphone is unavailable. Choose another microphone and try again.")
        def resolve_location():
            with state.library.connection() as db:
                return annotation_storage.capture_location(db)

        try:
            location = await run_in_threadpool(resolve_location)
        except annotation_storage.Unavailable as error:
            raise HTTPException(503, str(error)) from error
        if sys.platform == "win32":
            from windows_screencast import WindowsScreenRecording

            await require_windows_target("display" if source == "area" else source, target)
            take = WindowsScreenRecording(state.screen_draft_dir, source, target, microphone, computer_audio,
                                          microphone_device=microphone_device, notice=notice, annotation_location=location)
        else:
            from linux_screencast import ScreenRecording, supported
            from linux_x11 import recording_target, x11_session
            from portable_capture import CaptureError

            if not supported():
                raise HTTPException(503, "Install GStreamer and its capture plugins to record the screen.")
            native_x11 = source in ("display", "area") and x11_session()
            if area is not None:
                if not native_x11:
                    raise HTTPException(422, "Exact coordinates require native X11. On Wayland use --source area and approve the picker.")
                try:
                    area = await asyncio.to_thread(recording_target, area)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from error
                except CaptureError as error:
                    raise HTTPException(503, str(error)) from error
            take = ScreenRecording(state.screen_draft_dir, source, microphone, computer_audio,
                                   microphone_device=microphone_device, native_x11=native_x11, notice=notice,
                                   draw_on_start=draw_on_start, x11_area=area, show_area_picker=show_area_picker,
                                   annotation_location=location)
        take.annotation_languages = annotation_languages
        take.observe_annotation_origin = observe_origin
        take.annotation_started_at = time.time()
        state.portable_system_audio_starting = computer_audio
        try:
            if computer_audio and (dictation := getattr(state, "portable_dictation", None)):
                await dictation.audio_control.settle()
            state.portable_screencast = take
        finally:
            state.portable_system_audio_starting = False
        take.task = start_job(state, take.record())
    return take.status()


async def start_desktop_annotation(state, source, target="", *, require_shortcut=False, languages=None, draw=False, area=None):
    from portable_annotation_shortcut import ScreenRecordingNotice
    from portable_capture import CaptureError

    require_annotation()
    shortcut = state.portable_annotation_shortcut
    if shortcut is None:
        raise HTTPException(503, "The desktop screen recorder is unavailable.")
    if source == "phone":
        raise HTTPException(422, "Wired iPhone and iPad recording is available on macOS.")
    tray = Path(os.environ.get("JARO_DESKTOP_TRAY", ""))
    select_area = source == "area" and area is None
    if select_area and (not tray.is_absolute() or not tray.is_dir()):
        raise HTTPException(409, "Open Jaro’s workspace to select a recording area.")
    platform = state.portable_dictation.platform
    try:
        if platform == "windows" and not target:
            if source == "window":
                raise HTTPException(422, "Choose a window ID from `jardctl annotate targets --source window` and pass it with --target.")
            from windows_screencast import cursor_display

            target = await cursor_display()
        elif platform != "windows" and target:
            raise HTTPException(422, "Choose the source in your desktop’s screen capture picker.")
    except CaptureError as error:
        raise HTTPException(503, str(error)) from error
    notice = None if platform == "wayland" else ScreenRecordingNotice(shortcut, monitor=target.split(":")[0] if target else None)

    def open_area_picker():
        try:
            (tray / "open").touch()
        except OSError as error:
            raise CaptureError("Could not open Jaro to select a recording area.") from error

    wait_for_share = select_area and platform == "wayland"
    status = await start_screen_take(state, source, True, False, target, notice=notice,
                                    require_shortcut=require_shortcut, annotation_languages=languages, draw_on_start=draw,
                                    area=area, show_area_picker=open_area_picker if wait_for_share else None,
                                    observe_origin=True)
    if select_area and not wait_for_share:
        try:
            open_area_picker()
        except CaptureError as error:
            await discard_screen_take(state, status["id"])
            raise HTTPException(503, str(error)) from error
    return status


class PortableAnnotationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start", "status", "stop", "toggle", "cancel", "targets", "draw"]
    source: Literal["display", "window", "area", "phone"] = "display"
    target: str = Field(default="", max_length=27)
    languages: list[str] | None = Field(default=None, max_length=64)
    area: list[int] | None = Field(default=None, min_length=4, max_length=4)
    terminalFolder: str | None = Field(default=None, max_length=4096)


class PortableAnnotationHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["destinations", "send", "send-session", "deliveries", "undo", "correct", "remember", "forget", "route"]
    agent: Literal["opencode", "codex", "claude", "t3"] = "opencode"
    id: str = Field(default="", max_length=36)
    target: str = Field(default="", max_length=130)
    service: str | None = Field(default=None, max_length=64)
    query: str = Field(default="", max_length=300)
    scope: Literal["app", "site", "terminal"] | None = None
    scopeKey: str | None = Field(default=None, max_length=4096)
    ruleId: str = Field(default="", max_length=36)
    automatic: StrictBool = False
    routeAction: Literal["status", "cancel"] = "status"


class OpenCodeWslPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


def check_opencode_wsl_settings(request):
    if sys.platform != "win32" or not getattr(request.app.state, "portable_workspace", False):
        raise HTTPException(404, "OpenCode in WSL is available on Windows.")
    if not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Open Jaro’s desktop workspace to access this setting.")
    require_annotation()
    require_opencode()


@app.get("/api/portable/annotation-handoff/wsl")
def read_opencode_wsl(request: Request):
    check_opencode_wsl_settings(request)
    from opencode_wsl import enabled

    return JSONResponse({"enabled": enabled()}, headers={"Cache-Control": "no-store"})


@app.put("/api/portable/annotation-handoff/wsl")
def write_opencode_wsl(request: Request, preference: OpenCodeWslPreference):
    check_opencode_wsl_settings(request)
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to change this setting.")
    if request.app.state.draining:
        raise HTTPException(409, "Jaro is restarting.")
    from opencode_wsl import set_enabled

    try:
        set_enabled(preference.enabled)
    except OSError as error:
        raise HTTPException(503, "Could not save the OpenCode in WSL setting.") from error
    return JSONResponse({"enabled": preference.enabled}, headers={"Cache-Control": "no-store"})


async def run_portable_annotation_handoff(state, command):
    from annotation_routing import destinations, recent_deliveries

    require_annotation()
    if command.agent != "opencode" and command.action not in ("destinations", "send-session"):
        raise HTTPException(400, "Choose a saved session and an existing conversation to share.")
    root = state.library.root
    if command.action == "deliveries":
        return await asyncio.to_thread(recent_deliveries, root, command.id)
    panel = getattr(state, "annotation_panel", None)
    if command.action == "route":
        capture = str(UUID(command.id))
        if panel:
            if command.routeAction == "cancel":
                await panel.automation.cancel(capture)
            return await panel.automation.query(capture)
        from annotation_automation import stored_route_status
        from annotation_deliveries import AnnotationDeliveries

        def stored_status():
            outbox = AnnotationDeliveries(root)
            if command.routeAction == "cancel":
                outbox.set_pending_route(capture, "cancelled", "Automatic send cancelled.")
            return stored_route_status(outbox, outbox.get_route(capture), capture)

        return await run_in_threadpool(stored_status)
    if command.agent == "opencode":
        require_opencode()
    if state.draining:
        raise ValueError("Jaro is preparing to close.")
    if command.action == "destinations":
        if command.agent != "opencode":
            from annotation_agents import destinations as agent_destinations

            return {**await agent_destinations(command.agent, command.query), "service": None}
        return await destinations(command.query, root=root, capture=command.id or None)
    if command.action == "remember" and command.automatic and panel is None:
        raise ValueError("Automatic send requires Jaro’s desktop countdown controls.")

    active = getattr(state, "annotation_handoff_job", None)
    if active is not None and not active.done():
        raise ValueError("Another annotation delivery is in progress.")
    state.annotation_handoff_job = start_job(state, change_portable_annotation_delivery(root, command, panel))
    return await state.annotation_handoff_job


async def change_portable_annotation_delivery(root, command, panel=None):
    from annotation_routing import (correct_annotation, forget_destination_rule, send_annotation, send_session_prompt,
                                    set_destination_rule, withdraw_annotation)

    if command.action == "remember":
        return await set_destination_rule(root, command.id, command.scope, command.target, command.service,
                                          command.automatic, scope_key=command.scopeKey)
    if command.action == "forget":
        return await asyncio.to_thread(forget_destination_rule, root, command.id, command.ruleId)
    if command.action == "send-session":
        from session_agent_context import opencode_body, session_payload

        identifier = str(UUID(command.id))
        text, images, has_images, annotation = await asyncio.to_thread(session_payload, root, identifier, command.agent)
        if command.agent != "opencode":
            from annotation_agents import deliver

            await deliver(command.agent, command.target, text, images)
            return {"images": "references" if has_images and command.agent == "t3" else "attached" if has_images else "none"}
        body = opencode_body(text, images)
        if annotation:
            if panel:
                await panel.automation.cancel(identifier, "Automatic send replaced by a manual send.")
            await send_annotation(root, identifier, command.target, command.service, body=body)
        else:
            await send_session_prompt(body, command.target, command.service)
        return {"images": "attached" if has_images else "none"}
    if command.action == "send":
        if panel:
            from annotation_routing import capture_for_routing

            capture = await run_in_threadpool(capture_for_routing, root, command.id)
            await panel.automation.cancel(capture["id"], "Automatic send replaced by a manual send.")
        return await send_annotation(root, command.id, command.target, command.service)
    if command.action == "undo":
        return await withdraw_annotation(root, command.id)
    return await correct_annotation(root, command.id)


@app.post("/api/portable/annotation-handoff")
async def portable_annotation_handoff(request: Request, command: PortableAnnotationHandoff):
    state = request.app.state
    if sys.platform not in ("linux", "win32") or not getattr(state, "portable_workspace", False):
        raise HTTPException(404, "Agent handoff is unavailable in this workspace.")
    if not getattr(request.state, "portable_desktop", False) or request.headers.get("origin") != URL:
        raise HTTPException(403, "Use the Jaro desktop window to share session context.")
    if command.action == "send":
        raise HTTPException(400, "Choose a saved session to share.")
    return await annotation_handoff_response(state, command)


class AnnotationRulesList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list"]


class AnnotationRuleRemove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["remove"]
    id: UUID


class AnnotationRuleAutomatic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["setAutomatic"]
    id: UUID
    enabled: StrictBool


class AnnotationRuleRetarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["retarget"]
    id: UUID
    target: str = Field(max_length=4096)
    service: str = Field(max_length=64)
    project: StrictBool = False


class AnnotationRuleAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add"]
    kind: Literal["site", "app", "terminal"]
    source: str = Field(max_length=4096)
    target: str = Field(max_length=4096)
    service: str = Field(max_length=64)
    project: StrictBool = False


@app.post("/api/portable/annotation-rules")
async def portable_annotation_rules(request: Request, change: Annotated[
    AnnotationRulesList | AnnotationRuleRemove | AnnotationRuleAutomatic | AnnotationRuleRetarget | AnnotationRuleAdd,
    Body(discriminator="action"),
]):
    from annotation_deliveries import AnnotationDeliveries
    state = request.app.state
    if sys.platform not in ("linux", "win32") or not getattr(state, "portable_workspace", False):
        raise HTTPException(404, "OpenCode rules are unavailable in this workspace.")
    if not getattr(request.state, "portable_desktop", False) or request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to manage destinations.")
    require_annotation()
    if opencode_managed() and isinstance(change, AnnotationRulesList):
        return JSONResponse({"managed": True, "rules": []}, headers={"Cache-Control": "no-store"})
    require_opencode()
    store = AnnotationDeliveries(state.library.root)
    try:
        if not isinstance(change, AnnotationRulesList):
            if state.draining:
                raise ValueError("Jaro is preparing to close.")
            await asyncio.shield(start_job(state, update_portable_annotation_rule(state, store, change)))
        rules = await run_in_threadpool(store.list_rules)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse({"managed": False, "rules": rules}, headers={"Cache-Control": "no-store"})


async def update_portable_annotation_rule(state, store, change):
    if isinstance(change, (AnnotationRuleAdd, AnnotationRuleRetarget)):
        from annotation_routing import annotation_rule_scope, verify_settings_destination

        scope = annotation_rule_scope(change.kind, change.source, sys.platform) if isinstance(change, AnnotationRuleAdd) else None
        record, title, target = await verify_settings_destination(change.target, change.service, change.project)
        if state.draining:
            raise ValueError("Jaro is preparing to close.")
        if scope is not None:
            await run_in_threadpool(store.remember_rule, scope, title, target, record.channel, create_only=True)
        else:
            await run_in_threadpool(store.retarget_rule, str(change.id), title, target, record.channel)
    elif isinstance(change, AnnotationRuleAutomatic):
        if change.enabled and not getattr(state, "annotation_panel", None):
            raise ValueError("Automatic send requires Jaro’s desktop countdown controls.")
        await run_in_threadpool(store.manage_rule, str(change.id), automatic=change.enabled)
    else:
        await run_in_threadpool(store.manage_rule, str(change.id))


async def annotation_handoff_response(state, command):
    try:
        result = await run_portable_annotation_handoff(state, command)
    except MissingSession as error:
        raise HTTPException(404, "Session not found.") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def run_portable_annotation_command(state, command):
    from portable_capture import CaptureError

    require_annotation()
    shortcut = getattr(state, "portable_annotation_shortcut", None)
    if shortcut is None:
        raise HTTPException(503, "The desktop screen recorder is unavailable.")
    if command.area is not None and (command.action != "start" or command.source != "area" or command.target):
        raise HTTPException(422, "Use coordinates only when starting an area capture.")
    if command.area is not None and state.portable_dictation.platform != "x11":
        raise HTTPException(422, "Exact coordinates require native X11. Use --source area to select the region.")
    if command.terminalFolder is not None and command.action != "stop":
        raise HTTPException(422, "Use a terminal folder only when stopping an annotation.")
    take = state.portable_screencast
    if command.action == "status":
        if take:
            return take.status()
        return {"phase": "saved", "id": shortcut.saved} if shortcut.saved else {"phase": "idle"}
    if command.action == "targets":
        if state.portable_dictation.platform != "windows":
            return {"picker": "desktop"}
        if command.source not in ("display", "window"):
            raise HTTPException(422, "Choose display or window to list targets.")
        from windows_screencast import targets

        try:
            return {"targets": await targets(command.source)}
        except CaptureError as error:
            raise HTTPException(503, str(error)) from error
    if command.action == "cancel":
        if not take:
            return {"phase": "idle"}
        await discard_screen_take(state, take.draft.key)
        return {"phase": "cancelled", "id": take.draft.key}
    action = command.action
    if action == "draw":
        if state.portable_dictation.platform == "windows":
            raise HTTPException(422, "Use the recording controls to draw on Windows.")
        if take and take.phase != "cancelled":
            if take.phase == "recording" and not take.saving and not take.stopped:
                try:
                    await take.toggle_drawing()
                except CaptureError as error:
                    raise HTTPException(409, str(error)) from error
            return take.status()
    if action == "toggle" and take and take.phase != "cancelled":
        if take.saving or take.phase not in ("recording", "recorded"):
            return take.status()
        action = "stop"
    if action == "stop":
        if not take:
            raise HTTPException(409, "There is no screen recording ready to save.")
        origin = None
        if command.terminalFolder is not None:
            from portable_terminal_origin import explicit_terminal_origin

            require_opencode()
            origin = await run_in_threadpool(explicit_terminal_origin, command.terminalFolder, state.portable_dictation.platform)
            if not origin:
                raise HTTPException(422, "Run this command from a project or working folder.")
        await finish_screen_take(state, take.draft.key, (), origin_override=origin)
        shortcut.saved = take.draft.key
        return {"phase": "saved", "id": take.draft.key}
    preference = state.portable_dictation.language
    languages = tuple(dict.fromkeys(command.languages)) if command.languages is not None else ((preference,) if preference else ())
    if any(code not in LANGUAGES for code in languages):
        raise HTTPException(400, "Choose a language from the list.")
    if take and take.phase == "cancelled":
        # The chooser can be dismissed before any workspace is open to clear the take.
        await discard_screen_take(state, take.draft.key)
    return await start_desktop_annotation(state, command.source, command.target, languages=languages,
                                          draw=action == "draw", area=command.area)


class AnnotationShortcutChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool | None = None
    shortcut: Annotated[str, Field(min_length=1, max_length=64)] | None = None


@app.post("/api/portable/screencast/shortcut")
async def configure_annotation_shortcut(request: Request, change: AnnotationShortcutChange):
    from portable_capture import CaptureError

    state = screencast_action(request)
    binding = getattr(state, "desktop_annotation", None)
    if binding is None:
        raise HTTPException(404, "The Annotate shortcut is unavailable.")
    if not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Change the Annotate shortcut from Jaro’s desktop workspace.")
    require_annotation()
    async with state.portable_dictation.control, state.portable_recording_lock:
        if state.draining or state.portable_screencast:
            raise HTTPException(409, "Finish the screen recording before changing the shortcut.")
        try:
            await binding.configure(enabled=change.enabled, shortcut=change.shortcut)
        except CaptureError as error:
            raise HTTPException(409, str(error)) from error
    return binding.status()


class ScreencastRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    x: int
    y: int
    width: int
    height: int


@app.get("/api/portable/screencast/area/{key}")
async def screencast_preview(request: Request, key: UUID):
    take = screencast_workspace(request).portable_screencast
    if not take or take.preview_id != str(key) or take.phase != "selecting" or take.preview is None:
        raise HTTPException(404, "This display preview has expired.")
    return Response(take.preview, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/portable/screencast/area/{key}", status_code=202)
async def set_screencast_region(request: Request, key: UUID, region: ScreencastRegion | None = None):
    from portable_capture import CaptureError

    take = screencast_action(request).portable_screencast
    if not take or take.preview_id != str(key) or take.saving:
        raise HTTPException(409, "This display preview has expired.")
    try:
        take.select_region((region.x, region.y, region.width, region.height) if region else None)
    except ValueError as failure:
        raise HTTPException(422, str(failure)) from failure
    except CaptureError as failure:
        raise HTTPException(409, str(failure)) from failure
    return take.status()


@app.get("/api/portable/screencast/{key}/preview")
async def live_screencast_preview(request: Request, key: UUID, view: str | None = None):
    from screencast_ink import FRAME_BOUNDARY

    take = screencast_workspace(request).portable_screencast
    if not take or take.draft.key != str(key) or take.phase != "recording" or take.saving:
        raise HTTPException(404, "The live drawing preview has ended.")
    if take.drawing.token is not None or getattr(getattr(take, "pen", None), "active", False):
        raise HTTPException(409, "The live drawing preview is open in another view.")
    if (drawing_view := getattr(take, "drawing_view", None)) and drawing_view != view:
        raise HTTPException(409, "The live drawing preview is open in another view.")
    frames = take.drawing.stream(lambda: take.phase == "recording" and not take.saving)
    return StreamingResponse(frames, media_type=f"multipart/x-mixed-replace; boundary={FRAME_BOUNDARY}",
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.delete("/api/portable/screencast/{key}/preview", status_code=204)
async def close_live_screencast_preview(request: Request, key: UUID, view: str | None = None):
    take = screencast_action(request).portable_screencast
    if take and take.draft.key == str(key):
        drawing_view = getattr(take, "drawing_view", None)
        if not drawing_view or drawing_view == view:
            if drawing_view:
                take.drawing_view = None
            take.drawing.close_view()
    return Response(status_code=204)


class ScreencastInk(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    time: float
    points: list[Annotated[list[float], Field(min_length=3, max_length=3)]] = Field(max_length=1024)


@app.post("/api/portable/screencast/{key}/pen")
@app.delete("/api/portable/screencast/{key}/pen")
async def screencast_pen(request: Request, key: UUID):
    from portable_capture import CaptureError

    take = screencast_action(request).portable_screencast
    if not take or take.draft.key != str(key) or take.phase != "recording" or take.saving:
        raise HTTPException(409, "This screen recording has stopped.")
    pen = getattr(take, "pen", None)
    if pen is None:
        raise HTTPException(409, "Use the recording preview to draw on this desktop.")
    if take.stopped or take.started is None:
        raise HTTPException(409, "This screen recording has stopped.")
    if request.method == "DELETE":
        await pen.close()
    else:
        try:
            await pen.open(take.started, take.region)
        except CaptureError as failure:
            raise HTTPException(409, str(failure)) from failure
    return take.status()


@app.post("/api/portable/screencast/{key}/ink", status_code=204)
async def append_screencast_ink(request: Request, key: UUID, ink: ScreencastInk):
    take = screencast_action(request).portable_screencast
    if not take or take.draft.key != str(key) or take.started is None or take.phase != "recording" or take.saving:
        raise HTTPException(409, "This screen recording has stopped.")
    if getattr(getattr(take, "pen", None), "active", False):
        raise HTTPException(409, "Finish drawing on the desktop before using the preview.")
    try:
        take.drawing.add(ink.points, ink.time, take.recording_time() + .5)
    except ValueError as failure:
        raise HTTPException(422, str(failure)) from failure
    except OSError as failure:
        raise HTTPException(503, "Could not keep this drawing in the recording.") from failure
    return Response(status_code=204)


async def save_screen_draft(state, draft, languages, *, origin=None):
    from portable_capture import CaptureError
    from screen_contact_sheet import make_contact_sheet
    from screencast_ink import load_events

    if await run_in_threadpool(state.library.exists, draft.key):
        with suppress(OSError):
            await run_in_threadpool(draft.discard)
        return JSONResponse({"id": draft.key}, status_code=202)
    try:
        if draft.annotation_location is not None:
            def check_location():
                with state.library.connection() as db:
                    annotation_storage.location_root(db, draft.annotation_location)

            await run_in_threadpool(check_location)
        events = await run_in_threadpool(load_events, draft.ink)
        movie, duration = await draft.prepare_movie(events)
        assets = staged_annotation_paths(movie)
        frames, keyframes = await make_contact_sheet(movie, assets, duration, events)
        await run_in_threadpool(assets["manifest"].write_text, json.dumps({
            "version": 1, "source": draft.source, "coordinateSpace": "normalized-bottom-left", "events": events, "frames": frames,
            **({"keyframes": keyframes} if keyframes else {}),
            **({"origin": origin} if origin else {}),
        }), encoding="utf-8")
        annotation = await run_in_threadpool(read_staged_annotation, movie)
        result = await accept_audio(state, movie, "Screen recording", "video/mp4", languages,
                                    True, len(draft.audio_sources) == 2, False, True, stage_id=draft.key, annotation=annotation,
                                    annotation_location=draft.annotation_location)
    except annotation_storage.Unavailable as error:
        raise HTTPException(503, str(error)) from error
    except (CaptureError, ValueError) as failure:
        raise HTTPException(422, str(failure)) from failure
    except OSError as failure:
        raise HTTPException(503, "Could not save the screen recording.") from failure
    with suppress(OSError):
        await run_in_threadpool(draft.discard)
    return result


def screen_take_for_action(state, key):
    take = state.portable_screencast
    if take and key is not None and take.draft.key != str(key):
        raise HTTPException(409, "This screen recording has ended.")
    return take


@app.post("/api/portable/screencast/finish")
async def finish_screencast(request: Request, key: UUID | None = None):
    state = screencast_action(request)
    return await finish_screen_take(state, key, transcription_languages(request, require_ready=False))


async def finish_screen_take(state, key, languages, *, origin_override=None):
    async with state.portable_recording_lock:
        take = screen_take_for_action(state, key)
        if not take or take.phase not in ("recording", "recorded") or take.saving:
            raise HTTPException(409, "There is no screen recording ready to save.")
        take.saving = True

    async def stop_and_save():
        try:
            origin = origin_override or getattr(take, "annotation_origin", None)
            if (origin_override is None and take.phase == "recording"
                    and getattr(take, "observe_annotation_origin", False) and not opencode_managed()):
                from portable_annotation_origin import observed_annotation_origin

                origin, _ = await asyncio.gather(
                    observed_annotation_origin(getattr(state, "portable_dictation", None)), take.stop(),
                )
            else:
                await take.stop()
            take.annotation_origin = origin
            override = getattr(take, "annotation_languages", None)
            result = await save_screen_draft(state, take.draft, languages if override is None else override, origin=origin)
            state.portable_screencast = None
            if panel := getattr(state, "annotation_panel", None):
                try:
                    await panel.automation.enqueue(take.draft.key, origin, getattr(take, "annotation_started_at", None))
                except (OSError, sqlite3.Error):
                    logger.warning("Could not queue automatic annotation delivery.", exc_info=True)
            return result
        finally:
            take.saving = False

    return await asyncio.shield(start_job(state, stop_and_save()))


@app.delete("/api/portable/screencast", status_code=204)
async def discard_screencast(request: Request, key: UUID | None = None):
    state = screencast_action(request)
    await discard_screen_take(state, key)
    return Response(status_code=204)


async def discard_screen_take(state, key):
    async with state.portable_recording_lock:
        take = screen_take_for_action(state, key)
        if not take:
            return
        if take.saving:
            raise HTTPException(409, "Wait for the screen recording to finish saving.")
        take.saving = True

    async def discard():
        try:
            await take.discard()
            state.portable_screencast = None
        finally:
            take.saving = False

    await asyncio.shield(start_job(state, discard()))


@app.get("/api/sessions/{key}/audio")
def session_audio(request: Request, key: UUID, track: Literal["mic", "system"] | None = None):
    path, mime = request.app.state.library.media(str(key), track=track)
    return FileResponse(path, media_type=mime, headers=PRIVATE_MEDIA_HEADERS)


@app.get("/api/sessions/{key}/annotation/{kind}")
def session_annotation_asset(request: Request, key: UUID, kind: str):
    path, mime = request.app.state.library.annotation_asset(str(key), kind)
    return FileResponse(path, media_type=mime, headers=PRIVATE_MEDIA_HEADERS)


class AnnotationVideoRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    startMs: Annotated[int, Field(ge=0, le=MAX_TIMELINE_SECONDS * 1000)]
    endMs: Annotated[int, Field(ge=500, le=MAX_TIMELINE_SECONDS * 1000)]


@app.post("/api/portable/annotations/{key}/export")
async def export_portable_annotation(request: Request, key: UUID,
                                     span: Annotated[AnnotationVideoRange | None, Body(embed=True)] = None,
                                     kind: Annotated[Literal["video", "package"], Body(embed=True)] = "video"):
    if request.headers.get("origin") != URL or not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Export the annotation from Jaro’s desktop workspace.")
    from annotation_package import write_package
    from portable_video_export import save_annotation_video
    from desktop_file_dialog import choose_file, file_dialog

    if not (dialog := file_dialog()):
        raise HTTPException(404, "The desktop export dialog is unavailable.")
    if kind == "package" and span is not None:
        raise HTTPException(422, "A video range can only be used for a clip export.")
    if span and span.endMs - span.startMs < 500:
        raise HTTPException(422, "Select at least half a second within the recording to export.")
    state = request.app.state
    pending = getattr(state, "portable_annotation_export", None)
    if state.draining or (pending and not pending.done()):
        raise HTTPException(409, "Finish the current annotation export before saving another.")

    async def export_package():
        folder = await choose_file(dialog, "--choose-annotation-export-folder")
        if folder is None:
            return {"saved": False}
        return await asyncio.to_thread(write_package, state.library, str(key), folder)

    bounds = (span.startMs, span.endMs) if span else None
    work = export_package() if kind == "package" else save_annotation_video(state.library, dialog, str(key), bounds)
    state.portable_annotation_export = start_job(state, work)
    try:
        return await asyncio.shield(state.portable_annotation_export)
    except annotation_storage.Unavailable:
        raise
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(409, "Jaro could not save to the selected folder.") from error


@app.get("/api/sessions/{key}/export")
def export_meeting(request: Request, key: UUID, kind: Literal["md", "srt", "vtt", "notes"]):
    library = request.app.state.library
    session = get_meeting(library, str(key), turns=kind != "notes", staged=False)
    name = export_name(session, kind)
    with library.connection() as db:
        use_24_hour = kind in ("md", "notes") and meeting_exports.clock_24(db)
    body = render_export(meeting_detail(session), kind, use_24_hour=use_24_hour)
    media = {"md": "text/markdown", "notes": "text/markdown", "vtt": "text/vtt", "srt": "application/x-subrip"}
    return Response(body, media_type=media[kind], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe="")})


@app.get("/api/sessions/{key}/transcript-clipboard")
def meeting_clipboard(request: Request, key: UUID):
    library = SessionStore(request.app.state.library.root, read_only=True)
    try:
        text = saved_meeting_clipboard(library, str(key))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


@app.put("/api/native/meeting-clock/{hours}", status_code=204)
def set_meeting_clock(request: Request, hours: Literal["12", "24"]):
    check_native(request)
    with request.app.state.library.connection() as db:
        meeting_exports.set_clock_24(db, hours == "24")
    return Response(status_code=204)


@app.get("/api/sessions/{key}/managed-export")
def managed_meeting_export(request: Request, key: UUID):
    check_client(request)
    with request.app.state.library.connection() as db:
        value = meeting_exports.read(db, str(key))
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@app.get("/api/meeting-transcript-edits")
def meeting_edit_policy(request: Request):
    check_client(request)
    with request.app.state.library.connection() as db:
        return {"enabled": meeting_edits.enabled(db)}


@app.put("/api/meeting-transcript-edits")
def set_meeting_edit_policy(request: Request, enabled: Annotated[StrictBool, Body(embed=True)]):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Change this setting from the Jaro workspace.")
    require_meetings()
    with request.app.state.library.connection() as db:
        db.execute("INSERT OR REPLACE INTO meeting_edit_policy VALUES(1,?)", (enabled,))
    return {"enabled": enabled}


class MeetingLineDeletion(BaseModel):
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    lines: list[Annotated[int, Field(strict=True, ge=0)]] = Field(min_length=1, max_length=20000)


@app.post("/api/sessions/{key}/delete-transcript-lines")
async def delete_meeting_lines(request: Request, key: UUID, body: MeetingLineDeletion):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Edit transcripts from the Jaro workspace.")
    require_meetings()
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    async def edit():
        await run_in_threadpool(meeting_edits.delete_lines, state.library, str(key), body.revision, body.lines)
        state.insights.wake.set()
        await publish_meeting_exports(state, key=str(key))
        return await run_in_threadpool(state.library.get, str(key), include_turns=True)

    try:
        return await asyncio.shield(start_job(state, edit()))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.put("/api/sessions/{key}/meeting-title")
async def rename_saved_meeting(request: Request, key: UUID, title: Annotated[str, Body(embed=True, min_length=1, max_length=200)]):
    if request.headers.get("origin") != URL:
        place = "this computer" if getattr(request.app.state, "portable_workspace", False) else "this Mac"
        raise HTTPException(403, f"Rename meetings from the Jaro workspace on {place}.")
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    async def rename():
        name = await run_in_threadpool(state.library.rename_meeting, str(key), title)
        await publish_meeting_exports(state, key=str(key))
        with state.library.connection() as db:
            return {"title": name, "export": meeting_exports.read(db, str(key))}

    try:
        return await asyncio.shield(start_job(state, rename()))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/native/meeting-exports")
def meeting_export_preference(request: Request):
    check_native(request)
    with request.app.state.library.connection() as db:
        return meeting_exports.preferences(db)


class MeetingExportPreference(BaseModel):
    folder: str | None = Field(max_length=4096)


@app.put("/api/native/meeting-exports")
def set_meeting_export_preference(request: Request, preference: MeetingExportPreference):
    check_native(request)
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    try:
        with state.library.connection() as db:
            meeting_exports.set_folder(db, state.library.root, preference.folder)
            return meeting_exports.preferences(db)
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(400, getattr(error, "strerror", None) or str(error)) from error


@app.put("/api/native/meeting-exports/{option}")
def set_meeting_export_option(request: Request, option: Literal["notes", "descriptive"], enabled: Annotated[bool, Body(embed=True, strict=True)]):
    check_native(request)
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    with state.library.connection() as db:
        if option == "notes":
            meeting_exports.set_separate_notes(db, enabled)
        else:
            meeting_exports.set_descriptive_names(db, enabled)
        return meeting_exports.preferences(db)


@app.get("/api/native/meeting-exports/{key}")
def locate_meeting_export(request: Request, key: UUID):
    check_native(request)
    with request.app.state.library.connection() as db:
        row = db.execute("SELECT * FROM meeting_exports WHERE id=? AND status='ready'", (str(key),)).fetchone()
    if row is not None:
        try:
            with meeting_exports.checked_folder(row) as directory:
                return {"path": str(meeting_exports.transcript_path(directory, row))}
        except OSError:
            pass
    raise HTTPException(404, "The exported transcript is no longer at its saved location.")


@app.get("/api/native/meetings/{key}/recording-files")
def native_meeting_recording_files(request: Request, key: UUID):
    check_native(request)
    try:
        recording = request.app.state.library.meeting_recording_file(str(key))
    except MissingSession as error:
        raise HTTPException(404, "This meeting's recording files are no longer available.") from error
    return JSONResponse({"folder": str(recording.parent), "path": str(recording)}, headers={"Cache-Control": "no-store"})


@app.get("/api/native/meetings/{key}/excerpt")
def native_meeting_excerpt(request: Request, key: UUID):
    check_native(request)
    session = get_meeting(request.app.state.library, str(key), staged=False)
    transcript = session["transcript"]
    turns = session["meetingTurns"] or transcript["segments"]
    if not turns and transcript["text"].strip():
        turns = [{"speaker": None, "start": None, "end": None, "text": transcript["text"]}]
    value = {"turns": turns, "labels": transcript.get("speakerNames", {}),
             "available": True, "settled": session["status"] == "complete"}
    result = JSONResponse(value, headers={"Cache-Control": "no-store"})
    if len(result.body) > 2 * 1024 * 1024:
        raise HTTPException(413, "Open Sessions to copy or export this longer meeting transcript.")
    return result


@app.get("/api/native/meetings/{key}/completion")
def native_meeting_completion(request: Request, key: UUID):
    check_native(request)
    with request.app.state.library.connection() as db:
        row = db.execute(f"""SELECT s.name,s.options,s.status,s.duration,s.processed,s.error,
            length(trim(s.search_text)) AS speech,
            EXISTS(SELECT 1 FROM meeting_finalization f WHERE f.session_id=s.id) AS finalizing
            FROM {readable_source(db)} s WHERE s.id=? AND
            coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)""",
                         (str(key),)).fetchone()
        if not row:
            raise MissingSession(key)
        brief = meeting_brief.read(db, str(key)) or {}
        exported = meeting_exports.read(db, str(key)) or {}
    options = json.loads(row["options"])
    return {"status": "processing" if row["finalizing"] else row["status"],
            "title": options.get("meetingTitle") or (options.get("meetingCalendar") or {}).get("title") or brief.get("title") or
                     options.get("provisionalTitle") or row["name"],
            "duration": row["duration"] or row["processed"], "hasSpeech": bool(row["speech"]),
            "summaryStatus": brief.get("status"), "summary": brief.get("summary"), "error": row["error"],
            "exportStatus": exported.get("status"), "exportError": exported.get("error")}


@app.delete("/api/sessions/{key}/audio")
async def remove_meeting_audio(request: Request, key: UUID):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro from its local launcher.")
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    try:
        removed = await asyncio.shield(start_job(state, run_in_threadpool(state.library.remove_meeting_audio, str(key))))
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(503, "Could not prepare the meeting audio for the Trash.") from error
    return {"audioRemovedAt": removed}


@app.get("/api/meetings/audio-retention")
def get_audio_retention(request: Request):
    return {"days": request.app.state.library.meeting_audio_days()}


@app.get("/api/meetings/retention")
def get_meeting_retention(request: Request):
    return {"days": request.app.state.library.meeting_retention_days()}


class AnnotationRetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mediaDays: int | None
    recordDays: int | None
    includeExisting: bool


class AnnotationFolder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    folder: str | None


def annotation_folder_status(library):
    with library.connection() as db:
        return annotation_storage.status(db, library.audio)


def select_annotation_folder(library, folder):
    with library.connection(write_lock=True) as db:
        annotation_storage.select(db, folder, library.root)
        return annotation_storage.status(db, library.audio)


@app.get("/api/native/annotation-storage")
def get_annotation_storage(request: Request):
    check_native(request)
    return annotation_folder_status(request.app.state.library)


@app.put("/api/native/annotation-storage")
async def put_annotation_storage(request: Request, setting: AnnotationFolder):
    check_native(request)
    require_annotation()
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    try:
        return await asyncio.shield(start_job(state, run_in_threadpool(select_annotation_folder, state.library, setting.folder)))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except OSError as error:
        raise HTTPException(409, "Jaro could not use that folder. Choose a writable location.") from error


class PortableAnnotationStorageCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["storage"]
    storageAction: Literal["status", "set", "reset", "reveal"]
    folder: str | None = Field(default=None, max_length=4096)


def require_annotation_folder_idle(state):
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    take = state.portable_screencast
    if take and (take.saving or take.phase not in ("cancelled", "error")):
        raise HTTPException(409, "Finish the active annotation before changing its folder.")


async def change_portable_annotation_folder(state, folder):
    async with state.portable_recording_lock:
        require_annotation_folder_idle(state)
        return await run_in_threadpool(select_annotation_folder, state.library, folder)


async def run_portable_annotation_storage(state, action, folder=None):
    from desktop_file_dialog import choose_file, file_dialog, reveal_folder

    if not getattr(state, "screen_draft_dir", None):
        raise HTTPException(404, "Annotation storage is unavailable.")
    if (action == "set") != (folder is not None):
        raise HTTPException(422, "Specify a folder only when setting annotation storage.")
    if action == "status":
        return await run_in_threadpool(annotation_folder_status, state.library)
    require_annotation()

    async def choose(dialog):
        current = await run_in_threadpool(annotation_folder_status, state.library)
        selected = await choose_file(dialog, "--choose-annotation-storage-folder",
                                     *((current["folder"],) if current["available"] else ()))
        if selected is None:
            return await run_in_threadpool(annotation_folder_status, state.library)
        return await change_portable_annotation_folder(state, str(selected))

    try:
        if action == "reveal":
            location = await run_in_threadpool(annotation_folder_status, state.library)
            if not location["available"]:
                raise annotation_storage.Unavailable()
            await reveal_folder(location["folder"])
            return location
        if action == "choose":
            require_annotation_folder_idle(state)
            pending = getattr(state, "annotation_folder_picker", None)
            if pending and not pending.done():
                raise HTTPException(409, "The Annotations folder picker is already open.")
            if not (dialog := file_dialog()):
                raise HTTPException(503, "The Annotations folder picker is unavailable.")
            state.annotation_folder_picker = start_job(state, choose(dialog))
            return await asyncio.shield(state.annotation_folder_picker)
        return await asyncio.shield(start_job(state, change_portable_annotation_folder(state, folder)))
    except annotation_storage.Unavailable as error:
        raise HTTPException(503, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        detail = "Jaro could not open the Annotations folder." if action == "reveal" else "Jaro could not use that folder. Choose a writable location."
        raise HTTPException(409, detail) from error


@app.post("/api/portable/annotation-storage")
async def portable_annotation_storage(request: Request, action: Annotated[Literal["status", "choose", "reveal", "reset"], Body(embed=True)]):
    state = screencast_workspace(request)
    if not getattr(request.state, "portable_desktop", False) or request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to access annotation storage.")
    value = await run_portable_annotation_storage(state, action)
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


class AnnotationAgentSkillCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["guide", "status", "install", "update"]
    agent: Literal["claude", "codex", "opencode", "pi"] | None = None


@app.get("/api/portable/agent-setup")
def portable_agent_setup(request: Request):
    if not getattr(request.app.state, "portable_workspace", False):
        raise HTTPException(404, "Agent setup is unavailable in this workspace.")
    from portable_login import installed_launcher

    launcher = installed_launcher(WORKSPACE_ROOT)
    cli = str(launcher.with_name("jardctl.exe" if os.name == "nt" else "jardctl")) if launcher else "jardctl"
    return JSONResponse({"cli": cli}, headers={"Cache-Control": "no-store"})


@app.post("/api/portable/annotation-agent-skill")
async def portable_annotation_agent_skill(request: Request, command: AnnotationAgentSkillCommand):
    from annotation_agent_skill import manage

    state = screencast_workspace(request)
    if not getattr(request.state, "portable_desktop", False) or request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to manage annotation guides.")
    require_annotation()
    writing = command.action in ("install", "update")
    if writing and state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    try:
        work = run_in_threadpool(manage, command.action, command.agent)
        result = await asyncio.shield(start_job(state, work)) if writing else await work
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(409, "Jaro could not access the local agent guide.") from error
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/annotations/retention")
def get_annotation_retention(request: Request):
    with request.app.state.library.connection() as db:
        return annotation_retention.policy(db)


@app.put("/api/annotations/retention")
async def put_annotation_retention(request: Request, setting: AnnotationRetentionPolicy):
    check_client(request)
    require_annotation()
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    def save():
        with state.library.retention_lock, state.library.connection() as db:
            return annotation_retention.set_policy(db, setting.mediaDays, setting.recordDays, setting.includeExisting)

    try:
        result = await asyncio.shield(start_job(state, run_in_threadpool(save)))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    state.annotation_expiry_due = 0
    return result


@app.get("/api/meetings/automatic-notes")
def get_automatic_meeting_notes(request: Request):
    check_client(request)
    if meetings_managed():
        return JSONResponse({"enabled": False, "managed": True}, headers={"Cache-Control": "no-store"})
    with request.app.state.library.connection() as db:
        enabled, _ = meeting_brief.automatic_policy(db)
    return JSONResponse({"enabled": enabled}, headers={"Cache-Control": "no-store"})


@app.put("/api/meetings/automatic-notes")
def set_automatic_meeting_notes(request: Request, enabled: Annotated[bool, Body(embed=True, strict=True)]):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Change meeting notes from the Jaro workspace.")
    require_meetings()
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    with state.library.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        meeting_brief.set_automatic(db, enabled)
    return {"enabled": enabled}


@app.put("/api/meetings/retention")
async def set_meeting_retention(request: Request, days: int):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Change retention from the Jaro workspace.")
    if days not in (0, 1, 7, 30):
        raise HTTPException(400, "Choose one of the meeting retention periods.")
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    async def update():
        await run_in_threadpool(state.library.set_meeting_retention_days, days)
        start_job(state, sweep_meeting_retention(state))

    await asyncio.shield(start_job(state, update()))
    return {"days": days}


@app.put("/api/meetings/audio-retention")
async def set_audio_retention(request: Request, days: int):
    check_client(request)
    if getattr(request.app.state, "portable_workspace", False) and request.headers.get("origin") != URL:
        raise HTTPException(403, "Change retention from the Jaro workspace.")
    if days not in (-1, 0, 1, 7, 30):
        raise HTTPException(400, "Choose one of the meeting audio retention periods.")
    state = request.app.state
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    async def update():
        await run_in_threadpool(state.library.set_meeting_audio_days, days or None)
        start_job(state, sweep_meeting_retention(state))

    await asyncio.shield(start_job(state, update()))
    return {"days": days or None}


@app.delete("/api/sessions/{key}")
async def delete_session(request: Request, key: UUID):
    return await trash_session(request, key)


@app.delete("/api/native/meetings/{key}")
async def delete_meeting(request: Request, key: UUID):
    check_native(request)
    return await trash_session(request, key, meeting_only=True)


async def trash_session(request: Request, key: UUID, *, meeting_only=False):
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro from its local launcher.")
    waiting = await remove_saved_session(request.app.state, str(key), meeting_only=meeting_only)
    if waiting:
        return JSONResponse({"trashPending": True}, status_code=202)
    return Response(status_code=204)


async def remove_saved_session(state, key, *, meeting_only):
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    async def remove():
        waiting = await run_in_threadpool(state.library.delete, key, meeting_only=meeting_only)
        state.sessions_revision = token_hex(16)
        return waiting

    try:
        return await asyncio.shield(start_job(state, remove()))
    except MissingSession as error:
        if not meeting_only:
            raise
        raise HTTPException(404, "Meeting not found.") from error
    except annotation_storage.Unavailable:
        raise
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except (OSError, sqlite3.Error) as error:
        logger.exception("Could not remove a session.")
        raise HTTPException(503, "Could not prepare the session for removal. Try again.") from error


async def trash_meeting_from_linux_command(state, key):
    from portable_capture import CaptureError

    try:
        waiting = await remove_saved_session(state, key, meeting_only=True)
    except HTTPException as error:
        raise CaptureError(str(error.detail)) from error
    return {"id": key, "trashPending": waiting}


def check_audio_request(request: Request):
    if request.headers.get("origin", URL) != URL:
        raise HTTPException(403, "Open Jaro from its local launcher.")
    if request.headers.get("content-type") != "application/octet-stream":
        raise HTTPException(415, "Expected audio bytes.")


def transcription_languages(request: Request, *, require_ready=True) -> tuple[str, ...]:
    if require_ready and request.app.state.status != "ready":
        raise HTTPException(503, "The local model is unavailable.")

    languages = tuple(dict.fromkeys(request.query_params.getlist("language")))
    if any(code not in LANGUAGES for code in languages):
        raise HTTPException(400, "Choose a language from the list.")
    return languages


async def run_inference(state, function, *args, dictation=False, **kwargs):
    await state.inference.acquire(dictation=dictation)
    worker = asyncio.create_task(run_in_threadpool(function, *args, **kwargs))

    async def release_when_done():
        try:
            with suppress(BaseException):
                await worker
        finally:
            await state.inference.release()
            schedule_cache_trim(state)

    try:
        result = await asyncio.shield(worker)
    except BaseException:
        if not worker.done():
            # Caller was cancelled while the threadpool worker is still executing:
            # keep `state.inference.active` held until the worker actually exits so
            # no subsequent dictation or finalization runs concurrently on the model.
            release_task = asyncio.create_task(release_when_done())
            if hasattr(state, "jobs"):
                state.jobs.add(release_task)
                release_task.add_done_callback(state.jobs.discard)
        else:
            await state.inference.release()
            schedule_cache_trim(state)
        raise
    await state.inference.release()
    schedule_cache_trim(state)
    return result


async def speech_vocabulary(state):
    if getattr(state, "sdk_embedded", False):
        return state.embedded_vocabulary
    return await run_in_threadpool(shared_vocabulary)


async def decode_pcm(state, pcm: bytes, languages, speakers=None, offset=0, live=False, vocabulary=(), *,
                     timed_output=False, dictation=False, measure_inference=False, screen_nonspeech=False,
                     speech_prefix_samples=None):
    queued_at = time.monotonic_ns() if measure_inference else None

    def inference():
        queue_ns = time.monotonic_ns() - queued_at if queued_at is not None else None
        if live and speakers:
            speakers.push(pcm, offset)
            state.diarizer_last_used = time.monotonic()
        model = state.model
        if (screen_nonspeech and getattr(state, "sdk_embedded", False)
                and not getattr(state, "speech_screen_prepared", False)):
            state.speech_screen_prepared = True
            try:
                model.enable_alignment()
            except Exception:
                logger.warning("The local speech screen is unavailable.")

        def recognize(cache_implementation=None):
            options = {"vocabulary": vocabulary} if vocabulary else {}
            if screen_nonspeech:
                options["screen_nonspeech"] = True
                if speech_prefix_samples is not None:
                    options["speech_prefix_samples"] = speech_prefix_samples
            return model.transcribe(
                pcm, languages, timed=not live or speakers is not None or timed_output, cache_implementation=cache_implementation,
                **options,
            )

        started = time.monotonic_ns() if measure_inference else None
        try:
            # Fixed-size decoder storage avoids growing tensors per token across live, dictation, and final passes.
            recognized = recognize("static")
        except RuntimeError as error:
            if "out of memory" not in str(error).lower():
                raise
            recognized = None
        if recognized is None:
            # Retry outside the exception handler so its traceback can release tensors.
            model.release_device_cache()
            logger.warning("Retrying ASR with a dynamic cache after a memory error at %.2f seconds.", offset / SAMPLE_RATE)
            recognized = recognize()
        elapsed = time.monotonic_ns() - started if started is not None else None
        text, language, words = recognized
        duration = len(pcm) / (4 * SAMPLE_RATE)
        segments, word_times = speakers.label(
            text, words, offset, duration, include_word_times=not live or timed_output,
        ) if speakers else ([], [])
        if speakers:
            state.diarizer_last_used = time.monotonic()
        if not speakers and (not live or timed_output):
            from speaker_alignment import word_records

            word_times = word_records(text, words, offset / SAMPLE_RATE, duration)
        result = {"text": text, "language": language, "segments": segments, "words": word_times}
        if elapsed is not None:
            result["inferenceNs"] = elapsed
            result["speechQueueNs"] = queue_ns
        if speakers and not live:
            result["activity"] = speakers.intervals(offset, duration)
        return result

    return await run_inference(state, inference, dictation=dictation)


async def resample_capture(data, rate=MEETING_RATE):
    fast = await run_in_threadpool(decimate_pcm16_to_16k_f32, data, rate)
    if fast is not None:
        return fast
    proc = await asyncio.create_subprocess_exec(
        codec_binary("ffmpeg"), "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
        "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        pcm, _ = await proc.communicate(data)
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode or not pcm:
        raise ValueError("Could not decode captured audio.")
    return pcm


async def decode_meeting(state, tracks, start, frames, languages, *, vocabulary, live=False):
    window = await run_in_threadpool(read_window, tracks, start, frames)
    if window is None:
        raise HTTPException(409, "The requested audio has not been written yet.")
    clips, echo = window
    start_time, end_time = start / MEETING_RATE, (start + frames) / MEETING_RATE
    if frames < MEETING_FRAME:
        # A residual sub-20 ms buffer cannot carry an utterance. Count it in the timeline.
        result = merge_sources({name: {"words": []} for name in clips}, None, start_time, end_time)
    else:
        recognized = {}
        for name, data in clips.items():
            pcm = await resample_capture(data)
            recognized[name] = await decode_pcm(state, pcm, languages, offset=start * SAMPLE_RATE // MEETING_RATE,
                                                live=live, timed_output=True, vocabulary=vocabulary)
        result = await run_in_threadpool(merge_sources, recognized, echo, start_time, end_time)
    result["meetingCoverage"] = {str(speaker): [[start_time, end_time]] for speaker in (1, 2)}
    return result


def ensure_meeting_turn(result, start, end):
    if result["text"] and not result["segments"]:
        result["segments"] = [{"speaker": None, "start": start, "end": end, "text": result["text"]}]


async def live_speakers(request, session_id):
    state = request.app.state
    if not session_id:
        return None
    diarizer = await ensure_diarizer(state)
    if diarizer is None:
        return None
    now = time.monotonic()
    for key, (used, _) in list(state.live_speakers.items()):
        if now - used > 120:
            del state.live_speakers[key]
    existing = state.live_speakers.get(session_id)
    speakers = existing[1] if existing else diarizer.session(live=True)
    state.live_speakers[session_id] = (now, speakers)
    return speakers


@app.post("/api/transcribe")
async def transcribe(request: Request, session: UUID | None = None, offset: int = 0):
    check_audio_request(request)
    languages = transcription_languages(request)
    pcm = bytearray()
    async for packet in request.stream():
        if len(pcm) + len(packet) > MAX_BYTES:
            raise HTTPException(413, "Send up to 25 seconds of audio per request.")
        pcm.extend(packet)
    if not pcm or len(pcm) % 4:
        raise HTTPException(400, "The audio could not be read.")
    if offset < 0:
        raise HTTPException(400, "The live audio offset could not be read.")

    try:
        state = request.app.state
        vocabulary = await speech_vocabulary(state)
        async with state.insights.model.audio_priority():
            result = await decode_pcm(state, bytes(pcm), languages, await live_speakers(request, session), offset,
                                      live=True, vocabulary=vocabulary, screen_nonspeech=True)
        start_time = offset / SAMPLE_RATE
        ensure_meeting_turn(result, start_time, start_time + len(pcm) / (4 * SAMPLE_RATE))
        return result
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        logger.exception("Rumble inference failed.")
        raise HTTPException(500, "Transcription failed. Try the audio again.") from error


async def save_upload(request: Request, source: Path, *, maximum=None):
    size = 0
    with source.open("wb") as output:
        async for packet in request.stream():
            size += len(packet)
            if maximum is not None and size > maximum:
                raise HTTPException(413, {"code": "audio-too-large", "message": "The audio exceeds the SDK upload limit."})
            await asyncio.to_thread(output.write, packet)


@app.post("/api/playback")
async def playback(request: Request, saved: UUID | None = None, track: Literal["mic", "system"] | None = None):
    check_audio_request(request)
    folder = tempfile.TemporaryDirectory(prefix="soundfolio-player-")
    source = Path(folder.name) / "source"
    target = source.with_suffix(".m4a")
    try:
        if saved:
            source, _ = request.app.state.library.media(str(saved), track=track)
        else:
            await save_upload(request, source)
        await make_playable(source, target)
        return FileResponse(target, media_type="audio/mp4", background=BackgroundTask(folder.cleanup))
    except ValueError as error:
        folder.cleanup()
        raise HTTPException(400, str(error)) from error
    except BaseException:
        folder.cleanup()
        raise


@app.post("/api/transcribe-file")
async def transcribe_file(
    request: Request, recording: bool = False, diarization: bool = False, session: UUID | None = None,
    name: str = "", mime: str = "", live: bool = False, progress_only: bool = False,
    notes_draft: UUID | None = None,
):
    check_audio_request(request)
    languages = transcription_languages(request)
    if session:
        request.app.state.live_speakers.pop(session, None)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=request.app.state.library.root) as folder:
        source = Path(folder) / "audio"
        await save_upload(request, source)
        return await accept_audio(request.app.state, source, name, mime, languages, recording, diarization, live, progress_only,
                                  notes_draft=str(notes_draft) if notes_draft else None)


async def accept_audio(state, source, name, mime, languages, recording, diarization, live, progress_only, stage_id=None, *,
                       notes_draft=None, tracks=None, meeting_provider=None, meeting_calendar=None, meeting_started=None,
                       rolling=(), applies_retention=True, annotation=None, annotation_location=None,
                       provisional_title=None, meeting_title=None,
                       meeting_microphone_warning=None, meeting_voice_input=None):
    library = state.library
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    try:
        video_only = False
        if annotation and mime == "video/mp4":
            video_duration, has_audio = await annotation_movie_info(source)
            video_only = not has_audio
            if has_audio and state.status != "ready":
                raise HTTPException(503, "The local model is unavailable.")
        duration = video_duration if video_only else await audio_duration(source)
        limit = MAX_AUDIO_SECONDS + int(recording)
        if duration is not None and duration > limit:
            raise ValueError(DURATION_ERROR)
        meeting = bool(meeting_provider)
        identify_speakers = not video_only and (diarization or meeting) and diarization_ready(state)
        options = dict(recording=recording, live=live, diarization=identify_speakers,
                       languageHints=list(languages), model=MODEL_ID, modelRevision=REVISION,
                       speakerModel=DIARIZATION_ID if identify_speakers else None,
                       speakerModelRevision=DIARIZATION_REVISION if identify_speakers else None)
        if meeting:
            options.update(meeting=True, meetingProvider=meeting_provider, retentionApplies=int(applies_retention),
                           audioChunking=PAUSE_AWARE_CHUNKING, meetingSpeakerMode=MIXED_SPEAKERS)
            if provisional_title:
                options["provisionalTitle"] = provisional_title
            if meeting_title:
                options["meetingTitle"] = meeting_title
            if meeting_microphone_warning == "digitalSilence":
                options["meetingMicrophoneWarning"] = meeting_microphone_warning
            if meeting_voice_input and tracks and "mic" in tracks:
                options["meetingVoiceInput"] = meeting_voice_input
        if meeting_started:
            options["meetingStartedAt"] = meeting_started.isoformat()
            if duration is not None:
                options["meetingEndedAt"] = (meeting_started + timedelta(seconds=duration)).isoformat()
        if meeting_calendar:
            options["meetingCalendar"] = meeting_calendar
            name = meeting_calendar["title"].translate(str.maketrans({"/": "／", "\\": "＼", ":": "꞉"}))
        key, source = await run_in_threadpool(library.create, source, name, mime, options, duration,
                                              key=stage_id, notes_draft=notes_draft, tracks=tracks, rolling=rolling,
                                              annotation=annotation, annotation_location=annotation_location,
                                              video_only=video_only)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except annotation_storage.Unavailable:
        raise
    except FileNotFoundError as error:
        raise HTTPException(503, "Could not open the audio or find FFmpeg.") from error
    except OSError as error:
        logger.exception("Could not prepare audio.")
        raise HTTPException(500, "Could not prepare audio. Check available disk space.") from error

    if video_only:
        return JSONResponse({"id": key}, status_code=202)

    queue = asyncio.Queue()
    connected = stage_id is None

    def emit(**payload):
        if connected:
            queue.put_nowait(json.dumps(payload, ensure_ascii=False) + "\n")

    job = start_job(state, process_saved_audio(
        state, key, source, options, duration,
        progress_only=progress_only, emit=emit,
    ))
    job.add_done_callback(lambda _: queue.put_nowait(None))

    if stage_id is not None:
        return JSONResponse({"id": key}, status_code=202)

    async def stream():
        nonlocal connected
        try:
            while (line := await queue.get()) is not None:
                yield line
        finally:
            connected = False

    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"X-Session-ID": key})


def start_job(state, work):
    job = asyncio.create_task(work)
    state.jobs.add(job)
    job.add_done_callback(state.jobs.discard)
    return job


def portable_capture_state(request):
    state = request.app.state
    if not getattr(state, "portable_recording_dir", None):
        raise HTTPException(404, "The desktop audio recorder is unavailable.")
    if request.method != "GET" and request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to record audio.")
    return state


def portable_recording_busy(state):
    return (state.draining or state.portable_recording or getattr(state, "portable_screencast", None)
            or getattr(getattr(state, "portable_sdk_capture", None), "busy", False))


def portable_capture_busy(state):
    return (portable_recording_busy(state)
            or (state.portable_dictation and state.portable_dictation.busy))


@app.post("/api/portable/recording", status_code=201)
async def start_portable_recording(request: Request, mode: Literal["mic", "system", "both"] = "mic", meeting: bool = False,
                                   live: bool = False, diarization: bool = False):
    state = portable_capture_state(request)
    languages = transcription_languages(request)
    watcher = getattr(state, "portable_calls", None)
    if meeting and mode == "both" and watcher and watcher.preferences["stopRequestedCalls"]:
        from portable_call_pages import Call

        async with watcher.transition:
            key = await watcher.begin(Call("", ""), languages, show_unmatched=False)
        return {"id": key, "sampleRate": SAMPLE_RATE}
    return await begin_portable_recording(state, mode, meeting, languages, live=live, diarization=diarization)


async def begin_portable_recording(state, mode, meeting, languages, *, provider="Meeting", call_binaries=(), title="",
                                    live=False, diarization=False):
    from portable_capture import CaptureError, claim_meeting_draft, clear_draft
    from recording_power import keep_awake_until

    if meeting:
        require_meetings()
        if mode != "both":
            raise HTTPException(400, "Meeting recording uses the microphone and computer audio.")
    async with state.portable_recording_lock:
        if portable_recording_busy(state):
            raise HTTPException(409, "Finish or recover the current recording first.")
        key = uuid4()
        path = state.portable_recording_dir / f"{key}.wav"
        recorder = state.portable_meeting_recorder if meeting else state.portable_recorder
        capture = recorder(key, path, mode, call_binaries=call_binaries) if call_binaries else recorder(key, path, mode)
        capture.meeting = meeting
        claim = None
        state.portable_system_audio_starting = mode in ("system", "both")
        try:
            if meeting:
                voice = await run_in_threadpool(read_meeting_voice_input, state)
                metadata = {"meetingProvider": provider, "meetingStartedAt": datetime.now(timezone.utc).isoformat(),
                            "languageHints": list(languages), "meetingVoiceInput": voice}
                if title:
                    metadata["meetingTitle"] = title
                try:
                    claim = claim_meeting_draft(path, metadata)
                except OSError as error:
                    raise HTTPException(503, "Could not prepare the meeting draft.") from error
                if voice["noiseCleanup"]:
                    stupase_cleanup(state).prepare()
            if mode in ("system", "both") and (dictation := getattr(state, "portable_dictation", None)):
                await dictation.audio_control.settle()
            await capture.start()
        except BaseException as error:
            if claim:
                claim.close()
                if not capture.frames:
                    with suppress(OSError):
                        clear_draft(path)
            if isinstance(error, CaptureError):
                raise HTTPException(503, str(error)) from error
            raise
        else:
            state.portable_recording = capture
        finally:
            state.portable_system_audio_starting = False
        if meeting:
            capture.meeting_claim = claim
            capture.meeting_metadata = metadata
        capture.preview_live = live
        capture.preview_enabled = meeting or live
        capture.preview_chunks = deque(maxlen=160)
        capture.preview_processed = 0
        capture.preview_tentative = ""
        capture.preview_revision = 0
        capture.preview_task = start_job(state, preview_portable_recording(state, capture, languages, meeting or diarization))
        keep_awake_until(capture.worker, "Jaro is recording audio")
        schedule_prewarm(state)
    return {"id": key, "sampleRate": SAMPLE_RATE}


def portable_preview_pcm(path, start, frames):
    import numpy as np
    import wave

    try:
        with wave.open(str(path), "rb") as audio:
            audio.setpos(start)
            data = audio.readframes(frames)
    except wave.Error as error:
        raise ValueError("Could not read the live recording audio.") from error
    if len(data) != frames * 2:
        raise EOFError("The requested recording audio is still being written.")
    return (np.frombuffer(data, dtype="<i2").astype("<f4") / 32768).tobytes()


async def preview_portable_recording(state, capture, languages, diarization):
    conversion = MEETING_RATE // SAMPLE_RATE
    planner = LiveWindowPlanner(SAMPLE_RATE)
    speakers = None
    vocabulary = None
    delayed = False

    def read_slice(start, count):
        return read_wav_i16_slice(capture.path, start, count)

    while state.portable_recording is capture and not capture.worker.done():
        await asyncio.sleep(.2)
        if state.portable_recording is not capture or capture.worker.done():
            return
        if not capture.preview_enabled:
            if getattr(capture, "preview_tentative", ""):
                capture.preview_tentative = ""
                capture.preview_revision = getattr(capture, "preview_revision", 0) + 1
            continue
        if (state.draining or state.status != "ready"
                or (state.portable_dictation and state.portable_dictation.busy)):
            continue
        inference = getattr(state, "inference", None)
        under_load = bool(inference and (inference.active or inference.waiting_dictation))
        show_live = bool(getattr(capture, "preview_live", capture.preview_enabled))
        action = await run_in_threadpool(
            planner.plan, capture.frames, read_slice, show_live=show_live, under_load=under_load,
        )
        if isinstance(action, SkipAction):
            planner.jump_to(action.cursor)
            if getattr(capture, "preview_tentative", ""):
                capture.preview_tentative = ""
                capture.preview_revision = getattr(capture, "preview_revision", 0) + 1
            if not delayed:
                logger.warning("The live transcript is delayed. Recording continues.")
                delayed = True
            continue
        if not isinstance(action, (TentativeAction, CommitAction)):
            continue
        try:
            if vocabulary is None:
                vocabulary = await speech_vocabulary(state)
                diarizer = await ensure_diarizer(state) if diarization else None
                speakers = diarizer.session(live=True) if diarizer else None
            pcm = await run_in_threadpool(portable_preview_pcm, capture.path, action.start, action.frames)
            if state.portable_recording is not capture:
                return
            if not capture.preview_enabled or (state.portable_dictation and state.portable_dictation.busy):
                continue
            if isinstance(action, TentativeAction):
                if not getattr(capture, "preview_live", capture.preview_enabled):
                    continue
                async with state.insights.model.audio_priority():
                    result = await decode_pcm(state, pcm, languages, None, action.start, live=True, timed_output=False,
                                              vocabulary=vocabulary, screen_nonspeech=True)
                if state.portable_recording is not capture:
                    return
                planner.mark_tentative(action.start + action.frames)
                tentative = (result.get("text") or "").strip() if getattr(capture, "preview_live", capture.preview_enabled) else ""
                if tentative != getattr(capture, "preview_tentative", ""):
                    capture.preview_tentative = tentative
                    capture.preview_revision = getattr(capture, "preview_revision", 0) + 1
                delayed = False
                continue

            async with state.insights.model.audio_priority():
                decoded = await decode_pcm(state, pcm, languages, speakers, action.start, live=True, timed_output=True,
                                           vocabulary=vocabulary, screen_nonspeech=True)
            if state.portable_recording is not capture:
                return
            result = {**decoded, "segments": [dict(segment) for segment in decoded.get("segments", [])],
                      "words": [dict(word) for word in decoded.get("words", [])]}
            if action.forced and capture.preview_chunks and result.get("text"):
                trimmed = trim_boundary_overlap(capture.preview_chunks[-1]["text"], result["text"])
                if trimmed != result["text"]:
                    result["text"] = trimmed
                    if len(result["segments"]) == 1:
                        result["segments"][0]["text"] = trimmed
            end = action.start + action.frames
            ensure_meeting_turn(result, action.start / SAMPLE_RATE, end / SAMPLE_RATE)
            result["words"] = [{"speaker": None, **word} for word in result["words"]]
            result.update(activity=[], meetingSpeakerMode=MIXED_SPEAKERS)
            await run_in_threadpool(append_rolling, capture.path, action.start * conversion, end * conversion, result)
        except Exception as error:
            if not delayed:
                logger.warning("The live transcript is delayed (%s). Recording continues.", type(error).__name__)
            delayed = True
            continue
        delayed = False
        planner.mark_committed(end)
        capture.preview_processed = end
        capture.preview_tentative = ""
        capture.preview_revision = getattr(capture, "preview_revision", 0) + 1
        if result["text"]:
            capture.preview_chunks.append(result)


def portable_live_meeting(state):
    capture = state.portable_recording
    metadata = getattr(capture, "meeting_metadata", None)
    if not metadata:
        return None
    started = metadata["meetingStartedAt"]
    provider = metadata["meetingProvider"]
    return {"id": str(capture.key), "name": metadata.get("meetingTitle") or
            (f"{provider} meeting" if provider != "Meeting" else provider),
            "meetingProvider": provider, "meeting": True, "created": started, "meetingStartedAt": started,
            "duration": capture.frames / SAMPLE_RATE, "processed": capture.preview_processed / SAMPLE_RATE,
            "status": "recording", "meetingSpeakerMode": MIXED_SPEAKERS}


def portable_has_live_transcript(state):
    capture = state.portable_recording
    return bool(getattr(capture, "meeting_metadata", None) and capture.preview_chunks)


@app.get("/api/portable/recording/{key}/preview")
async def portable_recording_preview(request: Request, key: UUID, after: int = Query(0, ge=0),
                                     rev: int | None = Query(None, ge=0)):
    state = portable_capture_state(request)
    capture = state.portable_recording
    if not capture or capture.key != key:
        raise HTTPException(404, "This recording has ended.")
    revision = getattr(capture, "preview_revision", 0)
    if capture.preview_processed == after and (rev is None or revision == rev):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    chunks = tuple(capture.preview_chunks)
    return JSONResponse({"text": " ".join(chunk["text"] for chunk in chunks),
                         "tentative": getattr(capture, "preview_tentative", ""),
                         "segments": [segment for chunk in chunks for segment in chunk["segments"]],
                         "through": capture.preview_processed,
                         "rev": revision},
                        headers={"Cache-Control": "no-store"})


@app.put("/api/portable/recording/{key}/preview", status_code=204)
async def set_portable_recording_preview(request: Request, key: UUID, live: Annotated[bool, Body(embed=True, strict=True)]):
    state = portable_capture_state(request)
    async with state.portable_recording_lock:
        capture = state.portable_recording
        if not capture or capture.key != key or capture.worker.done():
            raise HTTPException(404, "This recording has ended.")
        # Meetings keep an excerpt available for Copy New and Copy All.
        capture.preview_live = live
        capture.preview_enabled = bool(getattr(capture, "meeting_metadata", None)) or live
        if not live and getattr(capture, "preview_tentative", ""):
            capture.preview_tentative = ""
            capture.preview_revision = getattr(capture, "preview_revision", 0) + 1
    return Response(status_code=204)


def portable_call_watcher(request):
    state = portable_capture_state(request)
    if watcher := getattr(state, "portable_calls", None):
        return watcher
    raise HTTPException(404, "Call detection is unavailable.")


class PortableCallSettings(BaseModel):
    enabled: StrictBool
    autoStop: StrictBool
    stopRequestedCalls: StrictBool = False
    policies: dict[str, Literal["ask", "never", "auto"]]
    defaultPolicy: Literal["ask", "never", "auto"] = "ask"


@app.get("/api/portable/calls")
async def portable_call_status(request: Request):
    return JSONResponse(portable_call_watcher(request).status(), headers={"Cache-Control": "no-store"})


@app.put("/api/portable/calls/settings")
async def change_portable_call_settings(request: Request, preferences: PortableCallSettings):
    watcher = portable_call_watcher(request)
    if watcher.state.draining:
        raise HTTPException(409, "Jard is preparing to restart.")
    async with watcher.transition:
        watcher.settings(preferences.model_dump(exclude_unset=True))
        return watcher.status()


class PortableCallSwitch(BaseModel):
    action: Literal["detection", "autoStop", "stopRequestedCalls"]
    enabled: StrictBool


class PortableCallDefault(BaseModel):
    action: Literal["setDefaultPolicy"]
    policy: Literal["ask", "never", "auto"]


class PortableCallRule(BaseModel):
    action: Literal["setPolicy"]
    provider: str
    policy: Literal["inherit", "ask", "never", "auto"]


@app.patch("/api/portable/calls/settings")
async def edit_portable_call_settings(request: Request, change: Annotated[
    PortableCallSwitch | PortableCallDefault | PortableCallRule, Field(discriminator="action")
]):
    watcher = portable_call_watcher(request)
    if watcher.state.draining:
        raise HTTPException(409, "Jard is preparing to restart.")
    async with watcher.transition:
        preferences = watcher.preferences.copy()
        if isinstance(change, PortableCallSwitch):
            preferences["enabled" if change.action == "detection" else change.action] = change.enabled
        elif isinstance(change, PortableCallDefault):
            preferences["defaultPolicy"] = change.policy
        else:
            policies = preferences["policies"].copy()
            if change.policy == "inherit":
                policies.pop(change.provider, None)
            else:
                policies[change.provider] = change.policy
            preferences["policies"] = policies
        watcher.settings(preferences)
        return watcher.status()


@app.post("/api/portable/calls/offer/{token}/{action}")
async def answer_portable_offer(request: Request, token: str, action: Literal["record", "dismiss", "more", "back", "always", "never"]):
    watcher = portable_call_watcher(request)
    languages = transcription_languages(request) if action in ("record", "always") else ()
    await watcher.answer(token, action, languages)
    return watcher.status()


@app.post("/api/portable/calls/recording/{key}/{action}")
async def act_on_portable_call(request: Request, key: UUID, action: Literal["stop", "keep"]):
    watcher = portable_call_watcher(request)
    if action == "stop":
        await watcher.stop(key)
    else:
        watcher.keep_recording(key)
    return watcher.status()


def read_meeting_voice_input(state):
    with state.library.connection() as db:
        row = db.execute("SELECT noiseCleanup, boostMicrophone FROM meeting_voice_input WHERE id=1").fetchone()
    return {"noiseCleanup": bool(row and row[0]), "boostMicrophone": bool(row and row[1])}


@app.get("/api/portable/meeting-voice-input")
def get_portable_meeting_voice_input(request: Request):
    state = portable_capture_state(request)
    voice = read_meeting_voice_input(state)
    cleanup = getattr(state, "stupase_cleanup", None)
    status = cleanup.status() if cleanup is not None else stupase_cleanup(state).status()
    return {**voice, "cleanupStatus": status}


class MeetingVoiceInputChange(BaseModel):
    setting: Literal["noiseCleanup", "boostMicrophone"]
    enabled: StrictBool


@app.put("/api/portable/meeting-voice-input")
async def change_portable_meeting_voice_input(request: Request, change: MeetingVoiceInputChange):
    require_meetings()
    state = portable_capture_state(request)
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")

    def update():
        with state.library.connection() as db:
            db.execute("INSERT OR IGNORE INTO meeting_voice_input(id) VALUES(1)")
            db.execute(f"UPDATE meeting_voice_input SET {change.setting}=? WHERE id=1", (int(change.enabled),))
        return read_meeting_voice_input(state)

    voice = await run_in_threadpool(update)
    cleanup = stupase_cleanup(state)
    if change.setting == "noiseCleanup" and change.enabled:
        cleanup.prepare(retry=True)
    return {**voice, "cleanupStatus": cleanup.status()}


@app.get("/api/portable/dictation")
def portable_dictation_status(request: Request):
    status = portable_dictation(request).snapshot()
    if status["platform"] == "windows" and getattr(request.state, "portable_desktop", False):
        from windows_capture import microphone_privacy_denied

        status["microphoneDenied"] = microphone_privacy_denied()
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


def portable_onboarding_state(state):
    if (onboarding := getattr(state, "portable_onboarding", None)) is None:
        raise HTTPException(404, "Dictation setup is unavailable on this desktop.")
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    if portable_capture_busy(state) or state.portable_dictation.authorizing or getattr(state.portable_dictation, "paste_authorizing", False):
        raise HTTPException(409, "Finish the current recording or desktop approval before changing dictation setup.")
    return onboarding


def restart_portable_onboarding(state, *, foreground=False):
    onboarding = portable_onboarding_state(state)
    try:
        if foreground:
            tray = Path(os.environ.get("JARO_DESKTOP_TRAY", ""))
            if not tray.is_absolute() or not tray.is_dir():
                raise OSError("Native workspace unavailable")
            (tray / "open").touch()
        return {"id": onboarding.restart()}
    except OSError as error:
        raise HTTPException(503, "Could not replay dictation setup. Reopen Jaro and try again.") from error


class OnboardingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["restart", "finish", "microphone"]
    id: UUID | None = None


@app.post("/api/portable/onboarding")
async def change_portable_onboarding(request: Request, operation: OnboardingAction):
    if request.headers.get("origin") != URL or not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Open Jaro’s desktop workspace to run dictation setup.")
    state = request.app.state
    if operation.action == "restart":
        return restart_portable_onboarding(state)
    onboarding = portable_onboarding_state(state)
    if operation.id is None or onboarding.id != str(operation.id):
        raise HTTPException(409, "This setup run has changed. Try again in the current window.")
    try:
        if operation.action == "microphone":
            if sys.platform != "win32":
                raise HTTPException(404, "Microphone privacy settings are available on Windows.")
            os.startfile("ms-settings:privacy-microphone")
            return {"id": onboarding.id}
        onboarding.finish(str(operation.id))
        return {"id": None}
    except OSError as error:
        raise HTTPException(503, "Could not finish the setup action. Try again.") from error


def portable_dictation(request):
    dictation = portable_capture_state(request).portable_dictation
    if dictation is None:
        raise HTTPException(404, "System dictation is unavailable on this desktop.")
    return dictation


@app.get(windows_dictation_commands.ROUTE)
def identify_windows_dictation(request: Request, challenge: str):
    record = getattr(request.app.state, "windows_dictation_control", None)
    if record is None or not re.fullmatch(r"[0-9a-f]{32}", challenge):
        raise HTTPException(404, "The Windows dictation command endpoint is unavailable.")
    return JSONResponse({"proof": sdk_proof(record, challenge)}, headers={"Cache-Control": "no-store"})


class WindowsDictationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop", "toggle", "cancel", "paste-last"]
    raw: StrictBool = False
    printed: StrictBool = False


@app.post(windows_dictation_commands.ROUTE)
async def run_windows_dictation(request: Request, command: WindowsDictationCommand):
    from portable_capture import CaptureError

    record = getattr(request.app.state, "windows_dictation_control", None)
    if not windows_dictation_commands.authorized(record, request.headers.get("authorization", "")):
        raise HTTPException(403, "Valid Windows dictation credentials are required.")
    if command.raw and command.action not in ("start", "toggle") or command.printed and command.action != "stop":
        raise HTTPException(422, "Use raw with start or toggle, and print with stop.")
    dictation = portable_dictation(request)
    if dictation.platform != "windows":
        raise HTTPException(404, "The Windows dictation command endpoint is unavailable.")
    try:
        result = await dictation.run_command(command.action, raw=command.raw, printed=command.printed)
    except CaptureError as failure:
        raise HTTPException(409, str(failure)) from failure
    if "error" in result:
        raise HTTPException(409, result["error"])
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


class MeetingTrashCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID


def windows_desktop_command_state(request):
    state = request.app.state
    record = getattr(state, "windows_dictation_control", None)
    if not windows_dictation_commands.authorized(record, request.headers.get("authorization", "")):
        raise HTTPException(403, "Valid Windows desktop credentials are required.")
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro from its local launcher.")
    return state


@app.post(windows_dictation_commands.ONBOARDING_ROUTE)
async def windows_onboarding_command(request: Request):
    return restart_portable_onboarding(windows_desktop_command_state(request), foreground=True)


async def run_linux_onboarding_command(state):
    from portable_capture import CaptureError

    try:
        return restart_portable_onboarding(state, foreground=True)
    except HTTPException as error:
        raise CaptureError(error.detail) from error


@app.post(windows_dictation_commands.MEETING_TRASH_ROUTE)
async def trash_meeting_from_windows_command(request: Request, command: MeetingTrashCommand):
    state = windows_desktop_command_state(request)
    key = str(command.id)
    waiting = await remove_saved_session(state, key, meeting_only=True)
    return {"id": key, "trashPending": waiting}


class PortableMeetingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["status", "record", "stop", "copy-last", "live"]
    title: str = Field(default="", max_length=200)


@app.post(windows_dictation_commands.MEETING_COMMAND_ROUTE)
async def windows_meeting_command(request: Request, command: PortableMeetingCommand):
    return await run_portable_meeting_command(windows_desktop_command_state(request), command)


@app.post(windows_dictation_commands.ANNOTATION_COMMAND_ROUTE)
async def windows_annotation_command(request: Request, command: PortableAnnotationCommand | PortableAnnotationHandoff | PortableAnnotationStorageCommand):
    state = windows_desktop_command_state(request)
    if isinstance(command, PortableAnnotationStorageCommand):
        return await run_portable_annotation_storage(state, command.storageAction, command.folder)
    if isinstance(command, PortableAnnotationHandoff):
        if command.action == "send-session":
            raise HTTPException(400, "Open the Jaro desktop window to perform this action.")
        return await annotation_handoff_response(state, command)
    return await run_portable_annotation_command(state, command)


async def run_linux_annotation_command(state, payload):
    from portable_capture import CaptureError

    try:
        if isinstance(payload, dict) and payload.get("action") == "storage":
            storage = PortableAnnotationStorageCommand.model_validate(payload)
            return await run_portable_annotation_storage(state, storage.storageAction, storage.folder)
        if isinstance(payload, dict) and payload.get("action") in ("destinations", "send", "undo", "correct", "remember", "forget", "route"):
            handoff = PortableAnnotationHandoff.model_validate(payload)
            return await run_portable_annotation_handoff(state, handoff)
        command = PortableAnnotationCommand.model_validate(payload)
        return await run_portable_annotation_command(state, command)
    except ValidationError as error:
        raise CaptureError("Choose a valid annotation command and source.") from error
    except MissingSession as error:
        raise CaptureError("Annotation not found.") from error
    except HTTPException as error:
        raise CaptureError(str(error.detail)) from error
    except (ManagedFeatureError, ValueError) as error:
        raise CaptureError(str(error)) from error


async def run_linux_meeting_command(state, payload):
    from portable_capture import CaptureError

    try:
        command = PortableMeetingCommand.model_validate(payload)
    except ValueError as error:
        raise CaptureError("Choose a valid meeting command and title.") from error
    try:
        return await run_portable_meeting_command(state, command)
    except HTTPException as error:
        raise CaptureError(str(error.detail)) from error
    except ManagedFeatureError as error:
        raise CaptureError(str(error)) from error


def latest_portable_meeting(library):
    with library.connection() as db:
        row = db.execute(f"""SELECT s.id FROM {readable_source(db)} s
            WHERE s.status='complete' AND trim(s.search_text)!=''
            AND coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)
            ORDER BY s.created DESC,s.id LIMIT 1""").fetchone()
    return row[0] if row else None


async def run_portable_meeting_command(state, command):
    from portable_call_pages import Call

    require_meetings()
    watcher = getattr(state, "portable_calls", None)
    if watcher is None:
        raise HTTPException(503, "Meeting recording is unavailable on this desktop.")
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    if command.title and command.action != "record":
        raise HTTPException(422, "A meeting title is only used when recording starts.")
    if command.action == "live":
        return {"meeting": portable_live_meeting(state)}
    if command.action == "copy-last":
        return await copy_latest_portable_meeting(state)
    if command.action == "stop":
        try:
            key = await watcher.stop_requested()
        except OSError as error:
            raise HTTPException(503, "Could not save the meeting. Open Home to recover the audio.") from error
        return {"phase": "saving", "id": str(key), "changed": True} if key else {"phase": "idle", "changed": False}
    async with watcher.transition:
        capture = state.portable_recording
        if capture and capture.meeting:
            result = {"phase": "recording", "id": str(capture.key), "changed": False}
            if watcher.active and watcher.active.id == capture.key:
                result["title"] = watcher.active.name
            return result
        if command.action == "status":
            return {"phase": "idle" if state.status == "ready" and not portable_recording_busy(state) else "unavailable"}
        if state.status != "ready":
            raise HTTPException(503, "The local speech model is unavailable.")
        try:
            title = normalize_meeting_title(command.title) if command.title.strip() else ""
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        try:
            key = await watcher.begin(Call("", ""), (), title=title)
        except OSError as error:
            raise HTTPException(503, "Could not start the meeting recording.") from error
        return {"phase": "recording", "id": str(key), "title": watcher.active.name, "changed": True}


async def copy_latest_portable_meeting(state):
    from desktop_clipboard import copy_text

    library = state.library
    snapshot = portable_live_meeting(state)

    def selection():
        live = read_portable_live_meeting(library.root, snapshot)
        if live and live["meetingTurns"]:
            current = latest_transcript(library, live)
            return current["session"], current["text"]
        key = latest_portable_meeting(library)
        if not key:
            raise HTTPException(404, "There is no saved meeting transcript to copy.")
        return key, saved_meeting_clipboard(library, key)

    try:
        key, text = await run_in_threadpool(selection)
        await copy_text(text)
    except (OSError, sqlite3.Error, ValueError, MissingSession) as error:
        raise HTTPException(409, "Could not copy the latest meeting transcript.") from error
    return {"id": key, "copied": True}


async def portable_microphone_options(dictation):
    from portable_capture import CaptureError

    if dictation.platform == "windows":
        from windows_capture import microphone_inputs
    else:
        from linux_capture import microphone_inputs
    try:
        inputs = await microphone_inputs()
    except CaptureError:
        return {"selected": dictation.microphone, "inputs": [], "available": False}
    return {"selected": dictation.microphone, "inputs": inputs, "available": True}


class PortableMicrophoneChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: str = Field(max_length=1024)


@app.get("/api/portable/dictation/microphone")
async def portable_microphone(request: Request):
    result = await portable_microphone_options(portable_dictation(request))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.post("/api/portable/dictation/microphone")
async def change_portable_microphone(request: Request, change: PortableMicrophoneChange):
    dictation = portable_dictation(request)
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        if change.selected:
            options = await portable_microphone_options(dictation)
            if not options["available"]:
                raise HTTPException(503, "Could not read the available microphones.")
            if not any(device["id"] == change.selected for device in options["inputs"]):
                raise HTTPException(400, "The selected microphone is unavailable.")
        if dictation.busy or dictation.state.draining:
            raise HTTPException(409, "Finish the current dictation before changing the microphone.")
        try:
            await run_in_threadpool(dictation.history.configure, microphone=change.selected)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save the dictation microphone.") from failure
        dictation.microphone = change.selected
    return {"selected": change.selected}


class PortableLanguageChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(max_length=16)


@app.post("/api/portable/dictation/language")
async def change_portable_language(request: Request, change: PortableLanguageChange):
    dictation = portable_dictation(request)
    if change.language and change.language not in LANGUAGES:
        raise HTTPException(400, "Choose a language from the list.")
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        if dictation.busy:
            raise HTTPException(409, "Finish the current dictation before changing the language.")
        try:
            await run_in_threadpool(dictation.history.configure, language=change.language)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save the dictation language.") from failure
        dictation.language = change.language
        return dictation.snapshot()

class PortableVocabularyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    term: str = Field(max_length=80)
    soundsLike: list[Annotated[str, Field(max_length=80)]] = Field(max_length=8)


class PortableVocabularyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: list[PortableVocabularyEntry] = Field(max_length=MAX_VOCABULARY)
    entries: list[PortableVocabularyEntry] = Field(max_length=MAX_VOCABULARY)


@app.post("/api/portable/dictation/vocabulary")
async def change_portable_vocabulary(request: Request, change: PortableVocabularyChange):
    dictation = portable_dictation(request)
    try:
        base = validate_dictation_vocabulary([entry.model_dump() for entry in change.base])
        entries = validate_dictation_vocabulary([entry.model_dump() for entry in change.entries])
    except ValueError as failure:
        raise HTTPException(400, str(failure)) from failure
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        try:
            saved = await run_in_threadpool(dictation.history.save_vocabulary, base, entries)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save the dictation vocabulary.") from failure
        if not saved:
            raise HTTPException(409, "Vocabulary changed in another window. Reopen the editor to load the saved terms.")
        dictation.vocabulary_entries = entries
    return await run_in_threadpool(dictation.snapshot)


class PortableReplacementChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_PROFILE_CORRECTIONS)


@app.post("/api/portable/dictation/replacements")
async def change_portable_replacements(request: Request, change: PortableReplacementChange):
    dictation = portable_dictation(request)
    try:
        correction_rules(change.text, scope="word")
    except ValueError as failure:
        raise HTTPException(400, str(failure)) from failure
    if "\0" in change.text:
        raise HTTPException(400, "Use text for word replacements.")
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        try:
            await run_in_threadpool(dictation.history.save_replacements, change.text)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save word replacements.") from failure
        dictation.replacements = change.text
    return await run_in_threadpool(dictation.snapshot)



class PortableProfileDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: str = Field(max_length=MAX_PROFILE_DOCUMENT)

    def parsed(self):
        try:
            return parse_portable_profiles(self.profiles)
        except ValueError as failure:
            raise HTTPException(400, str(failure)) from failure


class PortableProfileChange(PortableProfileDefinition):
    base: str = Field(max_length=MAX_PROFILE_DOCUMENT)


class PortableProfilePreview(PortableProfileDefinition):
    text: str = Field(max_length=8000)
    app: str = Field(default="", max_length=300)
    site: str = Field(default="", max_length=300)


@app.get("/api/portable/dictation/output-profiles/applications")
def portable_profile_applications(request: Request):
    dictation = portable_dictation(request)
    from profile_applications import application_entries

    try:
        entries = application_entries(dictation.platform)
    except (OSError, ValueError, subprocess.SubprocessError) as failure:
        raise HTTPException(503, "Could not list installed applications.") from failure
    return [{"id": ident, "name": label} for ident, label, _ in sorted(entries, key=lambda row: (row[1].casefold(), row[0]))]


@app.post("/api/portable/dictation/output-profiles/choose-application")
async def choose_profile_application(request: Request):
    dictation = portable_dictation(request)
    if not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Choose an application from Jaro’s desktop workspace.")
    from desktop_file_dialog import choose_file, file_dialog
    from profile_applications import selected_application

    if not (dialog := file_dialog()):
        raise HTTPException(404, "The desktop file dialog is unavailable.")
    state = request.app.state
    picker = getattr(state, "profile_application_picker", None)
    if state.draining or (picker and not picker.done()):
        raise HTTPException(409, "Finish the current operation before choosing an application.")
    state.profile_application_picker = start_job(state, choose_file(dialog, "--choose-application"))
    try:
        path = await state.profile_application_picker
        return await run_in_threadpool(selected_application, dictation.platform, path) if path else None
    except ValueError as failure:
        raise HTTPException(422, str(failure)) from failure
    except (OSError, subprocess.SubprocessError) as failure:
        raise HTTPException(503, "Could not read the selected application.") from failure


@app.post("/api/portable/dictation/output-profiles")
async def change_portable_profiles(request: Request, change: PortableProfileChange):
    dictation = portable_dictation(request)
    from profile_applications import profile_aliases

    profiles = change.parsed()
    aliases = await run_in_threadpool(profile_aliases, dictation.platform, profiles)
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        try:
            saved = await run_in_threadpool(dictation.history.save_output_profiles, change.base, change.profiles)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save output profiles.") from failure
        if not saved:
            raise HTTPException(409, "Profiles changed in another window. Reload the saved profiles before editing.")
        dictation.profiles = profiles
        dictation.app_aliases = aliases
        dictation.output_profiles = change.profiles
    return await run_in_threadpool(dictation.snapshot)


@app.post("/api/portable/dictation/output-profiles/describe")
def describe_portable_profiles(request: Request, definition: PortableProfileDefinition):
    portable_dictation(request)
    return {"profiles": definition.parsed()}


@app.post("/api/portable/dictation/output-profiles/preview")
async def preview_portable_profiles(request: Request, change: PortableProfilePreview):
    from portable_dictation import output_vocabulary, transform_output

    dictation = portable_dictation(request)
    from profile_applications import profile_aliases

    profiles = change.parsed()
    aliases = await run_in_threadpool(profile_aliases, dictation.platform, profiles)
    profile = profile_for(profiles, change.app, host=change.site, bundle_name=aliases.get(change.app.casefold(), ""))
    entries = dictation.vocabulary_entries
    result = await transform_output(dictation.state, change.text, entries, dictation.replacements, profile, preview=True)
    steps = profile.steps if profile else ()
    return {"text": result.text, "warnings": result.warnings, "steps": steps, "profile": profile.target if profile else "",
            "vocabulary": output_vocabulary(entries, profile), "skipped": [step for step in steps if step == "rewrite"]}


class PortableVoiceInputChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    setting: Literal["noiseCleanup", "boostMicrophone"]
    enabled: bool


class PortableIndicatorPositionChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: Literal["top", "bottom", "off"]


@app.post("/api/portable/dictation/indicator")
async def change_portable_indicator(request: Request, change: PortableIndicatorPositionChange):
    dictation = portable_dictation(request)
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        if dictation.busy:
            raise HTTPException(409, "Finish the current dictation before changing the indicator.")
        try:
            await run_in_threadpool(dictation.history.configure, indicator_position=change.position)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save the dictation indicator position.") from failure
        dictation.indicator_position = change.position
    return dictation.snapshot()


@app.post("/api/portable/dictation/voice-input")
async def change_portable_voice_input(request: Request, change: PortableVoiceInputChange):
    dictation = portable_dictation(request)
    field = "noise_cleanup" if change.setting == "noiseCleanup" else "boost_microphone"
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        if dictation.busy:
            raise HTTPException(409, "Finish the current dictation before changing voice input.")
        try:
            await run_in_threadpool(dictation.history.configure, **{field: change.enabled})
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save dictation voice input.") from failure
        setattr(dictation, field, change.enabled)
        if change.setting == "noiseCleanup" and change.enabled:
            stupase_cleanup(dictation.state).prepare(retry=True)
        return dictation.snapshot()


class PortableSoundChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool | None = None
    style: SoundStyle | None = None
    volume: int | None = Field(default=None, ge=0, le=100)


@app.post("/api/portable/dictation/sound")
async def change_portable_sound(request: Request, change: PortableSoundChange):
    dictation = portable_dictation(request)
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        if dictation.busy:
            raise HTTPException(409, "Finish the current dictation before changing sound settings.")
        settings = replace(dictation.sound, **change.model_dump(exclude_none=True))
        try:
            await run_in_threadpool(dictation.history.save_sound_preferences, settings)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save the dictation sound settings.") from failure
        dictation.sound = settings
        if not settings.enabled or settings.volume == 0:
            await dictation.sounds.stop()
    return dictation.snapshot()


@app.post("/api/portable/dictation/sound/preview")
async def preview_portable_sound(request: Request):
    dictation = portable_dictation(request)
    state = dictation.state
    async with dictation.control:
        async with state.portable_recording_lock:
            if portable_capture_busy(state):
                raise HTTPException(409, "Finish recording before auditioning a dictation sound.")
            settings = replace(dictation.sound, enabled=True, volume=dictation.sound.volume or 50)
            task = dictation.sounds.play("start", settings)
    if task is None:
        raise HTTPException(503, "Dictation sound playback is unavailable.")
    try:
        if not await asyncio.shield(task):
            raise HTTPException(503, "Could not play the dictation sound.")
    except asyncio.CancelledError:
        if not task.cancelled():
            raise
    return dictation.snapshot()


class PortableAudioChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    behavior: AudioBehavior | None = None
    percent: int | None = Field(default=None, ge=0, le=100)


@app.post("/api/portable/dictation/other-audio")
async def change_portable_audio(request: Request, change: PortableAudioChange):
    dictation = portable_dictation(request)
    async with dictation.control:
        if dictation.state.draining or dictation.busy:
            raise HTTPException(409, "Finish the current dictation before changing playback settings.")
        if change.behavior is not None and change.behavior not in dictation.audio_control.modes:
            raise HTTPException(400, "That playback control is unavailable on this desktop.")
        settings = replace(dictation.audio_preferences, **change.model_dump(exclude_none=True))
        try:
            await run_in_threadpool(dictation.history.save_audio_preferences, settings)
        except (OSError, ValueError, sqlite3.Error) as failure:
            raise HTTPException(503, "Could not save the dictation playback settings.") from failure
        dictation.audio_preferences = settings
    return dictation.snapshot()


async def expire_portable_dictations(history):
    delay = 0
    while True:
        await asyncio.sleep(delay)
        delay = 60
        try:
            next_expiry = await run_in_threadpool(history.expire)
            if next_expiry is not None:
                delay = min(delay, max(.1, next_expiry - time.time()))
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("Could not expire Portable dictation history.", exc_info=True)


class PortableShortcutChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shortcut: str = Field(min_length=1, max_length=64)


@app.post("/api/portable/dictation/shortcut")
async def change_portable_shortcut(request: Request, change: PortableShortcutChange):
    from portable_capture import CaptureError

    dictation = portable_dictation(request)
    if dictation.platform not in ("x11", "windows"):
        raise HTTPException(409, "Edit this shortcut through your desktop’s shortcut controls.")
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        try:
            await dictation.change_shortcut(change.shortcut)
        except CaptureError as failure:
            raise HTTPException(409, str(failure)) from failure
    return await run_in_threadpool(dictation.snapshot)


class PortableRawShortcutChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shortcut: str = Field(max_length=64)


class PortalRawShortcutChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


@app.post("/api/portable/dictation/raw-shortcut")
async def change_portable_raw_shortcut(request: Request, change: PortableRawShortcutChange | PortalRawShortcutChange):
    from portable_capture import CaptureError

    dictation = portable_dictation(request)
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        try:
            if dictation.platform in ("x11", "windows") and isinstance(change, PortableRawShortcutChange):
                await dictation.change_shortcut(change.shortcut, raw=True)
            elif dictation.platform == "wayland" and isinstance(change, PortalRawShortcutChange):
                await dictation.change_raw_shortcut(change.enabled)
            else:
                raise CaptureError("This raw dictation shortcut setting is unavailable on this desktop.")
        except CaptureError as failure:
            raise HTTPException(409, str(failure)) from failure
    return await run_in_threadpool(dictation.snapshot)


class PortableShortcutModeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["hold", "tap", "holdDouble", "doubleTap"]


@app.post("/api/portable/dictation/shortcut-mode")
async def change_portable_shortcut_mode(request: Request, change: PortableShortcutModeChange):
    from portable_capture import CaptureError

    dictation = portable_dictation(request)
    if dictation.platform == "wayland" and dictation.command_mode:
        raise HTTPException(409, "Shortcut mode uses compositor shortcuts. Configure command bindings in the desktop settings.")
    async with dictation.control:
        if dictation.state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        try:
            await dictation.change_mode(change.mode)
        except CaptureError as failure:
            raise HTTPException(409, str(failure)) from failure
    return await run_in_threadpool(dictation.snapshot)


class PortableHistoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["retention", "delete", "clear"]
    retention: Literal[0, 1, 7, 30] | None = None
    ids: list[UUID] = Field(default_factory=list, max_length=15)


@app.get("/api/portable/dictation/history")
def portable_history(request: Request, query: str = Query("", max_length=512),
                     offset: int = Query(0, ge=0, le=10_000)):
    history = portable_dictation(request).history
    try:
        result = history.page(query=query.strip() or None, offset=offset, limit=15, legacy=True,
                              include_timing=True, include_audio=True)
    except (OSError, ValueError, sqlite3.Error) as failure:
        raise HTTPException(503, "Could not read dictation history.") from failure
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/portable/dictation/history/{key}/audio")
def portable_history_audio(request: Request, key: UUID):
    history = portable_dictation(request).history
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
               "Cross-Origin-Resource-Policy": "same-origin"}
    try:
        with history.open_audio(str(key)) as (clip, size):
            # WebKit probes WAVs with a small byte range before fetching the rest.
            requested = re.fullmatch(r"bytes=([0-9]{0,12})-([0-9]{0,12})", request.headers.get("range", ""))
            if requested and any(requested.groups()):
                first, last = requested.groups()
                start = int(first) if first else max(0, size - int(last))
                end = min(size, int(last) + 1) if first and last else size
                if start >= end:
                    return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
                clip.seek(start)
                return Response(clip.read(end - start), 206, headers={**headers, "Content-Range": f"bytes {start}-{end-1}/{size}"},
                                media_type="audio/wav")
            return Response(clip.read(size), headers=headers, media_type="audio/wav")
    except (OSError, ValueError, sqlite3.Error):
        return Response(status_code=404, headers=headers)


@app.post("/api/portable/dictation/history")
def change_portable_history(request: Request, change: PortableHistoryChange):
    dictation = portable_dictation(request)
    if (change.action == "retention" and change.retention is None) or (change.action == "delete" and not change.ids):
        raise HTTPException(400, "Choose a retention period or select which dictations to delete.")
    try:
        if change.action == "retention":
            dictation.history.configure(retention=change.retention)
        else:
            dictation.history.remove([str(key) for key in change.ids] if change.action == "delete" else None)
    except (OSError, ValueError, sqlite3.Error) as failure:
        raise HTTPException(503, "Could not change dictation history.") from failure
    return dictation.snapshot()


@app.post("/api/portable/dictation/{action}")
async def change_portable_dictation(request: Request, action: Literal[
                                   "enable", "disable", "paste-shortcut", "configure", "toggle", "cancel", "allow-paste",
                                   "disable-paste", "cancel-approval", "enable-commands", "add-gnome-shortcuts", "remove-gnome-shortcuts"],
                               terminal: bool = False):
    from portable_capture import CaptureError

    state = portable_capture_state(request)
    dictation = portable_dictation(request)
    async with dictation.control:
        if action == "disable":
            if dictation.platform == "wayland" and (dictation.command_mode or dictation.gnome_enabled):
                try:
                    await dictation.disable_commands()
                except CaptureError as failure:
                    raise HTTPException(503, str(failure)) from failure
            try:
                try:
                    await dictation.remember_shortcut(False)
                finally:
                    if dictation.platform == "wayland":
                        await dictation.disable(forget_paste=True)
                    else:
                        await dictation.disable()
            except CaptureError as failure:
                raise HTTPException(503, str(failure)) from failure
        elif state.draining:
            raise HTTPException(409, "Jaro is preparing to restart.")
        elif action == "cancel":
            if not await dictation.cancel_dictation():
                raise HTTPException(409, "There’s no dictation left to cancel.")
        elif action == "paste-shortcut":
            if dictation.platform == "wayland" and not dictation.keyboard_allowed:
                raise HTTPException(404, "Automatic paste is unavailable on this desktop.")
            try:
                await run_in_threadpool(dictation.history.configure, terminal=terminal)
            except (OSError, ValueError, sqlite3.Error) as failure:
                raise HTTPException(503, "Could not save the paste shortcut.") from failure
            dictation.terminal = terminal
        elif action in ("configure", "toggle", "allow-paste", "disable-paste", "cancel-approval", "enable-commands",
                        "add-gnome-shortcuts", "remove-gnome-shortcuts"):
            if dictation.platform != "wayland":
                raise HTTPException(404, "This action uses the Wayland desktop.")
            if action == "cancel-approval":
                await dictation.cancel_approval()
            elif action == "disable-paste":
                try:
                    await dictation.disable_paste()
                except CaptureError as failure:
                    raise HTTPException(503, str(failure)) from failure
                dictation.feedback = ""
            elif action == "allow-paste":
                try:
                    dictation.allow_paste()
                except CaptureError as failure:
                    raise HTTPException(409, str(failure)) from failure
            elif action == "configure":
                try:
                    await dictation.configure_shortcut()
                except CaptureError as failure:
                    raise HTTPException(409, str(failure)) from failure
            elif action == "enable-commands":
                try:
                    await dictation.enable_commands()
                except CaptureError as failure:
                    raise HTTPException(409, str(failure)) from failure
            elif action in ("add-gnome-shortcuts", "remove-gnome-shortcuts"):
                if dictation.busy:
                    raise HTTPException(409, "Finish the current dictation action first.")
                try:
                    if action == "add-gnome-shortcuts":
                        await dictation.add_gnome_shortcuts()
                    else:
                        await dictation.remove_gnome_shortcuts()
                except CaptureError as failure:
                    raise HTTPException(409, str(failure)) from failure
            elif dictation.queue_full:
                from portable_dictation import QUEUE_FULL

                raise HTTPException(409, QUEUE_FULL)
            elif (dictation.authorizing or dictation.paste_authorizing or dictation.action_pending()
                  or (dictation.phase != "recording" and not dictation.can_start_recording)):
                raise HTTPException(409, "Finish the current dictation action first.")
            else:
                dictation.schedule()
        else:
            try:
                if dictation.platform == "wayland":
                    if not dictation.command_mode and not dictation.gnome_enabled:
                        await dictation.remember_shortcut(True)
                    await enable_portable_shortcut(dictation)
                else:
                    await enable_portable_shortcut(dictation)
                    try:
                        await dictation.remember_shortcut(True)
                    except CaptureError:
                        await dictation.disable()
                        raise
            except CaptureError as failure:
                raise HTTPException(409, str(failure)) from failure
    return await run_in_threadpool(dictation.snapshot)


@app.get("/api/portable/recording/drafts")
async def portable_recording_drafts(request: Request):
    state = portable_capture_state(request)
    saved, recordings = await run_in_threadpool(audio_recording_drafts, state)
    recovering = getattr(state, "startup_recovery", False) and state.status != "error"
    if recovering:
        recordings = [draft for draft in recordings if not (draft["meeting"] and draft["id"] in state.startup_stranded)]
    if folder := getattr(state, "screen_draft_dir", None):
        from screen_draft import ScreenDraft

        active = state.portable_screencast
        for path in folder.iterdir():
            if (active and path.name == active.draft.key) or path.name in saved:
                continue
            if draft := ScreenDraft.read(path):
                try:
                    seconds, has_audio = await annotation_movie_info(draft.movie)
                except (OSError, ValueError):
                    continue
                recordings.append({"id": draft.key, "seconds": seconds, "screen": True,
                                   "videoOnly": not has_audio and not draft.narration_frames()})
    headers = {"Cache-Control": "no-store"}
    if recovering:
        headers["X-Jard-Meeting-Recovery"] = "pending"
    return JSONResponse(recordings, headers=headers)


def audio_recording_drafts(state):
    from portable_capture import draft_frames, draft_metadata

    capture = state.portable_recording
    with state.library.connection() as db:
        saved = {row[0] for row in db.execute("SELECT id FROM sessions")}
    recordings = []
    for path in state.portable_recording_dir.glob("*.wav"):
        try:
            key = str(UUID(path.stem))
        except ValueError:
            continue
        if path.stem != key or key in saved or (capture and str(capture.key) == key):
            continue
        if frames := draft_frames(path):
            meeting = bool(draft_metadata(path).get("meetingProvider"))
            recordings.append({"id": key, "seconds": frames / SAMPLE_RATE, "meeting": meeting})
    return saved, recordings


def screen_draft_for_action(state, key):
    if folder := getattr(state, "screen_draft_dir", None):
        from screen_draft import ScreenDraft

        if state.portable_screencast and state.portable_screencast.draft.key == str(key):
            raise HTTPException(409, "Finish the active screen recording first.")
        return ScreenDraft.read(folder / str(key))
    return None


@app.get("/api/portable/recording/{key}/frames")
async def portable_recording_frames(request: Request, key: UUID, offset: int = Query(0, ge=0)):
    state = portable_capture_state(request)
    capture = state.portable_recording
    if capture is None or capture.key != key:
        raise HTTPException(404, "This recording has ended.")
    start, data = capture.recent(offset)
    headers = {
        "Cache-Control": "no-store",
        "X-Audio-Offset": str(start),
        "X-Audio-Finished": str(int(capture.worker.done())),
    }
    if capture.levels is not None:
        microphone, computer_audio = capture.levels
        headers.update({"X-Microphone-Level": f"{microphone:.4f}", "X-Computer-Audio-Level": f"{computer_audio:.4f}"})
    return Response(data, media_type="application/octet-stream", headers=headers)


@asynccontextmanager
async def stopped_portable_recording(state, key):
    capture = state.portable_recording
    if not capture or capture.key != key:
        yield None
        return
    try:
        try:
            with suppress(OSError):
                await capture.stop()
        finally:
            state.portable_recording = None
            if hasattr(state, "live_speakers"):
                state.live_speakers.pop(key, None)

        if preview := getattr(capture, "preview_task", None):
            await finish_live_preview(preview, 0.35, logger, key)
        yield capture
    finally:
        if claim := getattr(capture, "meeting_claim", None):
            claim.close()


@app.post("/api/portable/recording/{key}/finish")
async def save_portable_recording(request: Request, key: UUID, name: str = "", diarization: bool = False,
                                live: bool = False, session: UUID | None = None, notes_draft: UUID | None = None):
    state = portable_capture_state(request)
    return await finish_portable_capture(state, key, transcription_languages(request, require_ready=False), name=name,
                                         diarization=diarization, live=live, session=session, notes_draft=notes_draft)


async def finish_portable_capture(state, key, languages, *, name="", diarization=False, live=False, session=None,
                                  recovering=False, notes_draft=None):
    from portable_capture import clear_draft, draft_frames, draft_metadata, draft_tracks

    async with state.portable_recording_lock:
        if recovering and portable_capture_busy(state):
            return None
        if screen := screen_draft_for_action(state, key):
            return await save_screen_draft(state, screen, languages)
        if state.status != "ready":
            raise HTTPException(503, "The local model is unavailable.")
        path = state.portable_recording_dir / f"{key}.wav"
        async with stopped_portable_recording(state, key) as capture:
            if session:
                state.live_speakers.pop(session, None)
            if state.library.exists(str(key)):
                accepted = JSONResponse({"id": str(key)}, status_code=202)
            else:
                frames = draft_frames(path)
                if not frames:
                    raise HTTPException(404, "No captured audio is available to save.")
                tracks = await run_in_threadpool(draft_tracks, path, frames)
                metadata = getattr(capture, "meeting_metadata", None)
                if metadata is None:
                    metadata = await run_in_threadpool(draft_metadata, path)
                provider = metadata.get("meetingProvider")
                explicit_meeting = isinstance(provider, str) and bool(provider)
                started = None
                voice = None
                title = None
                rolling = await run_in_threadpool(lambda: tuple(read_rolling(path, frames * (MEETING_RATE // SAMPLE_RATE))))
                if explicit_meeting:
                    require_meetings()
                    with suppress(TypeError, ValueError):
                        value = datetime.fromisoformat(metadata.get("meetingStartedAt"))
                        if value.utcoffset() == timedelta(0) and 2000 <= value.year < 9999:
                            started = value
                    saved_languages = metadata.get("languageHints")
                    if isinstance(saved_languages, list) and all(isinstance(code, str) and code in LANGUAGES for code in saved_languages):
                        languages = tuple(dict.fromkeys(saved_languages))
                    if isinstance(inputs := metadata.get("meetingVoiceInput"), dict):
                        voice = {key: True for key in ("noiseCleanup", "boostMicrophone") if inputs.get(key) is True}
                    title = metadata.get("meetingTitle")
                    if title:
                        name = title
                    elif provider != "Meeting":
                        name = f"{provider} meeting"
                    name = name or "Meeting"
                resolved_draft = str(notes_draft) if notes_draft else None
                if not resolved_draft and isinstance(candidate := metadata.get("notesDraft"), str) and candidate:
                    with state.library.connection() as db:
                        if db.execute("SELECT 1 FROM personal_note_drafts WHERE id=?", (candidate,)).fetchone():
                            resolved_draft = candidate
                accepted = await accept_audio(state, path, name, "audio/wav", languages, True, diarization, live, True,
                                              stage_id=str(key), notes_draft=resolved_draft, tracks=tracks,
                                              meeting_provider=provider if explicit_meeting else None,
                                              meeting_started=started, meeting_title=title, meeting_voice_input=voice,
                                              rolling=rolling, applies_retention=explicit_meeting)
        with suppress(OSError):
            clear_draft(path)
        return accepted


@app.delete("/api/portable/recording/{key}", status_code=204)
async def discard_portable_recording(request: Request, key: UUID):
    from portable_capture import clear_draft
    import personal_notes

    state = portable_capture_state(request)
    async with state.portable_recording_lock:
        if screen := screen_draft_for_action(state, key):
            await run_in_threadpool(screen.discard)
        else:
            async with stopped_portable_recording(state, key):
                pass
            clear_draft(state.portable_recording_dir / f"{key}.wav")
            with state.library.connection() as db:
                personal_notes.delete_draft(db, str(key))
    return Response(status_code=204)


def publish_finished_meeting(state, key):
    events = state.meeting_subscriber
    if events is None:
        return
    if events.full():
        # A slow subscriber can catch up from the library after reconnecting.
        while not events.empty():
            events.get_nowait()
        events.put_nowait(None)
        state.meeting_subscriber = None
    else:
        events.put_nowait(key)


async def process_saved_audio(state, key, source, options, duration, *, tracks=None, checkpoint=0,
                              recovered=False, progress_only=True, emit=lambda **_: None):
    library = state.library
    meeting = options.get("meeting", options.get("sourceSpeakerTurns", False))
    languages = options["languageHints"]
    try:
        vocabulary = await speech_vocabulary(state)
        async with state.insights.model.audio_priority(release_idle=duration is not None and duration >= 15 * 60):
            if tracks:
                frames = (source.stat().st_size - 44) // 2
                if not frames or checkpoint > frames:
                    raise ValueError("The recorded audio ends before its saved transcript checkpoint.")
                block = MAX_SAMPLES * MEETING_RATE // SAMPLE_RATE
                for start in range(checkpoint, frames, block):
                    length = min(block, frames - start)
                    result = await decode_meeting(state, tracks, start, length, languages, vocabulary=vocabulary)
                    end = start + length
                    await run_in_threadpool(library.append, key, result, end / MEETING_RATE, next_frame=end)
                samples = frames * SAMPLE_RATE // MEETING_RATE
            else:
                samples = await process_mixed_audio(state, key, source, options, duration,
                                                    checkpoint=checkpoint, progress_only=progress_only,
                                                    emit=emit, vocabulary=vocabulary)
        await run_in_threadpool(library.finish, key)
        if meeting:
            await publish_meeting_exports(state, key=key)
        state.insights.wake.set()
        if meeting:
            publish_finished_meeting(state, key)
        emit(done=True, duration=samples / SAMPLE_RATE)
    except asyncio.CancelledError:
        library.finish(key, SHUTDOWN_INTERRUPTION, closing=True, recovered=recovered)
        raise
    except Exception as error:
        logger.exception("Could not complete session %s.", key)
        message = str(error) if isinstance(error, ValueError) else "Processing stopped. Open Sessions for the saved audio and completed text."
        try:
            await run_in_threadpool(library.finish, key, message)
        finally:
            emit(error=message)


async def meeting_recognition_audio(state, pcm, microphone, voice):
    if microphone.count(0) == len(microphone):
        return pcm, None

    boost = voice.get("boostMicrophone") is True
    fallback = replace_mixed_microphone(pcm, microphone, boost_microphone(microphone)) if boost else pcm
    if voice.get("noiseCleanup") is True:
        try:
            clean = await stupase_cleanup(state).clean(microphone)
        except CleanupFailed:
            return fallback, None
        return replace_mixed_microphone(pcm, microphone, boost_microphone(clean) if boost else clean), fallback
    return fallback, None


async def process_mixed_audio(state, key, source, options, duration, *, checkpoint, progress_only, emit, vocabulary):
    languages = options["languageHints"]
    meeting = options.get("meeting", options.get("sourceSpeakerTurns", False))
    diarizer = await ensure_diarizer(state) if options["diarization"] else None
    speakers = diarizer.session() if options["diarization"] and diarizer else None
    pending = deque()
    samples = 0
    conversion = MEETING_RATE // SAMPLE_RATE
    resume_at = checkpoint // conversion
    voice = (options.get("meetingVoiceInput") or {}) if meeting else {}
    checkpointed = options["recording"] or meeting

    async def transcript(pcm, offset, fallback):
        result = await decode_pcm(state, pcm, languages, speakers, offset, vocabulary=vocabulary)
        if fallback is not None and not result["text"].strip():
            result = await decode_pcm(state, fallback, languages, speakers, offset, vocabulary=vocabulary)
        end = offset + len(pcm) // 4
        seconds = end / SAMPLE_RATE
        ensure_meeting_turn(result, offset / SAMPLE_RATE, seconds)
        await run_in_threadpool(state.library.append, key, result, seconds, next_frame=end * conversion if checkpointed else None)
        emit(**({} if progress_only else result), seconds=seconds, duration=duration)

    if checkpoint % conversion:
        raise ValueError("The saved mixed-audio checkpoint is invalid.")
    emit(seconds=resume_at / SAMPLE_RATE, duration=duration)
    limit = MAX_AUDIO_SECONDS + int(options["recording"])
    # Checkpoints from earlier mixed meetings refer to the original fixed-window
    # decoder (including its multilingual pause split).
    pause_aware = not meeting or options.get("audioChunking") == PAUSE_AWARE_CHUNKING
    async with AsyncExitStack() as streams:
        blocks = await streams.enter_async_context(
            aclosing(audio_blocks(source, multilingual=len(languages) > 1, pause_aware=pause_aware))
        )
        microphone_stream = None
        mic_path = source.with_name("mic.wav")
        if (voice.get("noiseCleanup") is True or voice.get("boostMicrophone") is True) and mic_path.is_file():
            with suppress(OSError):
                microphone_stream = (await streams.enter_async_context(open_audio_pcm(mic_path))).stdout
        async for pcm in blocks:
            offset = samples
            samples += len(pcm) // 4
            if samples > limit * SAMPLE_RATE:
                raise ValueError(DURATION_ERROR)
            if offset < resume_at < samples:
                raise ValueError("The recorded audio does not match its saved transcript checkpoint.")
            fallback = None
            if microphone_stream is not None:
                try:
                    microphone = await microphone_stream.readexactly(len(pcm))
                except asyncio.IncompleteReadError:
                    # Keep transcribing the saved mix if the optional mic track ends early.
                    microphone_stream = None
                else:
                    if speakers or samples > resume_at:
                        pcm, fallback = await meeting_recognition_audio(state, pcm, microphone, voice)
            if speakers:
                await run_inference(state, speakers.push, pcm)
            if samples <= resume_at:
                # A local diarizer still sees the prefix to reconstruct its speaker identities.
                continue
            if speakers:
                pending.append((pcm, offset, fallback))
                while pending and speakers.covers(pending[0][1] + len(pending[0][0]) // 4):
                    await transcript(*pending.popleft())
            else:
                await transcript(pcm, offset, fallback)
    if not samples:
        raise ValueError(FILE_ERROR)
    if samples < resume_at:
        raise ValueError("The recorded audio ends before its saved transcript checkpoint.")
    if speakers and pending:
        await run_inference(state, speakers.push, b"", final=True)
        for block in pending:
            await transcript(*block)
    return samples


def check_native(request):
    check_client(request)
    token = request.headers.get("x-jard-token", "")
    if not hmac.compare_digest(request.app.state.native_token.encode(), token.encode()):
        raise HTTPException(403, "Open Jaro on this Mac to access desktop staging.")


@app.post("/api/native/dictation-rewrite")
async def rewrite_dictation(request: Request):
    check_native(request)
    payload = await request.json()
    text = payload.get("text") if isinstance(payload, dict) else None
    instruction = payload.get("instruction") if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise HTTPException(400, "Rewrite a dictation of up to 8,000 characters.")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2500:
        raise HTTPException(400, "Set rewrite instructions of up to 2,500 characters.")
    try:
        vocabulary = vocabulary_terms(payload.get("vocabulary", []))
        deadline = rewrite_deadline(payload.get("deadline", MAX_REWRITE_DEADLINE))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    try:
        output = await rewrite_locally(request.app.state.insights, text, instruction, vocabulary, deadline,
                                       application=payload.get("application"), browser_host=payload.get("browserHost"))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return {"text": output}


@app.post("/api/native/update-quiesce/{client}", status_code=202)
async def quiesce_update(request: Request, client: UUID):
    check_native(request)
    state = request.app.state
    require_update_idle(state, str(client))
    shutdown = getattr(state, "request_shutdown", None)
    if shutdown is None:
        raise HTTPException(503, "The local engine could not be restarted.")
    begin_draining(state)
    return Response(status_code=202, background=BackgroundTask(shutdown))


def require_update_idle(state, client, *, allow_browser=False, desktop=False):
    now = time.monotonic()
    streams = state.leases.streams
    owned = set()
    if allow_browser:
        candidates = state.leases.desktop_streams if desktop or client is None else streams - state.leases.desktop_streams
        owned = candidates if client is None else {stream for stream in candidates if stream[0] == client}
        if client is None and len(owned) == 1:
            client = next(iter(owned))[0]
    other_stream = len(owned) > 1 or bool(streams - owned)
    if other_stream or any(key != client and expiry > now for key, expiry in state.leases.deadlines.items()):
        raise HTTPException(409, "Close other Jaro windows or browser tabs before restarting.")
    if engine_work_in_progress(state):
        raise HTTPException(409, "Wait for the local engine to finish its current work.")


def engine_work_in_progress(state):
    return bool(state.draining or state.jobs or state.submitting or getattr(state, "screenshot_pending", False)
                or state.insights.active_job or state.insights.chat_count
                or state.insights.model.audio_users or state.status == "loading" or getattr(state, "sdk_pending", 0))


def workspace_updater(request):
    state = request.app.state
    updater = getattr(state, "portable_updater", None)
    if not getattr(state, "portable_workspace", False) or updater is None:
        raise HTTPException(404, "Updates are unavailable for this build.")
    return updater


class LaunchAtLoginChange(BaseModel):
    enabled: StrictBool


def portable_login_state(request, enabled=None, *, open_settings=False):
    login = getattr(request.app.state, "portable_login", None)
    if not getattr(request.app.state, "portable_workspace", False) or login is None:
        raise HTTPException(404, "Launch at login is unavailable for this build.")
    if (open_settings or enabled is not None) and request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to change Launch at Login.")
    try:
        return login.open_settings() if open_settings else login.set_state(enabled)
    except OSError as error:
        logger.warning("Could not access desktop startup registration: %s", error)
        if open_settings:
            raise HTTPException(503, "Could not open Startup settings. Go to Windows Settings → Apps → Startup.") from error
        raise HTTPException(503, "Could not access Jaro’s startup registration.") from error


@app.get("/api/portable/login-item")
def get_portable_login(request: Request):
    return portable_login_state(request)


@app.post("/api/portable/login-item")
def change_portable_login(request: Request, change: LaunchAtLoginChange):
    return portable_login_state(request, change.enabled)


@app.post("/api/portable/login-item/open-settings")
def open_portable_login_settings(request: Request):
    return portable_login_state(request, open_settings=True)


def require_portable_update_idle(state, client, *, desktop=False):
    require_update_idle(state, client, allow_browser=True, desktop=desktop)
    if portable_work_in_progress(state):
        raise HTTPException(409, "Finish the current recording, dictation, or model setup before restarting.")


def portable_work_in_progress(state):
    recording_lock = getattr(state, "portable_recording_lock", None)
    dictation = getattr(state, "portable_dictation", None)
    cleanup = getattr(state, "stupase_cleanup", None)
    screencast = state.portable_screencast
    if getattr(screencast, "phase", None) == "cancelled" and not screencast.saving:
        # A dismissed portal can leave a status for the CLI after all capture work ends.
        if screencast.task is None or screencast.task.done():
            screencast = None
    return bool(state.portable_recording or screencast
                or getattr(dictation, "busy", False)
                or getattr(getattr(state, "portable_sdk_capture", None), "busy", False)
                or getattr(dictation, "authorizing", False)
                or getattr(dictation, "paste_authorizing", False)
                or getattr(cleanup, "downloading", None)
                or (recording_lock and recording_lock.locked())
                or (getattr(state, "startup_recovery", False) and state.status == "ready")
                or (state.model_setup or {}).get("phase") in ("checking", "downloading", "starting"))


def portable_update_view(state, updater, client, *, desktop=False):
    status = updater.status()
    if not desktop:
        status.pop("openUpdateSettings", None)
        status.pop("trayRestart", None)
    if status.get("staged") and not status.get("updatesDisabled") and not status.get("restartInProgress"):
        try:
            require_portable_update_idle(state, client, desktop=desktop)
        except HTTPException as failure:
            status.update(restartBlocked=True, restartBlockReason=str(failure.detail))
    return status


@app.get("/api/portable/updates")
async def portable_update_status(request: Request, client: UUID | None = None):
    status = portable_update_view(request.app.state, workspace_updater(request), str(client) if client else None,
                                  desktop=getattr(request.state, "portable_desktop", False))
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


@app.post("/api/portable/updates/settings-opened", status_code=204)
async def portable_update_settings_opened(request: Request, token: Annotated[str, Query(pattern=r"^[a-f0-9]{24}$")]):
    updater = workspace_updater(request)
    if request.headers.get("origin") != URL or not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Open Jaro’s desktop workspace to handle the update request.")
    updater.acknowledge_settings(token)
    return Response(status_code=204)


@app.post("/api/portable/updates/claim-tray-restart", status_code=204)
async def portable_claim_tray_restart(request: Request, token: Annotated[str, Query(pattern=r"^[a-f0-9]{24}$")]):
    updater = workspace_updater(request)
    if request.headers.get("origin") != URL or not getattr(request.state, "portable_desktop", False):
        raise HTTPException(403, "Open Jaro’s desktop workspace to handle the update request.")
    if not clear_tray_restart(updater, token):
        raise HTTPException(409, "The tray restart request expired.")
    return Response(status_code=204)


@app.post("/api/portable/updates/{action}")
async def portable_update_action(request: Request, action: Literal["check", "download", "restart"], client: UUID):
    updater = workspace_updater(request)
    if request.headers.get("origin") != URL:
        raise HTTPException(403, "Check for updates from Jaro’s desktop workspace.")
    if updater.status().get("updatesDisabled"):
        raise HTTPException(403, "Updates are managed by your organization.")
    state = request.app.state
    client_key = str(client)
    desktop = getattr(request.state, "portable_desktop", False)
    if action == "check":
        await updater.check()
        return portable_update_view(state, updater, client_key, desktop=desktop)
    if action == "download":
        try:
            await updater.download()
        except (OSError, ValueError) as error:
            raise HTTPException(409, "Could not open the Jaro download.") from error
        return portable_update_view(state, updater, client_key, desktop=desktop)
    shutdown = await prepare_portable_restart(state, updater, client_key, desktop=desktop)
    return JSONResponse(portable_update_view(state, updater, client_key, desktop=desktop),
                        status_code=202, background=BackgroundTask(shutdown))


def clear_tray_restart(updater, token):
    if updater.state.get("trayRestart") != token:
        return False
    updater.state.pop("trayRestart")
    updater.publish()
    return True


async def restart_from_portable_tray(state, updater, window_open):
    if not window_open() and not state.leases.desktop_streams:
        shutdown = await prepare_portable_restart(state, updater, window_open=window_open)
        shutdown()
        return
    require_portable_update_idle(state, None)
    # The live WebView owns edits that have yet to reach the library.
    token = token_hex(12)
    updater.publish(trayRestart=token)
    try:
        async with asyncio.timeout(8):
            while updater.state.get("trayRestart") == token:
                await updater.changed.wait()
    except TimeoutError as error:
        raise HTTPException(409, "Choose Restart to Update in the workspace to save any open drafts.") from error
    finally:
        clear_tray_restart(updater, token)


async def prepare_portable_restart(state, updater, client=None, *, desktop=False, window_open=None):
    if updater.status().get("updatesDisabled"):
        raise HTTPException(403, "Updates are managed by your organization.")
    require_portable_update_idle(state, client, desktop=desktop)
    shutdown = getattr(state, "request_shutdown", None)
    if shutdown is None:
        raise HTTPException(503, "The local engine could not be restarted.")
    try:
        await updater.confirm_restart()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    require_portable_update_idle(state, client, desktop=desktop)
    if window_open is not None and (window_open() or state.leases.desktop_streams):
        raise HTTPException(409, "Choose Restart to Update in the workspace to save any open drafts.")
    if updater.status().get("updatesDisabled"):
        raise HTTPException(403, "Updates are managed by your organization.")
    try:
        updater.prepare_restart()
    except (OSError, ValueError) as error:
        raise HTTPException(409, "Could not prepare Jaro to restart.") from error
    begin_draining(state)
    return shutdown


def calendar_details(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid calendar details.")
    title, attendees = value.get("title"), value.get("attendees")
    if not isinstance(title, str) or not 1 <= len(title) <= 160 or not isinstance(attendees, list) or len(attendees) > 32:
        raise ValueError("Invalid calendar details.")
    if any(not isinstance(name, str) or len(name) > 120 for name in attendees):
        raise ValueError("Invalid calendar attendees.")
    if any(ord(char) < 32 or ord(char) == 127 for field in (title, *attendees) for char in field):
        raise ValueError("Invalid calendar details.")
    return {"title": title, "attendees": attendees}


@app.post("/api/native/staging", status_code=201)
async def new_stage(request: Request, recording: bool = False, dictation: bool = False, meeting: bool = False,
                    provider: str = Query("Meeting", min_length=1, max_length=80), calendar: dict | None = Body(None),
                    annotation_id: UUID | None = None, noise_cleanup: bool = False, boost_microphone_input: bool = False):
    check_native(request)
    if annotation_id is not None:
        require_annotation()
    if meeting:
        require_meetings()
    if request.app.state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    if meeting and (not recording or dictation):
        raise HTTPException(400, "A meeting must use a two-source recording stage.")
    metadata = {"meetingProvider": provider, "retentionApplies": True}
    if meeting and (noise_cleanup or boost_microphone_input):
        metadata["meetingVoiceInput"] = {"noiseCleanup": noise_cleanup, "boostMicrophone": boost_microphone_input}
    if calendar is not None:
        if not meeting:
            raise HTTPException(400, "Calendar details require a meeting.")
        try:
            metadata["meetingCalendar"] = calendar_details(calendar)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
    if annotation_id and (recording or dictation or meeting):
        raise HTTPException(400, "An annotation must use a movie staging file.")
    key = str(annotation_id or uuid4())
    if annotation_id and await run_in_threadpool(request.app.state.library.exists, key):
        raise HTTPException(409, "This annotation has already been saved.")
    path = request.app.state.staging / key
    files = [path]
    if dictation:
        files.append(path.with_suffix(".dictation"))
    elif recording:
        files.extend(source_tracks(path).values() if meeting else ())
        files.append(recording_marker(path))
    created = []
    try:
        for file in files:
            file.touch(mode=0o600, exist_ok=False)
            created.append(file)
        if meeting:
            recording_marker(path).write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf8")
    except OSError as error:
        for file in created:
            file.unlink(missing_ok=True)
        if annotation_id and isinstance(error, FileExistsError):
            raise HTTPException(409, "This annotation is already being staged.") from error
        raise
    if dictation or recording:
        schedule_prewarm(request.app.state)
    if meeting and noise_cleanup:
        stupase_cleanup(request.app.state).prepare()
    return {"id": key, "path": str(path)}


@app.get("/api/native/staging")
def recoverable_staging(request: Request):
    check_native(request)
    state = request.app.state
    drafts = list_drafts(state.staging)
    if state.startup_recovery:
        drafts = [draft for draft in drafts if not (draft["meeting"] and draft["id"] in state.startup_stranded)]
    return JSONResponse(drafts, headers={"Cache-Control": "no-store"})


def stage_path(state, key):
    path = state.staging / str(key)
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "The staged file is unavailable.")
    return path


@app.delete("/api/native/staging/{key}", status_code=204)
async def abandon_stage(request: Request, key: UUID, dictation: bool = False, discard: bool = False):
    check_native(request)
    if key in request.app.state.submitting:
        raise HTTPException(409, "Audio is being accepted.")
    path = stage_path(request.app.state, key)
    if dictation and not path.with_suffix(".dictation").is_file():
        raise HTTPException(404, "This dictation is unavailable.")
    request.app.state.live_speakers.pop(key, None)
    path.unlink()
    path.with_suffix(".dictation").unlink(missing_ok=True)
    recording_marker(path).unlink(missing_ok=True)
    rolling_path(path).unlink(missing_ok=True)
    meeting_title_path(path).unlink(missing_ok=True)
    for track in source_tracks(path).values():
        track.unlink(missing_ok=True)
    for asset in staged_annotation_paths(path).values():
        asset.unlink(missing_ok=True)
    if discard and not dictation:
        import personal_notes

        with request.app.state.library.connection() as db:
            personal_notes.delete_draft(db, str(key))
    return Response(status_code=204)


@app.post("/api/native/staging/{key}/submit")
async def submit_stage(request: Request, key: UUID, recording: bool = False, diarization: bool = False,
                       name: str = "", mime: str = "audio/wav", live: bool = False, recover: bool = False,
                       notes_draft: UUID | None = None, annotation: bool = False, annotation_location: UUID | None = None):
    check_native(request)
    return await submit_staged_audio(request.app.state, key, transcription_languages(request, require_ready=False), recording=recording,
                                    diarization=diarization, name=name, mime=mime, live=live, recover=recover,
                                    notes_draft=notes_draft, annotation=annotation,
                                    annotation_location=str(annotation_location) if annotation_location else None)


async def submit_staged_audio(state, key, languages, *, recording, diarization, name, mime, live, recover,
                              notes_draft=None, annotation=False, annotation_location=None):
    if annotation:
        require_annotation()
    if annotation_location is not None and not annotation:
        raise HTTPException(400, "An annotation folder requires a staged annotation.")
    if meetings_managed() and await run_in_threadpool(is_meeting_stage, state.staging / str(key)):
        require_meetings()
    if state.draining:
        raise HTTPException(409, "Jaro is preparing to restart.")
    if key in state.submitting:
        raise HTTPException(409, "Audio is being accepted.")
    state.submitting.add(key)
    try:
        if await run_in_threadpool(state.library.exists, str(key)):
            clear_recording_stage(state.staging / str(key))
            return {"id": str(key)}
        if state.status != "ready":
            raise HTTPException(503, "The local model is unavailable.")
        source = stage_path(state, key)
        if source.with_suffix(".dictation").is_file():
            raise HTTPException(409, "Dictation belongs in My notes.")
        annotation_info = None
        if annotation:
            if recording or recover or mime not in ("video/mp4", "audio/wav") or notes_draft is not None:
                raise HTTPException(400, "An annotation requires a staged MP4 or narration WAV.")
            try:
                annotation_info = await run_in_threadpool(read_staged_annotation, source)
            except (OSError, ValueError) as error:
                raise HTTPException(400, str(error)) from error
            if (mime == "audio/wav") != bool(annotation_info.get("missingVideo")):
                raise HTTPException(400, "The annotation manifest does not match its media.")
        meeting_provider = meeting_calendar = meeting_started = meeting_title = meeting_microphone_warning = None
        meeting_voice_input = None
        applies_retention = False
        if recover and recording_marker(source).is_file():
            with recording_marker(source).open("rb") as marker:
                metadata = recording_metadata(marker)
            applies_retention = metadata.get("retentionApplies") is True
            value = metadata.get("meetingProvider")
            if isinstance(value, str) and value:
                meeting_provider = value
                meeting_title = metadata.get("meetingTitle")
                meeting_microphone_warning = metadata.get("meetingMicrophoneWarning")
                voice = metadata.get("meetingVoiceInput")
                if isinstance(voice, dict):
                    meeting_voice_input = {key: True for key in ("noiseCleanup", "boostMicrophone") if voice.get(key) is True}
                saved_languages = metadata.get("languageHints")
                if not languages and isinstance(saved_languages, list) and all(isinstance(code, str) and code in LANGUAGES for code in saved_languages):
                    languages = tuple(dict.fromkeys(saved_languages))[:4]
                if isinstance(metadata.get("meetingStartedAt"), str):
                    with suppress(ValueError, OverflowError):
                        parsed = datetime.fromisoformat(metadata["meetingStartedAt"])
                        if parsed.utcoffset() == timedelta(0) and 2000 <= parsed.year < 9999:
                            meeting_started = parsed
                if "meetingCalendar" in metadata:
                    with suppress(ValueError):
                        meeting_calendar = calendar_details(metadata["meetingCalendar"])
            if not notes_draft and (candidate := metadata.get("notesDraft")) and isinstance(candidate, str):
                with state.library.connection() as db:
                    if db.execute("SELECT 1 FROM personal_note_drafts WHERE id=?", (candidate,)).fetchone():
                        notes_draft = candidate
        state.live_speakers.pop(key, None)
        tracks = None
        rolling = ()
        if recover:
            if not recording:
                raise HTTPException(400, "Only a recording can be recovered.")
            try:
                if not meeting_provider and any(path.is_file() for path in source_tracks(source).values()):
                    meeting_provider = "Meeting"
                frames = await run_in_threadpool(recover_wav, source)
                tracks = await run_in_threadpool(recover_tracks, source, frames)
                rolling = read_rolling(source, frames)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
        result = await accept_audio(state, source, name, mime, languages, recording, diarization, live, True, str(key),
                                    notes_draft=str(notes_draft) if notes_draft else None, tracks=tracks,
                                    meeting_provider=meeting_provider, meeting_calendar=meeting_calendar,
                                    meeting_started=meeting_started, rolling=rolling, applies_retention=applies_retention,
                                    annotation=annotation_info, annotation_location=annotation_location, meeting_title=meeting_title,
                                    meeting_microphone_warning=meeting_microphone_warning,
                                    meeting_voice_input=meeting_voice_input,
                                    provisional_title=read_meeting_title(source) if meeting_provider else None)
        clear_recording_stage(source)
        return result
    finally:
        state.submitting.discard(key)


def clear_recording_stage(path):
    for file in (path, *source_tracks(path).values(), recording_marker(path), rolling_path(path),
                 meeting_title_path(path), *staged_annotation_paths(path).values()):
        try:
            file.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove completed staging files for %s", path.name)


async def dictate_audio(request, source, languages, vocabulary=None, *, sdk=False, measure_inference=False,
                        screen_recording=False, release_frame=None):
    return await transcribe_dictation(request.app.state, source, languages, vocabulary, sdk=sdk,
                                     disconnected=request.is_disconnected if sdk else None,
                                     measure_inference=measure_inference, screen_recording=screen_recording,
                                     release_frame=release_frame)


async def transcribe_portable_dictation(state, source, languages=(), *, vocabulary=None, noise_cleanup=False, boost=False):
    return await transcribe_with_voice_input(state, source, languages, vocabulary, noise_cleanup=noise_cleanup, boost=boost,
                                             screen_recording=True, measure_inference=True)


async def transcribe_with_voice_input(state, source, languages, vocabulary=None, *, noise_cleanup=False, **options):
    return await transcribe_dictation(state, source, languages, vocabulary,
                                      cleanup=stupase_cleanup(state) if noise_cleanup else None, **options)


async def transcribe_dictation(state, source, languages, vocabulary=None, *, sdk=False, disconnected=None,
                               measure_inference=False, screen_recording=False, release_frame=None, cleanup=None, boost=False):
    parts = []
    samples = 0
    inference_ns = 0
    speech_queue_ns = 0
    audio_read_ns = 0
    cleanup_ns = None
    used_rust = False
    before_release = None if release_frame is None else release_frame * SAMPLE_RATE // 48_000

    async def recognize(pcm, options):
        nonlocal inference_ns, speech_queue_ns, used_rust
        if boost:
            pcm = boost_microphone(pcm)
        rust = rust_speech_client(state)
        if rust is not None:
            try:
                started = time.monotonic_ns()
                res = await rust.transcribe_pcm(
                    pcm,
                    sample_rate=SAMPLE_RATE,
                    sample_format="f32le",
                    languages=languages,
                    vocabulary=vocabulary or (),
                    screen_speech=screen_recording,
                )
                if measure_inference:
                    inference_ns += int(res.get("inferenceMs", 0)) * 1_000_000 or (time.monotonic_ns() - started)
                used_rust = True
                return str(res.get("text", "")).strip()
            except Exception:
                logger.warning("Rust speech engine request failed; falling back to local PyTorch model.", exc_info=True)
        result = await decode_pcm(state, pcm, languages, live=True, vocabulary=vocabulary, dictation=not sdk,
                                  measure_inference=measure_inference, screen_nonspeech=screen_recording, **options)
        if measure_inference:
            inference_ns += result["inferenceNs"]
            speech_queue_ns += result["speechQueueNs"]
        return result["text"].strip()

    try:
        gain = MIC_GAIN if boost else 1.0
        if screen_recording and not await run_in_threadpool(dictation_has_signal, source, release_frame, gain):
            if sdk:
                with wave.open(str(source), "rb") as wave_file:
                    seconds = wave_file.getnframes() / wave_file.getframerate()
                if seconds > MAX_AUDIO_SECONDS:
                    raise ValueError(DURATION_ERROR)
                return {"text": "", "durationMs": round(seconds * 1000)}
            return {"text": "", "inferenceMs": 0}
        if vocabulary is None:
            vocabulary = await speech_vocabulary(state)
        audio_priority = nullcontext() if getattr(state, "sdk_embedded", False) else state.insights.model.audio_priority()
        priority_requested_at = time.monotonic_ns() if measure_inference else None
        async with audio_priority:
            priority_ns = time.monotonic_ns() - priority_requested_at if priority_requested_at is not None else None
            async with aclosing(audio_blocks(source, multilingual=len(languages) > 1, pause_aware=True, fast_wav=True)) as blocks:
                reading_since = time.monotonic_ns() if measure_inference else None
                async for original in blocks:
                    if reading_since is not None:
                        audio_read_ns += time.monotonic_ns() - reading_since
                    count = len(original) // 4
                    if sdk:
                        if disconnected is not None and await disconnected():
                            raise ClientDisconnect()
                        samples += count
                        if samples > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                            raise ValueError(DURATION_ERROR)
                    options = {}
                    if before_release is not None:
                        if before_release == 0 and not parts:
                            break
                        if 0 < before_release < count:
                            options["speech_prefix_samples"] = before_release
                        before_release = max(0, before_release - count)
                    pcm = original
                    if cleanup is not None:
                        started = time.monotonic_ns() if measure_inference else None
                        try:
                            pcm = await cleanup.clean(original, wait=False)
                        except CleanupFailed:
                            logger.info("StuPASE unavailable for this dictation. Continuing with the original audio.")
                            cleanup = None
                        finally:
                            if started is not None:
                                cleanup_ns = (cleanup_ns or 0) + time.monotonic_ns() - started
                    text = await recognize(pcm, options)
                    if not text and pcm != original:
                        text = await recognize(original, options)
                    if text:
                        parts.append(text)
                    if reading_since is not None:
                        reading_since = time.monotonic_ns()
                else:
                    if reading_since is not None:
                        audio_read_ns += time.monotonic_ns() - reading_since
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except ClientDisconnect:
        raise
    except Exception as error:
        logger.exception("Dictation inference failed.")
        raise HTTPException(500, "Could not transcribe dictation. Try again.") from error
    result = {"text": " ".join(parts)}
    if sdk:
        result["durationMs"] = round(samples * 1000 / SAMPLE_RATE)
    if measure_inference:
        result["inferenceMs"] = round(inference_ns / 1_000_000)
        result["speechTiming"] = {"audioReadMs": round(audio_read_ns / 1_000_000),
                                  "speechQueueMs": round(speech_queue_ns / 1_000_000),
                                  "audioPriorityMs": round(priority_ns / 1_000_000)}
        if cleanup_ns is not None:
            result["speechTiming"]["voiceCleanupMs"] = round(cleanup_ns / 1_000_000)
    if used_rust:
        result["speechEngine"] = "rust"
    return result


@app.post("/api/personal-notes/dictation")
async def browser_dictation(request: Request):
    check_audio_request(request)
    languages = transcription_languages(request)
    with tempfile.TemporaryDirectory(prefix=".upload-", dir=request.app.state.library.root) as folder:
        source = Path(folder) / "dictation"
        await save_upload(request, source)
        return await dictate_audio(request, source, languages)


@app.post("/api/native/transcribe")
async def native_transcribe_file(request: Request):
    check_native(request)
    return await browser_dictation(request)


def sdk_record(request, *, authenticate=True):
    state = request.app.state
    if "origin" in request.headers:
        raise HTTPException(403, "Use the Jard SDK from a trusted local process.")
    record = getattr(state, "sdk", None)
    if record is None:
        raise HTTPException(503, "The Jard SDK is unavailable.")
    if authenticate:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), record["token"].encode()):
            raise HTTPException(401, "Valid Jard SDK credentials are required.", headers={"WWW-Authenticate": "Bearer"})
    return record


@sdk_router.get("/identify")
async def sdk_identify(request: Request, challenge: str = Query(pattern=r"^[0-9a-f]{32}$")):
    record = sdk_record(request, authenticate=False)
    return JSONResponse({"proof": sdk_proof(record, challenge)}, headers={"Cache-Control": "no-store"})


@sdk_router.get("/health")
async def sdk_health(request: Request):
    sdk_record(request)
    state = request.app.state
    return {"status": "stopping" if state.draining else state.status, "model": MODEL_ID}


@sdk_router.get("/capabilities")
async def sdk_capabilities(request: Request):
    sdk_record(request)
    try:
        available = await sdk_desktop_request(request.app.state, "sdk-available", timeout=.5)
    except HTTPException:
        available = False
    return {"apiVersion": SDK_VERSION, "transcription": True, "serviceCapture": available is True,
            "maxUploadBytes": SDK_MAX_UPLOAD, "maxAudioSeconds": MAX_AUDIO_SECONDS}


@sdk_router.get("/models")
async def sdk_models(request: Request):
    sdk_record(request)
    return {"models": [{"id": MODEL_ID, "ready": request.app.state.status == "ready",
                        "supportsLanguageDetection": True, "languages": LANGUAGE_OPTIONS}]}


async def sdk_desktop_request(state, action, *, timeout=11, **options):
    unavailable = {"code": "service-capture-unavailable", "message": "Open Jaro to use its microphone."}
    if getattr(state, "portable_workspace", False):
        if (capture := getattr(state, "portable_sdk_capture", None)) is None:
            raise HTTPException(503, unavailable)
        if action == "sdk-available":
            return state.status == "ready" and not state.draining
        if action == "sdk-start":
            async with state.portable_recording_lock:
                if state.status != "ready":
                    raise HTTPException(503, unavailable)
                if portable_capture_busy(state):
                    raise HTTPException(409, {"code": "dictation-busy", "message": "A recording is already active."})
                if state.portable_dictation is not None:
                    microphone = state.portable_dictation.microphone
                else:
                    from portable_dictation_history import PortableDictationHistory

                    microphone = ""
                    with suppress(OSError, ValueError, sqlite3.Error):
                        microphone = await run_in_threadpool(PortableDictationHistory(state.library.root).microphone)
                return await capture.start(options["languages"], microphone)
        return capture.request(action.removeprefix("sdk-"), options["capture"], options["owner"], options["cursor"])
    if getattr(state, "sdk_embedded", False) or not getattr(state, "native_token", None) or not hasattr(state, "library"):
        raise HTTPException(503, unavailable)
    try:
        async with asyncio.timeout(timeout):
            path = await asyncio.to_thread(desktop_control_path, state.library.root)
            reader, writer = await asyncio.open_unix_connection(path, limit=8 * 1024 * 1024)
            try:
                writer.write(json.dumps({"action": action, "token": state.native_token, **options}).encode() + b"\n")
                await writer.drain()
                reply = json.loads(await reader.readline())
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
    except (OSError, TimeoutError, ValueError) as error:
        raise HTTPException(503, unavailable) from error
    if not reply.get("ok"):
        raise HTTPException(reply.get("status", 409), {"code": reply.get("code", "dictation-busy"),
                            "message": reply.get("error", "The recording command failed.")})
    return reply["result"]


async def transcribe_sdk_microphone(state, path, languages):
    result = await transcribe_dictation(state, path, languages, vocabulary=(), sdk=True, screen_recording=True)
    return result["text"]


@sdk_router.post("/dictations")
async def sdk_start_dictation(request: Request, source: str = Query("SDK", max_length=80)):
    sdk_record(request)
    state = request.app.state
    if state.draining:
        raise HTTPException(503, {"code": "engine-stopping", "message": "Jard is preparing to restart."})
    return await sdk_desktop_request(state, "sdk-start", languages=transcription_languages(request), source=source)


@sdk_router.api_route("/dictations/{capture}/{action}", methods=["GET", "POST"])
async def sdk_control_dictation(request: Request, capture: UUID, action: str, cursor: int = Query(0, ge=0, le=10**9)):
    sdk_record(request)
    expected = {"audio": "GET", "levels": "GET", "finish": "POST", "cancel": "POST", "heartbeat": "POST"}.get(action)
    if expected is None or expected != request.method:
        raise HTTPException(404, "Recording command not found.")
    owner = request.headers.get("x-jard-dictation-token", "")
    if not re.fullmatch(r"[0-9a-f]{64}", owner):
        raise HTTPException(404, {"code": "dictation-not-found", "message": "This recording is unavailable."})
    return await sdk_desktop_request(request.app.state, "sdk-" + action, capture=str(capture), owner=owner, cursor=cursor)


@sdk_router.post("/transcriptions")
async def sdk_transcribe(request: Request, speech_screen: bool = False):
    sdk_record(request)
    state = request.app.state
    if state.draining:
        raise HTTPException(503, {"code": "engine-stopping", "message": "Jard is preparing to restart."})
    if state.sdk_pending >= SDK_MAX_REQUESTS:
        raise HTTPException(429, {"code": "queue-full", "message": "Jard is busy with other SDK requests."})
    if request.headers.get("content-type") not in ("application/octet-stream", "audio/wav"):
        raise HTTPException(415, "Expected audio bytes.")
    if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > SDK_MAX_UPLOAD:
        raise HTTPException(413, {"code": "audio-too-large", "message": "The audio exceeds the SDK upload limit."})
    languages = transcription_languages(request)
    state.sdk_pending += 1
    try:
        root = state.sdk_upload_root if getattr(state, "sdk_embedded", False) else state.library.root
        with tempfile.TemporaryDirectory(prefix=".upload-", dir=root) as folder:
            source = Path(folder) / "dictation"
            await save_upload(request, source, maximum=SDK_MAX_UPLOAD)
            result = await dictate_audio(request, source, languages, sdk=True, screen_recording=speech_screen)
            return {"transcript": result["text"], "durationMs": result["durationMs"]}
    except ClientDisconnect as error:
        raise HTTPException(499, "The SDK client disconnected.") from error
    finally:
        state.sdk_pending -= 1


app.include_router(sdk_router)


def stupase_cleanup(state):
    if (cleanup := getattr(state, "stupase_cleanup", None)) is None:
        cleanup = state.stupase_cleanup = StuPASECleanup(state.library.root)
    return cleanup


@app.post("/api/native/prewarm", status_code=202)
async def native_prewarm(request: Request):
    check_native(request)
    return {"warming": schedule_prewarm(request.app.state)}


@app.post("/api/native/dictation-cleanup")
async def native_dictation_cleanup(request: Request, action: Literal["status", "prepare"] = "status"):
    check_native(request)
    cleanup = stupase_cleanup(request.app.state)
    return {"status": cleanup.prepare(retry=True) if action == "prepare" else cleanup.status()}


@app.post("/api/native/staging/{key}/dictate")
async def native_dictation(request: Request, key: UUID,
                           release_frame: Annotated[int | None, Query(ge=0, le=3 * 3600 * 48_000)] = None,
                           noise_cleanup: bool = False, boost_microphone_input: bool = False):
    check_native(request)
    languages = transcription_languages(request)
    path = stage_path(request.app.state, key)
    if not path.with_suffix(".dictation").is_file():
        raise HTTPException(404, "This dictation is unavailable.")
    vocabulary = tuple(dict.fromkeys(word.strip() for word in request.query_params.getlist("vocabulary") if word.strip()))
    if len(vocabulary) > 32 or any(len(word) > 80 for word in vocabulary):
        raise HTTPException(400, "Use up to 32 vocabulary entries of at most 80 characters.")
    return await transcribe_with_voice_input(request.app.state, path, languages, vocabulary,
                                             noise_cleanup=noise_cleanup, boost=boost_microphone_input,
                                             screen_recording=True, measure_inference=True, release_frame=release_frame)


def staged_meeting_title_rows(path, through):
    import fcntl

    try:
        with recording_marker(path).open("rb") as marker:
            # A live recorder owns the marker exclusively. Recovery uses its stored title.
            try:
                fcntl.flock(marker, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return []
            except BlockingIOError:
                if not recording_metadata(marker).get("meetingProvider"):
                    return []
        rows = []
        for _, _, result in read_rolling(path, through):
            labels = {} if result.get("meetingSpeakerMode") == MIXED_SPEAKERS else SOURCE_LABELS
            language = result.get("language")
            if language not in LANGUAGES.values():
                language = None
            for turn in result["segments"]:
                if turn["text"].strip():
                    speaker = turn["speaker"]
                    label = labels.get(str(speaker), f"Speaker {speaker}") if speaker else None
                    rows.append({"start": turn["start"] or 0, "text": f"{label}: {turn['text']}" if label else turn["text"],
                                 "language": language})
        return rows
    except OSError:
        return []


@app.post("/api/native/staging/{key}/meeting-title")
async def write_staged_meeting_title(request: Request, key: UUID, through: int = Query(gt=0, le=(MAX_AUDIO_SECONDS + 1) * MEETING_RATE)):
    check_native(request)
    require_meetings()
    state = request.app.state
    if state.draining or key in state.submitting:
        raise HTTPException(409, "Meeting title generation is unavailable while saving or restarting.")

    def current_policy():
        with state.library.connection() as db:
            return meeting_brief.automatic_policy(db)

    policy = current_policy()
    if not policy[0]:
        return {"automatic": False}
    rows = await run_in_threadpool(staged_meeting_title_rows, stage_path(state, key), through)
    if not rows:
        raise HTTPException(409, "There is no live meeting transcript to title.")
    model = state.insights.model

    async def write():
        async with asyncio.timeout(30):
            await model.available.wait()
            async with model.lock:
                if current_policy() != policy:
                    return None
                await model.ready()
                if current_policy() != policy:
                    return None
                return await meeting_brief.generate_live_title(model, rows)

    job = start_job(state, write())
    try:
        while not job.done():
            if await request.is_disconnected():
                return Response(status_code=499)
            await asyncio.wait([job], timeout=.2)
        if current_policy() != policy:
            return {"automatic": False}
        return {"title": job.result()}
    except TimeoutError:
        raise HTTPException(504, "The local meeting title timed out.") from None
    except (RuntimeError, ValueError):
        raise HTTPException(503, "The local writer could not name this meeting.") from None
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@app.post("/api/native/staging/{key}/preview")
async def preview_stage(request: Request, key: UUID, start: int = Query(ge=0, le=MAX_AUDIO_SECONDS * 48000),
                        frames: int = Query(gt=0, le=25 * 48000), diarization: bool = False, meeting: bool = False,
                        tentative: bool = False):
    check_native(request)
    if meetings_managed() and (meeting or await run_in_threadpool(is_meeting_stage, request.app.state.staging / str(key))):
        require_meetings()
    languages = transcription_languages(request)
    state = request.app.state
    path = stage_path(state, key)
    # Our recorder writes PCM16 mono under a fixed 44-byte WAV header. Only use
    # committed samples; the RIFF length gets its final value after Stop.
    def read_pcm():
        with path.open("rb") as f:
            try:
                read_wav_header(f)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            f.seek(44 + start * 2)
            return f.read(frames * 2)

    data = await run_in_threadpool(read_pcm)
    if len(data) != frames * 2:
        raise HTTPException(409, "The requested audio has not been written yet.")
    dictation = path.with_suffix(".dictation").is_file()
    if not dictation and frames < MEETING_FRAME:
        return {"text": "", "language": None, "segments": [], "words": [], "activity": [], "meetingSpeakerMode": MIXED_SPEAKERS}
    try:
        pcm = await resample_capture(data)
    except ValueError as error:
        raise HTTPException(400, "Could not decode live audio.") from error
    vocabulary = () if dictation else await speech_vocabulary(state)
    async with state.insights.model.audio_priority():
        if dictation and (rust := rust_speech_client(state)) is not None:
            try:
                result = await rust.transcribe_pcm(pcm, languages=languages)
                return {**result, "segments": [], "words": [], "activity": []}
            except (httpx.HTTPError, ValueError):
                logger.debug("Rust dictation preview unavailable.", exc_info=True)
        if tentative and not dictation:
            result = await decode_pcm(state, pcm, languages, None, start * SAMPLE_RATE // MEETING_RATE,
                                      live=True, timed_output=False, dictation=False, vocabulary=vocabulary,
                                      screen_nonspeech=True)
            return {"text": result["text"], "language": result["language"], "segments": [], "words": [], "activity": [],
                    "tentative": True}
        speakers = await live_speakers(request, key) if (not dictation and (diarization or meeting)) else None
        result = await decode_pcm(state, pcm, languages, speakers, start * SAMPLE_RATE // MEETING_RATE,
                                  live=True, timed_output=not dictation, dictation=dictation, vocabulary=vocabulary,
                                  screen_nonspeech=True)
    if not dictation:
        ensure_meeting_turn(result, start / MEETING_RATE, (start + frames) / MEETING_RATE)
        result["words"] = [{"speaker": None, **word} for word in result["words"]]
        result.update(activity=[], meetingSpeakerMode=MIXED_SPEAKERS)
    return result


def start_in_background(*, open_page: bool):
    log = log_directory() / PROCESS_LOG
    with startup_lock():
        if not running_engine():
            with open_log(PROCESS_LOG, mode="a+b", create=True) as output:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "--serve"], cwd=WORKSPACE_ROOT,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                )
            deadline = time.monotonic() + 60
            while True:
                try:
                    if running_engine():
                        break
                except RuntimeError:
                    # A listener can be bound before the ASGI startup completes.
                    if process.poll() is not None:
                        raise
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"Jaro could not start. See {log}")
                time.sleep(0.15)
    if open_page:
        open_browser_workspace(library_path())


@app.put("/api/portable/workspace-drafts", status_code=204)
def checkpoint_workspace_drafts(request: Request, changes: workspace_drafts.DraftChanges):
    if not getattr(request.state, "portable_desktop", False) or request.headers.get("origin") != URL:
        raise HTTPException(403, "Open Jaro’s desktop workspace to keep drafts.")
    if request.app.state.draining:
        raise HTTPException(409, "Jaro is restarting.")
    with request.app.state.library.connection() as db:
        workspace_drafts.write(db, changes)
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def workspace_page(request: Request):
    headers = {"Cache-Control": "no-store"}
    try:
        html = (DIST / "index.html").read_bytes()
    except OSError:
        return Response("Jaro’s workspace is unavailable. Quit Jaro and reopen the installed app.",
                        status_code=503, media_type="text/plain", headers=headers)
    if getattr(request.app.state, "portable_workspace", False):
        from desktop_file_dialog import file_dialog

        html = html.replace(b"<html", b"<html data-jaro-portable-workspace", 1)
        desktop = getattr(request.state, "portable_desktop", False)
        if desktop:
            html = html.replace(b"<html ", b"<html data-jaro-desktop-client ", 1)
            with request.app.state.library.connection() as db:
                drafts = workspace_drafts.boot_data(db)
            html = html.replace(b"</head>", b'<script type="application/json" id="jaro-workspace-drafts">'
                                + drafts + b"</script></head>", 1)
        if desktop and file_dialog():
            html = html.replace(b"<html ", b"<html data-jaro-portable-video-export data-jaro-application-picker ", 1)
        if getattr(request.app.state, "portable_updater", None) is not None:
            html = html.replace(b"<html ", b"<html data-jaro-portable-updates ", 1)
        if getattr(request.app.state, "portable_login", None) is not None:
            html = html.replace(b"<html ", b"<html data-jaro-portable-login ", 1)
        if request.app.state.portable_recorder is not None:
            html = html.replace(b"<html ", b"<html data-jaro-portable-audio ", 1)
        if request.app.state.portable_dictation is not None:
            html = html.replace(b"<html ", b'<html data-jaro-portable-dictation ', 1)
        if getattr(request.app.state, "screen_draft_dir", None):
            html = html.replace(b"<html ", b"<html data-jaro-screen-recording ", 1)
            if desktop and file_dialog() and not annotate_managed():
                html = html.replace(b"<html ", b"<html data-jaro-annotation-storage ", 1)
            if (getattr(request.state, "portable_desktop", False)
                    and getattr(request.app.state, "portable_annotation_shortcut", None)):
                html = html.replace(b"<html ", b"<html data-jaro-annotation-shortcut ", 1)
                if request.app.state.desktop_annotation is not None:
                    html = html.replace(b"<html ", b"<html data-jaro-desktop-annotation ", 1)
        if sys.platform in ("linux", "win32") and desktop and not annotate_managed():
            handoff = b"managed" if opencode_managed() else b"available"
            html = html.replace(b"<html ", b'<html data-jaro-annotation-agents data-jaro-opencode-handoff="' + handoff + b'" ', 1)
            if sys.platform == "win32" and handoff == b"available":
                html = html.replace(b"<html ", b"<html data-jaro-opencode-wsl ", 1)
        if sys.platform == "linux":
            html = html.replace(b"<html ", b'<html data-jaro-linux-audio ', 1)
            if request.app.state.portable_dictation is not None:
                html = html.replace(b"<html ", b'<html data-jaro-linux-dictation ', 1)
                if request.app.state.portable_dictation.platform == "x11":
                    html = html.replace(b"<html ", b'<html data-jaro-x11-dictation ', 1)
    return Response(html, media_type="text/html", headers=headers)


def main():
    parser = argparse.ArgumentParser(description="Run Jaro locally.")
    parser.add_argument("--setup-model", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--ensure", action="store_true", help="Attach to or start the shared engine.")
    parser.add_argument("--background", action="store_true", help="Start a detached server and open the browser.")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--embedded", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prepare-speech-model", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--speech-model-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--repair-speech-model", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--portable-workspace", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if ((args.prepare_speech_model and not args.embedded)
            or ((args.speech_model_source is not None or args.repair_speech_model) and not args.prepare_speech_model)):
        parser.error("Speech preparation options require --embedded --prepare-speech-model.")
    if args.setup_model:
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        prepare_model()
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    if args.embedded:
        from sdk_embedded import embedded_installation_lock, run_embedded

        try:
            with embedded_installation_lock(WORKSPACE_ROOT):
                raise SystemExit(run_embedded(sdk_router, SpeechInference, RumbleModel, prepare=args.prepare_speech_model,
                                              source=args.speech_model_source, repair=args.repair_speech_model))
        except (OSError, RuntimeError, ValueError) as error:
            parser.exit(1, str(error) + "\n")
    if args.portable_workspace:
        from portable_login import load_login
        from portable_update import load_updater
        from portable_workspace import configure_workspace
        from sdk_embedded import run_owned_service

        if not (DIST / "index.html").is_file():
            parser.error("Build Jaro’s workspace before launching the desktop host.")

        def build_app(_scratch, record):
            global URL
            URL = f"http://{HOST}:{record['port']}"
            configure_workspace(app, record, URL)
            app.state.portable_updater = load_updater()
            app.state.portable_login = load_login(WORKSPACE_ROOT)
            app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
            return app

        try:
            raise SystemExit(run_owned_service(build_app, allow_audio_restore=True))
        except (OSError, RuntimeError) as error:
            parser.exit(1, str(error) + "\n")
    if os.name == "nt":
        parser.error("Run jardctl transcribe or start the embedded speech service on Windows.")
    if not (DIST / "index.html").is_file():
        parser.error("Run Setup Jaro to build the browser interface.")
    try:
        if args.ensure or args.background:
            start_in_background(open_page=args.background and not args.no_open and not args.ensure)
            return
        if not args.serve and running_engine():
            if not args.no_open:
                open_browser_workspace(library_path())
            return
    except RuntimeError as error:
        parser.exit(1, str(error) + "\n")

    import uvicorn

    from portable_workspace import configure_shared_workspace

    configure_shared_workspace(app, URL, PORT)
    app.mount("/", StaticFiles(directory=DIST, html=True), name="ui")
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, access_log=False, ws="none",
                                           timeout_graceful_shutdown=5))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    app.state.is_shutting_down = lambda: server.should_exit
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((HOST, PORT))
        except OSError as error:
            parser.exit(1, f"Cannot start Jaro at {URL}: {error}\n")
        app.state.sdk_port = listener.getsockname()[1]
        Thread(target=maintain_logs, name="jaro-log-retention", daemon=True).start()
        if not args.no_open and not args.serve:
            def open_when_ready():
                while not server.started and not server.should_exit:
                    time.sleep(0.1)
                if server.started:
                    open_browser_workspace(library_path())
            Thread(target=open_when_ready, daemon=True).start()
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
