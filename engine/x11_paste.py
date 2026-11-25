"""X11 clipboard delivery for dictation, including restoration of rich data."""
import asyncio
from contextlib import contextmanager, suppress
from time import monotonic

from Xlib import X, XK, Xatom, error
from Xlib.ext import xtest
from Xlib.protocol import event, request


MAX_CLIPBOARD = 32 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
READ_TIMEOUT = 1.2
RESTORE_DELAY = .5
PASTE_SETTLE = .1
PASTE_SENT = "Paste shortcut sent."


class ClipboardUnavailable(Exception):
    pass


class X11Paste:
    def __init__(self, connection):
        self.connection = connection
        self.window = self.new_window()
        names = ("CLIPBOARD", "CLIPBOARD_MANAGER", "TARGETS", "TIMESTAMP", "MULTIPLE",
                 "SAVE_TARGETS", "DELETE", "INSERT_SELECTION", "INSERT_PROPERTY", "INCR",
                 "ATOM_PAIR", "UTF8_STRING", "TEXT", "text/plain", "text/plain;charset=utf-8",
                 "_JARO_SELECTION", "_JARO_TIME")
        self.atoms = {name: connection.intern_atom(name) for name in names}
        self.waiting = None
        self.data = {}
        self.transfers = {}
        self.timestamp = X.CurrentTime
        self.delivery = asyncio.Lock()
        self.restore_task = None
        self.original = None

    def new_window(self):
        return self.connection.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask)

    def owner(self, name="CLIPBOARD"):
        return self.connection.get_selection_owner(self.atoms[name])

    @contextmanager
    def server_grab(self):
        # Keep the final focus and ownership checks adjacent to the XTEST events.
        self.connection.grab_server()
        try:
            yield
        finally:
            self.connection.ungrab_server()
            self.connection.flush()

    async def receive(self, predicate):
        future = asyncio.get_running_loop().create_future()
        self.waiting = predicate, future
        try:
            self.connection.flush()
            return await future
        finally:
            self.waiting = None

    async def server_time(self):
        atom = self.atoms["_JARO_TIME"]
        self.window.change_property(atom, Xatom.INTEGER, 8, b"")
        notice = await self.receive(lambda item: (item.type == X.PropertyNotify
            and item.window == self.window and item.atom == atom and item.state == X.PropertyNewValue))
        return notice.time

    async def convert(self, window, target, limit):
        atoms = self.atoms
        prop = atoms["_JARO_SELECTION"]
        window.convert_selection(atoms["CLIPBOARD"], target, prop, X.CurrentTime)
        notice = await self.receive(lambda item: (item.type == X.SelectionNotify
            and item.requestor == window and item.selection == atoms["CLIPBOARD"] and item.target == target))
        if notice.property != prop:
            raise ClipboardUnavailable
        value = self.read_property(window, prop, limit)
        if value.property_type != atoms["INCR"]:
            return value.property_type, value.format, value.value

        kind = None
        data = None
        while True:
            await self.receive(lambda item: (item.type == X.PropertyNotify
                and item.window == window and item.atom == prop and item.state == X.PropertyNewValue))
            value = self.read_property(window, prop, limit)
            if kind is None:
                kind = value.property_type, value.format
                data = bytearray() if value.format == 8 else value.value[:0]
            if kind != (value.property_type, value.format):
                raise ClipboardUnavailable
            if not value.value:
                return *kind, bytes(data) if value.format == 8 else data
            data.extend(value.value)
            limit -= len(value.value) * (value.format // 8)

    @staticmethod
    def read_property(window, prop, limit):
        value = window.get_property(prop, X.AnyPropertyType, 0, limit // 4 + 1, delete=True)
        if value is None or value.bytes_after or len(value.value) * (value.format // 8) > limit:
            raise ClipboardUnavailable
        return value

    async def snapshot(self, owner):
        if owner == self.window:
            return self.data
        if owner == X.NONE:
            return {}
        atoms = self.atoms
        metadata = {atoms[name] for name in ("TARGETS", "TIMESTAMP", "MULTIPLE", "SAVE_TARGETS",
                    "DELETE", "INSERT_SELECTION", "INSERT_PROPERTY", "INCR")}
        reader = self.new_window()
        try:
            kind, size, offered = await self.convert(reader, atoms["TARGETS"], 4096)
            if (kind, size) != (Xatom.ATOM, 32):
                raise ClipboardUnavailable
            data = {}
            remaining = MAX_CLIPBOARD
            for target in dict.fromkeys(offered):
                if target in metadata or target == X.NONE:
                    continue
                try:
                    result = await self.convert(reader, target, remaining)
                except ClipboardUnavailable:
                    continue
                remaining -= len(result[2]) * (result[1] // 8)
                data[target] = result
            return data if data else None
        finally:
            reader.destroy()
            self.connection.flush()

    def serve_target(self, window, target, prop):
        atoms = self.atoms
        if target == atoms["TARGETS"]:
            value = Xatom.ATOM, 32, [atoms["TARGETS"], atoms["MULTIPLE"], atoms["TIMESTAMP"], *self.data]
        elif target == atoms["TIMESTAMP"]:
            value = Xatom.INTEGER, 32, [self.timestamp]
        else:
            value = self.data.get(target)
        if value is None or prop == X.NONE:
            return False
        kind, size, data = value
        length = len(data) * (size // 8)
        gone = error.CatchError(error.BadWindow, error.BadAtom)
        if length <= CHUNK_BYTES:
            window.change_property(prop, kind, size, data, onerror=gone)
        else:
            mask = window.get_attributes().your_event_mask | X.PropertyChangeMask
            window.change_attributes(event_mask=mask, onerror=gone)
            window.change_property(prop, atoms["INCR"], 32, [length], onerror=gone)
            self.transfers[window, prop] = monotonic() + 2, value, 0
        return True

    def serve(self, item):
        window = item.requestor
        prop = item.property or item.target
        if not self.data or item.selection != self.atoms["CLIPBOARD"]:
            prop = X.NONE
        elif item.target == self.atoms["MULTIPLE"]:
            with_multiple = self.atoms["ATOM_PAIR"]
            pairs = window.get_property(prop, with_multiple, 0, 1024)
            if pairs is None or pairs.bytes_after or pairs.format != 32 or len(pairs.value) % 2:
                prop = X.NONE
            else:
                for index in range(0, len(pairs.value), 2):
                    target, dest = pairs.value[index:index + 2]
                    if dest == prop or not self.serve_target(window, target, dest):
                        pairs.value[index + 1] = X.NONE
                window.change_property(prop, with_multiple, 32, pairs.value, onerror=error.CatchError(error.BadWindow))
        elif not self.serve_target(window, item.target, prop):
            prop = X.NONE
        window.send_event(event.SelectionNotify(time=item.time, requestor=window, selection=item.selection,
            target=item.target, property=prop), onerror=error.CatchError(error.BadWindow))
        self.connection.flush()

    def handle(self, item):
        if item.type == X.SelectionRequest and item.owner == self.window:
            try:
                self.serve(item)
            except error.BadWindow:
                pass
        elif item.type == X.SelectionClear and item.window == self.window and item.atom == self.atoms["CLIPBOARD"]:
            # An old clear must not discard the new data if we already own it again.
            if self.owner() != self.window:
                self.data = {}
        elif item.type == X.PropertyNotify and item.state == X.PropertyDelete:
            self.send_chunk(item.window, item.atom)
        if self.waiting is not None:
            predicate, future = self.waiting
            if not future.done() and predicate(item):
                future.set_result(item)

    def send_chunk(self, window, prop):
        pending = self.transfers.pop((window, prop), None)
        if pending is None:
            return
        _, value, offset = pending
        kind, size, data = value
        width = CHUNK_BYTES // (size // 8)
        window.change_property(prop, kind, size, data[offset:offset + width], onerror=error.CatchError(error.BadWindow))
        if offset < len(data):
            self.transfers[window, prop] = monotonic() + 2, value, offset + width
        self.connection.flush()

    def expire(self):
        now = monotonic()
        for key, (deadline, _, _) in tuple(self.transfers.items()):
            if deadline < now:
                del self.transfers[key]

    def keys(self, terminal):
        connection = self.connection
        if not connection.has_extension(xtest.extname):
            raise ClipboardUnavailable
        codes = [connection.keysym_to_keycode(XK.string_to_keysym(name))
                 for name in ("Control_L", "Shift_L", "v")]
        if not all(codes):
            raise ClipboardUnavailable
        control, shift, paste = codes
        blocked = {code for group in connection.get_modifier_mapping() for code in group if code}
        for name in ("Caps_Lock", "Num_Lock", "Scroll_Lock"):
            blocked.discard(connection.keysym_to_keycode(XK.string_to_keysym(name)))
        return ([control, shift, paste] if terminal else [control, paste]), blocked | {paste}

    def held(self, codes):
        bitmap = self.connection.query_keymap()
        return any(bitmap[code // 8] & (1 << (code % 8)) for code in codes)

    def set_data(self, data, timestamp):
        if data:
            self.window.set_selection_owner(self.atoms["CLIPBOARD"], timestamp)
        else:
            request.SetSelectionOwner(display=self.connection.display, window=X.NONE,
                                      selection=self.atoms["CLIPBOARD"], time=timestamp)
        self.data = data
        self.timestamp = timestamp

    def send_keys(self, chord):
        connection = self.connection
        opcode = connection.display.get_extension_major(xtest.extname)
        failed = error.CatchError()
        released = list(reversed(chord))
        for event_type, keys in ((X.KeyPress, chord), (X.KeyRelease, released)):
            for key in keys:
                xtest.FakeInput(display=connection.display, onerror=failed, opcode=opcode,
                                event_type=event_type, detail=key, time=X.CurrentTime, root=X.NONE, x=0, y=0)
        connection.sync()
        if failed.get_error() is not None:
            for key in released:
                connection.xtest_fake_input(X.KeyRelease, key)
            raise ClipboardUnavailable

    async def paste(self, text, focus, still_focused, terminal=False):
        async with self.delivery:
            result = await self.deliver(text, focus, still_focused, terminal)
        if result == PASTE_SENT:
            await asyncio.sleep(PASTE_SETTLE)
        return result

    async def deliver(self, text, focus, still_focused, terminal):
        connection = self.connection
        atoms = self.atoms
        if not focus or connection.get_input_focus().focus != focus or not still_focused():
            return "Focus changed. Use Copy last to paste the dictation."
        try:
            chord, blocked = self.keys(terminal)
            owner = self.owner()
            async with asyncio.timeout(READ_TIMEOUT):
                original = self.original if owner == self.window and self.restore_task else await self.snapshot(owner)
                if original is None:
                    raise ClipboardUnavailable
                timestamp = await self.server_time()
                while self.held(blocked):
                    await asyncio.sleep(.02)
        except (ClipboardUnavailable, TimeoutError, error.XError):
            return "Automatic paste was unavailable. Use Copy last to paste the dictation."

        content = text.encode()
        utf8 = atoms["UTF8_STRING"]
        try:
            with self.server_grab():
                if connection.get_input_focus().focus != focus or not still_focused():
                    return "Focus changed. Use Copy last to paste the dictation."
                if self.owner() != owner or self.held(blocked):
                    return "The keyboard or clipboard changed. Use Copy last to paste the dictation."
                transcript = {atoms[name]: (utf8 if name == "TEXT" else atoms[name], 8, content)
                              for name in ("UTF8_STRING", "TEXT", "text/plain", "text/plain;charset=utf-8")}
                transcript[Xatom.STRING] = Xatom.STRING, 8, text.encode("latin-1", errors="replace")
                self.set_data(transcript, timestamp)
                if self.owner() != self.window:
                    self.data = {}
                    raise ClipboardUnavailable
                if self.restore_task:
                    self.restore_task.cancel()
                self.original = original
                try:
                    self.send_keys(chord)
                except (ClipboardUnavailable, error.XError):
                    self.restore_task = None
                    self.restore(timestamp)
                    raise
                self.restore_task = asyncio.create_task(self.restore_after_delay(timestamp))
        except (ClipboardUnavailable, error.XError):
            return "Automatic paste was unavailable. Use Copy last to paste the dictation."
        return PASTE_SENT

    def restore(self, timestamp):
        original, self.original = self.original, None
        if original is not None and self.owner() == self.window:
            self.set_data(original, timestamp)

    async def restore_after_delay(self, timestamp):
        await asyncio.sleep(RESTORE_DELAY)
        async with self.delivery:
            if self.restore_task is not asyncio.current_task():
                return
            self.restore_task = None
            with suppress(OSError, error.DisplayError, error.ConnectionClosedError, error.XError):
                with self.server_grab():
                    self.restore(timestamp)

    async def wait_for_restore(self):
        if self.restore_task:
            await asyncio.shield(self.restore_task)

    async def hand_off(self):
        """Let the desktop clipboard manager retain restored data on quit."""
        atoms = self.atoms
        if not self.data or self.owner() != self.window or self.owner("CLIPBOARD_MANAGER") == X.NONE:
            return
        prop = atoms["_JARO_SELECTION"]
        self.window.change_property(prop, Xatom.ATOM, 32, list(self.data))
        self.window.convert_selection(atoms["CLIPBOARD_MANAGER"], atoms["SAVE_TARGETS"], prop, X.CurrentTime)
        try:
            async with asyncio.timeout(.4):
                await self.receive(lambda item: (item.type == X.SelectionNotify and item.requestor == self.window
                    and item.selection == atoms["CLIPBOARD_MANAGER"] and item.target == atoms["SAVE_TARGETS"]))
        except TimeoutError:
            pass
