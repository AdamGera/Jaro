"""Exercise the shortcut protocol on an isolated D-Bus session."""
import asyncio
from importlib.util import find_spec
import os
import select
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch


if sys.platform == "linux" and find_spec("dbus_fast"):
    from dbus_fast import Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    from linux_capture import CaptureError
    from linux_desktop_portal import APP_ID, DBUS, DESKTOP, PATH, REGISTRY, REQUEST, SESSION
    from wayland_dictation import SHORTCUT, SHORTCUTS, GlobalShortcuts


class PortalFixture:
    def __init__(self):
        self.bus = None
        self.request = None
        self.session = None
        self.client = None
        self.registered = None
        self.bound = None
        self.approve = True
        self.bind_failure = None
        self.shortcut_changed = None
        self.listed_shortcut = "Super+D"
        self.list_count = 0
        self.configuration = 0
        self.was_closed = False
        self.request_closed = False

    async def open(self, address=None):
        self.bus = await MessageBus(bus_address=address).connect()
        await self.bus.request_name(DESKTOP)
        self.bus.add_message_handler(self.route)

    @staticmethod
    def label(value):
        return [[SHORTCUT, {"trigger_description": Variant("s", value)}]]

    def send(self, path, interface, member, signature="", body=None):
        self.bus.send(Message(path=path, interface=interface, member=member, signature=signature,
                              body=body or [], message_type=MessageType.SIGNAL, destination=self.client))

    def notify(self, member, *, shortcut=None, session=None):
        self.send(PATH, SHORTCUTS, member, "osta{sv}", [session or self.session, shortcut or SHORTCUT, 100, {}])

    def response(self, message, code, result):
        options = message.body[-1]
        req = options["handle_token"].value
        sender = message.sender[1:].replace(".", "_")
        self.request = f"{PATH}/request/{sender}/{req}"
        # A synchronous backend may emit Response before the method reply.
        if code is not None:
            self.send(self.request, REQUEST, "Response", "ua{sv}", [code, result])
        return Message.new_method_return(message, "o", [self.request])

    def route(self, message):
        if message.message_type is not MessageType.METHOD_CALL:
            return
        if message.interface == REGISTRY and message.member == "Register":
            self.client = message.sender
            self.registered = message.body[0]
            return Message.new_method_return(message)
        if message.interface == DBUS + ".Properties" and message.body == [SHORTCUTS, "version"]:
            return Message.new_method_return(message, "v", [Variant("u", 2)])
        if message.interface == SHORTCUTS and message.member == "CreateSession":
            sender = message.sender[1:].replace(".", "_")
            key = message.body[0]["session_handle_token"].value
            self.session = f"{PATH}/session/{sender}/{key}"
            return self.response(message, 0, {"session_handle": Variant("s", self.session)})
        if message.interface == SHORTCUTS and message.member == "BindShortcuts":
            self.bound = message.body[1]
            if self.shortcut_changed:
                self.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})",
                          [self.session, self.label(self.shortcut_changed)])
                self.notify("Activated")
            code = self.bind_failure if self.bind_failure is not None else (
                None if self.approve is None else int(not self.approve))
            return self.response(message, code,
                                 {"shortcuts": Variant("a(sa{sv})", self.label("Super+D"))})
        if message.interface == SHORTCUTS and message.member == "ListShortcuts":
            self.list_count += 1
            return self.response(message, 0, {"shortcuts": Variant("a(sa{sv})", self.label(self.listed_shortcut))})
        if message.interface == SHORTCUTS and message.member == "ConfigureShortcuts":
            self.configuration += 1
            return Message.new_method_return(message)
        if message.interface == SESSION and message.member == "Close":
            self.was_closed = True
            return Message.new_method_return(message)
        if message.interface == REQUEST and message.member == "Close":
            self.request_closed = True
            return Message.new_method_return(message)


@unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast") and shutil.which("dbus-daemon"),
                     "requires dbus-fast and a private session bus")
class WaylandShortcutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(daemon.stdout.close)
        self.addCleanup(daemon.wait, 5)
        self.addCleanup(daemon.terminate)
        readable, _, _ = select.select([daemon.stdout], [], [], 5)
        self.assertTrue(readable, "private session bus did not become ready")
        address = daemon.stdout.readline().strip()
        environment = patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": address})
        environment.start()
        self.addCleanup(environment.stop)
        self.desktop = PortalFixture()
        await self.desktop.open(address)
        self.addCleanup(self.desktop.bus.disconnect)
        self.events = []
        self.portal = GlobalShortcuts(lambda: self.events.append("press"), self.events.append,
                                      lambda: self.events.append("closed"))
        self.addAsyncCleanup(self.portal.close)

    async def wait_for(self, count):
        async with asyncio.timeout(3):
            while len(self.events) < count:
                await asyncio.sleep(.01)

    async def test_bind_updates_activation_and_close(self):
        self.assertEqual(await self.portal.bind(), "Super+D")
        self.assertEqual(self.desktop.registered, APP_ID)
        self.assertEqual(self.desktop.bound[0][1]["preferred_trigger"].value, "CTRL+ALT+SHIFT+d")
        self.desktop.notify("Activated", shortcut="another")
        self.desktop.notify("Activated", session=PATH + "/session/another")
        self.desktop.notify("Activated")
        self.desktop.notify("Activated")
        self.desktop.notify("Deactivated")
        self.desktop.notify("Activated")
        self.desktop.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})",
                          [self.desktop.session, self.desktop.label("Alt+D")])
        await self.wait_for(3)
        self.assertEqual(self.events, ["press", "press", "Alt+D"])
        await self.portal.call(SHORTCUTS, "ConfigureShortcuts", "osa{sv}", [self.portal.session, "", {}])
        self.assertEqual(self.desktop.configuration, 1)
        await self.portal.close()
        self.assertTrue(self.desktop.was_closed)

    async def test_dismissed_shortcut_has_no_activation(self):
        self.desktop.approve = False
        self.desktop.shortcut_changed = "Super+D"
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            with self.assertRaisesRegex(CaptureError, "canceled"):
                await self.portal.bind()
        await self.portal.close()
        self.assertTrue(self.desktop.was_closed)
        self.assertEqual(self.desktop.list_count, 0)
        self.assertEqual(self.events, [])

    async def test_gnome_can_confirm_assignment_despite_bind_status(self):
        self.desktop.bind_failure = 2
        self.desktop.shortcut_changed = "Super+D"
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            self.assertEqual(await self.portal.bind(), "Super+D")
        self.assertEqual(self.desktop.list_count, 1)
        self.assertEqual(self.events, [])
        self.desktop.notify("Activated")
        await self.wait_for(1)
        self.assertEqual(self.events, ["press"])

    async def test_gnome_rejects_binding_absent_from_active_list(self):
        self.desktop.bind_failure = 2
        self.desktop.shortcut_changed = "Super+D"
        self.desktop.listed_shortcut = ""
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            with self.assertRaisesRegex(CaptureError, "declined"):
                await self.portal.bind()
        self.assertEqual(self.desktop.list_count, 1)
        self.assertEqual(self.events, [])

    async def test_cancel_closes_the_pending_approval_and_session(self):
        self.desktop.approve = None
        binding = asyncio.create_task(self.portal.bind())
        try:
            async with asyncio.timeout(3):
                while self.desktop.bound is None:
                    await asyncio.sleep(.01)
        finally:
            binding.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await binding
        self.assertTrue(self.desktop.request_closed)
        await self.portal.close()
        self.assertTrue(self.desktop.was_closed)
