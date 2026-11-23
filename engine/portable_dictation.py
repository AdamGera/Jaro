"""Local dictation capture and history shared by the portable desktops."""
import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, time
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from desktop_clipboard import copy_text
from dictation_output import (dictation_vocabulary_aliases, merge_vocabulary, parse_portable_profiles, process,
                              profile_for, rewrite_locally)
from portable_audio_control import AudioPreferences, DictationAudio
from recording_power import keep_awake_until
from portable_capture import SAMPLE_RATE, CaptureError
from portable_dictation_history import DEFAULT
from portable_dictation_sounds import DONE_STYLES, DictationSounds, SoundPreferences
from rumble_model import LANGUAGES

logger = logging.getLogger(__name__)
HISTORY_ERRORS = (OSError, ValueError, sqlite3.Error)
INDICATOR_REFRESH = .08
DONE_DELAY = 2
PASTE_SENT = "Paste shortcut sent."
MAX_PENDING = 8
QUEUE_FULL = "Finish the queued dictations before starting another."
SENTENCE_WORDS = frozenset("a an and as because but for if nor or so the then though to when while yet".split())


def join_dictation(previous, text):
    sentence_start = previous.rstrip().rstrip("'\")]}").endswith((".", "?", "!"))
    initial = next((index for index, letter in enumerate(text) if letter.isalpha()), None)
    if initial is not None:
        letter = text[initial]
        word = re.match(r"(?:[^\W\d_]|')+", text[initial:]).group()
        if sentence_start:
            text = text[:initial] + letter.upper() + text[initial + 1:]
        elif word.lower() in SENTENCE_WORDS and (len(word) == 1 or not word.isupper()):
            text = text[:initial] + letter.lower() + text[initial + 1:]
    if not previous[-1].isspace() and previous[-1] not in "([{" and not text[0].isspace() and text[0] not in ",.?!;:)]}":
        return " " + text
    return text


@dataclass
class DictationJob:
    folder: TemporaryDirectory
    path: Path
    frames: int
    languages: tuple
    voice: dict
    output: tuple
    application: str
    delivery: Callable[[str], Awaitable[str]]
    sound: SoundPreferences
    interrupted: bool
    microphone_fallback: bool
    started: float
    stopped: float
    captured_at: float
    done: asyncio.Event


def indicator_helper():
    location = Path(os.environ.get("JARO_DICTATION_INDICATOR", ""))
    return location if location.is_absolute() and location.is_file() else None


def output_vocabulary(entries, profile):
    return tuple(merge_vocabulary(profile.vocabulary if profile else (), (entry["term"] for entry in entries)))


async def transform_output(state, text, entries, replacements, profile, *, application="", preview=False):
    rewrite = None
    if profile and "rewrite" in profile.steps and not preview:
        async def rewrite(value):
            return await rewrite_locally(state.insights, value, profile.instruction, output_vocabulary(entries, profile),
                                         profile.deadline, application=application)

    return await process(text, profile.steps if profile else (), rewrite=rewrite, builtins_only=preview,
                         global_corrections=replacements, corrections=profile.corrections if profile else "",
                         aliases=dictation_vocabulary_aliases(entries))


