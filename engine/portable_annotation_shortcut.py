"""Tap to capture or save, and hold to draw on a portable desktop."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import subprocess
from time import monotonic

from fastapi import HTTPException

from managed_preferences import ManagedFeatureError
from portable_capture import CaptureError

logger = logging.getLogger(__name__)
HOLD_SECONDS = .3


class AnnotationShortcut:
    def __init__(self, state, start, finish):
        self.state = state
        self.start = start
        self.finish = finish
        self.pressed = None
        self.hold = None
        self.pen = None
        self.action = None
        self.feedback = ""
        self.saved = None

    def edge(self, down):
        if not down:
            self.release()
            return
        if self.state.draining or self.pressed or (self.action and not self.action.done()):
            return False
        take = self.state.portable_screencast
        if take and (take.saving or take.phase not in ("recording", "recorded", "cancelled")):
            return False
        self.feedback = ""
        self.pressed = (take, monotonic())
        if take and take.phase == "recording":
            self.hold = asyncio.create_task(self.draw_after_hold(take))
        return True

    def tap(self):
        if not self.edge(True):
            return False
        self.release()
        return True

    def save_now(self, take):
        if (self.state.draining or take is not self.state.portable_screencast or take.phase != "recording"
                or take.saving or (self.action and not self.action.done())):
            return False
        self.pressed = (take, monotonic())
        self.release(save=True)
        return True

    def release(self, *, cancel=False, save=False):
        if self.pressed is None:
            return
        take, pressed_at = self.pressed
        self.pressed = None
        hold, self.hold = self.hold, None
        pen, self.pen = self.pen, None
        tap = not cancel and (save or hold is None or monotonic() - pressed_at < HOLD_SECONDS)
        if hold:
            hold.cancel()
        self.action = asyncio.create_task(self.complete(take, tap, hold, pen))

    async def draw_after_hold(self, take):
        await asyncio.sleep(HOLD_SECONDS)
        if self.state.draining or take is not self.state.portable_screencast or take.phase != "recording" or take.saving:
            return
        pen = take.pen
        if pen.active or take.stopped or take.started is None:
            return
        self.pen = pen
        try:
            await pen.open(take.started, take.region)
        except CaptureError as error:
            self.feedback = str(error)

    async def complete(self, take, tap, hold, pen):
        try:
            if hold:
                await asyncio.gather(hold, return_exceptions=True)
            if pen:
                await pen.close()
            if not tap or self.state.draining:
                return
            current = self.state.portable_screencast
            cancelled = take is not None and take.phase == "cancelled"
            if take is not current and not (cancelled and current is None):
                return
            if take is None or cancelled:
                await self.start(take)
            else:
                await self.finish(take)
                self.saved = take.draft.key
        except (CaptureError, ManagedFeatureError) as error:
            self.feedback = str(error)
        except HTTPException as error:
            self.feedback = str(error.detail)
        except OSError:
            logger.exception("The annotation shortcut could not complete.")
            self.feedback = "Could not complete the screen recording."

    async def close(self):
        self.release(cancel=True)
        if self.action:
            await asyncio.gather(self.action, return_exceptions=True)


def notice_helper():
    helper = Path(os.environ.get("JARO_CALL_SURFACE", ""))
    return helper if helper.is_absolute() and helper.is_file() else None


class ScreenRecordingNotice:
    def __init__(self, shortcut, *, monitor=None):
        self.shortcut = shortcut
        self.monitor = monitor
        self.process = None
        self.reader = None
        self.lost = False

    async def show(self, take):
        helper = notice_helper()
        if not helper:
            raise CaptureError("Open the workspace to record the screen. The recording control is unavailable.")
        self.process = await asyncio.create_subprocess_exec(
            helper, "screen", "Screen recording", "0", *((self.monitor,) if self.monitor else ()),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            async with asyncio.timeout(3):
                if (await self.process.stdout.readline()).strip() != b"ready":
                    raise CaptureError("Could not show the screen recording control.")
        except TimeoutError as error:
            raise CaptureError("Could not show the screen recording control.") from error
        self.reader = asyncio.create_task(self.watch(take))

    async def watch(self, take):
        command = await self.process.stdout.readline()
        self.lost = not (command.strip() == b"stop" and self.shortcut.save_now(take))
        if self.lost:
            take.request_stop()

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.process is not None:
            self.process.stdin.close()
            try:
                async with asyncio.timeout(2):
                    await self.process.wait()
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
