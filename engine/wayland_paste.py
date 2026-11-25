"""Keyboard and clipboard portal access for Wayland dictation paste."""
import asyncio
from contextlib import suppress
import os
import shutil

from dbus_fast import Variant
from dbus_fast.errors import DBusError

from linux_capture import CaptureError
from linux_desktop_portal import PATH, DesktopPortal, close_descriptors
from wayland_clipboard import ClipboardLease, clipboard_capabilities, paste_keycodes, run_tool


REMOTE = "org.freedesktop.portal.RemoteDesktop"
CLIPBOARD = "org.freedesktop.portal.Clipboard"
KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
CONTROL_L = 0xffe3
SHIFT_L = 0xffe1
LATIN_V = ord("v")
CLIPBOARD_LIMIT = 32 * 1024 * 1024
RESTORE_DELAY = .5
PASTE_SETTLE = .1
PASTE_SENT = "Paste shortcut sent."
PASTE_FALLBACK = "Dictation is ready. Use Copy last to paste it into your app."
TRANSFER_ERRORS = (DBusError, OSError, TimeoutError, EOFError, CaptureError)
INITIAL_CLIPBOARD_TIMEOUT = .8
EMPTY_RESTORE_FORMAT = "application/octet-stream"


async def clipboard_is_empty():
    tool = shutil.which("wl-paste") if os.environ.get("WAYLAND_DISPLAY") else None
    if tool is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            tool, "--list-types", env={**os.environ, "LC_ALL": "C"}, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, limit=512,
        )
    except OSError:
        return False
    try:
        async with asyncio.timeout(INITIAL_CLIPBOARD_TIMEOUT):
            report = await process.stderr.readline()
            return (report == b"Nothing is copied\n" and not await process.stderr.read(1)
                    and await process.wait() == 1)
    except (OSError, TimeoutError, ValueError):
        return False
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.communicate()


async def fd_ready(fd, *, writing=False):
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    watch, remove = (loop.add_writer, loop.remove_writer) if writing else (loop.add_reader, loop.remove_reader)
    watch(fd, ready.set)
    try:
        await ready.wait()
    finally:
        remove(fd)


async def read_clipboard(fd, limit):
    os.set_blocking(fd, False)
    data = bytearray()
    while True:
        try:
            block = os.read(fd, min(65536, limit - len(data) + 1))
        except BlockingIOError:
            await fd_ready(fd)
            continue
        if not block:
            return bytes(data)
        data.extend(block)
        if len(data) > limit:
            raise CaptureError("The clipboard is too large to preserve. Use Copy last to paste.")


async def write_clipboard(fd, data):
    os.set_blocking(fd, False)
    data = memoryview(data)
    while data:
        try:
            count = os.write(fd, data)
        except BlockingIOError:
            await fd_ready(fd, writing=True)
            continue
        data = data[count:]


def selection_formats(value):
    if value is None:
        return ()
    if value.signature == "as":
        return tuple(value.value)
    # Older GNOME portals forward Mutter's one-field structure as-is.
    if value.signature == "(as)":
        return tuple(value.value[0])
    return None