class PortableDictation:
    platform = ""
    shortcut = ""
    authorizing = False
    configurable = False

    def __init__(self, state, transcribe, history):
        self.state = state
        self.transcribe = transcribe
        self.history = history
        self.application = ""
        self.control = asyncio.Lock()
        self.action = None
        self.capture = None
        self.folder = None
        self.jobs = deque()
        self.output_task = None
        self.committing = False
        self.capture_idle = asyncio.Event()
        self.phase = "idle"
        self.canceling = False
        self.feedback = ""
        self.text = ""
        self.last_id = None
        self.last_clear_epoch = None
        self.microphone = ""
        self.microphone_fallback = False
        self.language = ""
        self.capture_languages = ()
        self.noise_cleanup = False
        self.boost_microphone = False
        self.capture_voice_input = {}
        self.capture_output = ([], "", None)
        self.vocabulary_entries = []
        self.replacements = ""
        self.output_profiles = ""
        self.profiles = []
        self.indicator_position = "top"
        self.indicator_helper = indicator_helper()
        self.indicator_issue = False
        self.indicator_task = None
        self.indicator_stop = asyncio.Event()
        self.sound = SoundPreferences()
        self.sounds = DictationSounds()
        self.capture_sound = self.sound
        self.audio_preferences = AudioPreferences()
        self.audio_control = DictationAudio()
        self.wake_guard = None
        self.shortcut_requested = False
        try:
            self.shortcut_requested = history.shortcut_enabled(portal=self.platform == "wayland") is True
        except HISTORY_ERRORS:
            logger.warning("Could not read the dictation shortcut preference.", exc_info=True)
        try:
            self.terminal = bool(history.status()[0]["terminal"])
        except HISTORY_ERRORS:
            self.terminal = False
        try:
            self.microphone = history.microphone()
        except HISTORY_ERRORS:
            logger.warning("Could not read the dictation microphone preference.", exc_info=True)
        try:
            language = history.language()
            if language in LANGUAGES:
                self.language = language
        except HISTORY_ERRORS:
            logger.warning("Could not read the dictation language preference.", exc_info=True)
        try:
            voice = history.voice_input()
            self.noise_cleanup = voice["noiseCleanup"]
            self.boost_microphone = voice["boostMicrophone"]
        except HISTORY_ERRORS:
            logger.warning("Could not read the dictation voice input preferences.", exc_info=True)
        try:
            self.vocabulary_entries, self.replacements, self.output_profiles = history.output_preferences()
            self.profiles = parse_portable_profiles(self.output_profiles)
        except HISTORY_ERRORS:
            logger.warning("Could not read the dictation output preferences.", exc_info=True)
        try:
            position = history.indicator_position()
            if position in ("top", "bottom", "off"):
                self.indicator_position = position
        except HISTORY_ERRORS:
            logger.warning("Could not read the dictation indicator position.", exc_info=True)
        try:
            settings = history.sound_preferences()
            if isinstance(settings, SoundPreferences):
                self.sound = settings
        except HISTORY_ERRORS:
            logger.warning("Could not read dictation sound preferences.", exc_info=True)
        try:
            settings = history.audio_preferences()
            if isinstance(settings, AudioPreferences):
                self.audio_preferences = settings
        except HISTORY_ERRORS:
            logger.warning("Could not read playback settings.", exc_info=True)

    @property
    def busy(self):
        return self.canceling or self.phase != "idle"

    @property
    def phase(self):
        if self._capture_phase != "idle" or not self.jobs:
            return self._capture_phase
        return "delivering" if self.committing else "transcribing"

    @phase.setter
    def phase(self, value):
        self._capture_phase = value
        if value == "idle":
            self.capture_idle.set()
        else:
            self.capture_idle.clear()

    @property
    def queue_full(self):
        return len(self.jobs) >= MAX_PENDING

    @property
    def can_start_recording(self):
        return (self._capture_phase == "idle" and not self.queue_full and not self.committing and not self.canceling
                and (self.action is None or self.action.done()))

    @property
    def can_cancel(self):
        action = self.action
        return not self.canceling and (self._capture_phase in ("starting", "recording", "transcribing")
                                       or len(self.jobs) > int(self.committing)
                                       or (not self.jobs and self.phase == "idle" and action is not None and not action.done()))

    def snapshot(self):
        text, key, epoch = self.text, self.last_id, self.last_clear_epoch
        try:
            settings, saved = self.history.status(key)
            if not saved or epoch is not None and epoch != settings["clearEpoch"]:
                text = ""
            history_available = True
        except HISTORY_ERRORS:
            settings, history_available = DEFAULT, False
        cleanup = getattr(self.state, "stupase_cleanup", None)
        return {"enabled": self.enabled, "platform": self.platform, "shortcut": self.shortcut,
                "shortcutRequested": self.shortcut_requested,
                "authorizing": self.authorizing, "configurable": self.configurable,
                "phase": self.phase, "message": self.feedback, "canCancel": self.can_cancel,
                "pendingJobs": len(self.jobs), "canStart": self.can_start_recording, "queueFull": self.queue_full,
                "language": self.language,
                "vocabularyEntries": self.vocabulary_entries, "replacements": self.replacements,
                "outputProfiles": self.output_profiles,
                "indicatorPosition": self.indicator_position,
                "indicatorAvailable": self.indicator_helper is not None and not self.indicator_issue,
                "sound": self.sound.enabled, "soundStyle": self.sound.style, "soundVolume": self.sound.volume,
                "soundAvailable": self.sounds.available,
                "otherAudio": self.audio_preferences.behavior, "otherAudioPercent": self.audio_preferences.percent,
                "otherAudioModes": self.audio_control.modes,
                "noiseCleanup": self.noise_cleanup, "boostMicrophone": self.boost_microphone,
                "cleanupStatus": cleanup.status() if self.noise_cleanup and cleanup is not None else "notInstalled",
                "text": text, "terminal": self.terminal, "retention": settings["retention"],
                "historyRevision": settings["revision"], "historyAvailable": history_available}

    async def remember_shortcut(self, enabled):
        if self.shortcut_requested is enabled:
            return
        try:
            await run_in_threadpool(self.history.save_shortcut_enabled, enabled, portal=self.platform == "wayland")
        except HISTORY_ERRORS as failure:
            raise CaptureError("Could not save the dictation shortcut preference.") from failure
        self.shortcut_requested = enabled

    def watch_focus(self):
        pass

    def clear_focus(self):
        pass

    async def retain(self, result, job):
        text = result["text"]
        key = str(uuid4())
        entry = {"id": key, "time": job.captured_at, "text": text, "app": job.application,
                 "seconds": job.frames / SAMPLE_RATE, "inferenceMs": result["inferenceMs"],
                 "totalMs": round((monotonic() - job.started) * 1000)}
        self.text, self.last_id, self.last_clear_epoch = text, None, None
        try:
            saved, epoch = await run_in_threadpool(self.history.record, entry, job.path)
        except HISTORY_ERRORS:
            logger.warning("Could not retain the dictation.", exc_info=True)
            return "Could not save dictation history."
        self.last_id, self.last_clear_epoch = key if saved else None, epoch
        return ""

    def recording_ended(self, worker):
        if self.capture is not None and self.capture.worker is worker and self.phase == "recording":
            self.schedule()

    def recover_interruption(self):
        return True

    async def copy_interrupted_text(self, text):
        try:
            await copy_text(text)
        except (OSError, ValueError):
            return "Microphone interrupted. Use Copy last or Paste Last to recover your dictation."
        return "Microphone interrupted. The dictation was copied."

    def schedule(self, *, released_at=None):
        if not self.canceling and (self.action is None or self.action.done()):
            if self.capture is None:
                if not self.can_start_recording:
                    if self.queue_full:
                        self.feedback = QUEUE_FULL
                    return False
                self.watch_focus()
                self.phase = "starting"
            self.action = asyncio.create_task(self.toggle(released_at=released_at))
            return True
        return False

    def prepare_delivery(self):
        return self.deliver

    async def start_microphone(self, filename, microphone, *, meter=False):
        self.capture = self.state.portable_recorder(
            uuid4(), Path(self.folder.name) / filename, "mic", seconds=120, microphone=microphone)
        self.capture.measure_level = meter
        self.capture.dictation = True
        await self.capture.start()

    async def run_indicator(self, position):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = await asyncio.create_subprocess_exec(
                self.indicator_helper, position, creationflags=flags, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            self.indicator_issue = True
            return
        self.indicator_issue = False
        sent = None
        try:
            while not self.indicator_stop.is_set() and process.returncode is None:
                capture = self.capture
                active = capture is not None and capture.worker is not None and not capture.worker.done()
                levels = capture.levels if active else None
                level = round(levels[0] * 100) if levels else 0
                frame = f"R{level}\n".encode("ascii") if active else b"P\n"
                if frame != sent:
                    process.stdin.write(frame)
                    await asyncio.wait_for(process.stdin.drain(), .25)
                    sent = frame
                try:
                    await asyncio.wait_for(self.indicator_stop.wait(), INDICATOR_REFRESH)
                except TimeoutError:
                    continue
        except (OSError, TimeoutError):
            self.indicator_issue = True
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), .25)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            if process.returncode and not self.indicator_stop.is_set():
                self.indicator_issue = True

    def start_sound(self, *, delay=0, while_valid=None):
        capture = self.capture

        def active():
            return (self.capture is capture and self.phase == "recording"
                    and (while_valid is None or while_valid()))

        ready = self.audio_control.begin(self.audio_preferences, delay=delay, while_valid=active)
        if capture is not None and (self.wake_guard is None or self.wake_guard.done()):
            self.wake_guard = keep_awake_until(capture.worker, "Jaro is taking dictation", delay=delay, while_valid=active)
        self.sounds.play("start", self.capture_sound, delay=delay, while_valid=active, ready=ready)

    async def toggle(self, *, released_at=None):
        state = self.state
        final_cue = None
        take = self.capture
        try:
            if self.capture is not None:
                job = await self.finish_take(released_at)
                if job is not None and asyncio.current_task() is not self.action:
                    await job.done.wait()
                return
            async with state.portable_recording_lock:
                if state.status != "ready" or state.draining or state.portable_recording or getattr(state, "portable_screencast", None):
                    self.feedback = "Dictation is unavailable while Jaro starts or records audio."
                    return
                self.phase = "starting"
                self.capture_sound = self.sound
                # An audition or previous completion sound cannot run into the new take.
                await self.sounds.stop()
                await self.audio_control.settle()
                self.folder = TemporaryDirectory(prefix=".dictation-", dir=state.library.root)
                microphone = self.microphone
                position = self.indicator_position
                meter = self.indicator_helper is not None and position != "off"
                self.capture_languages = (self.language,) if self.language else ()
                self.capture_voice_input = {"noise_cleanup": self.noise_cleanup, "boost": self.boost_microphone}
                self.capture_output = self.vocabulary_entries, self.replacements, profile_for(self.profiles, self.application)
                self.microphone_fallback = False
                try:
                    await self.start_microphone("speech.wav", microphone, meter=meter)
                except CaptureError:
                    if not microphone:
                        raise
                    await self.start_microphone("fallback.wav", "", meter=meter)
                    self.microphone_fallback = True
                self.feedback = ""
                self.phase = "recording"
                self.capture.worker.add_done_callback(self.recording_ended)
                self.start_sound()
                if meter:
                    if self.indicator_stop.is_set():
                        await self.stop_indicator()
                    if self.indicator_task is None or self.indicator_task.done():
                        self.indicator_stop.clear()
                        self.indicator_task = asyncio.create_task(self.run_indicator(position))
        except CaptureError as failure:
            self.feedback = str(failure)
            final_cue = "error"
        except Exception:
            logger.exception("Portable dictation failed.")
            self.feedback = "Could not finish the dictation."
            final_cue = "error"
        finally:
            if (self.capture is take or take is None) and (self._capture_phase != "recording" or final_cue):
                await self.clear_capture()
                self.phase = "idle"
                if final_cue:
                    self.sounds.play(final_cue, self.capture_sound, after=self.audio_control.task)

    async def finish_take(self, released_at):
        self.phase = "transcribing"
        started, captured_at = monotonic(), time()
        capture = self.capture
        if released_at is None:
            await capture.stop()
        else:
            await capture.finish_dictation(released_at)
        restored = self.audio_control.release()
        if capture.interrupted:
            if not self.recover_interruption():
                return None
        else:
            self.sounds.play("stop", self.capture_sound, after=restored)
        job = DictationJob(
            self.folder, capture.path, capture.frames, self.capture_languages, self.capture_voice_input,
            self.capture_output, self.application,
            self.copy_interrupted_text if capture.interrupted else self.prepare_delivery(), self.capture_sound,
            capture.interrupted, self.microphone_fallback, started, monotonic(), captured_at, asyncio.Event(),
        )
        self.folder = None
        try:
            await self.clear_capture(keep_focus=True)
        except BaseException:
            self.drop_job(job)
            raise
        self.jobs.append(job)
        self.phase = "idle"
        if self.output_task is None or self.output_task.done():
            self.output_task = asyncio.create_task(self.process_queue())
        return job

    async def process_queue(self):
        while self.jobs:
            job = self.jobs[0]
            try:
                await self.process_job(job)
            except Exception:
                logger.exception("Portable dictation failed.")
                self.feedback = "Could not finish the dictation."
            finally:
                self.committing = False
                self.jobs.popleft()
                job.folder.cleanup()
                try:
                    if not self.jobs and self._capture_phase == "idle" and self.capture is None:
                        self.clear_focus()
                        await self.stop_indicator()
                finally:
                    job.done.set()

    async def process_job(self, job):
        vocabulary, replacements, profile = job.output
        terms = output_vocabulary(vocabulary, profile)
        result = await self.transcribe(self.state, job.path, job.languages, vocabulary=terms or None, **job.voice)
        text = result["text"]
        warnings = ()
        if text and (vocabulary or replacements or profile):
            output = await transform_output(self.state, text, vocabulary, replacements, profile,
                                            application=job.application)
            text, warnings = output.text, output.warnings
            if not text:
                self.feedback = "Nothing left to paste after applying the output settings."
                return
            result = {**result, "text": text}
        # Do not send paste shortcuts or play completion cues into a newer capture.
        while self._capture_phase != "idle":
            await self.capture_idle.wait()
        final_cue = None
        if text:
            self.committing = True
            history_error = await self.retain(result, job)
            self.feedback = await job.delivery(text)
            if self.feedback == PASTE_SENT and job.sound.style in DONE_STYLES and monotonic() - job.stopped >= DONE_DELAY:
                final_cue = "done"
            self.feedback = " ".join(filter(None, (self.feedback, history_error, *warnings)))
        else:
            self.feedback = ("Microphone interrupted before Jaro could recover any speech."
                             if job.interrupted else "No speech detected.")
            final_cue = "error"
        if job.microphone_fallback:
            self.feedback = f"{self.feedback} Used the system microphone because the selected one was unavailable.".strip()
        if final_cue:
            self.sounds.play(final_cue, job.sound, after=self.audio_control.task)

    async def stop_indicator(self):
        if self.indicator_task is not None:
            task = self.indicator_task
            self.indicator_stop.set()
            await asyncio.shield(task)
            if self.indicator_task is task:
                self.indicator_task = None

    async def clear_capture(self, *, keep_focus=False):
        if not keep_focus and not self.jobs:
            self.clear_focus()
            await self.stop_indicator()
        self.application = ""
        if self.capture is not None:
            capture, self.capture = self.capture, None
            with suppress(OSError):
                await capture.stop()
        self.audio_control.release()
        self.wake_guard = None
        if self.folder is not None:
            folder, self.folder = self.folder, None
            folder.cleanup()

    async def cancel_capture(self, *, audible=False, include_pending=False):
        play_cancel = audible and self._capture_phase in ("recording", "transcribing")
        self.canceling = True
        try:
            if self.action is not None:
                if self.phase != "delivering":
                    self.action.cancel()
                await asyncio.gather(self.action, return_exceptions=True)
                self.action = None
            await self.sounds.stop()
            await self.clear_capture()
            if include_pending:
                await self.cancel_pending()
            self.phase = "idle"
            if play_cancel:
                self.sounds.play("cancel", self.capture_sound, after=self.audio_control.task)
            else:
                await self.audio_control.settle()
        finally:
            self.canceling = False

    async def cancel_pending(self):
        # Keep a paste and its clipboard restoration atomic, even during shutdown.
        while len(self.jobs) > 1:
            self.drop_job(self.jobs.pop())
        if self.output_task is not None:
            if not self.committing:
                self.output_task.cancel()
            await asyncio.gather(self.output_task, return_exceptions=True)
            self.output_task = None
        if self.jobs:
            self.drop_job(self.jobs.pop())
        if self.capture is None:
            self.clear_focus()
            await self.stop_indicator()

    @staticmethod
    def drop_job(job):
        job.folder.cleanup()
        job.done.set()

    async def cancel_dictation(self):
        if not self.can_cancel:
            return False
        if self._capture_phase != "idle" or not self.jobs:
            await self.cancel_capture(audible=True)
        elif len(self.jobs) > 1:
            self.drop_job(self.jobs.pop())
        else:
            self.canceling = True
            try:
                await self.cancel_pending()
            finally:
                self.canceling = False
        self.feedback = "Dictation canceled."
        return True
