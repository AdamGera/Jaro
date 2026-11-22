"""Global X11 shortcuts and focus-aware dictation delivery."""
import asyncio
from contextlib import suppress
import os

from Xlib import X, XK, display, error

from linux_capture import CaptureError
from portable_dictation import PortableDictation
from x11_paste import X11Paste


def supported():
    return bool(os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
                and os.environ.get("XDG_SESSION_TYPE", "x11").lower() == "x11")


class X11Dictation(PortableDictation):
    platform = "x11"
    shortcut = "Ctrl+Alt+Shift+D"

    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        self.display = None
        self.binding = None
        self.paste = None
        self.focus = None
        self.listener = None

    @property
    def enabled(self):
        return self.binding is not None

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
            grab = X.ControlMask | X.ShiftMask | masks.pop()
            locks = {0, X.LockMask}
            for name in ("Num_Lock", "Scroll_Lock"):
                lock = connection.keysym_to_keycode(XK.string_to_keysym(name))
                if lock:
                    for index, codes in enumerate(modifiers):
                        if lock in codes:
                            locks.update(mask | (1 << index) for mask in tuple(locks))
            errors = error.CatchError()
            root = connection.screen().root
            masks = {grab | mask for mask in locks}
            for mask in masks:
                root.grab_key(key, mask, False, X.GrabModeAsync, X.GrabModeAsync, onerror=errors)
            connection.sync()
            if errors.get_error() is not None:
                for mask in masks:
                    root.ungrab_key(key, mask)
                connection.flush()
                raise CaptureError("Ctrl+Alt+Shift+D is already in use.")
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
        self.binding = key, masks
        self.feedback = ""

    async def monitor(self, connection):
        held = False
        try:
            while True:
                while connection.pending_events():
                    event = connection.next_event()
                    if self.paste is not None:
                        self.paste.handle(event)
                    if (event.type == X.FocusOut and event.window == self.focus
                            and event.mode in (X.NotifyNormal, X.NotifyWhileGrabbed)):
                        self.clear_focus()
                    if (self.binding is not None and event.type == X.KeyPress
                            and event.detail == self.binding[0] and not held):
                        held = True
                        self.schedule()
                # The physical bitmap stays down through X11's synthetic auto-repeat releases.
                if held:
                    if self.binding is None:
                        held = False
                    else:
                        key = self.binding[0]
                        held = bool(connection.query_keymap()[key // 8] & (1 << (key % 8)))
                if self.paste is not None:
                    self.paste.expire()
                await asyncio.sleep(.01 if self.paste and self.paste.waiting else .04)
        except (OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
            self.focus = None
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                connection.close()
            self.binding = None
            await self.cancel_capture()
            self.display = None
            self.paste = None
            self.feedback = "The X11 shortcut disconnected. Enable it again to reconnect."

    def clear_focus(self):
        if self.focus is not None:
            focus = self.focus
            self.focus = None
            with suppress(error.BadWindow):
                mask = focus.get_attributes().your_event_mask & ~X.FocusChangeMask
                focus.change_attributes(event_mask=mask, onerror=error.CatchError(error.BadWindow))

    def watch_focus(self):
        focus = self.display.get_input_focus().focus
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
        try:
            if self.paste is None:
                self.paste = X11Paste(self.display)
            focus = self.focus
            return await self.paste.paste(text, focus, lambda: self.focus == focus, self.terminal)
        except (OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
            return "Automatic paste was unavailable. Use Copy last to paste the dictation."

    async def disable(self):
        if self.display is not None and self.binding is not None:
            key, masks = self.binding
            root = self.display.screen().root
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                for mask in masks:
                    root.ungrab_key(key, mask)
                self.display.flush()
            self.binding = None
        await self.cancel_capture()

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
