"""Keyboard and clipboard portal access for Wayland dictation paste."""
import asyncio
from contextlib import suppress
import os

from dbus_fast import Variant
from dbus_fast.errors import DBusError

from linux_capture import CaptureError
from linux_desktop_portal import PATH, DesktopPortal, close_descriptors


REMOTE = "org.freedesktop.portal.RemoteDesktop"
CLIPBOARD = "org.freedesktop.portal.Clipboard"
KEYBOARD = 1
CLIPBOARD_LIMIT = 32 * 1024 * 1024
RESTORE_DELAY = .5
PASTE_FALLBACK = "Dictation is ready. Use Copy last to paste it into your app."
TRANSFER_ERRORS = (DBusError, OSError, TimeoutError, EOFError, CaptureError)


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
        self.formats = None
        self.selection_revision = 0
        self.owns_selection = False
        self.data = {}
        self.changed = asyncio.Event()
        self.transfers = set()
        self.delivery = asyncio.Lock()

    @property
    def active(self):
        return self.ready and self.session is not None and self.bus is not None and self.bus.connected

    async def approve(self):
        await self.connect()
        try:
            devices = await self.property(REMOTE, "AvailableDeviceTypes")
            await self.property(CLIPBOARD, "version")
        except DBusError as failure:
            raise CaptureError("This desktop doesn't offer keyboard and clipboard access for paste.") from failure
        if not devices & KEYBOARD:
            raise CaptureError("This desktop doesn't offer keyboard access for paste.")
        await self.create_session(REMOTE)
        await self.request(REMOTE, "SelectDevices", "oa{sv}", [self.session], {"types": Variant("u", KEYBOARD)})
        await self.call(CLIPBOARD, "RequestClipboard", "oa{sv}", [self.session, {}])
        approved = await self.request(REMOTE, "Start", "osa{sv}", [self.session, ""], seconds=90)
        if ("devices" not in approved or not approved["devices"].value & KEYBOARD
                or "clipboard_enabled" not in approved or not approved["clipboard_enabled"].value):
            raise CaptureError("Paste needs both keyboard and clipboard permission.")
        self.ready = True

    def revoke(self):
        self.ready = False
        self.formats = None
        self.owns_selection = False
        self.data = {}
        self.changed.set()
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
                fd = await self.descriptor("SelectionRead", "os", [mime])
                try:
                    data[mime] = await read_clipboard(fd, remaining)
                    remaining -= len(data[mime])
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

    async def key(self, keysym, pressed):
        await self.call(REMOTE, "NotifyKeyboardKeysym", "oa{sv}iu",
                        [self.session, {}, keysym, int(pressed)])

    async def paste_keys(self, terminal, allowed):
        keys = (0xffe3, 0xffe1, ord("v")) if terminal else (0xffe3, ord("v"))
        pressed = []
        try:
            for key in keys:
                if not allowed():
                    return False
                pressed.append(key)
                await self.key(key, True)
        finally:
            for key in reversed(pressed):
                with suppress(*TRANSFER_ERRORS):
                    if self.session is not None:
                        await self.key(key, False)
        return True

    async def paste(self, text, terminal, can_send):
        async with self.delivery:
            if not self.active or not can_send():
                return PASTE_FALLBACK
            try:
                return await self.deliver(text, terminal, can_send)
            except CaptureError as failure:
                return str(failure)
            except TRANSFER_ERRORS:
                return PASTE_FALLBACK

    async def deliver(self, text, terminal, can_send):
        revision = self.selection_revision
        original = await self.snapshot()
        if revision != self.selection_revision or not can_send() or not self.active:
            return PASTE_FALLBACK
        encoded = text.encode()
        transcript = {"text/plain;charset=utf-8": encoded, "text/plain": encoded, "UTF8_STRING": encoded}
        self.changed.clear()
        try:
            await self.publish(transcript)
            async with asyncio.timeout(.45):
                while self.selection_revision == revision and self.active:
                    await self.changed.wait()
                    self.changed.clear()
            if not self.owns_selection or set(self.formats or ()) != transcript.keys() or not can_send() or not self.active:
                return PASTE_FALLBACK
            claimed = self.selection_revision
            if not await self.paste_keys(terminal, lambda: (claimed == self.selection_revision and self.owns_selection
                                                          and can_send() and self.active)):
                return PASTE_FALLBACK
            await asyncio.sleep(RESTORE_DELAY)
        finally:
            if self.session is not None and self.owns_selection:
                await self.publish(original)
        return "Paste shortcut sent."

    async def close(self):
        self.ready = False
        self.changed.set()
        async with self.delivery:
            for task in tuple(self.transfers):
                task.cancel()
            await asyncio.gather(*tuple(self.transfers), return_exceptions=True)
            await super().close()
            self.data = {}
