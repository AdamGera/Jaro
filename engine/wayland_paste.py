"""Keyboard and clipboard portal access for Wayland dictation paste."""
import asyncio
from contextlib import suppress
import os
import shutil

from dbus_fast import Variant
from dbus_fast.errors import DBusError

from linux_capture import CaptureError
from linux_desktop_portal import PATH, DesktopPortal, close_descriptors


REMOTE = "org.freedesktop.portal.RemoteDesktop"
CLIPBOARD = "org.freedesktop.portal.Clipboard"
KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_V = 47
CLIPBOARD_LIMIT = 32 * 1024 * 1024
RESTORE_DELAY = .5
IDLE_CLOSE_DELAY = 15.0
PASTE_FALLBACK = "Dictation is ready. Use Copy last to paste it into your app."
TRANSFER_ERRORS = (DBusError, OSError, TimeoutError, EOFError, CaptureError)
INITIAL_CLIPBOARD_TIMEOUT = .8
EMPTY_RESTORE_FORMAT = "application/octet-stream"


def is_gnome_desktop():
    return "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")


async def run_wl_tool(args, *, input_data=None, timeout=1.2):
    process = await asyncio.create_subprocess_exec(
        *args, env={**os.environ, "LC_ALL": "C"},
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate(input_data)
        return process.returncode, stdout, stderr
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


async def wl_snapshot():
    tool = shutil.which("wl-paste")
    if not tool:
        return None
    code, stdout, stderr = await run_wl_tool([tool, "--list-types"], timeout=INITIAL_CLIPBOARD_TIMEOUT)
    if code != 0:
        return {} if stderr.strip() == b"Nothing is copied" else None
    formats = [line for line in stdout.decode("utf-8", errors="ignore").splitlines() if line]
    preferred = [mime for mime in ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "text/html", "image/png")
                 if mime in formats]
    data = {}
    remaining = CLIPBOARD_LIMIT
    for mime in preferred[:4]:
        code, value, _ = await run_wl_tool([tool, "--no-newline", "--type", mime], timeout=.6)
        if code == 0 and len(value) <= remaining:
            data[mime] = value
            remaining -= len(value)
    return data if data or not formats else None


