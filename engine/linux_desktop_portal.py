"""Asynchronous XDG portal sessions attached to a single desktop bus owner."""
import asyncio
from contextlib import suppress
import os
import stat
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError

from linux_capture import CaptureError


DESKTOP = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
DBUS = "org.freedesktop.DBus"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
REGISTRY = "org.freedesktop.host.portal.Registry"
SCREENSHOT = "org.freedesktop.portal.Screenshot"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
VIDEO_SOURCES = {"display": 1, "window": 2}
SCREENSHOT_TARGETS = {"area": 4, "window": 2, "display": 1}
MAX_SCREENSHOT_BYTES = 30 * 1024 * 1024
APP_ID = "local.jardaudio.desktop"


def token():
    return "jaro_" + uuid4().hex


def close_descriptors(reply):
    for fd in reply.unix_fds:
        os.close(fd)


def close_abandoned_reply(task):
    if not task.cancelled() and task.exception() is None:
        close_descriptors(task.result())


class PortalResponseError(CaptureError):
    def __init__(self, code, result):
        super().__init__("Desktop approval was canceled." if code == 1 else "The desktop declined the request.")
        self.code = code
        self.result = result


class DesktopPortal:
    def __init__(self, closed, *, negotiate_fds=False):
        self.closed = closed
        self.negotiate_fds = negotiate_fds
        self.bus = None
        self.owner = None
        self.session = None
        self.pending = {}

    async def reply(self, interface, member, signature="", body=None, *, path=PATH, destination=None, fd=False):
        target = destination or self.owner or DESKTOP
        message = Message(destination=target, path=path, interface=interface,
                          member=member, signature=signature, body=body or [])
        async with asyncio.timeout(4):
            if fd:
                task = asyncio.create_task(self.bus.call(message))
                try:
                    reply = await asyncio.shield(task)
                except asyncio.CancelledError:
                    task.add_done_callback(close_abandoned_reply)
                    raise
            else:
                reply = await self.bus.call(message)
        if reply.message_type is MessageType.ERROR:
            close_descriptors(reply)
            raise DBusError(reply.error_name, reply.body[0] if reply.body else member)
        return reply

    async def call(self, interface, member, signature="", body=None, *, path=PATH, destination=None):
        reply = await self.reply(interface, member, signature, body, path=path, destination=destination)
        close_descriptors(reply)
        return reply.body

    async def dbus(self, member, signature, body):
        return await self.call(DBUS, member, signature, body, path="/org/freedesktop/DBus", destination=DBUS)

    async def connect(self):
        self.bus = MessageBus(negotiate_unix_fd=self.negotiate_fds)
        async with asyncio.timeout(4):
            await self.bus.connect()
        # Activate the desktop service and pin both method replies and signals to that owner.
        try:
            self.owner, = await self.dbus("GetNameOwner", "s", [DESKTOP])
        except DBusError as failure:
            if failure.type != DBUS + ".Error.NameHasNoOwner":
                raise
            await self.dbus("StartServiceByName", "su", [DESKTOP, 0])
            self.owner, = await self.dbus("GetNameOwner", "s", [DESKTOP])
        self.bus.add_message_handler(self.signal)
        await self.dbus("AddMatch", "s", [f"type='signal',sender='{self.owner}',path_namespace='{PATH}'"])
        await self.dbus("AddMatch", "s", [f"type='signal',sender='{DBUS}',interface='{DBUS}',member='NameOwnerChanged',arg0='{DESKTOP}'"])
        # Hosts without Registry support still supply their desktop association themselves.
        try:
            await self.call(REGISTRY, "Register", "sa{sv}", [APP_ID, {}])
        except DBusError as failure:
            if failure.type not in (DBUS + ".Error.UnknownMethod", DBUS + ".Error.UnknownInterface"):
                raise

    async def property(self, interface, name):
        property_value, = await self.call(DBUS + ".Properties", "Get", "ss", [interface, name])
        return property_value.value

    async def create_session(self, interface):
        ready = await self.request(interface, "CreateSession", "a{sv}", [],
                                   {"session_handle_token": Variant("s", token())})
        session = ready.get("session_handle")
        if session is None or not session.value.startswith(PATH + "/session/"):
            raise CaptureError("The desktop returned an invalid session.")
        self.session = session.value

    async def request(self, interface, member, signature, args, options=None, *, seconds=8):
        handle = token()
        sender = self.bus.unique_name[1:].replace(".", "_")
        path = f"{PATH}/request/{sender}/{handle}"
        future = asyncio.get_running_loop().create_future()
        self.pending[path] = future
        actual = path
        try:
            actual, = await self.call(interface, member, signature, [*args, {**(options or {}),
                                                                            "handle_token": Variant("s", handle)}])
            if actual != path:
                raise CaptureError("The desktop returned an unexpected portal request.")
            async with asyncio.timeout(seconds):
                code, result = await future
        except (asyncio.CancelledError, TimeoutError, CaptureError):
            with suppress(DBusError, TimeoutError, OSError, EOFError):
                await self.call(REQUEST, "Close", path=actual)
            raise
        finally:
            self.pending.pop(path, None)
        if code != 0:
            raise PortalResponseError(code, result)
        return result

    def signal(self, message):
        if message.message_type is not MessageType.SIGNAL:
            return False
        if (message.sender == DBUS and message.interface == DBUS and message.member == "NameOwnerChanged"
                and message.signature == "sss" and message.body[:2] == [DESKTOP, self.owner]):
            self.lose_session()
            return False
        if message.sender != self.owner:
            return False
        if message.interface == REQUEST and message.member == "Response" and message.signature == "ua{sv}":
            future = self.pending.get(message.path)
            if future is not None and not future.done():
                future.set_result(message.body)
        elif message.interface == SESSION and message.member == "Closed" and message.path == self.session:
            self.lose_session()
        return self.session is not None

    def lose_session(self):
        self.session = None
        for response in self.pending.values():
            response.cancel()
        self.closed()

    async def close(self):
        if self.bus is not None:
            if self.session is not None and self.bus.connected:
                with suppress(DBusError, TimeoutError, OSError, EOFError):
                    await self.call(SESSION, "Close", path=self.session)
            self.session = None
            self.bus.disconnect()
            self.bus = None


