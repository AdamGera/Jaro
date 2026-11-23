"""Own the audio-control worker for one system dictation."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal


AudioBehavior = Literal["none", "mute", "lower", "pause"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioPreferences:
    behavior: AudioBehavior = "none"
    percent: int = 20


class DictationAudio:
    def __init__(self):
        self.helper = None
        self.modes = ["none"]
        if sys.platform == "linux":
            self.helper = (sys.executable, "-B", str(Path(__file__).with_name("linux_dictation_audio.py")))
            if shutil.which("pactl"):
                self.modes.extend(("mute", "lower"))
            self.modes.append("pause")
        elif os.name == "nt":
            path = Path(os.environ.get("JARO_DICTATION_AUDIO", ""))
            if path.is_absolute() and path.is_file():
                self.helper = (path,)
                self.modes.extend(("mute", "lower", "pause"))
        self.task = None
        self.process = None
        self.ready = asyncio.Event()
        self.ended = asyncio.Event()

    def begin(self, settings, *, delay=0, while_valid=None):
        if settings.behavior == "none" or settings.behavior not in self.modes:
            return None
        if self.task is None or self.task.done():
            self.ended.clear()
            self.ready.clear()
            self.task = asyncio.create_task(self._own(settings, delay, while_valid))
        return None if self.ended.is_set() else self.ready

    def release(self):
        self.ended.set()
        if self.process is not None:
            self.process.stdin.close()
        return self.task

    async def settle(self):
        task = self.release()
        if task is not None:
            # The worker still owns restoration if a sound server is slow to reply.
            with suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), .8)

    async def _own(self, settings, delay, while_valid):
        try:
            if delay:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.ended.wait(), delay)
            if self.ended.is_set() or (while_valid is not None and not while_valid()):
                return
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB) if os.name == "nt" else 0
            extra = (str(os.getpid()),) if os.name == "nt" else ()
            self.process = await asyncio.create_subprocess_exec(
                *self.helper, settings.behavior, str(settings.percent), *extra, creationflags=flags,
                start_new_session=os.name != "nt",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            if self.ended.is_set():
                self.process.stdin.close()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.stdout.readline(), .55)
            self.ready.set()
            await self.ended.wait()
        except (OSError, ValueError):
            logger.debug("Could not control playback during dictation.", exc_info=True)
        finally:
            self.ready.set()
            if self.process is not None:
                self.process.stdin.close()
                await self.process.wait()
                self.process = None
