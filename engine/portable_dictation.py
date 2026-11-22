"""Local dictation capture and history shared by the portable desktops."""
import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic, time
from uuid import uuid4

from starlette.concurrency import run_in_threadpool

from portable_capture import SAMPLE_RATE, CaptureError
from portable_dictation_history import DEFAULT

logger = logging.getLogger(__name__)
HISTORY_ERRORS = (OSError, ValueError, sqlite3.Error)


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
        self.phase = "idle"
        self.feedback = ""
        self.text = ""
        self.last_id = None
        self.last_clear_epoch = None
        try:
            self.terminal = bool(history.status()[0]["terminal"])
        except HISTORY_ERRORS:
            self.terminal = False

    @property
    def busy(self):
        return self.phase != "idle"

    def snapshot(self):
        text, key, epoch = self.text, self.last_id, self.last_clear_epoch
        try:
            settings, saved = self.history.status(key)
            if not saved or epoch is not None and epoch != settings["clearEpoch"]:
                text = ""
            history_available = True
        except HISTORY_ERRORS:
            settings, history_available = DEFAULT, False
        return {"enabled": self.enabled, "platform": self.platform, "shortcut": self.shortcut,
                "authorizing": self.authorizing, "configurable": self.configurable,
                "phase": self.phase, "message": self.feedback,
                "text": text, "terminal": self.terminal, "retention": settings["retention"],
                "historyRevision": settings["revision"], "historyAvailable": history_available}

    def watch_focus(self):
        pass

    def clear_focus(self):
        pass

    async def retain(self, result, started):
        text = result["text"]
        key = str(uuid4())
        entry = {"id": key, "time": time(), "text": text, "app": self.application,
                 "seconds": self.capture.frames / SAMPLE_RATE, "inferenceMs": result["inferenceMs"],
                 "totalMs": round((monotonic() - started) * 1000)}
        self.text, self.last_id, self.last_clear_epoch = text, None, None
        try:
            saved, epoch = await run_in_threadpool(self.history.record, entry)
        except HISTORY_ERRORS:
            logger.warning("Could not retain the dictation.", exc_info=True)
            return "Could not save dictation history."
        self.last_id, self.last_clear_epoch = key if saved else None, epoch
        return ""

    def recording_ended(self, worker):
        if self.capture is not None and self.capture.worker is worker and self.phase == "recording":
            self.schedule()

    def schedule(self):
        if self.action is None or self.action.done():
            if self.capture is None:
                self.watch_focus()
            self.action = asyncio.create_task(self.toggle())

    async def toggle(self):
        state = self.state
        try:
            if self.capture is not None:
                self.phase = "transcribing"
                started = monotonic()
                await self.capture.stop()
                result = await self.transcribe(state, self.capture.path)
                text = result["text"]
                if text:
                    history_error = await self.retain(result, started)
                    self.feedback = await self.deliver(text)
                    if history_error:
                        self.feedback = f"{self.feedback} {history_error}".strip()
                else:
                    self.feedback = "No speech detected."
                return
            async with state.portable_recording_lock:
                if state.status != "ready" or state.draining or state.portable_recording:
                    self.feedback = "Dictation is unavailable while Jaro starts or records audio."
                    return
                self.phase = "starting"
                self.folder = TemporaryDirectory(prefix=".dictation-", dir=state.library.root)
                self.capture = state.portable_recorder(uuid4(), Path(self.folder.name) / "speech.wav", "mic", seconds=120)
                await self.capture.start()
                self.feedback = ""
                self.phase = "recording"
                self.capture.worker.add_done_callback(self.recording_ended)
        except CaptureError as failure:
            self.feedback = str(failure)
            self.phase = "idle"
        except Exception:
            logger.exception("Portable dictation failed.")
            self.feedback = "Could not finish the dictation."
            self.phase = "idle"
        finally:
            if self.phase != "recording":
                await self.clear_capture()
                self.phase = "idle"

    async def clear_capture(self):
        self.clear_focus()
        self.application = ""
        if self.capture is not None:
            capture, self.capture = self.capture, None
            with suppress(OSError):
                await capture.stop()
        if self.folder is not None:
            folder, self.folder = self.folder, None
            folder.cleanup()

    async def cancel_capture(self):
        if self.action is not None:
            self.action.cancel()
            await asyncio.gather(self.action, return_exceptions=True)
            self.action = None
        await self.clear_capture()
        self.phase = "idle"
