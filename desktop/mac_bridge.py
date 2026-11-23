"""App-owned device and file operations for the shared React interface.

The Native SDK host accepts messages from its own local setup document and the
authenticated loopback engine. It sends one request per line over a private pipe.
The engine remains the only owner of sessions and model inference.
"""

import asyncio
import contextlib
import datetime
import fcntl
import hmac
import json
import math
import mimetypes
import os
import shutil
import signal
import sys
import tempfile
from time import monotonic, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4
from urllib.parse import unquote

import httpx

workspace = Path(__file__).resolve().parents[1]
engine = workspace / "engine" if (workspace / "pyproject.toml").is_file() else workspace
sys.path[:0] = [str(engine), str(workspace)] if engine != workspace else [str(engine)]
from engine_lifecycle import HOST, PORT, URL, library_path, running_engine
from annotation_assets import (ASSETS as ANNOTATION_ASSETS, annotation_input, annotation_movie_info,
                               annotation_origin, staged_annotation_paths)
from annotation_rollback import undo_annotation
from annotation_routing import send_session_prompt
from annotation_routing import (RouteUnavailable, capture_for_routing, correct_annotation, destinations as annotation_destinations,
                                forget_destination_rule, recent_deliveries, route_scopes, send_annotation, set_destination_rule)
from diagnostics import write_dictation_timing, write_event
from managed_preferences import ManagedFeatureError, meetings_managed, opencode_managed, require_annotation, require_meetings, require_opencode
from output_transform_runtime import CAPABILITIES, is_embedded_config
from output_transform_policy import DISABLED_MESSAGE, PROGRAM_MESSAGE, TransformPolicy, current_policy
from output_transform_workspace import ConfigInspector, config_directory, find_config, initialize_workspace
from dictation_output import (MAX_REWRITE_DEADLINE, is_script_step, is_transform_step, merge_vocabulary, parse_profiles,
                              process as process_output, profile_for, review_output_script, vocabulary_terms)
from dictation_settings import validate_changes
from local_qwen import MODEL_NAME as REWRITE_MODEL_NAME
from meeting_brief import LIVE_TITLE_INTERVAL, LiveTitlePlan
from meeting_rolling import MIXED_SPEAKERS, append_rolling
from native_recordings import normalize_meeting_title, recording_metadata, write_meeting_title, write_recording_metadata
from saved_annotations import annotation_context, annotation_detail
from saved_meetings import latest_transcript, saved_meeting_clipboard
from session_store import MissingSession, SessionStore
from desktop import annotation_agents
from desktop.annotation_automation import AnnotationAutomation, describe_delivery
from desktop.sdk_capture import CaptureError, SDKCapture


def send(message):
    try:
        print(
            json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True
        )
    except (BrokenPipeError, OSError):
        # Closing a window must still finalize the recording and release the engine lease.
        pass


def emit(event, **values):
    send({"event": "jard-capture", "detail": {"event": event, **values}})


def explain(error):
    if isinstance(error, httpx.HTTPStatusError):
        with contextlib.suppress(ValueError, KeyError):
            return error.response.json()["detail"]
    return str(error) or type(error).__name__


