"""Global X11 shortcuts and focus-aware dictation delivery."""
import asyncio
from contextlib import closing, nullcontext, suppress
from functools import partial

from Xlib import X, XK, Xatom, display, error
from starlette.concurrency import run_in_threadpool

from linux_capture import CaptureError
from linux_dictation import LinuxDictation
from linux_x11 import x11_session
from portable_dictation import HISTORY_ERRORS, OutputContext, PASTE_SENT, join_dictation
from portable_shortcuts import shortcut_parts
from x11_paste import X11Paste


DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+D"
RECOVERY_SHORTCUT = "Ctrl+Alt+V"
MODIFIER_KEYS = {
    "Ctrl": ("Control_L", "Control_R"),
    "Alt": ("Alt_L", "Alt_R"),
    "Shift": ("Shift_L", "Shift_R"),
    "Super": ("Super_L", "Super_R"),
}
def keycode(connection, name):
    return connection.keysym_to_keycode(XK.string_to_keysym(name))


def lock_masks(connection, mapping):
    locks = {0, X.LockMask}
    for name in ("Num_Lock", "Scroll_Lock"):
        lock = keycode(connection, name)
        if lock:
            for index, codes in enumerate(mapping):
                if lock in codes:
                    locks.update(mask | (1 << index) for mask in tuple(locks))
    return locks


def resolve_shortcut(connection, shortcut, mapping, locks):
    _, selected, keysym = shortcut_parts(shortcut)
    if selected == ["Ctrl", "Alt"] and keysym in {f"F{index}" for index in range(1, 13)}:
        raise CaptureError("X11 reserves Ctrl+Alt+F1 through Ctrl+Alt+F12 for switching consoles.")
    key = keycode(connection, keysym)
    if not key:
        raise CaptureError(f"{shortcut} has no matching key in the current X11 keyboard layout.")
    modifier_mask = 0
    for name in selected:
        keys = {keycode(connection, symbol) for symbol in MODIFIER_KEYS[name]} - {0}
        masks = {1 << index for index, group in enumerate(mapping) if keys.intersection(group)}
        if len(masks) != 1:
            raise CaptureError(f"X11 could not resolve the {name} modifier for this shortcut.")
        mask = masks.pop()
        if mask & modifier_mask or any(mask & lock for lock in locks):
            raise CaptureError(f"X11 cannot distinguish the modifiers in {shortcut}.")
        modifier_mask |= mask
    return key, modifier_mask


def supported():
    return x11_session()


