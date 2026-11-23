"""Global X11 shortcuts and focus-aware dictation delivery."""
import asyncio
from contextlib import suppress
import os

from Xlib import X, XK, display, error
from starlette.concurrency import run_in_threadpool

from linux_capture import CaptureError
from linux_dictation import LinuxDictation
from x11_paste import X11Paste


def supported():
    return bool(os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                and os.environ.get("XDG_SESSION_TYPE", "x11").lower() == "x11")


class X11Dictation(LinuxDictation):
    platform = "x11"
    shortcut = "Ctrl+Alt+Shift+D"
    recovery_shortcut = "Ctrl+Alt+V"

    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        self.display = None
        self.binding = None
        self.recovery_binding = None
        self.cancel_binding = None
        self.cancel_locks = {0}
        self.cancel_attempted = False
        self.paste = None
        self.focus = None
        self.focus_revision = 0
        self.listener = None

    @property
    def enabled(self):
        return self.binding is not None

    def snapshot(self):
        conflict = self.cancel_attempted and not self.cancel_binding
        return {**super().snapshot(), "recoveryShortcut": self.recovery_shortcut if self.recovery_binding else None,
                "cancelShortcut": "Esc" if self.enabled and self.shortcut_mode != "hold" and not conflict else "",
                "cancelShortcutConflict": conflict}

    @staticmethod
    def grab(connection, key, modifiers, locks):
        if not key:
            return None
        failed = error.CatchError()
        root = connection.screen().root
        masks = {modifiers | lock for lock in locks}
        for mask in masks:
            root.grab_key(key, mask, False, X.GrabModeAsync, X.GrabModeAsync, onerror=failed)
        connection.sync()
        if failed.get_error() is None:
            return key, masks
        for mask in masks:
            root.ungrab_key(key, mask)
        connection.flush()
        return None

    def enable(self):
        if self.binding is not None:
            return
        connection = self.display
        try:
            if connection is None:
                connection = display.Display()
            key = connection.keysym_to_keycode(XK.string_to_keysym("d"))
            modifiers = connection.get_modifier_mapping()
            alt_codes = {connection.keysym_to_keycode(XK.string_to_keysym(name))
                         for name in ("Alt_L", "Alt_R")} - {0}
            masks = {1 << index for index, codes in enumerate(modifiers) if alt_codes.intersection(codes)}
            if not key or len(masks) != 1:
                raise CaptureError("The X11 keyboard has no usable Alt+D binding.")
            ctrl_alt = X.ControlMask | masks.pop()
            locks = {0, X.LockMask}
            for name in ("Num_Lock", "Scroll_Lock"):
                lock = connection.keysym_to_keycode(XK.string_to_keysym(name))
                if lock:
                    for index, codes in enumerate(modifiers):
                        if lock in codes:
                            locks.update(mask | (1 << index) for mask in tuple(locks))
            binding = self.grab(connection, key, ctrl_alt | X.ShiftMask, locks)
            if binding is None:
                raise CaptureError("Ctrl+Alt+Shift+D is already in use.")
            recovery_key = connection.keysym_to_keycode(XK.string_to_keysym("v"))
            recovery = self.grab(connection, recovery_key, ctrl_alt, locks)
        except (OSError, error.DisplayError, error.ConnectionClosedError) as failure:
            if connection is not None and self.display is None:
                with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                    connection.close()
            raise CaptureError("Jaro could not connect to this X11 desktop.") from failure
        except CaptureError:
            if self.display is None:
                connection.close()
            raise
        if self.display is None:
            self.display = connection
            self.listener = asyncio.create_task(self.monitor(connection))
        self.binding = binding
        self.recovery_binding = recovery
        self.cancel_locks = locks
        self.feedback = "" if recovery else "Ctrl+Alt+V is unavailable. Use Copy last to paste the dictation."

    def update_cancel_binding(self, connection, held):
        # A held Escape keeps its grab until the physical release, including auto-repeat.
        if self.cancel_binding and self.cancel_binding[0] in held:
            return
        active = self.enabled and not self.state.draining and self.can_cancel and (
            self.cancel_attempted or self.shortcut_mode == "tap" or self.shortcut_take == "locked")
        if active and not self.cancel_attempted:
            self.cancel_attempted = True
            key = connection.keysym_to_keycode(XK.string_to_keysym("Escape"))
            self.cancel_binding = self.grab(connection, key, 0, self.cancel_locks)
        elif not active and self.cancel_attempted:
            self.cancel_attempted = False
            binding, self.cancel_binding = self.cancel_binding, None
            if binding:
                root = connection.screen().root
                for mask in binding[1]:
                    root.ungrab_key(binding[0], mask)
                connection.flush()

    async def monitor(self, connection):
        held = set()
        try:
            while True:
                while connection.pending_events():
                    event = connection.next_event()
                    if self.paste is not None:
                        self.paste.handle(event)
                    if (event.type == X.FocusOut and event.window == self.focus
                            and event.mode in (X.NotifyNormal, X.NotifyWhileGrabbed)):
                        self.clear_focus()
                    if event.type != X.KeyPress:
                        continue
                    key = event.detail
                    modifiers = event.state & 0xff
                    if self.phase == "pasting" and self.recovery_binding and key != self.recovery_binding[0]:
                        self.clear_focus()
                    if key in held:
                        continue
                    if self.cancel_binding and key == self.cancel_binding[0] and modifiers in self.cancel_binding[1]:
                        held.add(key)
                        async with self.control:
                            if self.enabled and not self.state.draining:
                                await self.cancel_dictation()
                    elif self.binding and key == self.binding[0] and modifiers in self.binding[1]:
                        held.add(key)
                        self.shortcut_edge(True)
                    elif self.recovery_binding and key == self.recovery_binding[0] and modifiers in self.recovery_binding[1]:
                        held.add(key)
                        if not self.busy and not self.state.draining and (self.action is None or self.action.done()):
                            self.watch_focus()
                            self.phase = "pasting"
                            self.action = asyncio.create_task(self.paste_last())
                # The physical bitmap stays down through X11's synthetic auto-repeat releases.
                if held:
                    bitmap = connection.query_keymap()
                    down = {key for key in held if bitmap[key // 8] & (1 << (key % 8))}
                    if self.binding and self.binding[0] in held - down:
                        self.shortcut_edge(False)
                    held = down
                self.update_cancel_binding(connection, held)
                if self.paste is not None:
                    self.paste.expire()
                await asyncio.sleep(.01 if self.paste and self.paste.waiting else .04)
        except (OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
            self.focus = None
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                connection.close()
            self.binding = None
            self.recovery_binding = None
            self.cancel_binding = None
            self.cancel_attempted = False
            await self.cancel_capture(include_pending=True)
            self.display = None
            self.paste = None
            self.feedback = "The X11 shortcut disconnected. Enable it again to reconnect."

    async def paste_last(self):
        try:
            text = (await run_in_threadpool(self.snapshot))["text"]
            self.feedback = await self.deliver(text) if text else "There’s no last dictation to paste."
        finally:
            self.clear_focus()
            self.application = ""
            self.phase = "idle"

    def clear_focus(self):
        self.focus_revision += 1
        if self.focus is not None:
            focus = self.focus
            self.focus = None
            with suppress(error.BadWindow):
                mask = focus.get_attributes().your_event_mask & ~X.FocusChangeMask
                focus.change_attributes(event_mask=mask, onerror=error.CatchError(error.BadWindow))

    def watch_focus(self):
        focus = self.display.get_input_focus().focus
        if focus != self.focus:
            self.clear_focus()
        if focus not in (X.NONE, X.PointerRoot, self.display.screen().root):
            focus.change_attributes(event_mask=X.FocusChangeMask, onerror=error.CatchError(error.BadWindow))
            self.focus = focus
            self.application = self.focused_application(focus)

    def focused_application(self, window):
        root = self.display.screen().root
        with suppress(error.BadWindow):
            while window not in (root, X.NONE):
                names = window.get_wm_class()
                if names:
                    return (names[-1] or names[0])[:256]
                window = window.query_tree().parent
        return ""

    async def deliver(self, text):
        focus = self.focus
        return await self.paste_to(text, focus, lambda: self.focus == focus, self.terminal)

    def prepare_delivery(self):
        focus, revision, terminal = self.focus, self.focus_revision, self.terminal

        async def deliver(text):
            return await self.paste_to(text, focus, lambda: self.focus_revision == revision, terminal)

        return deliver

    async def paste_to(self, text, focus, allowed, terminal):
        try:
            if self.paste is None:
                self.paste = X11Paste(self.display)
            return await self.paste.paste(text, focus, allowed, terminal)
        except (OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
            return "Automatic paste was unavailable. Use Copy last to paste the dictation."

    async def disable(self):
        if self.display is not None:
            root = self.display.screen().root
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                for binding in (self.binding, self.recovery_binding, self.cancel_binding):
                    if binding is not None:
                        key, masks = binding
                        for mask in masks:
                            root.ungrab_key(key, mask)
                self.display.flush()
            self.binding = None
            self.recovery_binding = None
            self.cancel_binding = None
            self.cancel_attempted = False
        await self.cancel_capture(include_pending=True)

    async def close(self):
        await self.disable()
        if self.paste is not None:
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
                await self.paste.hand_off()
        if self.listener is not None:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
            self.listener = None
        if self.display is not None:
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                self.display.close()
            self.display = None
        self.paste = None
