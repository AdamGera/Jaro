"""Exercise the portal paste transaction with real D-Bus file descriptor transfers."""
import asyncio
from importlib.util import find_spec
import os
import select
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_wayland_shortcuts import PortalFixture


if sys.platform == "linux" and find_spec("dbus_fast"):
    from dbus_fast import Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    from linux_desktop_portal import DBUS, DESKTOP, PATH, SESSION
    from wayland_paste import CLIPBOARD, KEYBOARD, REMOTE, WaylandPaste, read_clipboard, write_clipboard


class RemoteFixture(PortalFixture):
    def __init__(self):
        super().__init__()
        self.original = {"text/plain": b"Keep this", "text/html": b"<b>Keep this</b>", "image/png": b"\x89PNG" * 5000}
        self.selection = self.original
        self.owners = []
        self.devices = None
        self.requested_clipboard = False
        self.permissions = True
        self.notify_initial = True
        self.gnome_formats = False
        self.key_events = []
        self.on_key = None
        self.readers = {}
        self.serial = 0
        self.written = {}

    async def open(self, address=None):
        self.bus = await MessageBus(bus_address=address, negotiate_unix_fd=True).connect()
        await self.bus.request_name(DESKTOP)
        self.bus.add_message_handler(self.route)

    def owner(self, formats, owned):
        types = Variant("(as)", [formats]) if self.gnome_formats else Variant("as", formats)
        self.send(PATH, CLIPBOARD, "SelectionOwnerChanged", "oa{sv}", [self.session, {
            "mime_types": types, "session_is_owner": Variant("b", owned),
        }])

    def foreign_copy(self, selection):
        self.selection = selection
        self.owner(list(selection), False)

    async def read_owned(self, mime):
        self.serial += 1
        serial = self.serial
        self.send(PATH, CLIPBOARD, "SelectionTransfer", "osu", [self.session, mime, serial])
        async with asyncio.timeout(2):
            while serial not in self.readers:
                await asyncio.sleep(.005)
            fd = self.readers.pop(serial)
            try:
                value = await read_clipboard(fd, 30000)
            finally:
                os.close(fd)
            while serial not in self.written:
                await asyncio.sleep(.005)
        if not self.written[serial]:
            raise RuntimeError("The clipboard transfer failed.")
        return value

    def reply_fd(self, message, fd):
        future = self.bus.send(Message.new_method_return(message, "h", [0], [fd]))
        future.add_done_callback(lambda _: os.close(fd))
        return True

    def route(self, message):
        if message.message_type is not MessageType.METHOD_CALL:
            return
        if message.interface == DBUS + ".Properties" and message.body in ([REMOTE, "AvailableDeviceTypes"], [CLIPBOARD, "version"]):
            return Message.new_method_return(message, "v", [Variant("u", 7)])
        if message.interface == REMOTE:
            if message.member == "CreateSession":
                sender = message.sender[1:].replace(".", "_")
                self.session = f"{PATH}/session/{sender}/{message.body[0]['session_handle_token'].value}"
                return self.response(message, 0, {"session_handle": Variant("o", self.session)})
            if message.member == "SelectDevices":
                self.devices = message.body[1]["types"].value
                return self.response(message, 0, {})
            if message.member == "Start":
                if self.notify_initial:
                    self.owner(list(self.selection), False)
                return self.response(message, 0, {"devices": Variant("u", KEYBOARD),
                                                  "clipboard_enabled": Variant("b", self.permissions)})
            if message.member == "NotifyKeyboardKeysym":
                self.key_events.append(tuple(message.body[-2:]))
                if self.on_key is not None:
                    self.on_key(*self.key_events[-1])
                return Message.new_method_return(message)
        if message.interface == CLIPBOARD:
            if message.member == "RequestClipboard":
                self.requested_clipboard = True
                return Message.new_method_return(message)
            if message.member == "SetSelection":
                options = message.body[1]
                formats = options["mime_types"].value if "mime_types" in options else []
                self.owners.append(formats)
                self.owner(formats, bool(formats))
                return Message.new_method_return(message)
            if message.member == "SelectionRead":
                read_fd, write_fd = os.pipe()
                data = self.selection[message.body[1]]
                asyncio.create_task(self.send_data(write_fd, data))
                return self.reply_fd(message, read_fd)
            if message.member == "SelectionWrite":
                read_fd, write_fd = os.pipe()
                self.readers[message.body[1]] = read_fd
                return self.reply_fd(message, write_fd)
            if message.member == "SelectionWriteDone":
                _, serial, written = message.body
                self.written[serial] = written
                return Message.new_method_return(message)
        return super().route(message)

    @staticmethod
    async def send_data(fd, data):
        try:
            await write_clipboard(fd, data)
        finally:
            os.close(fd)


@unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast") and shutil.which("dbus-daemon"),
                     "requires dbus-fast and a private session bus")
class WaylandPasteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(daemon.stdout.close)
        self.addCleanup(daemon.wait, 5)
        self.addCleanup(daemon.terminate)
        self.assertTrue(select.select([daemon.stdout], [], [], 5)[0])
        address = daemon.stdout.readline().strip()
        environment = patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": address})
        environment.start()
        self.addCleanup(environment.stop)
        self.desktop = RemoteFixture()
        await self.desktop.open(address)
        self.addCleanup(self.desktop.bus.disconnect)
        self.paste = WaylandPaste(lambda: None)
        self.addAsyncCleanup(self.paste.close)

    async def key_sent(self):
        async with asyncio.timeout(3):
            while (ord("v"), 0) not in self.desktop.key_events:
                await asyncio.sleep(.01)

    async def test_paste_uses_keyboard_and_restores_all_formats(self):
        await self.paste.approve()
        self.assertEqual(self.desktop.devices, KEYBOARD)
        self.assertTrue(self.desktop.requested_clipboard)
        text = "The notebook is on the table. دفتر"
        delivery = asyncio.create_task(self.paste.paste(text, True, lambda: True))
        await self.key_sent()
        self.assertEqual(await self.desktop.read_owned("text/plain;charset=utf-8"), text.encode())
        self.assertEqual(await delivery, "Paste shortcut sent.")
        self.assertEqual(self.desktop.key_events, [(0xffe3, 1), (0xffe1, 1), (ord("v"), 1),
                                                   (ord("v"), 0), (0xffe1, 0), (0xffe3, 0)])
        for mime, data in self.desktop.original.items():
            self.assertEqual(await self.desktop.read_owned(mime), data)

    async def test_new_copy_is_left_in_place(self):
        await self.paste.approve()
        delivery = asyncio.create_task(self.paste.paste("New dictation.", False, lambda: True))
        await self.key_sent()
        self.desktop.foreign_copy({"text/plain": b"New copy"})
        self.assertEqual(await delivery, "Paste shortcut sent.")
        self.assertEqual(len(self.desktop.owners), 1)
        self.assertEqual(self.desktop.selection["text/plain"], b"New copy")

    async def test_gnome_clipboard_format_structure_preserves_contents(self):
        self.desktop.gnome_formats = True
        await self.paste.approve()
        delivery = asyncio.create_task(self.paste.paste("Local dictation.", False, lambda: True))
        await self.key_sent()
        self.assertEqual(await self.desktop.read_owned("text/plain"), b"Local dictation.")
        self.assertEqual(await delivery, "Paste shortcut sent.")
        self.assertEqual(await self.desktop.read_owned("image/png"), self.desktop.original["image/png"])

    async def test_unknown_clipboard_keeps_copy_last_available(self):
        self.desktop.notify_initial = False
        await self.paste.approve()
        self.assertIn("Copy last", await self.paste.paste("Available", False, lambda: True))
        self.assertEqual(self.desktop.owners, [])
        self.assertEqual(self.desktop.key_events, [])

    async def test_copy_during_modifier_press_prevents_pasting_foreign_text(self):
        await self.paste.approve()
        def replace_on_control(key, pressed):
            if (key, pressed) == (0xffe3, 1):
                self.desktop.foreign_copy({"text/plain": b"New content"})

        self.desktop.on_key = replace_on_control
        self.assertIn("Copy last", await self.paste.paste("Available", False, lambda: True))
        self.assertEqual(self.desktop.key_events, [(0xffe3, 1), (0xffe3, 0)])
        self.assertEqual(len(self.desktop.owners), 1)
        self.assertEqual(self.desktop.selection["text/plain"], b"New content")

    async def test_revocation_during_shortcut_does_not_send_more_keys(self):
        await self.paste.approve()
        def revoke_on_control(key, pressed):
            if (key, pressed) == (0xffe3, 1):
                self.desktop.send(self.desktop.session, SESSION, "Closed", "a{sv}", [{}])

        self.desktop.on_key = revoke_on_control
        self.assertIn("Copy last", await self.paste.paste("Available", False, lambda: True))
        self.assertFalse(self.paste.active)
        self.assertEqual(self.desktop.key_events, [(0xffe3, 1)])
        self.assertEqual(self.paste.data, {})