class X11Dictation(LinuxDictation):
    platform = "x11"
    shortcut = DEFAULT_SHORTCUT
    recovery_shortcut = RECOVERY_SHORTCUT

    def __init__(self, state, transcribe, history):
        super().__init__(state, transcribe, history)
        try:
            self.shortcut = shortcut_parts(history.desktop_shortcut() or DEFAULT_SHORTCUT)[0]
        except (*HISTORY_ERRORS, CaptureError):
            self.shortcut = DEFAULT_SHORTCUT

        self.raw_shortcut = ""
        try:
            if saved := history.desktop_shortcut(raw=True):
                self.raw_shortcut = shortcut_parts(saved)[0]
        except (*HISTORY_ERRORS, CaptureError):
            pass
        self.raw_shortcut_error = ""
        self.display = None
        self.binding = None
        self.raw_binding = None
        self.recovery_binding = None
        self.cancel_binding = None
        self.cancel_locks = {0}
        self.cancel_attempted = False
        self.paste = None
        self.focus = None
        self.focus_revision = 0
        self.take_field = None
        self.field = None
        self.field_task = None
        self.last_insertion = None
        self.listener = None

    @property
    def enabled(self):
        return self.binding is not None

    def snapshot(self):
        conflict = self.cancel_attempted and not self.cancel_binding
        return {**super().snapshot(), "defaultShortcut": DEFAULT_SHORTCUT, "recoveryShortcut": self.recovery_shortcut if self.recovery_binding else None,
                "rawShortcut": self.raw_shortcut, "rawShortcutError": self.raw_shortcut_error,
                "cancelShortcut": "Esc" if self.enabled and self.shortcut_mode != "hold" and not conflict else "",
                "cancelShortcutConflict": conflict}

    @staticmethod
    def grab(connection, key, modifiers, locks, *, preserve=None):
        if not key:
            return None
        failed = error.CatchError()
        root = connection.screen().root
        masks = {modifiers | lock for lock in locks}
        additional = masks - preserve[1] if preserve and preserve[0] == key else masks
        for mask in additional:
            root.grab_key(key, mask, False, X.GrabModeAsync, X.GrabModeAsync, onerror=failed)
        connection.sync()
        if failed.get_error() is None:
            return key, masks
        for mask in additional:
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
            mapping = connection.get_modifier_mapping()
            locks = lock_masks(connection, mapping)
            primary = resolve_shortcut(connection, self.shortcut, mapping, locks)
            recovery_key = self.resolve_recovery(connection, mapping, locks)
            if primary == recovery_key:
                raise CaptureError("This shortcut is reserved for Paste last dictation.")
            binding = self.grab(connection, *primary, locks)
            if binding is None:
                raise CaptureError(f"{self.shortcut} is already in use.")
            recovery = self.grab(connection, *recovery_key, locks) if recovery_key else None
            raw = None
            raw_error = ""
            if self.raw_shortcut:
                try:
                    proposed = resolve_shortcut(connection, self.raw_shortcut, mapping, locks)
                    if proposed == primary or proposed == recovery_key:
                        raise CaptureError("Choose a different shortcut for dictation without processing.")
                    raw = self.grab(connection, *proposed, locks)
                    if raw is None:
                        raise CaptureError(f"{self.raw_shortcut} is already in use.")
                except CaptureError as failure:
                    raw_error = str(failure)
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
        self.raw_binding = raw
        self.raw_shortcut_error = raw_error
        self.recovery_binding = recovery
        self.cancel_locks = locks
        self.feedback = "" if recovery else "Ctrl+Alt+V is unavailable. Use Copy last to paste the dictation."
        with suppress(ImportError):
            from wayland_field import WaylandField

            self.field = WaylandField(window_probe=self.accessible_window)
            self.field_task = asyncio.create_task(self.field.start())

    def resolve_recovery(self, connection, mapping, locks):
        with suppress(CaptureError):
            return resolve_shortcut(connection, self.recovery_shortcut, mapping, locks)
        return None

    @staticmethod
    def ungrab(connection, binding, *, preserve=None):
        key, masks = binding
        if preserve and preserve[0] == key:
            masks = masks - preserve[1]
        root = connection.screen().root
        for mask in masks:
            root.ungrab_key(key, mask)
        connection.flush()

    async def change_shortcut(self, value, *, raw=False):
        shortcut = "" if raw and value == "" else shortcut_parts(value)[0]
        if self.busy or self.action_pending() or self.pressed_at is not None or self.tap_timer is not None:
            raise CaptureError("Finish the current dictation before changing the shortcut.")
        current = self.raw_shortcut if raw else self.shortcut
        if current == shortcut and (not raw or not self.raw_shortcut_error):
            return
        other = self.shortcut if raw else self.raw_shortcut
        if shortcut and shortcut == other:
            raise CaptureError("Choose different shortcuts for regular dictation and dictation without processing.")
        previous = self.raw_binding if raw else self.binding
        connection = self.display if self.enabled else None
        candidate = None
        self.changing_shortcut = True
        try:
            if shortcut:
                if connection is None:
                    connection = display.Display()
                mapping = connection.get_modifier_mapping()
                locks = lock_masks(connection, mapping)
                key, modifiers = resolve_shortcut(connection, shortcut, mapping, locks)
                if (key, modifiers) == self.resolve_recovery(connection, mapping, locks):
                    raise CaptureError("This shortcut is reserved for Paste last dictation.")
                other_key = None
                if other:
                    with suppress(CaptureError):
                        other_key = resolve_shortcut(connection, other, mapping, locks)
                if (key, modifiers) == other_key:
                    raise CaptureError("Choose different shortcuts for regular dictation and dictation without processing.")
                keys = connection.query_keymap()
                if keys[key // 8] & (1 << (key % 8)):
                    raise CaptureError("Release the new shortcut before saving it.")
                if (key, {modifiers | lock for lock in locks}) != previous:
                    candidate = self.grab(connection, key, modifiers, locks, preserve=previous)
                    if candidate is None:
                        raise CaptureError(f"{shortcut} is already in use.")
            try:
                await run_in_threadpool(self.history.save_desktop_shortcut, shortcut, raw=raw)
            except HISTORY_ERRORS as failure:
                raise CaptureError("Could not save the dictation shortcut.") from failure
            if raw:
                self.raw_shortcut = shortcut
                self.raw_shortcut_error = ""
            else:
                self.shortcut = shortcut
            self.last_tap.clear()
            if self.enabled and self.display is connection and (candidate or not shortcut):
                binding, candidate = candidate, None
                if raw:
                    self.raw_binding = binding
                else:
                    self.binding = binding
                    self.cancel_locks = locks
                if previous:
                    self.ungrab(connection, previous, preserve=binding)
        except (OSError, error.DisplayError, error.ConnectionClosedError, error.XError) as failure:
            raise CaptureError("Jaro could not rebind the shortcut on this X11 desktop.") from failure
        finally:
            self.changing_shortcut = False
            if connection is not None:
                with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                    if connection is not self.display:
                        connection.close()
                    elif candidate:
                        self.ungrab(connection, candidate, preserve=previous)

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
        held = {}
        try:
            while True:
                while connection.pending_events():
                    event = connection.next_event()
                    if self.paste is not None:
                        self.paste.handle(event)
                    if (event.type == X.FocusOut and event.window == self.focus
                            and event.mode in (X.NotifyNormal, X.NotifyWhileGrabbed)):
                        self.last_insertion = None
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
                        held[key] = None
                        async with self.control:
                            if self.enabled and not self.state.draining:
                                await self.cancel_dictation()
                    elif self.binding and key == self.binding[0] and modifiers in self.binding[1]:
                        held[key] = False
                        self.shortcut_edge(True)
                    elif self.raw_binding and key == self.raw_binding[0] and modifiers in self.raw_binding[1]:
                        held[key] = True
                        self.shortcut_edge(True, raw=True)
                    elif self.recovery_binding and key == self.recovery_binding[0] and modifiers in self.recovery_binding[1]:
                        held[key] = None
                        if not (self.busy or self.state.draining or self.changing_shortcut) and (self.action is None or self.action.done()):
                            self.watch_focus()
                            self.phase = "pasting"
                            self.action = asyncio.create_task(self.paste_last())
                # The physical bitmap stays down through X11's synthetic auto-repeat releases.
                if held:
                    bitmap = connection.query_keymap()
                    down = {key: raw for key, raw in held.items() if bitmap[key // 8] & (1 << (key % 8))}
                    for key in held.keys() - down.keys():
                        if held[key] is not None:
                            self.shortcut_edge(False, raw=held[key])
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
            self.raw_binding = None
            self.recovery_binding = None
            self.cancel_binding = None
            self.cancel_attempted = False
            await self.cancel_capture(include_pending=True)
            await self.close_field()
            self.display = None
            self.paste = None
            self.feedback = "The X11 shortcut disconnected. Enable it again to reconnect."

    async def command_paste_last(self):
        self.watch_focus()
        self.phase = "pasting"
        await self.paste_last()
        if self.feedback != PASTE_SENT:
            raise CaptureError(self.feedback)

    async def paste_last(self):
        try:
            text = (await run_in_threadpool(self.snapshot))["text"]
            self.feedback = await self.prepare_delivery(raw=self.last_raw)(text) if text else "There’s no last dictation to paste."
        finally:
            self.clear_focus()
            self.application = ""
            self.phase = "idle"

    def clear_focus(self):
        self.focus_revision += 1
        self.take_field = None
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
            with suppress(error.BadWindow):
                mask = focus.get_attributes().your_event_mask | X.FocusChangeMask
                focus.change_attributes(event_mask=mask, onerror=error.CatchError(error.BadWindow))
            self.focus = focus
            self.application = self.focused_application(focus)
            self.take_field = self.field.focus if self.field else None

    async def accessible_window(self, focused=None, *, include_title=True, connection=None):
        from wayland_field import FocusWindow

        connection = connection or self.display
        if connection is None:
            return None
        with suppress(OSError, error.XError, error.ConnectionClosedError):
            root = connection.screen().root
            window = connection.get_input_focus().focus if focused is None else focused
            pid_atom = connection.intern_atom("_NET_WM_PID")
            title_atom = connection.intern_atom("_NET_WM_NAME")
            utf8 = connection.intern_atom("UTF8_STRING")
            while window not in (root, X.NONE, X.PointerRoot):
                pid = window.get_property(pid_atom, Xatom.CARDINAL, 0, 1)
                if pid is not None and pid.format == 32 and len(pid.value) == 1 and pid.value[0] > 0:
                    if not include_title:
                        return FocusWindow(window.id, int(pid.value[0]), "")
                    title = window.get_property(title_atom, utf8, 0, 1024)
                    if title is not None and title.format == 8:
                        name = bytes(title.value).decode("utf8", errors="replace")
                    else:
                        name = window.get_wm_name()
                    return FocusWindow(window.id, int(pid.value[0]), name if isinstance(name, str) else "")
                window = window.query_tree().parent
        return None

    def focused_application(self, window, connection=None):
        root = (connection or self.display).screen().root
        with suppress(error.BadWindow):
            while window not in (root, X.NONE):
                names = window.get_wm_class()
                if names:
                    return (names[-1] or names[0])[:256]
                window = window.query_tree().parent
        return ""

    async def output_context(self, read_site, *, annotation=False):
        with suppress(OSError, error.DisplayError, error.XError, error.ConnectionClosedError):
            with (nullcontext(self.display) if self.display else closing(display.Display())) as connection:
                focus = connection.get_input_focus().focus
                if focus in (X.NONE, X.PointerRoot):
                    return OutputContext()
                application = self.focused_application(focus, connection)
                app_rules = any(profile.target.lower().startswith("app:") for profile in self.capture_output[2])
                probe = partial(self.accessible_window, connection=connection)
                if (app_rules or read_site) and (window := await probe(focus, include_title=read_site)):
                    from linux_call_pages import focused_browser_address, focused_browser_host, process_application

                    if annotation:
                        from portable_terminal_origin import focused_terminal_context

                        terminal = await focused_terminal_context(window, probe, application)
                        if terminal:
                            return OutputContext(application, terminal=terminal)
                    host = await focused_browser_host(window, probe) if read_site and not annotation else ""
                    address = await focused_browser_address(window, probe) if read_site and annotation else ""
                    return OutputContext(application, host, process_application(window.pid) if app_rules else "", address)
                return OutputContext(application)
        return OutputContext()

    async def copy_interrupted_text(self, text):
        self.last_insertion = None
        return await super().copy_interrupted_text(text)

    async def deliver(self, text):
        return await self.prepare_delivery()(text)

    def prepare_delivery(self, *, raw=False):
        focus, revision, terminal = self.focus, self.focus_revision, self.terminal
        field, expected = self.field, self.take_field

        def allowed():
            return not self.state.draining and self.focus_revision == revision and (
                expected is None or (field is self.field and field.focus == expected))

        async def deliver(text):
            return await self.paste_to(text, focus, allowed, terminal, field if expected is not None else None, raw=raw)

        return deliver

    async def paste_to(self, text, focus, allowed, terminal, field, *, raw=False):
        previous, self.last_insertion = self.last_insertion, None
        if not allowed():
            return "Focus changed. Use Copy last to paste the dictation."
        before = await field.snapshot() if field and not raw else None
        if not allowed():
            return "Focus changed. Use Copy last to paste the dictation."
        inserted = join_dictation(previous[2], text) if before and previous and previous[:2] == (focus, before) else text
        join_changed = False

        def allow_join():
            nonlocal join_changed
            current = allowed()
            join_changed = current and not field.matches(before)
            return current and not join_changed

        try:
            if self.paste is None:
                self.paste = X11Paste(self.display)
            result = await self.paste.paste(inserted, focus, allow_join if inserted != text else allowed, terminal)
            if result != PASTE_SENT and join_changed and allowed():
                return await self.paste.paste(text, focus, allowed, terminal)
        except (OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
            return "Automatic paste was unavailable. Use Copy last to paste the dictation."
        if result == PASTE_SENT and before and allowed():
            with suppress(TimeoutError):
                async with asyncio.timeout(.04):
                    after = await field.snapshot()
                if (after and after.focus == before.focus and after.revision != before.revision
                        and after.caret == before.caret + len(inserted) and after.length == before.length + len(inserted)):
                    self.last_insertion = (focus, after, inserted)
        return result

    async def close_field(self):
        if self.field_task is not None:
            self.field_task.cancel()
            await asyncio.gather(self.field_task, return_exceptions=True)
            self.field_task = None
        if self.field is not None:
            self.field.close()
            self.field = None
        self.last_insertion = None

    async def disable(self):
        if self.display is not None:
            root = self.display.screen().root
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError):
                for binding in (self.binding, self.raw_binding, self.recovery_binding, self.cancel_binding):
                    if binding is not None:
                        key, masks = binding
                        for mask in masks:
                            root.ungrab_key(key, mask)
                self.display.flush()
            self.binding = None
            self.raw_binding = None
            self.recovery_binding = None
            self.cancel_binding = None
            self.cancel_attempted = False
        await self.cancel_capture(include_pending=True)
        if self.paste is not None:
            await self.paste.wait_for_restore()
        await self.close_field()

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
