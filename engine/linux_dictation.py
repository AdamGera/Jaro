"""Dictation shortcut gestures shared by X11 and the Wayland portal."""
import asyncio
import logging
from time import monotonic

from starlette.concurrency import run_in_threadpool

from portable_dictation import HISTORY_ERRORS, QUEUE_FULL, PortableDictation
from portable_capture import CaptureError
from wayland_dictation_commands import PRINT_ACTION, RAW_ACTIONS


logger = logging.getLogger(__name__)
SHORTCUT_MODES = ("tap", "hold", "holdDouble", "doubleTap")
TAP_LIMIT = .25
DOUBLE_TAP_WINDOW = .30


class LinuxDictation(PortableDictation):
    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        self.shortcut_mode = "tap"
        self.pressed_at = None
        self.pressed_raw = False
        self.last_tap = {}
        self.shortcut_owner = None
        self.shortcut_take = None
        self.tap_timer = None
        self.discard_task = None
        self.changing_shortcut = False
        try:
            mode = history.linux_shortcut_mode()
            if mode in SHORTCUT_MODES:
                self.shortcut_mode = mode
        except HISTORY_ERRORS:
            logger.warning("Could not read the Linux dictation shortcut mode.", exc_info=True)

    def snapshot(self):
        return {**super().snapshot(), "shortcutMode": self.shortcut_mode}

    async def change_mode(self, mode):
        if self.busy or self.action_pending() or self.pressed_at is not None or self.tap_timer is not None:
            raise CaptureError("Finish the current dictation before changing the shortcut mode.")
        self.changing_shortcut = True
        try:
            await run_in_threadpool(self.history.configure, linux_shortcut_mode=mode)
        except HISTORY_ERRORS as failure:
            raise CaptureError("Could not save the Linux dictation shortcut mode.") from failure
        finally:
            self.changing_shortcut = False
        self.shortcut_mode = mode
        self.last_tap.clear()

    def action_pending(self):
        return ((self.action is not None and not self.action.done())
                or (self.discard_task is not None and not self.discard_task.done()))

    async def run_command(self, action):
        if action == PRINT_ACTION:
            return await self.print_current_take()
        arrival = monotonic()
        async with self.control:
            if self.state.draining:
                raise CaptureError("Jaro is preparing to restart.")
            if not self.enabled:
                if self.platform == "wayland":
                    raise CaptureError("Enable desktop shortcut commands or the global shortcut in Jaro’s Dictation settings.")
                raise CaptureError("Enable system dictation in Jaro first.")
            return await self.perform_command(action, arrival)

    async def perform_command(self, action, _arrival):
        raw = action in RAW_ACTIONS
        action = RAW_ACTIONS.get(action, action)
        running = self.action_pending()
        if action == "cancel":
            await self.cancel_dictation()
            return self.phase
        if action == "stop" and running and self.phase in ("idle", "starting"):
            await self.cancel_capture()
            self.feedback = "Dictation canceled."
            return "idle"
        if action == "paste-last":
            if self.busy or running:
                raise CaptureError("Finish the current dictation before pasting the last one.")
            await self.command_paste_last()
            return "pasted"
        if action in ("start", "toggle") and self.can_start_recording and not running:
            state = self.state
            if state.status != "ready":
                raise CaptureError("Dictation is unavailable until the local speech model is ready.")
            self.schedule(raw=raw)
            return "starting"
        if action in ("stop", "toggle") and not running and self.phase == "recording":
            if action == "toggle" and raw != self.capture_raw:
                return self.phase
            self.toggle_from_shortcut()
            return "transcribing"
        if action in ("start", "toggle") and self.queue_full:
            raise CaptureError(QUEUE_FULL)
        return self.phase

    def toggle_from_shortcut(self, *, released_at=None, raw=False):
        return self.schedule(released_at=released_at, raw=raw)

    def start_sound(self, **_options):
        if self.shortcut_mode == "holdDouble" and self.shortcut_take == "held":
            if self.pressed_at is not None:
                super().start_sound(delay=max(0, self.pressed_at + TAP_LIMIT - monotonic()),
                                    while_valid=lambda: self.pressed_at is not None or self.shortcut_take == "locked")
        else:
            super().start_sound()

    def shortcut_edge(self, pressed, *, raw=False):
        if self.state.draining or self.canceling or self.changing_shortcut or (self.discard_task and not self.discard_task.done()):
            return
        if self.shortcut_owner is not None and self.shortcut_owner != raw:
            return
        if self.pressed_at is not None and self.pressed_raw != raw:
            return
        if self.shortcut_mode == "tap":
            if pressed:
                starting = self.capture is None
                if (starting or self.shortcut_owner is not None or not raw) and self.toggle_from_shortcut(
                    released_at=monotonic(), raw=raw
                ) and starting:
                    self.shortcut_owner = raw
        elif pressed:
            self.press_shortcut(monotonic(), raw)
        else:
            self.release_shortcut(monotonic(), raw)

    def press_shortcut(self, now, raw):
        if self.shortcut_take == "locked":
            self.finish_shortcut(released_at=now)
            return
        if self.tap_timer is not None:
            self.tap_timer.cancel()
            self.tap_timer = None
            self.shortcut_take = "locked"
            if self.phase == "recording":
                self.start_sound()
            return
        if not self.can_start_recording or self.action_pending():
            if self.queue_full:
                self.feedback = QUEUE_FULL
            return
        previous = self.last_tap.pop(raw, None)
        second_tap = previous is not None and now - previous <= DOUBLE_TAP_WINDOW
        self.pressed_at, self.pressed_raw = now, raw
        if self.shortcut_mode == "doubleTap" and not second_tap:
            return
        self.shortcut_take = "locked" if second_tap else "held"
        if self.toggle_from_shortcut(released_at=now, raw=raw):
            self.shortcut_owner = raw

    def release_shortcut(self, now, raw):
        pressed_at, self.pressed_at = self.pressed_at, None
        if pressed_at is None or self.shortcut_take == "locked":
            return
        brief = now - pressed_at < TAP_LIMIT
        if self.shortcut_mode == "doubleTap":
            if brief:
                self.last_tap[raw] = now
        elif self.shortcut_take == "held":
            if self.shortcut_mode == "holdDouble" and brief:
                # Reuse the microphone opening if the second tap arrives in time.
                self.tap_timer = asyncio.get_running_loop().call_later(DOUBLE_TAP_WINDOW, self.expire_tap)
            else:
                self.finish_shortcut(released_at=now)

    def finish_shortcut(self, *, released_at):
        if self.phase == "recording" and not self.action_pending():
            self.shortcut_take = None
            self.toggle_from_shortcut(released_at=released_at)
        elif self.phase in ("idle", "starting"):
            self.discard_shortcut()

    def expire_tap(self):
        self.tap_timer = None
        self.discard_shortcut()

    def discard_shortcut(self):
        had_take = self.shortcut_take is not None
        self.reset_shortcut()
        if had_take and not (self.discard_task is not None and not self.discard_task.done()):
            self.discard_task = asyncio.create_task(self.discard_take(self.action))

    async def discard_take(self, action):
        async with self.control:
            if self.action is action and self.phase in ("idle", "starting", "recording"):
                await self.cancel_capture()

    def reset_shortcut(self):
        if self.tap_timer is not None:
            self.tap_timer.cancel()
            self.tap_timer = None
        self.pressed_at = self.shortcut_owner = self.shortcut_take = None
        self.last_tap.clear()

    def recover_interruption(self):
        return self.tap_timer is None and (self.shortcut_take != "held" or self.pressed_at is None
                                           or monotonic() - self.pressed_at >= TAP_LIMIT)

    async def clear_capture(self, *, keep_focus=False):
        self.reset_shortcut()
        await super().clear_capture(keep_focus=keep_focus)