async def wl_copy_data(data):
    tool = shutil.which("wl-copy")
    if not tool:
        raise CaptureError("wl-copy is unavailable. Use Copy last to paste.")
    if not data:
        await run_wl_tool([tool, "--clear"], timeout=.8)
        return
    mime, payload = next(iter(data.items()))
    code, _, _ = await run_wl_tool([tool, "--type", mime], input_data=payload, timeout=.8)
    if code != 0:
        raise CaptureError(PASTE_FALLBACK)


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
        self.use_portal_clipboard = True
        self.use_wtype = False
        self.idle_closed = False
        self.idle_timer = None
        self.formats = None
        self.selection_revision = 0
        self.owns_selection = False
        self.data = {}
        self.changed = asyncio.Event()
        self.transfers = set()
        self.delivery = asyncio.Lock()

    @property
    def active(self):
        if self.use_wtype:
            return self.ready
        if self.idle_closed and self.restore_token:
            return self.ready
        return self.ready and self.session is not None and self.bus is not None and self.bus.connected

    def cancel_idle_close(self):
        if self.idle_timer is not None:
            self.idle_timer.cancel()
            self.idle_timer = None

    def schedule_idle_close(self):
        self.cancel_idle_close()
        # Keep KDE sessions open (KDE re-prompts on reopen); close idle GNOME sessions so the banner clears.
        if not self.use_wtype and self.restore_token and is_gnome_desktop() and self.session is not None:
            self.idle_timer = asyncio.get_running_loop().call_later(
                IDLE_CLOSE_DELAY, lambda: asyncio.create_task(self.close_idle_session())
            )

    async def close_idle_session(self):
        self.idle_timer = None
        if self.delivery.locked() or not self.ready or self.session is None or not self.restore_token:
            return
        async with self.delivery:
            if not self.ready or self.session is None or not self.restore_token:
                return
            self.closing = True
            try:
                await super().close()
                self.formats = None
                self.owns_selection = False
                self.data = {}
                self.idle_closed = True
            finally:
                self.closing = False

    async def approve(self, restore_token=""):
        self.cancel_idle_close()
        self.idle_closed = False
        self.use_wtype = False
        try:
            await self.connect()
            devices = await self.property(REMOTE, "AvailableDeviceTypes")
        except DBusError as failure:
            if shutil.which("wtype") and shutil.which("wl-copy"):
                self.use_wtype = True
                self.use_portal_clipboard = False
                self.ready = True
                return
            raise CaptureError("This desktop doesn't offer keyboard access for paste.") from failure
        if not devices & KEYBOARD:
            raise CaptureError("This desktop doesn't offer keyboard access for paste.")
        portal_clipboard = True
        try:
            await self.property(CLIPBOARD, "version")
        except DBusError:
            portal_clipboard = False
        if not portal_clipboard and not shutil.which("wl-copy"):
            raise CaptureError("This desktop doesn't offer clipboard access for paste. Install wl-clipboard or use Copy last.")
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
        if not has_keyboard or (portal_clipboard and not has_portal_clipboard and not shutil.which("wl-copy")):
            raise CaptureError("Paste needs both keyboard and clipboard permission.")
        self.use_portal_clipboard = has_portal_clipboard
        token = approved.get("restore_token")
        if version >= 2 and token is not None and token.signature == "s" and 0 < len(token.value) <= 1024:
            self.restore_token = token.value
        # Some desktops announce only clipboard changes. Check once during
        # interactive approval, before a take targets an external editor.
        revision = self.selection_revision
        if (self.use_portal_clipboard and self.formats is None and not restore_token
                and await clipboard_is_empty() and self.session is not None and self.selection_revision == revision):
            self.formats = ()
        self.ready = self.session is not None
        self.schedule_idle_close()

    def revoke(self):
        self.cancel_idle_close()
        self.ready = False
        self.idle_closed = False
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
                if self.active and self.owns_selection:
                    await self.publish({})
                return
            if self.formats != (EMPTY_RESTORE_FORMAT,) or not self.active or not self.owns_selection:
                return
        await self.publish(original)

    async def key(self, keycode, pressed):
        await self.call(REMOTE, "NotifyKeyboardKeycode", "oa{sv}iu",
                        [self.session, {}, keycode, int(pressed)])

    async def paste_keys(self, terminal, allowed):
        if self.use_wtype:
            if not allowed():
                return False
            args = [shutil.which("wtype"), "-M", "ctrl"]
            if terminal:
                args.extend(["-M", "shift"])
            args.extend(["-k", "v"])
            if terminal:
                args.extend(["-m", "shift"])
            args.extend(["-m", "ctrl"])
            code, _, _ = await run_wl_tool(args, timeout=1.2)
            return code == 0
        keys = (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V) if terminal else (KEY_LEFTCTRL, KEY_V)
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
            self.cancel_idle_close()
            if self.idle_closed and self.restore_token:
                try:
                    await self.approve(self.restore_token)
                except TRANSFER_ERRORS:
                    self.ready = False
                    self.idle_closed = False
                    return PASTE_FALLBACK
            if not self.active or not can_send():
                return PASTE_FALLBACK
            try:
                if not self.use_portal_clipboard:
                    return await self.deliver_with_wl_clipboard(text, terminal, can_send)
                return await self.deliver(text, terminal, can_send)
            except CaptureError as failure:
                return str(failure)
            except TRANSFER_ERRORS:
                return PASTE_FALLBACK
            finally:
                self.schedule_idle_close()

    async def deliver_with_wl_clipboard(self, text, terminal, can_send):
        original = await wl_snapshot()
        if not can_send() or not self.active:
            return PASTE_FALLBACK
        encoded = text.encode()
        await wl_copy_data({"text/plain;charset=utf-8": encoded})
        try:
            if not await self.paste_keys(terminal, lambda: can_send() and self.active):
                return PASTE_FALLBACK
            await asyncio.sleep(RESTORE_DELAY)
        finally:
            if original is not None:
                with suppress(*TRANSFER_ERRORS):
                    current = await wl_snapshot()
                    if current is not None and any(value == encoded for value in current.values()):
                        await wl_copy_data(original)
        return "Paste shortcut sent."

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
            await self.wait_for_selection(revision)
            if not self.owns_selection or set(self.formats or ()) != transcript.keys() or not can_send() or not self.active:
                return PASTE_FALLBACK
            claimed = self.selection_revision
            if not await self.paste_keys(terminal, lambda: (claimed == self.selection_revision and self.owns_selection
                                                          and can_send() and self.active)):
                return PASTE_FALLBACK
            await asyncio.sleep(RESTORE_DELAY)
        finally:
            await self.restore(original)
        return "Paste shortcut sent."

    async def close(self):
        self.cancel_idle_close()
        self.closing = True
        self.ready = False
        self.idle_closed = False
        self.changed.set()
        async with self.delivery:
            for task in tuple(self.transfers):
                task.cancel()
            await asyncio.gather(*tuple(self.transfers), return_exceptions=True)
            await super().close()
            self.data = {}