async def run_helper(path, *args):
    child = await asyncio.create_subprocess_exec(
        str(path), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await child.communicate()
    finally:
        if child.returncode is None:
            child.terminate()
            await child.wait()
    if child.returncode:
        raise RuntimeError(
            err.decode(errors="replace").strip() or f"{Path(path).name} failed."
        )
    return out.decode()


def transcript_export(data):
    fields = (
        "id",
        "created",
        "audioUrl",
        "audioPath",
        "recording",
        "live",
        "diarization",
        "model",
        "modelRevision",
        "speakerModel",
        "speakerModelRevision",
    )
    return {
        **{k: data[k] for k in fields},
        "source": data["audioName"],
        "languageHints": data["languageHints"],
        **data["transcript"],
    }


@dataclass
class WarmWorker:
    child: asyncio.subprocess.Process
    options: tuple[str, ...]
    on_demand: bool = False
    reader: asyncio.Task | None = None
    events: asyncio.Queue | None = None
    availability: dict | None = None
    meter: tuple[int, asyncio.Future] | None = None
    input_ready: asyncio.Event = field(default_factory=asyncio.Event)
    annotation_reply: tuple[int, asyncio.Future] | None = None
    meeting: tuple[str, asyncio.Queue] | None = None


@dataclass
class Capture:
    child: asyncio.subprocess.Process
    stage: dict
    marker: BinaryIO
    settings: dict
    state: dict
    global_dictation: bool
    job: asyncio.Task | None = None
    started: bool = False
    finished: bool = False
    copy_state: dict | None = None
    title_plan: LiveTitlePlan | None = None
    title_job: asyncio.Task | None = None
    warm_events: asyncio.Queue | None = None
    microphone_peak: tuple[float, float] | None = None

    def stop_titles(self):
        if self.title_job:
            self.title_job.cancel()

    async def input_events(self):
        if self.warm_events is None:
            async for raw in self.child.stdout:
                yield json.loads(raw)
        else:
            while (event := await self.warm_events.get()) is not None:
                yield event

    async def command(self, message):
        if self.warm_events is not None and (meeting := self.state.get("meeting")):
            message = json.dumps({"meeting": meeting, "command": message})
        self.child.stdin.write((message + "\n").encode())
        await self.child.stdin.drain()


class MacBridge:
    def __init__(self, helpers, http):
        self.approved_output_script = ("", "", frozenset())
        self.helpers = Path(helpers)
        self.transform_configs = ConfigInspector(self.helpers / "jaro-transforms")
        self.http = http
        self.root = library_path()
        self.client_id = str(uuid4())
        self.connected = False
        self.lease_task = None
        self.setup_lock = asyncio.Lock()
        self.meeting_clock_lock = asyncio.Lock()
        self.meeting_clock = os.environ.get("JARD_MEETING_CLOCK")
        self.capture_lock = asyncio.Lock()
        self.captures: dict[str, Capture] = {}
        self.dialog_lock = asyncio.Lock()
        self.dictation_stages = {}
        self.dictation_release_frames = {}
        self.dictation_jobs = set()
        self.meeting_mic_lease: tuple[Capture, str] | None = None
        self.meeting_mic_available = asyncio.Event()
        self.meeting_mic_available.set()
        self.warm_worker: WarmWorker | None = None
        self.warm_requested = None
        self.warm_request_id = None
        self.warm_notice = None
        self.meter_sequence = 0
        self.updating = False
        self.annotation = None
        self.annotation_location = None
        self.annotation_worker: WarmWorker | None = None
        self.annotation_narration: Path | None = None
        self.annotation_narrating = False
        self.annotation_narration_stopped = False
        self.annotation_command = 0
        self.annotation_delivery_busy = False
        self.annotation_started_at = None
        self.automatic_annotations = AnnotationAutomation(
            self.root, self.automatic_capture, self.automatic_send,
            lambda detail: send({"event": "jard-annotation-confirmation" if "nonce" in detail else "jard-annotation-routing", "detail": detail}),
        )
        self.control_server = None
        self.control_dir = None
        self.control_path = None
        self.control_pending = {}
        self.sdk_capture = SDKCapture(self.helpers / "jard-capture", self.transcribe_sdk_take, self.release_sdk_capture)

    async def api(self, method, route, **options):
        response = await self.http.request(method, "/api/" + route, **options)
        response.raise_for_status()
        return response.json() if response.content else None

    async def dialog(self, command, *args):
        async with self.dialog_lock:
            result = await run_helper(self.helpers / "jard-dialog", command, *args)
            return json.loads(result)

    async def lease(self):
        while True:
            try:
                await self.api("POST", "clients/" + self.client_id)
            except httpx.ConnectError:
                # Another client may have explicitly quit the shared engine.
                # Finalize on disk so the recording is recoverable at next launch.
                await asyncio.gather(*(self.stop(take) for take in list(self.captures.values())))
                await self.sdk_capture.close()
            except httpx.HTTPError:
                pass
            await asyncio.sleep(18)

    async def prepare(self):
        async with self.setup_lock:
            return await self.connect_if_ready()

    async def connect_if_ready(self):
        # Never pass a token or expose native controls to an unrelated local service.
        existing = await asyncio.to_thread(running_engine)
        if existing:
            response = await self.http.get("/", timeout=5)
            if response.status_code in (500, 503) and not self.connected:
                await self.recover_workspace()
                existing = None
            else:
                response.raise_for_status()
        if not existing:
            status = json.loads(
                await run_helper(self.helpers / "jard-engine", "model-status")
            )
            if not status["ready"]:
                return status
            await run_helper(self.helpers / "jard-engine", "start")
            if not await asyncio.to_thread(running_engine):
                raise RuntimeError("The local engine stopped during startup.")
            response = await self.http.get("/", timeout=5)
            response.raise_for_status()
        self.http.headers["X-Jard-Token"] = (
            (self.root / ".native-token").read_text().strip()
        )
        await self.api("POST", "clients/" + self.client_id)
        if not self.connected:
            write_event("desktop", "connected")
        self.connected = True
        if self.meeting_clock in ("12", "24"):
            await self.sync_meeting_clock(self.meeting_clock)
        await self.start_control()
        if self.lease_task is None:
            self.lease_task = asyncio.create_task(self.lease())
        send({"navigate": "app"})
        if not self.updating:
            with contextlib.suppress(ValueError, OSError):
                await self.automatic_annotations.resume()
        return {"ready": True}

    async def sync_meeting_clock(self, clock):
        async with self.meeting_clock_lock:
            self.meeting_clock = clock
            try:
                await self.api("PUT", f"native/meeting-clock/{clock}", timeout=3)
            except httpx.HTTPError:
                write_event("desktop", "meeting_clock_sync_failed")

    async def recover_workspace(self):
        # An earlier install can leave its engine healthy but its bundled page missing.
        # The restart guard protects every other client and admitted engine job.
        token = (self.root / ".native-token").read_text().strip()
        await self.api("POST", "native/update-quiesce/" + self.client_id, headers={"X-Jard-Token": token})
        await self.wait_for_engine_exit()

    async def start_control(self):
        if self.control_server:
            return
        self.control_dir = tempfile.TemporaryDirectory(prefix="jard-control-", dir="/tmp")
        self.control_path = Path(self.control_dir.name) / "socket"
        try:
            self.control_server = await asyncio.start_unix_server(self.control_connection, str(self.control_path))
            descriptor, temporary = tempfile.mkstemp(prefix=".control-", dir=self.root)
            with os.fdopen(descriptor, "w") as pointer:
                pointer.write(str(self.control_path) + "\n")
            os.replace(temporary, self.root / ".native-control")
        except OSError:
            if self.control_server:
                self.control_server.close()
                await self.control_server.wait_closed()
            self.control_server = None
            self.control_dir.cleanup()
            self.control_dir = None
            self.control_path = None

    async def control_connection(self, reader, writer):
        own = action = None
        sdk_started = False
        try:
            async with asyncio.timeout(16) as deadline:
                request = json.loads(await reader.readline())
                token = request.get("token")
                if not isinstance(token, str) or not hmac.compare_digest(
                    token.encode(), self.http.headers.get("X-Jard-Token", "").encode()
                ):
                    raise ValueError("Invalid Jard control credentials.")
                action = request.get("action")
                if action not in ("status", "start", "stop", "toggle", "cancel", "paste-last", "setup", "history",
                                  "dictation-start-raw", "dictation-toggle-raw", "dictation-stop-print", "history-clear",
                                  "history-checkpoint", "history-snapshot",
                                  "meeting-status", "meeting-record", "meeting-stop", "meeting-copy-last", "meeting-live",
                                  "annotation-status", "annotation-start", "annotation-stop", "annotation-cancel", "annotation-storage",
                                  "annotation-route", "annotation-destinations", "annotation-remember", "annotation-forget", "annotation-send", "annotation-undo", "annotation-correct",
                                  "desktop-status", "desktop-quit", "desktop-restart", "settings-list", "settings-get", "settings-apply",
                                  "vocabulary-list", "vocabulary-add", "vocabulary-remove",
                                  "window-show", "window-hide", "launch-at-login", "check-for-updates", "update-channel",
                                  "sdk-available", "sdk-start", "sdk-finish", "sdk-cancel", "sdk-heartbeat", "sdk-levels", "sdk-audio"):
                    raise ValueError("Unknown Jard control action.")
                if action == "meeting-live":
                    result = {"ok": True, "result": self.live_meeting(turns=request.get("turns", True) is True)}
                    await self.write_control_reply(writer, result)
                    return
                if action == "annotation-route":
                    result = await self.annotation_route({"id": request.get("id"), "action": request.get("routeAction", "status")})
                    await self.write_control_reply(writer, {"ok": True, "result": result})
                    return
                if action == "annotation-storage":
                    result = await self.annotation_storage(request, allow_path=True)
                    await self.write_control_reply(writer, {"ok": True, "result": result})
                    return
                if action in ("annotation-destinations", "annotation-remember", "annotation-forget"):
                    result = await self.annotation_routes(action.removeprefix("annotation-"), request)
                    await self.write_control_reply(writer, {"ok": True, "result": result})
                    return
                if action in ("annotation-send", "annotation-undo", "annotation-correct"):
                    if action == "annotation-send":
                        result = await self.deliver_annotation(request)
                    elif action == "annotation-undo":
                        deadline.reschedule(asyncio.get_running_loop().time() + 24)
                        result = await self.change_annotation_delivery(undo_annotation, request.get("deliveryId"),
                                                                      restore_files=request.get("restoreFiles") is True)
                    else:
                        result = await self.change_annotation_delivery(correct_annotation, request.get("deliveryId"))
                    await self.write_control_reply(writer, {"ok": True, "result": result})
                    return
                if action == "sdk-available":
                    await self.write_control_reply(writer, {"ok": True, "result": True})
                    return
                if action.startswith("sdk-") and action != "sdk-start":
                    identifier = str(UUID(request["capture"]))
                    owner, cursor = request.get("owner"), request.get("cursor", 0)
                    if not isinstance(owner, str) or not owner.isascii() or len(owner) != 64 or type(cursor) is not int or cursor < 0:
                        raise CaptureError("dictation-not-found", "This recording is unavailable.", 404)
                    if action == "sdk-finish" and self.updating:
                        raise CaptureError("engine-stopping", "Jaro is preparing to restart.", 503)
                    data = self.sdk_capture.request(action.removeprefix("sdk-"), identifier, owner, cursor)
                    await self.write_control_reply(writer, {"ok": True, "result": data})
                    return
                options = {}
                if action == "dictation-stop-print":
                    deadline.reschedule(asyncio.get_running_loop().time() + 65)
                elif action == "history":
                    query, offset = request.get("query", ""), request.get("offset", 0)
                    if not isinstance(query, str) or len(query) > 500 or type(offset) is not int or not 0 <= offset <= 100_000:
                        raise ValueError("Invalid history query or page offset.")
                    options = {"query": query, "offset": offset}
                elif action == "annotation-start":
                    require_annotation()
                    if "source" in request:
                        source = request["source"]
                        if source not in ("display", "window", "area", "phone"):
                            raise ValueError("Choose a display, window, area, or wired iPhone or iPad to annotate.")
                        options["source"] = source
                    if "languages" in request:
                        languages = request["languages"]
                        if not isinstance(languages, list) or len(languages) > 25 or any(
                                not isinstance(language, str) or not 1 <= len(language) <= 12 for language in languages):
                            raise ValueError("Invalid annotation language hints.")
                        options["languages"] = languages
                elif action == "meeting-record":
                    require_meetings()
                    if "title" in request:
                        options = {"title": normalize_meeting_title(request["title"])}
                elif action == "history-clear":
                    if request.get("confirm") is not True:
                        raise ValueError("Confirm deletion of the entire dictation history.")
                elif action == "window-show":
                    pane = request.get("pane", "settings")
                    if pane not in ("settings", "general", "dictation", "history", "output", "meetings", "meeting-library", "annotate", "home"):
                        raise ValueError("Unknown Jaro workspace pane.")
                    options = {"pane": pane}
                elif action == "settings-get":
                    key = request.get("key")
                    if not isinstance(key, str) or not 1 <= len(key) <= 80:
                        raise ValueError("Provide a setting key or prefix.")
                    options = {"key": key}
                elif action == "settings-apply":
                    changes = request.get("changes")
                    validate_changes(changes)
                    options = {"changes": changes}
                elif action in ("vocabulary-add", "vocabulary-remove"):
                    term, spellings = request.get("term"), request.get("soundsLike", [])
                    if (
                        not isinstance(term, str) or not 1 <= len(term) <= 80 or
                        not isinstance(spellings, list) or len(spellings) > 8 or
                        any(not isinstance(value, str) or not 1 <= len(value) <= 80 for value in spellings)
                    ):
                        raise ValueError("Provide a term and up to eight alternate spellings of at most 80 characters.")
                    options = {"term": term, "soundsLike": spellings}
                elif action in ("desktop-quit", "desktop-restart") and "force" in request:
                    if request["force"] is not True:
                        raise ValueError("Invalid forced-exit request.")
                    deadline.reschedule(asyncio.get_running_loop().time() + 85)
                    options = {"force": True}
                elif action == "launch-at-login" and "enabled" in request:
                    if type(request["enabled"]) is not bool:
                        raise ValueError("Choose on or off for Launch at Login.")
                    options = {"enabled": request["enabled"]}
                elif action == "update-channel":
                    channel, offer = request.get("channel"), request.get("offer")
                    if channel is not None:
                        if channel not in ("stable", "dev") or offer is not None:
                            raise ValueError("Choose Stable or Development.")
                        options["channel"] = channel
                    elif offer is not None:
                        if not isinstance(offer, str) or len(offer) != 64 or any(char not in "0123456789abcdef" for char in offer):
                            raise ValueError("Provide the digest of the current release offer.")
                        options["offer"] = offer
                if len(self.control_pending) >= 8:
                    raise ValueError("Jard is busy. Try again.")
                own = str(uuid4())
                loop = asyncio.get_running_loop()
                if action == "history-checkpoint":
                    history_file = self.control_path.parent / f"history-{own}.plist"
                    options = {"path": str(history_file)}
                    loop.call_later(30, history_file.unlink, True)
                future = loop.create_future()
                self.control_pending[own] = future
                send({"nativeRequest": {"id": own, "action": action, **options}})
                result = await future
                if action == "history-checkpoint" and result.get("ok"):
                    result = {"ok": True, "result": history_file.name}
                if action == "sdk-start" and result.get("ok"):
                    data = await self.start_sdk_capture(own, result["result"], request.get("languages", []))
                    sdk_started = True
                    result = {"ok": True, "result": data}
        except CaptureError as error:
            result = {"ok": False, "error": str(error), "code": error.code, "status": error.status}
        except ManagedFeatureError as error:
            result = {"ok": False, "error": str(error), "code": "managed", "status": 403}
        except (ValueError, TimeoutError, AttributeError, KeyError, OSError) as error:
            result = {"ok": False, "error": str(error) or "The Jard control request timed out."}
        finally:
            self.control_pending.pop(own, None)
            if own and action == "sdk-start" and not sdk_started:
                self.release_sdk_capture(own)
        try:
            await self.write_control_reply(writer, result)
        finally:
            if own and action in ("desktop-quit", "desktop-restart") and result.get("ok"):
                send({"desktopExitAccepted": own})

    async def start_sdk_capture(self, identifier, settings, languages):
        async with self.capture_lock:
            if not self.control_server or not self.control_server.is_serving():
                raise CaptureError("service-capture-unavailable", "Jard is closing.", 503)
            if self.updating or self.annotation or self.captures or self.dictation_stages or self.dictation_jobs or self.sdk_capture.active:
                raise CaptureError("dictation-busy", "A recording is already active or Jard is preparing to restart.")
            if (await self.api("GET", "status"))["status"] != "ready":
                raise CaptureError("model-unavailable", "Wait for the speech model to finish loading.", 503)
            await self.close_warm()
            return await self.sdk_capture.start(identifier, self.control_dir.name, self.input_options(settings),
                                                languages or settings["languages"])

    def release_sdk_capture(self, identifier):
        send({"sdkCaptureEnded": identifier})
        asyncio.create_task(self.reconcile_warm())

    async def transcribe_sdk_take(self, path, languages):
        async def chunks():
            with path.open("rb") as audio:
                while block := audio.read(65536):
                    yield block

        result = await self.api("POST", "native/transcribe", content=chunks(), params={"language": languages},
                                headers={"Content-Type": "application/octet-stream"}, timeout=180)
        return result["text"]

    @staticmethod
    async def write_control_reply(writer, result):
        try:
            with contextlib.suppress(ConnectionError):
                writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def live_meeting(self, *, turns):
        take = self.captures.get("recording")
        if not take or not take.state.get("meetingStartedAt"):
            return None
        capture = take.state
        started = capture["meetingStartedAt"]
        provider = capture["meetingLabel"]
        calendar = capture.get("meetingCalendar")
        title = capture.get("provisionalTitle")
        name = capture.get("meetingTitle") or (calendar or {}).get("title") or title or f"{provider} meeting"
        return {"id": take.stage["id"], "name": name,
                "meetingProvider": provider, "meeting": True, "meetingCalendar": calendar, "created": started,
                "meetingStartedAt": started, "duration": capture["seconds"], "processed": capture["seconds"],
                "status": "recording" if not take.finished else "processing", "meetingSpeakerMode": MIXED_SPEAKERS,
                **({"meetingMicrophoneWarning": "digitalSilence"} if capture.get("meetingMicrophoneWarning") else {}),
                **({"provisionalTitle": title} if title else {}),
                **({"meetingTurns": capture["meetingTurns"]} if turns else {})}

    async def close_control(self):
        if not self.control_server:
            return
        self.control_server.close()
        for future in self.control_pending.values():
            if not future.done():
                future.set_result({"ok": False, "error": "Jard is closing."})
        await self.control_server.wait_closed()
        async with self.capture_lock:
            await self.sdk_capture.close()
        pointer = self.root / ".native-control"
        with contextlib.suppress(OSError):
            if pointer.read_text().strip() == str(self.control_path):
                pointer.unlink()
        self.control_dir.cleanup()

    async def setup(self, source):
        async with self.setup_lock:
            args = []
            if source == "folder":
                path = (await self.dialog("open-models")).get("path")
                if not path:
                    return {"ready": False}
                args = ["--from", path]
            elif source != "download":
                raise ValueError("Select a model folder or download the models.")
            p = await asyncio.create_subprocess_exec(
                str(self.helpers / "jard-engine"),
                "models",
                *args,
                stdout=asyncio.subprocess.PIPE,
            )
            error = ""
            try:
                async for line in p.stdout:
                    event = json.loads(line)
                    if event.get("event") == "error":
                        error = event["message"]
                    send({"event": "setup", "detail": event})
                if await p.wait():
                    raise ValueError(error or "Model setup failed. Try again.")
            finally:
                if p.returncode is None:
                    p.terminate()
                    await p.wait()
            return await self.connect_if_ready()

    async def input_devices(self):
        devices = json.loads(await run_helper(self.helpers / "jard-capture", "devices"))
        result = {"microphone": devices["microphone"], "inputs": devices.get("inputs", [])}
        if "dictationMicrophones" in devices:
            result["dictationMicrophones"] = devices["dictationMicrophones"]
        return result

    async def devices(self):
        devices = await self.input_devices()
        drafts = await self.api("GET", "native/staging")
        active = self.workspace_capture()
        active_stages = {take.stage["id"] for take in self.captures.values()}
        hide_meeting_drafts = meetings_managed()
        return {
            **devices,
            "capture": active.state if active and active.started else None,
            "drafts": [
                {"id": d["id"], "seconds": max(0, (d["bytes"] - 44) / 96000), "meeting": d.get("meeting", False)}
                for d in drafts
                if d["id"] not in active_stages and not (hide_meeting_drafts and d.get("meeting"))
            ],
        }

    @staticmethod
    def options(args):
        hints = args.get("languages", [])
        if not isinstance(hints, list) or any(
            not isinstance(code, str) for code in hints
        ):
            raise ValueError("Invalid language hints.")
        return {
            "language": hints,
            "diarization": "true" if args.get("diarization") is True else "false",
            "live": "true" if args.get("live") is True else "false",
        }

    async def submit(self, stage_id, options, *, name, mime, recording, recover=False, notes_draft=None):
        return await self.api(
            "POST",
            f"native/staging/{stage_id}/submit",
            params={
                **options,
                **({"notes_draft": notes_draft} if notes_draft else {}),
                "name": name,
                "mime": mime,
                "recording": str(recording).lower(),
                "recover": str(recover).lower(),
            },
        )

    async def open_audio(self, args):
        if self.captures:
            raise ValueError("Stop the recording before opening another file.")
        selection = (await self.dialog("open-audio")).get("path")
        if not selection:
            return None
        stage = await self.api("POST", "native/staging")
        try:
            await asyncio.to_thread(shutil.copyfile, selection, stage["path"])
            return await self.submit(
                stage["id"],
                self.options(args),
                name=Path(selection).name,
                mime=mimetypes.guess_type(selection)[0] or "application/octet-stream",
                recording=False,
                notes_draft=args.get("notesDraft"),
            )
        except BaseException:
            with contextlib.suppress(httpx.HTTPError):
                await self.api("DELETE", "native/staging/" + stage["id"])
            raise

    @staticmethod
    def input_options(args):
        uid = args.get("inputDevice")
        options = ("--input", uid) if isinstance(uid, str) and uid and "\x00" not in uid else ()
        return options + ("--avoid-bluetooth",) if args.get("avoidBluetooth") is True else options

    def publish_warm(self, availability=None):
        if self.warm_request_id is None:
            return
        detail = {"request": self.warm_request_id, **(availability or {"phase": "idle"})}
        if detail != self.warm_notice:
            self.warm_notice = detail
            send({"event": "jard-microphone", "detail": detail})

    async def read_warm(self, worker):
        try:
            async for raw in worker.child.stdout:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                if "sharedMeeting" in event:
                    if worker.meeting and event["sharedMeeting"] == worker.meeting[0]:
                        queue = worker.meeting[1]
                        if kind != "levels" or queue.qsize() < 128:
                            queue.put_nowait(event)
                        if kind == "takeEnded":
                            worker.meeting = None
                elif kind == "warmAvailability":
                    phase, microphone, message = (event.get(key) for key in ("phase", "microphone", "message"))
                    if phase in ("ready", "fallback", "unavailable") and isinstance(microphone, str) and isinstance(message, str):
                        worker.availability = {"phase": phase, "microphone": microphone[:240], "message": message[:600]}
                        if phase == "unavailable":
                            worker.input_ready.clear()
                        else:
                            worker.input_ready.set()
                        if self.warm_worker is worker and worker.options == self.warm_requested:
                            self.publish_warm(worker.availability)
                elif kind == "warmMeter":
                    if worker.meter and event.get("request") == worker.meter[0] and not worker.meter[1].done():
                        peak = event.get("peak")
                        worker.meter[1].set_result(peak if type(peak) in (int, float) and 0 <= peak <= 1 else None)
                elif kind == "warmAnnotation":
                    if worker.annotation_reply and event.get("request") == worker.annotation_reply[0] and not worker.annotation_reply[1].done():
                        worker.annotation_reply[1].set_result(event.get("error"))
                elif kind == "microphoneClockReset" and event.get("result") in ("reset", "unavailable", "failed", "restoreFailed"):
                    write_event("microphone", "clock_" + event["result"])
                elif worker.events is not None:
                    if kind != "levels" or worker.events.qsize() < 128:
                        worker.events.put_nowait(event)
                    if kind == "takeEnded":
                        worker.events = None
        except (OSError, ValueError):
            if worker.child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    worker.child.terminate()
        finally:
            worker.input_ready.set()
            if worker.annotation_reply and not worker.annotation_reply[1].done():
                worker.annotation_reply[1].set_result("The shared microphone stopped.")
            if worker.meter and not worker.meter[1].done():
                worker.meter[1].set_result(None)
            if worker.events is not None:
                worker.events.put_nowait(None)
                worker.events = None
            if worker.meeting is not None:
                worker.meeting[1].put_nowait(None)
                worker.meeting = None
            if self.warm_worker is worker and worker.options == self.warm_requested:
                self.publish_warm({"phase": "unavailable", "message": "The microphone stopped. Try the dictation shortcut to reconnect."})

    async def close_warm(self):
        worker = self.warm_worker
        self.warm_worker = None
        if worker is None:
            return
        self.publish_warm()
        child = worker.child
        if child.returncode is None:
            child.stdin.close()
            try:
                await asyncio.wait_for(child.wait(), 2)
            except TimeoutError:
                child.kill()
                await child.wait()
        if worker.reader is not None:
            await worker.reader

    async def open_warm(self, options, *, on_demand=False):
        worker = self.warm_worker
        if (worker and worker.child.returncode is None and worker.options == options and worker.on_demand == on_demand
                and worker.reader and not worker.reader.done()):
            if worker.availability is not None:
                self.publish_warm(worker.availability)
            return worker
        await self.close_warm()
        child = await asyncio.create_subprocess_exec(
            str(self.helpers / "jard-capture"), "warm", *(("--on-demand",) if on_demand else ()), *options,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )
        worker = self.warm_worker = WarmWorker(child, options, on_demand=on_demand)
        try:
            raw = await asyncio.wait_for(child.stdout.readline(), 3)
            ready = json.loads(raw)
            if not isinstance(ready, dict) or ready.get("event") != "warmReady":
                message = ready.get("message") if isinstance(ready, dict) else None
                raise RuntimeError(message or "The microphone could not stay ready.")
        except (TimeoutError, ValueError, OSError, RuntimeError) as error:
            await self.close_warm()
            message = str(error) if isinstance(error, RuntimeError) else "The microphone could not stay ready."
            if options == self.warm_requested:
                self.publish_warm({"phase": "unavailable", "message": message[:600]})
            return None
        worker.reader = asyncio.create_task(self.read_warm(worker))
        return worker

    async def reconcile_warm(self):
        async with self.capture_lock:
            recording = self.captures.get("recording")
            if (self.updating or self.annotation or self.sdk_capture.active or self.meeting_mic_lease
                    or "dictation" in self.captures or (recording and (not recording.state.get("meeting") or recording.warm_events is not None))):
                return
            if self.warm_requested is None:
                await self.close_warm()
            else:
                await self.open_warm(self.warm_requested)

    async def warm(self, args):
        self.warm_requested = self.input_options(args) if args.get("keep") is True else None
        request = args.get("request")
        self.warm_request_id = request if self.warm_requested is not None and isinstance(request, str) and len(request) == 36 else None
        await self.reconcile_warm()
        return {"warm": bool(self.warm_worker and self.warm_worker.child.returncode is None)}

    async def microphone_level(self):
        worker = self.warm_worker
        if worker and worker.child.returncode is None and worker.options == self.warm_requested:
            if worker.meter:
                return {"peak": None}
            self.meter_sequence += 1
            future = asyncio.get_running_loop().create_future()
            worker.meter = (self.meter_sequence, future)
            try:
                worker.child.stdin.write(json.dumps({"meter": self.meter_sequence}).encode() + b"\n")
                await asyncio.wait_for(worker.child.stdin.drain(), 0.2)
                peak = await asyncio.wait_for(future, 0.25)
                return {"peak": peak if self.warm_worker is worker and worker.options == self.warm_requested else None}
            except (TimeoutError, OSError):
                return {"peak": None}
            finally:
                worker.meter = None
        take = self.captures.get("dictation")
        if take and take.global_dictation and not take.finished and take.microphone_peak:
            at, peak = take.microphone_peak
            if monotonic() - at <= 0.25:
                return {"peak": peak}
        return {"peak": None}

    def workspace_capture(self):
        dictation = self.captures.get("dictation")
        return self.captures.get("recording") or (dictation if dictation and not dictation.global_dictation else None)

    async def allow_meeting_mic_recovery(self, take, identifier):
        async with self.capture_lock:
            if self.captures.get("recording") is not take or take.finished or take.child.returncode is not None:
                return
            allowed = not (self.meeting_mic_lease or "dictation" in self.captures or self.dictation_stages
                           or self.dictation_jobs or self.annotation or self.sdk_capture.active or self.updating)
            if allowed:
                if take.warm_events is None:
                    await self.close_warm()
                self.meeting_mic_lease = (take, identifier)
                self.meeting_mic_available.clear()
            try:
                command = "recover-mic" if allowed else "defer-mic"
                await take.command(f"{command} {identifier}")
            except (BrokenPipeError, ConnectionResetError):
                if allowed:
                    self.release_meeting_mic(take, identifier)

    def release_meeting_mic(self, take, identifier=None):
        lease = self.meeting_mic_lease
        if lease and lease[0] is take and (identifier is None or lease[1] == identifier):
            self.meeting_mic_lease = None
            self.meeting_mic_available.set()
            asyncio.create_task(self.reconcile_warm())

    async def select_meeting_input(self, args):
        uid = args.get("inputDevice")
        if not isinstance(uid, str) or len(uid) > 1024 or "\x00" in uid:
            raise ValueError("Invalid microphone selection.")
        async with self.capture_lock:
            take = self.captures.get("recording")
            if not take or not take.state.get("meeting") or take.finished or take.child.returncode is not None:
                return None
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await take.command("input " + json.dumps(uid))

    async def record(self, args):
        if args.get("meeting") is not None:
            require_meetings()
        # Reserve each slot under one lock so repeated starts cannot create a second take.
        async with self.capture_lock:
            try:
                await asyncio.wait_for(self.meeting_mic_available.wait(), 3)
            except TimeoutError:
                raise ValueError("The meeting microphone is reconnecting. Try dictation again.") from None
            if self.updating:
                raise ValueError("Jard is preparing to restart.")
            if self.sdk_capture.active:
                raise ValueError("A recording is already active.")
            dictation = args.get("dictation")
            if dictation:
                dictation = str(UUID(dictation))
            mode = "mic" if dictation else args.get("mode", "both")
            if mode not in ("both", "mic", "system"):
                raise ValueError("Unknown recording source.")
            meeting = args.get("meeting")
            meeting_label = args.get("meetingLabel", "Meeting")
            if meeting is not None:
                meeting = str(UUID(meeting))
                if dictation or mode != "both":
                    raise ValueError("A meeting requires microphone and Mac audio.")
                if not isinstance(meeting_label, str) or not 1 <= len(meeting_label) <= 80 or any(
                    char in "/\\:" or not char.isprintable() for char in meeting_label
                ):
                    raise ValueError("Invalid meeting provider name.")
            slot = "dictation" if dictation else "recording"
            recording, speaking = self.captures.get("recording"), self.captures.get("dictation")
            global_dictation = bool(dictation and args.get("global") is True)
            annotation_owns_worker = self.annotation_worker is not None and self.annotation_worker is self.warm_worker
            shared_annotation = annotation_owns_worker and (global_dictation or (meeting and self.annotation_narrating))
            if self.annotation and not shared_annotation:
                raise ValueError("Stop the screen annotation first.")
            shared_worker = (shared_annotation or (global_dictation and recording is not None and recording.warm_events is not None)
                             or (meeting and speaking is not None and speaking.warm_events is not None))
            compatible = (
                global_dictation and recording and recording.state.get("meeting")
                or meeting and speaking and speaking.global_dictation
            )
            if slot in self.captures or (self.captures and not compatible):
                raise ValueError("A recording is already active.")
            live = args.get("live") is True if global_dictation else bool(dictation or args.get("live"))
            settings = self.options({**args, "diarization": False, "live": live} if dictation else args)
            if (await self.api("GET", "status"))["status"] != "ready":
                raise ValueError("Wait for the speech model to finish loading.")
            stage_options = {"dictation" if dictation else "recording": "true"}
            if meeting:
                stage_options.update(meeting="true", provider=meeting_label)
                if args.get("noiseCleanup") is True:
                    stage_options["noise_cleanup"] = "true"
                if args.get("boostMicrophone") is True:
                    stage_options["boost_microphone_input"] = "true"
            calendar = args.get("meetingCalendar") if meeting else None
            title = normalize_meeting_title(args["meetingTitle"]) if meeting and "meetingTitle" in args else None
            stage = await self.api("POST", "native/staging", params=stage_options, **({"json": calendar} if calendar else {}))
            marker = None
            try:
                marker = (
                    Path(stage["path"]).with_suffix(".dictation" if dictation else ".recording").open("r+b")
                )
                fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                notes_draft = str(UUID(args["notesDraft"])) if not dictation and args.get("notesDraft") else None
                if meeting and (title or notes_draft):
                    metadata = recording_metadata(marker)
                    if title:
                        metadata["meetingTitle"] = title
                    if notes_draft:
                        metadata["notesDraft"] = notes_draft
                    write_recording_metadata(marker, metadata)
                elif notes_draft:
                    marker.write(notes_draft.encode("ascii"))
                    marker.flush()
                options = self.input_options(args) if global_dictation else ()
                worker = self.warm_worker
                cold_recovery = global_dictation and args.get("keep") is not True
                if cold_recovery and not shared_worker:
                    warm = await self.open_warm(options, on_demand=True)
                elif meeting and not shared_worker and speaking is None:
                    warm = await self.open_warm(self.input_options({"inputDevice": args.get("inputDevice")}), on_demand=True)
                else:
                    warm = worker if shared_worker or (global_dictation and worker and worker.options == options) else None
                warm_events = None
                if meeting:
                    options = ("--separate-tracks",)
                    application = args.get("meetingApplication")
                    if isinstance(application, str) and 0 < len(application) <= 255:
                        options += ("--call-application", application)
                    backend = args.get("meetingSystemAudio")
                    if backend in ("tap", "tapAndScreen", "screen"):
                        options += ("--system-audio", backend, "--host-pid", str(os.getppid()))
                    options += self.input_options({"inputDevice": args.get("inputDevice")})
                worker_available = warm and warm.child.returncode is None and warm.reader and not warm.reader.done()
                if meeting and worker_available:
                    application = args.get("meetingApplication")
                    backend = args.get("meetingSystemAudio")
                    warm_events = asyncio.Queue()
                    warm.meeting = (meeting, warm_events)
                    message = {"meeting": meeting, "start": stage["path"], "hostPID": os.getppid()}
                    if backend in ("tap", "tapAndScreen", "screen"):
                        message["systemAudio"] = backend
                    if isinstance(application, str) and 0 < len(application) <= 255:
                        message["callApplication"] = application
                    input_options = self.input_options({"inputDevice": args.get("inputDevice")})
                    if input_options:
                        message["inputDevice"] = input_options[1]
                    try:
                        warm.child.stdin.write((json.dumps(message) + "\n").encode())
                        await warm.child.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        warm.meeting = None
                        raise ValueError("The shared microphone stopped. Try the meeting again.") from None
                elif worker_available:
                    try:
                        warm_events = warm.events = asyncio.Queue()
                        queued_at = args.get("queuedAt")
                        warm.child.stdin.write((json.dumps({
                            "start": stage["path"], "preRoll": args.get("preRoll") is not False and not (cold_recovery and not shared_worker),
                            **({"queuedAt": queued_at} if type(queued_at) in (int, float) and 0 < queued_at < math.inf else {}),
                        }) + "\n").encode())
                        await warm.child.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        if shared_worker:
                            raise ValueError("The shared microphone stopped. Try again when it reconnects.") from None
                        await self.close_warm()
                        warm = warm_events = None
                else:
                    if shared_worker:
                        raise ValueError("The shared microphone is unavailable.")
                    if not meeting:
                        await self.close_warm()
                    warm = None
                child = warm.child if warm else await asyncio.create_subprocess_exec(
                    str(self.helpers / "jard-capture"),
                    "record", "--mode", mode, "--output", stage["path"],
                    *(("--meter",) if global_dictation else ()),
                    *(("--cold-recovery",) if cold_recovery else ()), *options,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                )
            except BaseException:
                if marker:
                    marker.close()
                await self.api("DELETE", "native/staging/" + stage["id"])
                raise
            state = {
                "mode": mode,
                "live": live,
                "warm": warm is not None,
                **({"dictation": dictation} if dictation else {}),
                **({"meeting": meeting, "meetingLabel": meeting_label, "meetingCalendar": calendar,
                    "meetingTurns": [], **({"meetingTitle": title} if title else {})} if meeting else {}),
                "seconds": 0,
                "text": "",
                "segments": [],
            }
            take = Capture(child, stage, marker, settings, state, global_dictation,
                           title_plan=LiveTitlePlan() if meeting and live and not title else None,
                           warm_events=warm_events)
            self.captures[slot] = take
            take.job = asyncio.create_task(self.record_events(slot, take))
        return {"starting": True}

    async def live_titles(self, take):
        plan = take.title_plan
        path = Path(take.stage["path"])
        while not take.finished and not plan.finished:
            await asyncio.sleep(LIVE_TITLE_INTERVAL)
            if (not take.started or take.finished or not plan.due() or self.capture_lock.locked()
                    or "dictation" in self.captures or self.dictation_jobs):
                continue
            timeline = plan.timeline
            try:
                value = await self.api("POST", f"native/staging/{take.stage['id']}/meeting-title",
                                       params={"through": plan.through}, timeout=35)
                if take.finished or asyncio.current_task().cancelling() or self.captures.get("recording") is not take:
                    return
                if isinstance(value, dict) and value.get("automatic") is False:
                    continue
                title = write_meeting_title(path, value["title"])
            except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError):
                plan.record(published=False, timeline=timeline)
                continue
            plan.record(published=True, timeline=timeline)
            take.state["provisionalTitle"] = title
            emit("meetingTitle", meeting=take.state["meeting"], title=title)

    async def live_preview(self, take):
        block = 4 * 48000
        start = 0
        parts = []
        warned = False
        warned_storage = False
        capture = take.state
        capture_id = capture.get("dictation")
        metadata = {"dictation": capture_id} if capture_id else {}
        meeting = capture.get("meeting")
        copy_state = take.copy_state
        if meeting:
            metadata["meeting"] = meeting
        path = Path(take.stage["path"])
        while True:
            await asyncio.sleep(0.35)
            if not take.started:
                continue
            if meeting and (self.capture_lock.locked() or "dictation" in self.captures or self.dictation_jobs):
                # Capture continues to disk. Wait before queuing another background
                # decode so the spoken shortcut does not compete for the same model.
                continue
            available = max(0, (path.stat().st_size - 44) // 2)
            cut = copy_state.get("cut") if copy_state else None
            if not capture["live"] and cut is None:
                if not copy_state:
                    start = available
                continue
            size = min(block, cut - start) if cut is not None and cut > start else block
            if available - start < size:
                continue
            if not meeting and available - start > block * 3:
                start = available - block
            try:
                value = await self.api(
                    "POST",
                    f"native/staging/{take.stage['id']}/preview",
                    params={**take.settings, "start": start, "frames": size, **({"meeting": "true"} if meeting else {})},
                )
                if meeting:
                    try:
                        append_rolling(path, start, start + size, value)
                        if take.title_plan:
                            take.title_plan.observe(start + size, value["segments"])
                    except (OSError, ValueError):
                        if not warned_storage:
                            emit("warning", **metadata, message="Live text could not be saved. Recording continues.")
                            warned_storage = True
                if (capture["live"] or copy_state) and value.get("text"):
                    if meeting:
                        capture["meetingTurns"].extend(value["segments"])
                    # Keep the provisional UI bounded. The final pass always uses the full take.
                    parts = (parts + [value])[-160:]
                    capture["text"] = " ".join(part["text"] for part in parts)
                    capture["segments"] = [
                        segment
                        for part in parts
                        for segment in part.get("segments", [])
                    ]
                    emit(
                        "preview",
                        **metadata,
                        **({"delta": value["text"], "deltaSegments": value["segments"], "through": start + size} if meeting else {}),
                        text=capture["text"],
                        segments=capture["segments"],
                    )
                warned = False
            except httpx.HTTPError:
                if not warned:
                    emit(
                        "warning",
                        **metadata,
                        message="Live preview is delayed. The recording is still being saved.",
                    )
                warned = True
                if meeting:
                    continue
            start += size
            if copy_state:
                copy_state["through"] = start
                copy_state["changed"].set()

    async def copy_meeting_through_now(self, args):
        expected = str(UUID(args["meeting"]))
        take = self.captures.get("recording")
        state = take.copy_state if take else None
        if not state or take.state.get("meeting") != expected or take.finished:
            raise ValueError("This meeting is no longer active.")
        if state.get("request"):
            raise ValueError("Jard is already preparing a meeting excerpt.")
        request = str(uuid4())
        state["request"] = request
        state["cut"] = None
        try:
            async with asyncio.timeout(3):
                await take.command(f"copy {request}")
                while not state.get("closed"):
                    cut = state["cut"]
                    if cut is not None and state["through"] >= cut:
                        return {"through": cut, "complete": True}
                    state["changed"].clear()
                    await state["changed"].wait()
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            state["request"] = state["cut"] = None
            state["changed"].set()
        return {"through": state["through"], "complete": False}

    async def record_events(self, slot, take):
        child, stage, capture = take.child, take.stage, take.state
        capture_id = capture.get("dictation")
        meeting = capture.get("meeting")

        def notify(kind, **fields):
            if capture_id:
                fields["dictation"] = capture_id
            if take.global_dictation:
                fields["global"] = True
            if meeting:
                fields["meeting"] = meeting
            emit(kind, **fields)
            if not capture_id and kind in ("started", "finished", "accepted", "error"):
                write_event("meeting" if meeting else "recording", kind)

        path = Path(stage["path"])
        copy_state = {"through": 0, "changed": asyncio.Event()} if meeting else None
        take.copy_state = copy_state
        preview = asyncio.create_task(self.live_preview(take))
        if take.title_plan:
            take.title_job = asyncio.create_task(self.live_titles(take))
        reason = ""
        permission = {}
        interruption = {}
        opening_abandoned = False
        warm_ended = False
        try:
            async for event in take.input_events():
                kind = event.get("event")
                if capture_id and kind in ("error", "finished") and event.get("interruption") in ("microphone", "discontinuity"):
                    interruption = {"interruption": event["interruption"]}
                    for field in ("failedAt", "capturedThrough"):
                        stamp = event.get(field)
                        if type(stamp) in (int, float) and 0 < stamp < math.inf:
                            interruption[field] = stamp
                    if type(event.get("hasLiveAudio")) is bool:
                        interruption["hasLiveAudio"] = event["hasLiveAudio"]
                if kind == "started":
                    take.started = True
                    if meeting:
                        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        capture["meetingStartedAt"] = started
                        with contextlib.suppress(OSError):
                            take.marker.seek(0)
                            metadata = recording_metadata(take.marker)
                            metadata.update(meetingStartedAt=started, languageHints=take.settings["language"])
                            write_recording_metadata(take.marker, metadata)
                    notify(
                        "started",
                        mode=capture["mode"],
                        microphone=event["microphone"],
                        **({"warm": True} if take.warm_events is not None else {}),
                        **({"live": True, "session": stage["id"]} if meeting else {}),
                        **({"microphoneAvailable": event["microphoneAvailable"]}
                           if meeting and type(event.get("microphoneAvailable")) is bool else {}),
                        **({"openedAt": event["openedAt"]} if capture_id and "openedAt" in event else {}),
                        **({"usingDefaultMicrophone": True} if capture_id and event.get("usingDefaultMicrophone") is True else {}),
                    )
                elif kind == "levels" and take.started:
                    capture["seconds"] = event["seconds"]
                    if take.global_dictation and type(event["mic"]) in (int, float) and 0 <= event["mic"] <= 1:
                        take.microphone_peak = (monotonic(), event["mic"])
                    notify(
                        "levels",
                        seconds=event["seconds"],
                        mic=event["mic"],
                        system=event["system"],
                        microphone=event["microphone"],
                        **({"micRms": event["micRms"]} if "micRms" in event else {}),
                        **({"meetingRms": event["meetingRms"]} if meeting and "meetingRms" in event else {}),
                    )
                elif kind == "sourceChanged" and take.started:
                    notify(kind, microphone=event["microphone"], message=event["message"],
                           **({"microphoneAvailable": event["microphoneAvailable"]}
                              if meeting and type(event.get("microphoneAvailable")) is bool else {}))
                elif kind == "meetingSystemAudio" and meeting and take.started and type(event.get("available")) is bool:
                    notify(kind, available=event["available"])
                elif kind == "meetingMicWarning" and meeting and take.started and type(event.get("active")) is bool:
                    active = event["active"]
                    if active and not capture.get("meetingMicrophoneWarning"):
                        capture["meetingMicrophoneWarning"] = "digitalSilence"
                        with contextlib.suppress(OSError):
                            take.marker.seek(0)
                            metadata = recording_metadata(take.marker)
                            metadata["meetingMicrophoneWarning"] = capture["meetingMicrophoneWarning"]
                            write_recording_metadata(take.marker, metadata)
                    notify(kind, active=active)
                elif kind == "meetingMicRecoveryRequest" and meeting and take.started:
                    identifier = event.get("id")
                    if isinstance(identifier, str):
                        with contextlib.suppress(ValueError):
                            await self.allow_meeting_mic_recovery(take, str(UUID(identifier)))
                elif kind == "meetingMicRecoveryDone" and meeting:
                    identifier = event.get("id")
                    if isinstance(identifier, str):
                        self.release_meeting_mic(take, identifier)
                elif kind == "microphoneClockReset" and event.get("result") in ("reset", "unavailable", "failed", "restoreFailed"):
                    write_event("microphone", "clock_" + event["result"])
                elif kind == "openingAbandoned":
                    opening_abandoned = True
                elif kind == "error":
                    take.stop_titles()
                    reason = event["message"]
                    if event.get("permission") in ("mic", "system"):
                        permission = {"permission": event["permission"]}
                elif kind == "meetingCopy" and copy_state and copy_state.get("request") == event.get("request"):
                    copy_state["cut"] = max(copy_state["through"], int(event["frame"]))
                    copy_state["changed"].set()
                elif kind == "finished":
                    take.stop_titles()
                    reason = event.get("reason", "Stopped")
                    if capture_id and type(event.get("releaseFrame")) is int:
                        capture["releaseFrame"] = event["releaseFrame"]
                elif kind == "takeEnded" and capture["warm"]:
                    warm_ended = True
                    break
            if not warm_ended:
                status = await child.wait()
                if capture["warm"] and self.warm_worker and self.warm_worker.child is child:
                    self.warm_worker = None
                if status and not reason:
                    reason = "The recorder exited unexpectedly."
        except (OSError, ValueError, KeyError) as e:
            reason = explain(e)
            if child.returncode is None:
                if take.warm_events is not None:
                    with contextlib.suppress(OSError):
                        await take.command("stop")
                else:
                    child.terminate()
                    await child.wait()
        finally:
            take.finished = True
            if meeting:
                self.release_meeting_mic(take)
            take.stop_titles()
            if take.title_job:
                await asyncio.gather(take.title_job, return_exceptions=True)
            if copy_state:
                # Allow a cut pressed before Stop to read the final committed samples.
                if copy_state.get("cut") is not None:
                    with contextlib.suppress(OSError):
                        committed = (path.stat().st_size - 44) // 2
                        copy_state["cut"] = min(copy_state["cut"], max(0, committed))
                        copy_state["changed"].set()
                        while copy_state.get("request"):
                            copy_state["changed"].clear()
                            await copy_state["changed"].wait()
                copy_state["closed"] = True
                copy_state["changed"].set()
            preview.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await preview
            take.started = False
        try:
            if capture_id:
                if not capture.get("discard") and not permission and not opening_abandoned and path.is_file() and path.stat().st_size > 44:
                    self.dictation_stages[capture_id] = stage["id"]
                    if "releaseFrame" in capture:
                        self.dictation_release_frames[stage["id"]] = capture["releaseFrame"]
                    notify("dictationReady", id=stage["id"], interrupted=bool(reason and reason != "Stopped"),
                           seconds=max(0, path.stat().st_size - 44) / 96000, reason=reason, **interruption)
                else:
                    await self.api("DELETE", "native/staging/" + stage["id"])
                    if not capture.get("discard"):
                        message = reason if reason and reason != "Stopped" else "Hold the shortcut a little longer to capture audio."
                        notify("error", message=message, noSpeech=not permission and reason in (None, "", "Stopped"),
                               **permission, **interruption, **({"openingAbandoned": True} if opening_abandoned else {}))
                return
            recorded_bytes = path.stat().st_size if path.is_file() else 0
            if recorded_bytes > 44:
                notify("finished", reason=reason or "Stopped",
                       **({"seconds": (recorded_bytes - 44) / 96000} if meeting else {}))
                prefix = "Recording"
                if meeting:
                    label = capture["meetingLabel"]
                    prefix = "Meeting" if label == "Meeting" else f"{label} meeting"
                data = await self.submit(
                    stage["id"],
                    take.settings,
                    name=f"{prefix} {datetime.datetime.now():%Y-%m-%d %H.%M.%S}",
                    mime="audio/wav",
                    recording=True,
                    recover=True,
                )
                notify("accepted", id=data["id"], **({"interruption": reason} if meeting and reason and reason != "Stopped" else {}))
            else:
                await self.api("DELETE", "native/staging/" + stage["id"])
                notify(
                    "error",
                    message=reason
                    or "No audio was captured. Check the selected source and its macOS permission.",
                    **permission,
                )
        except (httpx.HTTPError, OSError) as e:
            recoverable = {}
            if meeting:
                with contextlib.suppress(OSError):
                    if path.stat().st_size > 44:
                        recoverable["draft"] = stage["id"]
            notify(
                "error",
                message=explain(e) if capture_id else "The recording was preserved for recovery. " + explain(e),
                **recoverable,
            )
        finally:
            take.marker.close()
            self.captures.pop(slot)
            asyncio.create_task(self.reconcile_warm())

    @staticmethod
    async def stop(take, *, release_tail=False, release_at=None):
        take.stop_titles()
        child = take.child
        if not take.finished and child.returncode is None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                command = "finish" if release_tail else "stop"
                if take.state.get("warm") and take.state.get("discard"):
                    command = "cancel"
                if command == "finish" and type(release_at) in (int, float) and 0 < release_at < math.inf:
                    command += f" {release_at:.9f}"
                await take.command(command)

    async def discard_dictation(self, key):
        owner = next((capture for capture, stage in self.dictation_stages.items() if stage == key), None)
        if owner is None:
            return
        try:
            await self.api("DELETE", "native/staging/" + key, params={"dictation": "true"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
        self.dictation_stages.pop(owner, None)
        self.dictation_release_frames.pop(key, None)

    async def export(self, args):
        key, kind = str(UUID(args["id"])), args["kind"]
        annotation_kind = kind.removeprefix("annotation-") if isinstance(kind, str) and kind.startswith("annotation-") else ""
        if kind not in ("audio", "json", "md", "notes", "srt", "vtt") and annotation_kind not in ANNOTATION_ASSETS:
            raise ValueError("Unsupported export format.")
        session = await self.api("GET", "sessions/" + key)
        library = SessionStore(self.root, read_only=True)
        protected = self.root.resolve()
        capture_folder = None
        if session.get("annotation") and not session.get("annotationStorageUnavailable"):
            with contextlib.suppress(MissingSession):
                capture_folder = (await asyncio.to_thread(library.annotation_asset, key, "manifest"))[0].parent
        data = None
        if annotation_kind:
            name = Path(session["audioName"]).stem + "-" + ANNOTATION_ASSETS[annotation_kind][0]
            response = await self.http.get(f"/api/sessions/{key}/annotation/{annotation_kind}")
            response.raise_for_status()
            data = response.content
        elif kind == "audio":
            if not session.get("audioUrl"):
                raise ValueError("The recording is unavailable.")
            name = session["audioName"]
        elif kind == "json":
            name = Path(session["audioName"]).stem + ".json"
        else:
            response = await self.http.get(f"/api/sessions/{key}/export", params={"kind": kind})
            response.raise_for_status()
            name = unquote(response.headers["Content-Disposition"].split("UTF-8''", 1)[1])
            data = response.content
        choice = (await self.dialog("save", name)).get("path")
        if not choice:
            return None
        dest = Path(choice)
        resolved = dest.resolve()
        if resolved.is_relative_to(protected) or (capture_folder and resolved.is_relative_to(capture_folder)):
            raise ValueError(
                "Choose an export destination outside the session library."
            )

        def write():
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as temp:
                partial = Path(temp.name)
            try:
                if kind == "audio":
                    source, _ = library.media(key)
                    shutil.copyfile(source, partial)
                elif data is not None:
                    partial.write_bytes(data)
                else:
                    partial.write_text(
                        json.dumps(
                            transcript_export(session), indent=2, ensure_ascii=False
                        )
                        + "\n"
                    )
                partial.replace(dest)
            finally:
                partial.unlink(missing_ok=True)

        await asyncio.to_thread(write)
        return {"saved": True}

    async def reveal_session_media(self, args):
        key = str(UUID(args["id"]))
        library = SessionStore(self.root, read_only=True)
        try:
            source, _ = await asyncio.to_thread(library.media, key)
        except MissingSession as error:
            raise ValueError("The recording file is no longer available.") from error
        await run_helper("/usr/bin/open", "-R", str(source))

    async def meeting_exports(self, args):
        action = args.get("action")
        if action == "revealFiles":
            key = str(UUID(args["id"]))
            result = await self.api("GET", f"native/meetings/{key}/recording-files")
            if recording := result.get("path"):
                await run_helper("/usr/bin/open", "-R", recording)
            else:
                await self.reveal_session_media(args)
            return result
        if action == "reveal" and args.get("id"):
            key = str(UUID(args["id"]))
            result = await self.api("GET", f"native/meeting-exports/{key}")
            await run_helper("/usr/bin/open", "-R", result["path"])
            return {"folder": str(Path(result["path"]).parent)}
        if action == "separateNotes":
            return await self.api("PUT", "native/meeting-exports/notes", json={"enabled": args.get("enabled")})
        if action == "descriptiveNames":
            return await self.api("PUT", "native/meeting-exports/descriptive", json={"enabled": args.get("enabled")})
        if action not in ("status", "choose", "disable", "reveal"):
            raise ValueError("Choose a meeting export action.")
        route = "native/meeting-exports"
        if action == "disable":
            return await self.api("PUT", route, json={"folder": None})
        status = await self.api("GET", route)
        if action == "choose":
            choice = (await self.dialog("meeting-export-folder", status["folder"] or "")).get("path")
            if choice:
                return await self.api("PUT", route, json={"folder": choice})
        if action == "reveal" and status["folder"]:
            await run_helper("/usr/bin/open", status["folder"])
        return status

    async def annotation_lease(self, args):
        active = args.get("active") is True
        if active:
            require_annotation()
        key = str(UUID(args["id"]))
        async with self.capture_lock:
            if active:
                if self.meeting_mic_lease:
                    raise ValueError("The meeting microphone is reconnecting. Try Annotate again.")
                if self.updating or self.annotation or self.sdk_capture.active:
                    raise ValueError("Finish the active recording or dictation first.")
                worker = self.warm_worker if self.captures else None
                if self.captures and (worker is None or worker.child.returncode is not None or
                        worker.reader is None or worker.reader.done() or any(
                            take.warm_events is None or take.child is not worker.child for take in self.captures.values())):
                    raise ValueError("Finish the active recording or dictation first.")
                if worker is None and (self.dictation_stages or self.dictation_jobs):
                    raise ValueError("Finish the active recording or dictation first.")
                storage = await self.api("GET", "native/annotation-storage")
                if not storage["available"]:
                    raise ValueError("The Annotations folder is unavailable. Reconnect it or choose another folder.")
                self.annotation = key
                self.annotation_worker = worker
                self.annotation_location = storage["storageId"] if storage["custom"] else None
                self.annotation_started_at = time()
            elif self.annotation == key:
                worker = self.annotation_worker
                if self.annotation_narrating and worker:
                    with contextlib.suppress(ValueError, OSError):
                        await self.warm_annotation_command(worker, "cancel")
                self.annotation_worker = self.annotation_narration = None
                self.annotation_narrating = False
                self.annotation_narration_stopped = False
                self.annotation = None
                self.annotation_location = None
                self.annotation_started_at = None
                asyncio.create_task(self.reconcile_warm())
        return {"active": self.annotation == key}

    async def warm_annotation_command(self, worker, action, **fields):
        if worker.child.returncode is not None or worker.reader is None or worker.reader.done():
            raise ValueError("The shared microphone stopped.")
        self.annotation_command += 1
        request = self.annotation_command
        future = asyncio.get_running_loop().create_future()
        worker.annotation_reply = (request, future)
        try:
            message = {"annotation": request, "action": action, **fields}
            worker.child.stdin.write((json.dumps(message, allow_nan=False) + "\n").encode())
            await asyncio.wait_for(worker.child.stdin.drain(), 1)
            error = await asyncio.wait_for(future, 4)
            if isinstance(error, str):
                raise ValueError(error[:600])
        except TimeoutError:
            raise ValueError("The shared microphone did not finish preparing the annotation.") from None
        finally:
            worker.annotation_reply = None

    async def annotation_audio(self, args):
        key = str(UUID(args["id"]))
        action = args.get("action")
        async with self.capture_lock:
            if self.annotation != key or self.updating:
                raise ValueError("This annotation is no longer active.")
            if action == "prepare":
                path = Path(args["path"])
                parent = path.parent
                if (not path.is_absolute() or parent.name != f"jard-annotation-{key}" or path.name != "Narration.wav"
                        or parent.is_symlink() or not parent.is_dir() or path.exists() or path.is_symlink()):
                    raise ValueError("The annotation narration destination is invalid.")
                options = self.input_options(args)
                worker = self.annotation_worker or await self.open_warm(options)
                if worker is None:
                    raise ValueError("Could not open the shared microphone.")
                try:
                    await asyncio.wait_for(worker.input_ready.wait(), 4)
                except TimeoutError:
                    raise ValueError("The shared microphone is unavailable. Check the selected input.") from None
                if not worker.availability or worker.availability["phase"] == "unavailable" or worker.reader.done():
                    raise ValueError("The shared microphone did not become ready.")
                self.annotation_worker, self.annotation_narration = worker, path
            elif action in ("start", "stop") and self.annotation_worker and self.annotation_narration:
                boundary = args.get("at")
                if type(boundary) not in (int, float) or not 0 < boundary < math.inf:
                    raise ValueError("The annotation timing is invalid.")
                if action == "start":
                    if self.annotation_narrating:
                        raise ValueError("Annotation narration is already recording.")
                    # A failed acknowledgement can leave the file sink open. Release
                    # the lease through the usual cancellation path in that case.
                    self.annotation_narrating = True
                    await self.warm_annotation_command(self.annotation_worker, "start", path=str(self.annotation_narration), at=boundary)
                else:
                    if not self.annotation_narrating:
                        raise ValueError("The annotation has no microphone narration.")
                    await self.warm_annotation_command(self.annotation_worker, "stop", at=boundary)
                    self.annotation_narration_stopped = True
            else:
                raise ValueError("This annotation microphone operation is unavailable.")
        return {"ready": True}

    async def annotation_storage(self, args, *, allow_path=False):
        action = args.get("storageAction") if allow_path else args.get("action")
        actions = {"status", "reveal", "reset", "set"} if allow_path else {"status", "reveal", "choose", "reset"}
        if action not in actions:
            raise ValueError("Choose an annotation storage action.")
        route = "native/annotation-storage"
        status = await self.api("GET", route)
        if action == "status":
            return status
        require_annotation()
        if action == "reveal":
            if not status["available"]:
                raise ValueError("The Annotations folder is unavailable.")
            await run_helper("/usr/bin/open", status["folder"])
            return status
        folder = args.get("folder") if action == "set" else None
        if action == "choose":
            folder = (await self.dialog("annotation-folder", status["folder"])).get("path")
            if not folder:
                return await self.api("GET", route)
        async with self.capture_lock:
            if self.updating or self.annotation:
                raise ValueError("Finish the active annotation before changing its folder.")
            return await self.api("PUT", route, json={"folder": folder})

    async def import_annotation(self, args):
        require_annotation()
        key = str(UUID(args["id"]))
        source = Path(args["path"])
        narration_only = args.get("narrationOnly") is True
        filename = "Narration.wav" if narration_only else "Screen annotation.mp4"
        if self.annotation != key or not source.is_absolute() or source.name != filename or (
            source.parent.name != "jard-annotation-" + key or source.is_symlink() or source.parent.is_symlink()
        ):
            raise ValueError("This screen annotation is unavailable.")
        if not source.is_file():
            raise ValueError("The capture did not produce usable media.")
        assets = args.get("assets") is True
        narration = args.get("narration")
        if narration_only and (not assets or narration is not None or source != self.annotation_narration or
                               not self.annotation_narration_stopped):
            raise ValueError("The shared annotation narration has not been saved.")
        if narration is not None:
            if not isinstance(narration, str) or self.annotation_narration != Path(narration):
                raise ValueError("The shared annotation narration is unavailable.")
            narration = self.annotation_narration
            if narration.is_symlink() or narration.parent != source.parent or not narration.is_file():
                raise ValueError("The shared annotation narration could not be read.")
        start = self.annotation_started_at
        origin = annotation_origin(args.get("routeOrigin")) if assets else None
        stage = await self.api("POST", "native/staging", params={"annotation_id": key})
        try:
            target = Path(stage["path"])
            def copy_annotation():
                if narration is None:
                    shutil.copyfile(source, target)
                if assets:
                    for kind, destination in staged_annotation_paths(target).items():
                        original = source.with_name(ANNOTATION_ASSETS[kind][0])
                        if kind == "manifest" or (not narration_only and original.exists()):
                            shutil.copyfile(annotation_input(original, kind), destination)
                            destination.chmod(0o600)

            if narration is not None:
                packaged_ffmpeg = self.helpers / "ffmpeg"
                ffmpeg = str(packaged_ffmpeg) if packaged_ffmpeg.is_file() else shutil.which("ffmpeg")
                if not ffmpeg:
                    raise ValueError("Could not find FFmpeg to attach the narration.")
                try:
                    packaged_probe = self.helpers / "ffprobe"
                    _, has_audio = await annotation_movie_info(source, packaged_probe if packaged_probe.is_file() else "ffprobe")
                    audio = ["-map", "1:a:0", "-af", "apad"]
                    if has_audio:
                        mix = ("[0:a:0]aresample=48000:async=1:first_pts=0[mac];"
                               "[1:a:0]aresample=48000:async=1:first_pts=0[mic];"
                               "[mac][mic]amix=inputs=2:duration=longest,apad[mix]")
                        audio = ["-filter_complex", mix, "-map", "[mix]"]
                    await run_helper(ffmpeg, "-nostdin", "-v", "error", "-y", "-i", source, "-i", narration,
                                     "-map", "0:v:0", *audio, "-c:v", "copy", "-c:a", "aac", "-shortest",
                                     "-movflags", "+faststart", "-f", "mp4", target)
                except (RuntimeError, OSError, ValueError) as error:
                    raise ValueError("Could not attach narration. The screen recording and narration are available in Finder.") from error
            await asyncio.to_thread(copy_annotation)
            location = {"annotation_location": self.annotation_location} if assets and self.annotation_location else {}
            mime = "audio/wav" if narration_only else "video/mp4"
            result = await self.submit(stage["id"], {**self.options(args), **location, "annotation": str(assets).lower()}, name=source.name,
                                       mime=mime, recording=False)
            try:
                await self.automatic_annotations.enqueue(key, origin, start)
            except (ValueError, OSError):
                # Routing preferences do not affect whether the local recording was saved.
                pass
            routing = self.automatic_annotations.status(key)
            if routing["phase"] != "idle":
                result["routing"] = routing
            return result
        except BaseException:
            with contextlib.suppress(httpx.HTTPError):
                await self.api("DELETE", "native/staging/" + stage["id"])
            raise

    def annotation_handoff(self, key, *, require_narration=False):
        if require_narration:
            route = self.automatic_annotations.receipts.get_route(key)
            if route and route["state"] not in ("cancelled", "failed"):
                raise ValueError("Automatic delivery is pending or has already been attempted. Check the annotation in Sessions.")
        library = SessionStore(self.root, read_only=True)
        try:
            detail = annotation_detail(library, key)
        except MissingSession as error:
            raise ValueError("Annotation not found.") from error
        if require_narration and detail["status"] == "processing":
            return None
        markdown = annotation_context(detail)["markdown"]
        if len(json.dumps(markdown, ensure_ascii=False).encode()) > 3 * 1024 * 1024:
            raise ValueError("This annotation is too long to copy from Jard. Use jardctl annotate context.")
        return {"markdown": markdown, "imagePath": detail["contactSheetPath"]}

    def annotation_review(self, key):
        library = SessionStore(self.root, read_only=True)
        try:
            path, _ = library.annotation_asset(key, "contact-sheet")
        except (MissingSession, OSError) as error:
            raise ValueError("The contact sheet is unavailable. Open the annotation in Sessions.") from error
        if path.parent.is_symlink():
            raise ValueError("The contact sheet is unavailable. Open the annotation in Sessions.")
        return {"imagePath": str(path)}

    async def annotation_routes(self, action, args):
        capture = args.get("id")
        if action == "destinations":
            return await annotation_destinations(args.get("query", ""), root=self.root, capture=capture)
        if action == "remember":
            return await set_destination_rule(self.root, capture, args.get("scope"), args.get("target"), args.get("service"), args.get("automatic", False))
        return await asyncio.to_thread(forget_destination_rule, self.root, capture, args.get("ruleId"))

    async def automatic_capture(self, identifier):
        return await self.api("GET", f"sessions/{identifier}")

    async def automatic_send(self, capture, target, service, authorize):
        require_opencode()
        if self.annotation_delivery_busy or self.updating:
            raise RouteUnavailable("Another annotation operation is in progress.")
        return await self.change_annotation_delivery(send_annotation, capture, target, service, authorize=authorize)

    async def send_session_to_agent(self, args):
        agent, target = args.get("agent"), args.get("target")
        identifier = str(UUID(args["id"]))
        if agent not in (*annotation_agents.AGENT_NAMES, "opencode"):
            raise ValueError("Choose a coding agent.")
        if agent == "opencode":
            require_opencode()
        if self.annotation_delivery_busy or self.updating:
            raise ValueError("Wait for the current delivery or restart to finish.")
        self.annotation_delivery_busy = True
        try:
            text, images, has_images, annotation = await asyncio.to_thread(
                annotation_agents.session_payload, self.root, identifier, agent, URL)
            if agent == "opencode":
                annotation_agents.check_size(agent, text, images)
                files = [{"name": f"jaro-image-{index}.{'png' if mime == 'image/png' else 'jpg'}",
                          "uri": f"data:{mime};base64,{data}"}
                         for index, (mime, data) in enumerate(images, 1)]
                body = {"text": text, "files": files, "delivery": "queue"}
                if annotation:
                    await self.automatic_annotations.cancel(identifier, "Automatic send replaced by a manual send.")
                    await send_annotation(self.root, identifier, target, args.get("service"), body=body)
                else:
                    await send_session_prompt(body, target, args.get("service"))
            else:
                await annotation_agents.deliver(agent, target, text, images)
            opened = args.get("open") is True and await annotation_agents.open_agent(agent, target)
            return {"images": "references" if has_images and agent == "t3" else "attached" if has_images else "none",
                    "opened": opened}
        except MissingSession as error:
            raise ValueError("Session not found.") from error
        finally:
            self.annotation_delivery_busy = False

    async def annotation_route(self, args):
        identifier = str(UUID(args["id"]))
        action = args.get("action", "status")
        if action not in ("status", "cancel"):
            raise ValueError("Choose annotation routing status or cancel.")
        if action == "cancel":
            await self.automatic_annotations.cancel(identifier)
        return await self.automatic_annotations.query(identifier)

    async def undo_automatic_annotation(self, identifier):
        async def undo_from_route(root):
            receipt = await asyncio.to_thread(self.automatic_annotations.automatic_receipt, identifier)
            if not receipt:
                raise ValueError("The automatic annotation delivery could not be found.")
            if receipt["status"] == "queued" and receipt["canUndo"]:
                await undo_annotation(root, receipt["deliveryId"])

        await self.change_annotation_delivery(undo_from_route)
        status = await self.automatic_annotations.query(identifier)
        send({"event": "jard-annotation-routing", "detail": status})
        return status

    async def change_annotation_delivery(self, operation, *values, **options):
        require_opencode()
        if self.annotation_delivery_busy or self.updating:
            raise ValueError("Wait for the current annotation delivery or restart to finish.")
        self.annotation_delivery_busy = True
        try:
            return await operation(self.root, *values, **options)
        except MissingSession as error:
            raise ValueError("Annotation not found.") from error
        finally:
            self.annotation_delivery_busy = False

    async def deliver_annotation(self, args):
        require_opencode()
        detail = await asyncio.to_thread(capture_for_routing, self.root, args.get("id"))
        identifier = detail["id"]
        await self.automatic_annotations.cancel(identifier, "Automatic send replaced by a manual send.")
        return await self.change_annotation_delivery(send_annotation, identifier, args.get("target"), args.get("service"))

    @staticmethod
    def panel_delivery_status(receipt):
        return {"capture": receipt["annotation"], "deliveryId": receipt["deliveryId"],
                "phase": "unconfirmed" if receipt["status"] == "unconfirmed" else "sent",
                "message": "OpenCode accepted the annotation.", **describe_delivery(receipt)}

    async def send_panel_annotation(self, identifier, target, service, remember_kind):
        if not service:
            raise ValueError("Refresh the OpenCode destinations and choose again.")

        async def send_from_preview(root):
            receipts = self.automatic_annotations.receipts
            scope = None
            if remember_kind is not None:
                detail = await asyncio.to_thread(capture_for_routing, root, identifier)
                scope = next((value for value in route_scopes(detail) if value["kind"] == remember_kind), None)
                if not scope:
                    raise ValueError("This annotation does not have that captured app, site, or terminal project.")
            route = await asyncio.to_thread(receipts.get_route, identifier)
            if route and route["state"] not in ("cancelled", "failed"):
                previous = await asyncio.to_thread(self.automatic_annotations.route_receipt, route, identifier)
                if not previous or previous["status"] not in ("withdrawn", "reverted"):
                    raise ValueError("An automatic delivery is pending or was already attempted. Check Sessions.")
            try:
                async with asyncio.timeout(12):
                    while (await self.automatic_capture(identifier))["status"] == "processing":
                        await asyncio.sleep(.3)
            except TimeoutError as error:
                raise ValueError("Narration is still processing. Try Send again shortly.") from error
            recent = await asyncio.to_thread(receipts.recent, identifier)
            if any(item["status"] == "unconfirmed" or (item["service"] == service and item["session"]["id"] == target and
                   item["status"] not in ("withdrawn", "reverted", "refused")) for item in recent):
                raise ValueError("A delivery was already attempted. Review it in Sessions before sending again.")

            attempt = None
            destination_title = None

            async def authorize(service_key, destination, included_sheet):
                nonlocal attempt, destination_title
                destination_title = destination["title"]
                attempt = await asyncio.to_thread(receipts.begin, identifier, service_key, destination, included_sheet,
                                                  remember_scope=scope)
                return attempt

            try:
                receipt = await send_annotation(root, identifier, target, service, authorize=authorize, remember_scope=scope)
            except (ValueError, OSError) as error:
                if attempt is None:
                    raise
                receipt = None
                with contextlib.suppress(ValueError, OSError):
                    receipt = await asyncio.to_thread(receipts.get, attempt)
                if receipt and receipt["status"] == "refused":
                    raise
                return {"capture": identifier, "phase": "unconfirmed", "title": destination_title, "message": str(error)}
            finally:
                if attempt:
                    send({"event": "jard-annotation-delivery-changed", "detail": {"capture": identifier}})
            return self.panel_delivery_status(receipt)

        return await self.change_annotation_delivery(send_from_preview)

    async def undo_panel_annotation(self, identifier, delivery):
        async def undo_from_preview(root):
            receipt = await asyncio.to_thread(self.automatic_annotations.receipts.get, delivery)
            if not receipt or receipt["annotation"] != identifier:
                raise ValueError("This delivery does not belong to the annotation.")
            if receipt["status"] == "queued" and receipt["canUndo"]:
                try:
                    receipt = await undo_annotation(root, delivery)
                except (ValueError, OSError) as error:
                    current = await asyncio.to_thread(self.automatic_annotations.receipts.get, delivery)
                    if not current or current.get("rememberedRule") == receipt.get("rememberedRule"):
                        raise
                    status = self.panel_delivery_status(current)
                    return {**status, "message": f"{status['message']} {error}"}
            return self.panel_delivery_status(receipt)

        try:
            return await self.change_annotation_delivery(undo_from_preview)
        finally:
            send({"event": "jard-annotation-delivery-changed", "detail": {"capture": identifier}})

    async def rewrite_dictation(self, text, instruction, vocabulary, deadline, *, application="", browser_host=""):
        if not instruction:
            raise ValueError("Set rewrite instructions in Dictation Settings.")
        try:
            result = await self.api("POST", "native/dictation-rewrite",
                                    json={"text": text, "instruction": instruction, "vocabulary": vocabulary,
                                          "deadline": deadline, "application": application, "browserHost": browser_host})
        except httpx.HTTPError as error:
            raise ValueError(explain(error)) from error
        return result["text"]

    async def quiesce_for_update(self):
        async with self.capture_lock:
            if self.annotation_delivery_busy:
                raise ValueError("Wait for the annotation delivery to finish before restarting.")
            if self.annotation or self.captures or self.dictation_stages or self.dictation_jobs or self.sdk_capture.busy:
                raise ValueError("Finish your recording or dictation before restarting Jard.")
            self.updating = True
            await self.automatic_annotations.close()
            try:
                await self.api("POST", "native/update-quiesce/" + self.client_id)
                await self.close_warm()
                await self.wait_for_engine_exit()
                return {"ready": True}
            except BaseException:
                self.updating = False
                with contextlib.suppress(ValueError, OSError):
                    await self.automatic_annotations.resume()
                raise

    @staticmethod
    async def wait_for_engine_exit():
        for _ in range(40):
            try:
                _, writer = await asyncio.open_connection(HOST, PORT)
            except ConnectionRefusedError:
                return
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.2)
        raise RuntimeError("The local engine is still shutting down. Try restarting again.")

    async def quiesce_desktop(self, *, force=False):
        async with self.capture_lock:
            if self.annotation_delivery_busy:
                raise ValueError("Wait for the annotation delivery to finish before closing.")
            if self.annotation or (not force and (self.captures or self.dictation_stages or self.dictation_jobs or self.sdk_capture.busy)):
                raise ValueError("Finish your recording or dictation before closing Jard.")
            self.updating = True
            await self.automatic_annotations.close()
            captures = list(self.captures.values())
            if force and (dictation := self.captures.get("dictation")):
                dictation.state["discard"] = True
        try:
            if force:
                await self.sdk_capture.stop_for_exit()
                if not await self.finish_recorder_jobs(captures):
                    raise RuntimeError("Jard is still saving a recording. Try again once it finishes.")
            await self.close_warm()
        except BaseException:
            self.updating = False
            with contextlib.suppress(ValueError, OSError):
                await self.automatic_annotations.resume()
            raise
        return {"ready": True}

    async def dispatch(self, command, args):
        if command == "jard.prepare":
            return await self.prepare()
        if command == "jard.setup":
            return await self.setup(args.get("source"))
        if command == "jard.diagnosticPhase":
            phase = args.get("phase")
            if self.connected and phase in ("starting", "listening", "processing", "inserted", "saved", "cancelled", "empty", "error"):
                write_event("dictation", phase)
            return None
        if command == "jard.diagnosticTiming":
            if self.connected:
                write_dictation_timing(args)
            return None
        if not self.connected:
            raise ValueError("Open the local workspace first.")
        if command == "jard.meetingClock":
            clock = args.get("hours")
            if clock not in ("12", "24"):
                raise ValueError("Choose a 12- or 24-hour meeting clock.")
            return await self.sync_meeting_clock(clock)
        if command == "jard.annotationLease":
            return await self.annotation_lease(args)
        if command == "jard.annotationAudio":
            return await self.annotation_audio(args)
        if command == "jard.importAnnotation":
            return await self.import_annotation(args)
        if command == "jard.annotationRoutingPolicy":
            return {"managed": opencode_managed()}
        if command == "jard.annotationStorage":
            return await self.annotation_storage(args)
        if command == "jard.annotationRoute":
            return await self.annotation_route(args)
        if command == "jard.annotationRouteUndo":
            return await self.undo_automatic_annotation(str(UUID(args["id"])))
        if command == "jard.annotationRouteConfirm":
            return {"accepted": self.automatic_annotations.confirm_from_panel(args.get("capture"), args.get("nonce"), args.get("state"))}
        if command == "jard.annotationPanelSend":
            return await self.send_panel_annotation(str(UUID(args["id"])), args.get("target"), args.get("service"), args.get("rememberScope"))
        if command == "jard.annotationPanelUndo":
            return await self.undo_panel_annotation(str(UUID(args["id"])), str(UUID(args["deliveryId"])))
        if command == "jard.annotationRoutePanel":
            await self.automatic_annotations.panel_action(args.get("capture"), args.get("action"), args.get("held") is True)
            return None
        if command == "jard.annotationHandoff":
            key = str(UUID(args["id"]))
            require_narration = args.get("waitForNarration") is True
            try:
                async with asyncio.timeout(12):
                    while True:
                        handoff = await asyncio.to_thread(self.annotation_handoff, key, require_narration=require_narration)
                        if handoff is not None:
                            return handoff
                        await asyncio.sleep(.3)
            except TimeoutError as error:
                raise ValueError("Narration is still processing. Try Paste again shortly.") from error
        if command == "jard.annotationReview":
            return await asyncio.to_thread(self.annotation_review, str(UUID(args["id"])))
        route_action = {"jard.annotationDestinations": "destinations", "jard.rememberAnnotationDestination": "remember",
                        "jard.forgetAnnotationDestination": "forget"}.get(command)
        if route_action:
            return await self.annotation_routes(route_action, args)
        if command == "jard.sendAnnotation":
            return await self.deliver_annotation(args)
        if command == "jard.annotationAgentDestinations":
            return await annotation_agents.destinations(args.get("agent"), args.get("query"))
        if command == "jard.sendSessionToAgent":
            return await self.send_session_to_agent(args)
        if command == "jard.sendAnnotationToAgent":
            if self.annotation_delivery_busy or self.updating:
                raise ValueError("Wait for the current annotation delivery or restart to finish.")
            self.annotation_delivery_busy = True
            try:
                return await annotation_agents.send_annotation(self.root, args.get("agent"), args.get("id"), args.get("target"))
            except MissingSession as error:
                raise ValueError("Annotation not found.") from error
            finally:
                self.annotation_delivery_busy = False
        if command == "jard.annotationDeliveries":
            return await asyncio.to_thread(recent_deliveries, self.root, args.get("id"))
        if command == "jard.undoAnnotationDelivery":
            return await self.change_annotation_delivery(undo_annotation, args.get("deliveryId"),
                                                         restore_files=args.get("restoreFiles") is True)
        if command == "jard.correctAnnotationDelivery":
            return await self.change_annotation_delivery(correct_annotation, args.get("deliveryId"))
        if command == "jard.controlReply":
            future = self.control_pending.get(args.get("id"))
            if future and not future.done():
                future.set_result(args.get("response"))
            return None
        if command == "jard.updateQuiesce":
            return await self.quiesce_for_update()
        if command == "jard.desktopQuiesce":
            return await self.quiesce_desktop(force=args.get("force") is True)
        if command == "jard.updateAbort":
            self.updating = False
            return await self.prepare()
        if command == "jard.findOutputConfig":
            policy = current_policy()
            if policy == TransformPolicy.DISABLED:
                return {"policy": policy, "message": DISABLED_MESSAGE}
            path = await asyncio.to_thread(find_config, config_directory())
            return {"name": path.name if path else "", "policy": policy}
        if command == "jard.createOutputConfig":
            if current_policy() == TransformPolicy.DISABLED:
                raise ValueError(DISABLED_MESSAGE)
            return await asyncio.to_thread(initialize_workspace)
        if command in ("jard.inspectOutputScript", "jard.outputScriptApproval"):
            path = args.get("path", "")
            if not isinstance(path, str) or len(path) > 4096:
                raise ValueError("Select an executable local script in Dictation Settings.")
            if command == "jard.inspectOutputScript":
                policy = current_policy()
                if policy == TransformPolicy.DISABLED:
                    return {"policy": policy, "message": DISABLED_MESSAGE}
                if is_embedded_config(path):
                    review = await self.transform_configs.inspect(path, fresh=args.get("fresh") is True)
                elif policy == TransformPolicy.LOOPBACK:
                    review = {"kind": "executable", "message": PROGRAM_MESSAGE}
                else:
                    review = await asyncio.to_thread(review_output_script, path)
                return {**review, "policy": policy}
            digest = args.get("digest", "")
            if not isinstance(digest, str) or (digest and (
                len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
            )):
                raise ValueError("Invalid output script approval.")
            grants = args.get("grants", {})
            if not isinstance(grants, dict) or set(grants) - set(CAPABILITIES) or any(type(value) is not bool for value in grants.values()):
                raise ValueError("Invalid transformation permissions.")
            allowed = frozenset(key for key, granted in grants.items() if granted)
            if allowed and (not digest or not is_embedded_config(path)):
                raise ValueError("Invalid transformation permissions.")
            self.approved_output_script = (path, digest, allowed)
            return None
        if command == "jard.output":
            text = args.get("text", "")
            if not isinstance(text, str) or len(text) > 8_000:
                raise ValueError("The output preview is too large.")
            profiles = parse_profiles(args.get("profiles", ""))
            if args.get("describe") is True:
                return {"profiles": [vars(profile) for profile in profiles]}
            profile = profile_for(profiles, args.get("app", ""), args.get("bundle", ""), args.get("site", ""))
            vocabulary = merge_vocabulary(profile.vocabulary if profile else (), vocabulary_terms(args.get("vocabulary", [])))
            instruction = (profile.instruction if profile else "") or args.get("instruction", "")
            steps = profile.steps if profile else ()
            builtins_only = args.get("builtinsOnly") is True
            policy = current_policy()
            skipped = tuple(dict.fromkeys(step for step in steps if builtins_only and
                            (step == "rewrite" or is_script_step(step) or
                             (policy == TransformPolicy.DISABLED and is_transform_step(step)))))
            deadline = profile.deadline if profile else MAX_REWRITE_DEADLINE
            processed = await process_output(
                text, steps, args.get("replacements", ""), preview=True,
                rewrite=lambda value: self.rewrite_dictation(value, instruction, vocabulary, deadline),
                corrections=profile.corrections if profile else "", builtins_only=builtins_only, aliases=args.get("aliases", ()),
            )
            return {"text": processed.text, "warnings": processed.warnings, "steps": steps,
                    "profile": profile.target if profile else "", "vocabulary": vocabulary,
                    **({"skipped": skipped} if builtins_only else {})}
        if command == "jard.warm":
            return await self.warm(args)
        if command == "jard.devices":
            return await self.devices()
        if command == "jard.inputDevices":
            return await self.input_devices()
        if command == "jard.microphoneLevel":
            return await self.microphone_level()
        if command == "jard.record":
            return await self.record(args)
        if command == "jard.copyMeeting":
            return await self.copy_meeting_through_now(args)
        if command == "jard.meetingInput":
            return await self.select_meeting_input(args)
        if command == "jard.meetingExports":
            return await self.meeting_exports(args)
        if command == "jard.latestMeeting":
            response = await self.http.get("/api/sessions", params={"meetings": "true"})
            response.raise_for_status()
            rows = response.json()
            latest = max(rows, key=lambda row: (row.get("meetingStartedAt") or row["created"], row["id"]), default=None)
            return {"session": latest["id"] if latest else None,
                    "recovering": response.headers.get("X-Jard-Meeting-Recovery") == "pending"}
        if command == "jard.meetingExcerpt":
            key = str(UUID(args["session"]))
            try:
                return await self.api("GET", f"native/meetings/{key}/excerpt")
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    return {"available": False, "settled": True, "turns": []}
                raise
        if command == "jard.meetingCompletion":
            key = str(UUID(args["session"]))
            try:
                return await self.api("GET", f"native/meetings/{key}/completion", timeout=5)
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 404:
                    return {"status": "unavailable"}
                raise
        if command == "jard.meetingMarkdown":
            key = str(UUID(args["session"]))
            response = await self.http.get(f"/api/sessions/{key}/export", params={"kind": "md"})
            response.raise_for_status()
            if len(response.content) > 2 * 1024 * 1024:
                raise ValueError("Open Sessions to export this longer meeting.")
            return {"text": response.text}
        if command == "jard.stopMeeting":
            expected = str(UUID(args["meeting"]))
            async with self.capture_lock:
                take = self.captures.get("recording")
                if not take or take.state.get("meeting") != expected:
                    raise ValueError("This meeting is no longer active.")
                await self.stop(take)
            await asyncio.shield(take.job)
            return None
        if command == "jard.latestMeetingTranscript":
            live = self.live_meeting(turns=True)
            if live:
                live["meetingTurns"] = list(live["meetingTurns"])
            result = await asyncio.to_thread(latest_transcript, SessionStore(self.root, read_only=True), live)
            if len(result["text"].encode("utf-8")) > 500_000:
                raise ValueError("Open Sessions to copy or export this longer meeting transcript.")
            return result
        if command == "jard.savedMeetingTranscript":
            key = str(UUID(args["id"]))
            try:
                text = await asyncio.to_thread(saved_meeting_clipboard, SessionStore(self.root, read_only=True), key)
            except MissingSession as error:
                raise ValueError("This meeting is no longer available.") from error
            result = {"text": text}
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 3 * 1024 * 1024:
                raise ValueError("Use Meeting exports to save this longer transcript.")
            return result
        if command == "jard.stop":
            if take := self.workspace_capture():
                await self.stop(take)
                await asyncio.shield(take.job)
            return None
        if command == "jard.dictationCleanup":
            return await self.api("POST", "native/dictation-cleanup", params={"action": args.get("action", "status")})
        if command == "jard.dictation":
            action = args.get("action")
            key = str(UUID(args["id"]))
            if action in ("stop", "cancel"):
                take = self.captures.get("dictation")
                if take and take.state.get("dictation") == key:
                    if action == "cancel":
                        take.state["discard"] = True
                    await self.stop(take, release_tail=action == "stop" and args.get("releaseTail") is True,
                                    release_at=args.get("releaseAt"))
                    await asyncio.shield(take.job)
                if action == "cancel" and (stage := self.dictation_stages.get(key)):
                    await self.discard_dictation(stage)
                return None
            if action == "discard":
                return await self.discard_dictation(key)
            if action == "transcribe":
                if key not in self.dictation_stages.values():
                    raise ValueError("This dictation is unavailable.")
                if key in self.dictation_jobs:
                    raise ValueError("This dictation is already being transcribed.")
                vocabulary = vocabulary_terms(args.get("vocabulary", []))
                succeeded = False
                self.dictation_jobs.add(key)
                try:
                    settings = args.get("outputProfile") or {}
                    profile = None
                    warning = None
                    if isinstance(settings, dict) and settings.get("rules"):
                        try:
                            profile = profile_for(parse_profiles(settings["rules"]), settings.get("app", ""),
                                                  settings.get("bundle", ""), settings.get("site", ""),
                                                  bundle_name=settings.get("bundleName", ""))
                        except ValueError as error:
                            warning = str(error)
                    vocabulary = merge_vocabulary(profile.vocabulary if profile else (), vocabulary)
                    params = {**self.options(args), "vocabulary": vocabulary}
                    if args.get("noiseCleanup") is True:
                        params["noise_cleanup"] = "true"
                    if args.get("boostMicrophone") is True:
                        params["boost_microphone_input"] = "true"
                    if key in self.dictation_release_frames:
                        params["release_frame"] = self.dictation_release_frames[key]
                    requested_at = monotonic()
                    result = await self.api("POST", "native/staging/" + key + "/dictate",
                                            params=params)
                    speech_request_ms = round((monotonic() - requested_at) * 1000)
                    succeeded = True
                    write_event("dictation", "inference_complete", inference_ms=result.get("inferenceMs"))
                    if not result.get("text"):
                        return result
                    result["speechRequestMs"] = speech_request_ms
                    if profile:
                        result["profile"] = profile.target
                    if profile and (profile.steps or profile.corrections):
                        instruction = profile.instruction or settings.get("instruction", "")
                        script = settings.get("script") or ""
                        script_digest = settings.get("scriptDigest") or ""
                        grants = settings.get("scriptGrants", {})
                        if not isinstance(grants, dict):
                            grants = {}
                        allowed = frozenset(key for key in CAPABILITIES if grants.get(key) is True)
                        expected_approval = (script, script_digest, allowed)
                        script_context = {"source": "dictation", **{key: settings[field] for key, field in
                            (("application", "app"), ("browserHost", "site")) if settings.get(field)}}
                        if ("pageContext" in allowed and is_embedded_config(script) and
                                self.approved_output_script == expected_approval):
                            for field, limit in (("browserUrl", 4096), ("windowTitle", 1024)):
                                value = settings.get(field)
                                if isinstance(value, str) and value and len(value.encode("utf-8", errors="replace")) <= limit:
                                    script_context[field] = value
                        processed = await process_output(
                            result["text"], profile.steps, settings.get("replacements", ""), script,
                            rewrite=lambda value: self.rewrite_dictation(
                                value, instruction, vocabulary, profile.deadline,
                                application=settings.get("app", ""), browser_host=settings.get("site", ""),
                            ),
                            corrections=profile.corrections, aliases=settings.get("aliases", ()),
                            script_digest=script_digest, script_allowed=lambda: self.approved_output_script == expected_approval,
                            script_grants={key: key in allowed for key in CAPABILITIES},
                            script_runtime=self.helpers / "jaro-transforms",
                            script_context=script_context,
                        )
                        result["processed"] = processed.text
                        if observation := processed.processing:
                            result["processing"] = {
                                "latencyMs": observation.latency_ms, "fallbacks": observation.fallbacks,
                            }
                            if observation.rewrite_attempted:
                                result["processing"]["model"] = REWRITE_MODEL_NAME
                        warning = " ".join(processed.warnings)
                    if warning:
                        result["outputWarning"] = warning
                    return result
                finally:
                    if succeeded or args.get("cleanupOnFailure") is True:
                        with contextlib.suppress(httpx.HTTPError):
                            await self.discard_dictation(key)
                    self.dictation_jobs.discard(key)
            raise ValueError("Unsupported dictation action.")
        if command == "jard.live":
            enabled = args.get("enabled") is True
            if take := self.workspace_capture():
                take.state["live"] = enabled
                take.settings["live"] = str(enabled).lower()
            return None
        if command == "jard.openAudio":
            return await self.open_audio(args)
        if command == "jard.revealSessionMedia":
            return await self.reveal_session_media(args)
        if command == "jard.export":
            return await self.export(args)
        if command == "jard.open":
            if not await asyncio.to_thread(running_engine):
                raise ValueError("Local engine is unavailable.")
            await run_helper("/usr/bin/open", URL)
            return None
        if command == "jard.meetingDrafts":
            if meetings_managed():
                return []
            drafts = await self.api("GET", "native/staging")
            return [{"id": draft["id"], "provider": draft.get("provider") or "Meeting",
                     **({"title": draft["meetingTitle"]} if draft.get("meetingTitle") else {})}
                    for draft in drafts if draft.get("meeting")][:20]
        if command in ("jard.discard", "jard.recover", "jard.recoverMeeting"):
            if command == "jard.recoverMeeting":
                require_meetings()
            key = str(UUID(args["id"]))
            async with self.capture_lock:
                if self.updating or self.captures or self.annotation:
                    raise ValueError("Finish the active recording or update first.")
                drafts = await self.api("GET", "native/staging")
                draft = next((d for d in drafts if d["id"] == key), None)
                if not draft or (command == "jard.recoverMeeting" and not draft.get("meeting")):
                    raise ValueError("Recording draft is unavailable.")
                if draft.get("meeting") and command != "jard.discard":
                    require_meetings()
                with Path(draft["path"]).with_suffix(".recording").open("rb") as claim:
                    try:
                        fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise ValueError("This recording is already being recovered.") from None
                    if command == "jard.discard":
                        return await self.api("DELETE", "native/staging/" + key)
                    result = await self.submit(
                        key, self.options(args), name="Recovered meeting" if draft.get("meeting") else "Recovered recording",
                        mime="audio/wav", recording=True, recover=True,
                    )
                    if command == "jard.recoverMeeting":
                        emit("accepted", id=result["id"], meeting=key)
                    return result
        raise ValueError("Unsupported native action.")

    async def reply(self, message):
        try:
            args = message.get("payload", {})
            if not isinstance(args, dict):
                raise TypeError("Expected native command options.")
            result = await self.dispatch(message["command"], args)
            response = {"id": message["id"], "ok": True, "result": result}
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            OSError,
            RuntimeError,
            httpx.HTTPError,
        ) as e:
            response = {
                "id": message["id"],
                "ok": False,
                "error": {"message": explain(e)},
            }
        send({"response": response})

    async def finish_recorder_jobs(self, captures):
        await asyncio.gather(*(self.stop(take) for take in captures))

        async def finish(take):
            try:
                await asyncio.wait_for(asyncio.shield(take.job), 15)
            except TimeoutError:
                if not take.finished and take.child.returncode is None:
                    take.child.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(take.job), 15)

        await asyncio.gather(*(finish(take) for take in captures))
        return all(take.job.done() for take in captures)

    async def close(self):
        await self.automatic_annotations.close()
        self.warm_requested = None
        await self.close_control()
        if dictation := self.captures.get("dictation"):
            dictation.state["discard"] = True
        await self.finish_recorder_jobs(list(self.captures.values()))
        await self.reconcile_warm()
        for key in list(self.dictation_stages.values()):
            with contextlib.suppress(httpx.HTTPError):
                await self.discard_dictation(key)
        if self.lease_task:
            self.lease_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.lease_task
        if self.connected:
            with contextlib.suppress(httpx.HTTPError):
                await self.api("DELETE", "clients/" + self.client_id)
            write_event("desktop", "disconnected")


async def main():
    helpers = Path(os.environ["JARD_UI_EXECUTABLE"]).resolve().parent
    async with httpx.AsyncClient(
        base_url=URL, trust_env=False, timeout=httpx.Timeout(120, connect=3)
    ) as http:
        bridge = MacBridge(helpers, http)
        loop = asyncio.get_running_loop()
        # Match the native host's line bound for older paginated history clients.
        reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        loop.add_signal_handler(signal.SIGTERM, transport.close)
        tasks = set()
        try:
            while raw := await reader.readline():
                try:
                    message = json.loads(raw)
                    if not isinstance(message.get("id"), str) or not isinstance(
                        message.get("command"), str
                    ):
                        continue
                except (ValueError, AttributeError):
                    continue
                task = asyncio.create_task(bridge.reply(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bridge.close()
            transport.close()


if __name__ == "__main__":
    asyncio.run(main())
