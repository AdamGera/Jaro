"""System dictation through a lifecycle-owned Windows keyboard and paste helper."""
import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
from secrets import token_hex
import subprocess
from time import monotonic

from starlette.concurrency import run_in_threadpool

from portable_capture import CaptureError
from portable_dictation import HISTORY_ERRORS, QUEUE_FULL, PortableDictation, join_dictation


COPY_LAST = "Dictation is ready. Use Copy last to paste it into your app."
PASTE_CONTINUATION_CHANGED = 6
MODES = ("hold", "tap", "holdDouble", "doubleTap")
logger = logging.getLogger(__name__)


def helper_path():
    override = os.environ.get("JARO_WINDOWS_DICTATION")
    if override:
        path = Path(override)
        if path.is_absolute() and path.is_file():
            return path
    return None


async def stop_process(process):
    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


class WindowsDictation(PortableDictation):
    platform = "windows"
    shortcut = "Left Alt"
    recovery_shortcut = "Ctrl+Left Alt+V"

    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        self.listener = None
        self.events = None
        self.focus = None
        self.focus_revision = 0
        self.activity_channel = None
        self.input_revision = 0
        self.take_input_revision = 0
        self.last_insertion = None
        self.recovery_focus = None
        self.gesture_accepted = False
        self.mode = "hold"
        try:
            stored = history.windows_shortcut_mode()
            if stored in MODES:
                self.mode = stored
        except HISTORY_ERRORS:
            logger.warning("Could not read the Windows dictation shortcut mode.", exc_info=True)

    @property
    def enabled(self):
        return self.listener is not None

    async def spawn(self, *arguments):
        helper = helper_path()
        if helper is None:
            raise CaptureError("Jaro’s Windows shortcut helper is missing. Rebuild the desktop host.")
        return await asyncio.create_subprocess_exec(
            helper, *arguments, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=2048,
        )

    async def enable(self):
        if self.listener is not None:
            return
        self.authorizing = True
        helper = None
        channel = token_hex(16)
        try:
            helper = await self.spawn("listen", self.mode, channel)
            if (await asyncio.wait_for(helper.stdout.readline(), 4)).strip() != b"ready":
                raise CaptureError("Windows did not start Jaro’s keyboard shortcut.")
            self.listener = helper
            self.activity_channel = channel
            self.input_revision = 0
            self.events = asyncio.create_task(self.read_events(helper))
            self.feedback = ""
        except (OSError, TimeoutError) as failure:
            raise CaptureError("Windows did not start Jaro’s keyboard shortcut.") from failure
        finally:
            self.authorizing = False
            if helper is not None and self.listener is None:
                await stop_process(helper)

    async def read_events(self, helper):
        try:
            while event := await helper.stdout.readline():
                await self.handle_event(event.decode("utf-8", errors="replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            if self.listener is helper:
                async with self.control:
                    if self.listener is helper:
                        self.listener = None
                        self.activity_channel = None
                        self.last_insertion = None
                        self.gesture_accepted = False
                        await stop_process(helper)
                        await self.cancel_capture(include_pending=True)
                        self.feedback = "The Windows shortcut disconnected. Enable it again to reconnect."

    def snapshot(self):
        return {**super().snapshot(), "recoveryShortcut": self.recovery_shortcut, "shortcutMode": self.mode,
                "cancelShortcut": "Esc" if self.enabled and self.mode != "hold" else ""}

    async def change_mode(self, mode):
        if mode == self.mode:
            return
        if self.busy or self.gesture_accepted or (self.action is not None and not self.action.done()):
            raise CaptureError("Finish the current dictation before changing the shortcut mode.")
        previous = self.mode
        listening = self.enabled
        if listening:
            await self.disable()
        self.mode = mode
        try:
            if listening:
                await self.enable()
            await run_in_threadpool(self.history.configure, windows_shortcut_mode=mode)
        except (CaptureError, *HISTORY_ERRORS) as failure:
            if self.enabled:
                await self.disable()
            self.mode = previous
            if listening:
                try:
                    await self.enable()
                except CaptureError:
                    logger.warning("Could not restore the Windows dictation shortcut.", exc_info=True)
            raise CaptureError("Could not change the Windows dictation shortcut mode.") from failure

    def reset_gesture(self):
        if self.listener is not None:
            with suppress(OSError, RuntimeError):
                self.listener.stdin.write(b"r")

    async def clear_capture(self, *, keep_focus=False):
        if self.gesture_accepted:
            self.gesture_accepted = False
            self.reset_gesture()
        await super().clear_capture(keep_focus=keep_focus)

    async def handle_event(self, event):
        action = event.partition(" ")[0]
        if action == "activity":
            revision = event.removeprefix("activity ")
            if revision.isascii() and revision.isdecimal() and len(revision) <= 10:
                self.input_revision = int(revision)
                self.last_insertion = None
        elif action in ("start", "paste-last"):
            available = self.can_start_recording and (action == "start" or not self.jobs)
            if available and not self.state.draining:
                fields = event.split(" ", 4)
                if len(fields) < 4 or not all(part.isascii() and part.isdecimal() for part in fields[1:4]):
                    return
                focus = tuple(fields[1:4]) if all(part != "0" for part in fields[1:4]) else None
                if action == "paste-last":
                    self.recovery_focus = focus
                    self.phase = "pasting"
                    self.action = asyncio.create_task(self.paste_last())
                    return
                if self.focus != focus:
                    self.clear_focus()
                    self.focus = focus
                self.take_input_revision = self.input_revision
                self.application = fields[4].removesuffix(".exe")[:256] if len(fields) == 5 else ""
                self.gesture_accepted = True
                self.schedule()
            elif action == "start" and not self.gesture_accepted:
                if self.queue_full:
                    self.feedback = QUEUE_FULL
                self.reset_gesture()
        elif event == "focus-lost":
            self.clear_focus()
            self.last_insertion = None
        elif event == "paste-focus-lost":
            if self.recovery_focus is not None:
                self.last_insertion = None
            self.recovery_focus = None
        elif event in ("finish", "cancel") and self.gesture_accepted:
            self.gesture_accepted = False
            if event == "finish" and self.phase == "recording":
                # The recorder is already running. Keep the target through inference.
                self.schedule(released_at=monotonic())
            elif event == "cancel":
                async with self.control:
                    if not self.state.draining:
                        await self.cancel_dictation()
            elif self.phase in ("idle", "starting"):
                await self.cancel_capture()

    def clear_focus(self):
        self.focus = None
        self.focus_revision += 1

    async def paste_last(self):
        try:
            text = (await run_in_threadpool(self.snapshot))["text"]
            self.feedback = await self.deliver(text, recovery=True) if text else "There’s no last dictation to paste."
        finally:
            self.recovery_focus = None
            self.phase = "idle"

    async def copy_interrupted_text(self, text):
        self.last_insertion = None
        return await super().copy_interrupted_text(text)

    async def deliver(self, text, *, recovery=False):
        def current_focus():
            return self.recovery_focus if recovery else self.focus

        focus = current_focus()
        revision = self.input_revision if recovery else self.take_input_revision
        return await self.paste_to(text, focus, lambda: current_focus() is focus, self.terminal, revision)

    def prepare_delivery(self):
        focus, revision, terminal = self.focus, self.focus_revision, self.terminal
        input_revision = self.take_input_revision

        async def deliver(text):
            return await self.paste_to(text, focus, lambda: self.focus_revision == revision, terminal, input_revision)

        return deliver

    async def paste_to(self, text, focus, allowed, terminal, revision):
        channel = self.activity_channel
        can_continue = channel is not None and self.input_revision == revision
        previous, self.last_insertion = self.last_insertion, None
        inserted = text
        if focus is None or not allowed():
            return COPY_LAST
        if can_continue and previous and previous[:2] == (focus, revision):
            inserted = join_dictation(previous[2], text)
        guard = (channel, str(revision)) if inserted != text else ()
        status = await self.paste_attempt(inserted, focus, allowed, terminal, guard)
        if status == PASTE_CONTINUATION_CHANGED and guard:
            inserted = text
            if not allowed():
                return COPY_LAST
            status = await self.paste_attempt(text, focus, allowed, terminal, ())
            can_continue = False
        if status != 0:
            return COPY_LAST
        if can_continue and self.activity_channel == channel and self.input_revision == revision and allowed():
            self.last_insertion = (focus, revision, inserted)
        return "Paste shortcut sent."

    async def paste_attempt(self, text, focus, allowed, terminal, guard):
        try:
            helper = await self.spawn("paste", *focus, "terminal" if terminal else "standard", *guard)
        except (OSError, CaptureError):
            return None
        if not allowed():
            await stop_process(helper)
            return None
        payload = text.replace("\r\n", "\n").replace("\n", "\r\n").encode()
        attempt = asyncio.create_task(self.send_text(helper, payload))
        try:
            await asyncio.shield(attempt)
        except asyncio.CancelledError:
            # A paste already in flight must get the opportunity to restore the clipboard.
            await asyncio.gather(attempt, return_exceptions=True)
            raise
        return helper.returncode

    async def send_text(self, helper, payload):
        try:
            await asyncio.wait_for(helper.communicate(payload), 6)
        except (OSError, TimeoutError):
            await stop_process(helper)

    async def disable(self):
        helper, self.listener = self.listener, None
        self.activity_channel = None
        self.last_insertion = None
        self.gesture_accepted = False
        if self.events is not None:
            self.events.cancel()
            await asyncio.gather(self.events, return_exceptions=True)
            self.events = None
        if helper is not None:
            await stop_process(helper)
        await self.cancel_capture(include_pending=True)

    async def close(self):
        await self.disable()