class ScreenshotPortal(DesktopPortal):
    def __init__(self):
        super().__init__(lambda: None)

    async def targets(self):
        version = await self.property(SCREENSHOT, "version")
        if version < 2:
            return []
        if version < 3:
            return ["picker"]
        targets = await self.property(SCREENSHOT, "AvailableTargets")
        return [source for source, bit in SCREENSHOT_TARGETS.items() if targets & bit] or ["picker"]

    async def screenshot(self, source):
        if source not in await self.targets():
            raise CaptureError("That screenshot choice is unavailable on this desktop.")
        options = {"interactive": Variant("b", True)}
        if source != "picker":
            options["target"] = Variant("u", SCREENSHOT_TARGETS[source])
        try:
            result = await self.request(SCREENSHOT, "Screenshot", "sa{sv}", [""], options, seconds=90)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise CaptureError("The desktop screenshot service disconnected.") from None
        uri = result.get("uri")
        if uri is None or uri.signature != "s":
            raise CaptureError("The desktop did not return a screenshot.")
        return uri.value


class ScreenCastPortal(DesktopPortal):
    def __init__(self, closed):
        super().__init__(closed, negotiate_fds=True)
        self.display_properties = None

    async def sources(self):
        available = await self.property(SCREENCAST, "AvailableSourceTypes")
        return [source for source, bit in VIDEO_SOURCES.items() if available & bit]

    async def select(self, source):
        if source not in await self.sources():
            raise CaptureError("That source is unavailable for screen recording.")
        await self.create_session(SCREENCAST)
        options = {"types": Variant("u", VIDEO_SOURCES[source]), "multiple": Variant("b", False)}
        if await self.property(SCREENCAST, "AvailableCursorModes") & 2:
            options["cursor_mode"] = Variant("u", 2)
        await self.request(SCREENCAST, "SelectSources", "oa{sv}", [self.session], options)
        result = await self.request(SCREENCAST, "Start", "osa{sv}", [self.session, ""], seconds=90)
        streams = result.get("streams")
        if streams is None or streams.signature != "a(ua{sv})" or len(streams.value) != 1:
            raise CaptureError("The desktop did not provide a screen recording stream.")
        node, properties = streams.value[0]
        self.display_properties = properties if source == "display" else None
        serial = properties.get("pipewire-serial")
        return f"target-object={serial.value}" if serial is not None and serial.signature == "t" else f"path={node}"

    async def open_remote(self):
        if self.session is None:
            raise CaptureError("The desktop ended screen sharing.")
        reply = await self.reply(SCREENCAST, "OpenPipeWireRemote", "oa{sv}", [self.session, {}], fd=True)
        try:
            if reply.signature != "h" or len(reply.body) != 1 or not 0 <= reply.body[0] < len(reply.unix_fds):
                raise CaptureError("The desktop did not provide a PipeWire connection.")
            return os.dup(reply.unix_fds[reply.body[0]])
        finally:
            close_descriptors(reply)


def read_screenshot(uri):
    url = urlsplit(uri)
    path = unquote(url.path, errors="strict")
    if (url.scheme != "file" or url.netloc not in ("", "localhost") or url.query or url.fragment
            or not os.path.isabs(path) or "\0" in path):
        raise CaptureError("The desktop returned an invalid screenshot location.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise CaptureError("The desktop returned a screenshot Jaro cannot open.")
        if info.st_size > MAX_SCREENSHOT_BYTES:
            raise CaptureError("The screenshot exceeds the 30 MB size limit.")
        image = file.read(MAX_SCREENSHOT_BYTES + 1)
    if len(image) > MAX_SCREENSHOT_BYTES:
        raise CaptureError("The screenshot exceeds the 30 MB size limit.")
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return image, "image/png"
    if image.startswith(b"\xff\xd8\xff"):
        return image, "image/jpeg"
    raise CaptureError("The desktop returned an unsupported screenshot format.")
