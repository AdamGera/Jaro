"""Asynchronous XDG portal sessions attached to a single desktop bus owner."""
import asyncio
from contextlib import suppress
import os
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
