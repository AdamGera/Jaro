"""System dictation through a lifecycle-owned Windows keyboard and paste helper."""
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import subprocess

from starlette.concurrency import run_in_threadpool

from portable_capture import CaptureError
from portable_dictation import PortableDictation


COPY_LAST = "Dictation is ready. Use Copy last to paste it into your app."


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
        self.recovery_focus = None
        self.gesture_accepted = False

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
        try:
            helper = await self.spawn("listen")
            if (await asyncio.wait_for(helper.stdout.readline(), 4)).strip() != b"ready":
                raise CaptureError("Windows did not start Jaro’s keyboard shortcut.")
            self.listener = helper
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
                        self.gesture_accepted = False
                        await stop_process(helper)
                        await self.cancel_capture()
                        self.feedback = "The Windows shortcut disconnected. Enable it again to reconnect."

    def snapshot(self):
        return {**super().snapshot(), "recoveryShortcut": self.recovery_shortcut}

    async def handle_event(self, event):
        action = event.partition(" ")[0]
        if action in ("start", "paste-last"):
            if not self.busy and not self.state.draining and (self.action is None or self.action.done()):
                fields = event.split(" ", 4)
                if len(fields) < 4 or not all(part.isascii() and part.isdecimal() for part in fields[1:4]):
                    return
                focus = tuple(fields[1:4]) if all(part != "0" for part in fields[1:4]) else None
                if action == "paste-last":
                    self.recovery_focus = focus
                    self.phase = "pasting"
                    self.action = asyncio.create_task(self.paste_last())
                    return
                self.focus = focus
                self.application = fields[4].removesuffix(".exe")[:256] if len(fields) == 5 else ""
                self.gesture_accepted = True
                self.schedule()
        elif event == "focus-lost":
            self.clear_focus()
        elif event == "paste-focus-lost":
            self.recovery_focus = None
        elif event in ("finish", "cancel") and self.gesture_accepted:
            self.gesture_accepted = False
            if event == "finish" and self.phase == "recording":
                # The recorder is already running. Keep the target through inference.
                self.schedule()
            elif event == "cancel" or self.phase in ("idle", "starting"):
                await self.cancel_capture()

    def clear_focus(self):
        self.focus = None

    async def paste_last(self):
        try:
            text = (await run_in_threadpool(self.snapshot))["text"]
            self.feedback = await self.deliver(text, recovery=True) if text else "There’s no last dictation to paste."
        finally:
            self.recovery_focus = None
            self.phase = "idle"

    async def deliver(self, text, *, recovery=False):
        def current_focus():
            return self.recovery_focus if recovery else self.focus

        focus = current_focus()
        if focus is None:
            return COPY_LAST
        try:
            helper = await self.spawn("paste", *focus, "terminal" if self.terminal else "standard")
        except (OSError, CaptureError):
            return COPY_LAST
        if current_focus() is not focus:
            await stop_process(helper)
            return COPY_LAST
        payload = text.replace("\r\n", "\n").replace("\n", "\r\n").encode()
        attempt = asyncio.create_task(self.send_text(helper, payload))
        try:
            await asyncio.shield(attempt)
        except asyncio.CancelledError:
            # A paste already in flight must get the opportunity to restore the clipboard.
            await asyncio.gather(attempt, return_exceptions=True)
            raise
        return "Paste shortcut sent." if helper.returncode == 0 else COPY_LAST

    async def send_text(self, helper, payload):
        try:
            await asyncio.wait_for(helper.communicate(payload), 6)
        except (OSError, TimeoutError):
            await stop_process(helper)

    async def disable(self):
        helper, self.listener = self.listener, None
        self.gesture_accepted = False
        if self.events is not None:
            self.events.cancel()
            await asyncio.gather(self.events, return_exceptions=True)
            self.events = None
        if helper is not None:
            await stop_process(helper)
        await self.cancel_capture()

    async def close(self):
        await self.disable()