class WaylandPaste(DesktopPortal):
    def __init__(self, revoked):
        super().__init__(self.revoke, negotiate_fds=True)
        self.revoked = revoked
        self.ready = False
        self.closing = False
        self.restore_token = ""
        self.compositor_clipboard = None
        self.wtype = None
        self.formats = None
        self.selection_revision = 0
        self.owns_selection = False
        self.data = {}
        self.changed = asyncio.Event()
        self.transfers = set()
        self.delivery = asyncio.Lock()
        self.restore_task = None
        self.original_selection = None
        self.transcript_revision = None
        self.clipboard_lease = None

    @property
    def active(self):
        if self.wtype:
            return self.ready
        return self.ready and self.session is not None and self.bus is not None and self.bus.connected

    async def approve(self, restore_token="", *, virtual_only=False):
        self.compositor_clipboard, virtual_keyboard = await clipboard_capabilities()
        self.wtype = shutil.which("wtype") if virtual_keyboard and not restore_token else None
        if self.wtype:
            self.ready = True
            return
        if virtual_only:
            raise CaptureError("Saved automatic paste is unavailable on this desktop. Choose Allow paste to approve it here.")
        try:
            await self.connect()
            devices = await self.property(REMOTE, "AvailableDeviceTypes")
        except DBusError as failure:
            raise CaptureError("This desktop doesn't offer keyboard access for paste. Use Copy last to paste.") from failure
        if not devices & KEYBOARD:
            raise CaptureError("This desktop doesn't offer keyboard access for paste. Use Copy last to paste.")
        portal_clipboard = False
        if self.compositor_clipboard is None:
            try:
                await self.property(CLIPBOARD, "version")
                portal_clipboard = True
            except DBusError as failure:
                raise CaptureError("This desktop doesn't offer clipboard access for paste. Use Copy last to paste.") from failure
        version = 0
        with suppress(DBusError):
            version = await self.property(REMOTE, "version")
        options = {"types": Variant("u", KEYBOARD)}
        if version >= 2:
            options["persist_mode"] = Variant("u", PERSIST_UNTIL_REVOKED)
            if restore_token:
                options["restore_token"] = Variant("s", restore_token)
        elif restore_token:
            raise CaptureError("The desktop could not restore automatic paste. Choose Allow paste to approve it again.")
        await self.create_session(REMOTE)
        await self.request(REMOTE, "SelectDevices", "oa{sv}", [self.session], options)
        if portal_clipboard:
            await self.call(CLIPBOARD, "RequestClipboard", "oa{sv}", [self.session, {}])
        approved = await self.request(REMOTE, "Start", "osa{sv}", [self.session, ""], seconds=90)
        devices, clipboard = approved.get("devices"), approved.get("clipboard_enabled")
        has_keyboard = self.session is not None and devices is not None and devices.signature == "u" and bool(devices.value & KEYBOARD)
        has_portal_clipboard = portal_clipboard and clipboard is not None and clipboard.signature == "b" and clipboard.value
        if not has_keyboard or (not has_portal_clipboard and self.compositor_clipboard is None):
            raise CaptureError("Paste needs both keyboard and clipboard permission. Use Copy last to paste.")
        token = approved.get("restore_token")
        if version >= 2 and token is not None and token.signature == "s" and 0 < len(token.value) <= 1024:
            self.restore_token = token.value
        # Some desktops announce only clipboard changes. Check once during
        # interactive approval, before a take targets an external editor.
        revision = self.selection_revision
        if (has_portal_clipboard and self.formats is None and not restore_token
                and await clipboard_is_empty() and self.session is not None and self.selection_revision == revision):
            self.formats = ()
        # Closing the portal also releases any clipboard selection it restored.
        # Keep the session alive until the user disables paste or exits Jaro.
        self.ready = self.session is not None

    def revoke(self):
        self.ready = False
        self.formats = None
        self.owns_selection = False
        self.data = {}
        self.changed.set()
        if not self.closing:
            self.revoked()

    def signal(self, message):
        if not super().signal(message) or message.interface != CLIPBOARD or message.path != PATH:
            return
        if not message.body or message.body[0] != self.session:
            return
        if message.member == "SelectionOwnerChanged" and message.signature == "oa{sv}":
            options = message.body[1]
            self.formats = selection_formats(options.get("mime_types"))
            owner = options.get("session_is_owner")
            self.owns_selection = owner is not None and owner.signature == "b" and owner.value
            self.selection_revision += 1
            self.changed.set()
        elif message.member == "SelectionTransfer" and message.signature == "osu":
            _, mime, serial = message.body
            task = asyncio.create_task(self.serve(serial, self.data.get(mime)))
            self.transfers.add(task)
            task.add_done_callback(self.transfers.discard)

    async def descriptor(self, member, signature, args):
        reply = await self.reply(CLIPBOARD, member, signature, [self.session, *args], fd=True)
        if reply.signature == "h" and reply.body == [0] and len(reply.unix_fds) == 1:
            return reply.unix_fds[0]
        close_descriptors(reply)
        raise CaptureError("The desktop didn't provide a clipboard transfer.")

    async def serve(self, serial, data):
        written = False
        try:
            if data is not None:
                async with asyncio.timeout(2):
                    fd = await self.descriptor("SelectionWrite", "ou", [serial])
                    try:
                        await write_clipboard(fd, data)
                        written = True
                    finally:
                        os.close(fd)
        except TRANSFER_ERRORS:
            pass
        finally:
            if self.session is not None:
                with suppress(*TRANSFER_ERRORS):
                    await self.call(CLIPBOARD, "SelectionWriteDone", "oub", [self.session, serial, written])

    async def snapshot(self):
        if self.formats is None:
            revision = self.selection_revision
            if await clipboard_is_empty() and self.session is not None and self.selection_revision == revision:
                self.formats = ()
            else:
                raise CaptureError("The desktop hasn't reported the clipboard yet. Use Copy last to paste.")
        if self.owns_selection:
            return self.data
        formats = tuple(dict.fromkeys(self.formats))
        if len(formats) > 128:
            raise CaptureError("The clipboard has too many formats to preserve. Use Copy last to paste.")
        remaining = CLIPBOARD_LIMIT
        data = {}
        async with asyncio.timeout(1.2):
            # Some implementations support one active read at a time.
            for mime in formats:
                try:
                    fd = await self.descriptor("SelectionRead", "os", [mime])
                except TRANSFER_ERRORS:
                    continue
                try:
                    data[mime] = await read_clipboard(fd, remaining)
                    remaining -= len(data[mime])
                except CaptureError:
                    raise
                except TRANSFER_ERRORS:
                    continue
                finally:
                    os.close(fd)
        return data

    async def publish(self, data):
        options = {"mime_types": Variant("as", list(data))} if data else {}
        previous = self.data
        self.data = data
        try:
            await self.call(CLIPBOARD, "SetSelection", "oa{sv}", [self.session, options])
        except TRANSFER_ERRORS:
            self.data = previous
            raise

    async def wait_for_selection(self, revision):
        async with asyncio.timeout(.45):
            while self.selection_revision == revision and self.active:
                await self.changed.wait()
                self.changed.clear()

    async def restore(self, original):
        if self.session is None or not self.owns_selection:
            return
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
        if not original and "gnome" in desktop:
            # Mutter keeps a copy of the latest text selection after its owner
            # releases it. An empty binary offer drops that stored text.
            revision = self.selection_revision
            self.changed.clear()
            await self.publish({EMPTY_RESTORE_FORMAT: b""})
            try:
                await self.wait_for_selection(revision)
            except TimeoutError:
                # Releasing this session's offer cannot clear another owner.
                if not self.active or not self.owns_selection:
                    return
            else:
                if self.formats != (EMPTY_RESTORE_FORMAT,) or not self.active or not self.owns_selection:
                    return
        revision = self.selection_revision
        await self.publish(original)
        try:
            await self.wait_for_selection(revision)
        except TimeoutError:
            # A slow restore notification must not be mistaken for the next paste's offer.
            if self.selection_revision == revision:
                self.formats = None
                self.owns_selection = False

    async def key(self, method, value, pressed):
        await self.call(REMOTE, method, "oa{sv}iu", [self.session, {}, value, int(pressed)])

    async def kde_layout(self):
        try:
            async with asyncio.timeout(.25):
                group, = await self.call("org.kde.KeyboardLayouts", "getLayout", path="/Layouts",
                                         destination="org.kde.keyboard")
            return group if type(group) is int and 0 <= group <= 65535 else None
        except (DBusError, OSError, TimeoutError, ValueError):
            return None

    async def portal_chord(self, terminal):
        codes = await paste_keycodes()
        kde = "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
        if codes is not None:
            control, shift, letter, paste = codes
            ambiguous = control < 0 or letter < 0 or (terminal and shift < 0)
            # Plasma's portal can resolve key symbols against a different layout group.
            if kde and ambiguous and (group := await self.kde_layout()) is not None:
                active_codes = await paste_keycodes(group)
                if active_codes is not None and await self.kde_layout() == group:
                    control, shift, letter, paste = active_codes
            if control > 0 and letter > 0 and (not terminal or shift > 0):
                keys = (control, shift, letter) if terminal else (control, letter)
                return "NotifyKeyboardKeycode", keys
            if letter == 0 or kde:
                # A dedicated Paste key uses the clipboard on layouts without Latin V.
                return "NotifyKeyboardKeycode", (paste,) if paste > 0 else ()
        elif kde:
            return "NotifyKeyboardKeycode", ()
        keys = (CONTROL_L, SHIFT_L, LATIN_V) if terminal else (CONTROL_L, LATIN_V)
        return "NotifyKeyboardKeysym", keys

    async def paste_keys(self, terminal, allowed):
        if self.wtype:
            if not allowed():
                return False
            args = [self.wtype, "-M", "ctrl"]
            if terminal:
                args.extend(["-M", "shift"])
            args.extend(["-k", "v"])
            if terminal:
                args.extend(["-m", "shift"])
            args.extend(["-m", "ctrl"])
            return await run_tool(*args) is not None
        method, keys = await self.portal_chord(terminal)
        if not keys:
            return False
        pressed = []
        try:
            for key in keys:
                if not allowed():
                    return False
                pressed.append(key)
                await self.key(method, key, True)
        finally:
            for key in reversed(pressed):
                with suppress(*TRANSFER_ERRORS):
                    if self.session is not None:
                        await self.key(method, key, False)
        return True

    async def paste(self, text, terminal, can_send, *, confirm=None):
        async with self.delivery:
            if self.closing or not self.active or not can_send():
                return PASTE_FALLBACK
            try:
                if self.compositor_clipboard is not None:
                    result = await self.deliver_with_data_control(text, terminal, can_send, confirm)
                else:
                    result = await self.deliver(text, terminal, can_send, confirm)
                if result == PASTE_SENT:
                    await asyncio.sleep(PASTE_SETTLE)
                return result
            except CaptureError as failure:
                return str(failure)
            except TRANSFER_ERRORS:
                return PASTE_FALLBACK

    async def deliver_with_data_control(self, text, terminal, can_send, confirm):
        self.cancel_restore()
        sent = False
        try:
            if self.clipboard_lease is not None and not self.clipboard_lease.owned():
                await self.close_lease()
            if self.clipboard_lease is None:
                self.clipboard_lease = await ClipboardLease.start(self.compositor_clipboard)
            lease = self.clipboard_lease

            def allowed():
                return lease.owned() and can_send() and self.active and not self.closing

            if not await lease.offer(text.encode()) or not allowed() or (confirm is not None and not await confirm()):
                return PASTE_FALLBACK
            if not await self.paste_keys(terminal, allowed):
                return PASTE_FALLBACK
            sent = True
            self.schedule_restore(self.close_lease)
            return PASTE_SENT
        finally:
            if not sent:
                await self.close_lease()

    async def close_lease(self):
        lease, self.clipboard_lease = self.clipboard_lease, None
        if lease is not None:
            await lease.close()

    async def deliver(self, text, terminal, can_send, confirm):
        revision = self.selection_revision
        if self.owns_selection and revision == self.transcript_revision:
            original = self.original_selection
        else:
            original = await self.snapshot()
        if revision != self.selection_revision or not can_send() or not self.active or self.closing:
            return PASTE_FALLBACK
        self.cancel_restore()
        self.original_selection = self.transcript_revision = None
        encoded = text.encode()
        transcript = {"text/plain;charset=utf-8": encoded, "text/plain": encoded, "UTF8_STRING": encoded}
        self.changed.clear()
        sent = False
        try:
            await self.publish(transcript)
            await self.wait_for_selection(revision)
            if not self.owns_selection or set(self.formats or ()) != transcript.keys() or not can_send() or not self.active or self.closing:
                return PASTE_FALLBACK
            claimed = self.selection_revision
            if confirm is not None and not await confirm():
                return PASTE_FALLBACK
            if not await self.paste_keys(terminal, lambda: (claimed == self.selection_revision and self.owns_selection
                                                          and can_send() and self.active and not self.closing)):
                return PASTE_FALLBACK
            self.original_selection = original
            self.transcript_revision = claimed
            sent = True
            self.schedule_restore(self.restore_portal_selection)
            return PASTE_SENT
        finally:
            if not sent:
                await self.restore(original)

    def cancel_restore(self):
        if self.restore_task is not None:
            self.restore_task.cancel()
            self.restore_task = None

    def schedule_restore(self, restore):
        async def later():
            await asyncio.sleep(RESTORE_DELAY)
            async with self.delivery:
                if self.restore_task is asyncio.current_task():
                    self.restore_task = None
                    with suppress(*TRANSFER_ERRORS):
                        await restore()

        self.restore_task = asyncio.create_task(later())

    async def restore_portal_selection(self):
        original, self.original_selection = self.original_selection, None
        revision, self.transcript_revision = self.transcript_revision, None
        if original is not None and revision == self.selection_revision:
            await self.restore(original)

    async def wait_for_restore(self):
        async with self.delivery:
            task = self.restore_task
        if task is not None:
            await asyncio.shield(task)

    async def close(self):
        self.closing = True
        await self.wait_for_restore()
        self.ready = False
        self.changed.set()
        async with self.delivery:
            await self.close_lease()
            for task in tuple(self.transfers):
                task.cancel()
            await asyncio.gather(*tuple(self.transfers), return_exceptions=True)
            await super().close()
            self.data = {}
