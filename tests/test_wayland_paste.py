"""Exercise the portal paste transaction with real D-Bus file descriptor transfers."""
import asyncio
from importlib.util import find_spec
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_wayland_shortcuts import PortalFixture


if sys.platform == "linux" and find_spec("dbus_fast"):
    from dbus_fast import Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    from linux_desktop_portal import DBUS, DESKTOP, PATH, SESSION
    from portable_dictation import PASTE_SENT
    from portable_dictation_history import PortableDictationHistory
    from wayland_dictation import WaylandDictation
    from wayland_field import EDITABLE_AND_FOCUSED, Field, WaylandField
    from wayland_paste import (
        CLIPBOARD, EMPTY_RESTORE_FORMAT, KEYBOARD, KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V, PASTE_FALLBACK, REMOTE,
        WaylandPaste, read_clipboard, write_clipboard,
    )


class RemoteFixture(PortalFixture):
    def __init__(self):
        super().__init__()
        self.original = {"text/plain": b"Keep this", "text/html": b"<b>Keep this</b>", "image/png": b"\x89PNG" * 5000}
        self.selection = self.original
        self.owners = []
        self.devices = None
        self.requested_clipboard = False
        self.remote_version = 2
        self.select_options = []
        self.renewal = ""
        self.started = 0
        self.permissions = True
        self.notify_initial = True
        self.gnome_formats = False
        self.key_events = []
        self.on_key = None
        self.on_set_selection = None
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
        if message.interface == DBUS + ".Properties" and message.body == [REMOTE, "version"]:
            return Message.new_method_return(message, "v", [Variant("u", self.remote_version)])
        if message.interface == REMOTE:
            if message.member == "CreateSession":
                sender = message.sender[1:].replace(".", "_")
                self.session = f"{PATH}/session/{sender}/{message.body[0]['session_handle_token'].value}"
                return self.response(message, 0, {"session_handle": Variant("o", self.session)})
            if message.member == "SelectDevices":
                self.select_options.append({key: variant.value for key, variant in message.body[1].items()})
                self.devices = message.body[1]["types"].value
                return self.response(message, 0, {})
            if message.member == "Start":
                self.started += 1
                if self.notify_initial:
                    self.owner(list(self.selection), False)
                grants = {"devices": Variant("u", KEYBOARD), "clipboard_enabled": Variant("b", self.permissions)}
                if self.renewal:
                    grants["restore_token"] = Variant("s", self.renewal)
                return self.response(message, 0, grants)
            if message.member in ("NotifyKeyboardKeysym", "NotifyKeyboardKeycode"):
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
                if self.on_set_selection is not None:
                    self.on_set_selection(formats)
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
    async def test_continuation_requires_matching_focus_caret_and_edit_revision(self):
        with TemporaryDirectory() as directory:
            history = PortableDictationHistory(directory)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(), None, history)
            focus = (":1.8", "/field", 1)
            field = dictation.field = SimpleNamespace(focus=focus)
            before = Field(focus, 3, 2, 3)
            first = Field(focus, 4, 10, 11)
            second = Field(focus, 6, 20, 21)
            snapshots = field.snapshot = AsyncMock(side_effect=[before, first, first, second,
                                                               Field(focus, 8, 20, 21), None])
            field.matches = lambda _field: True
            keyboard = SimpleNamespace(paste=AsyncMock(return_value=PASTE_SENT))
            allowed = lambda: True
            await dictation.paste_in_field("I spoke,", keyboard, allowed, False, focus)
            await dictation.paste_in_field("And wrote", keyboard, allowed, False, focus)
            await dictation.paste_in_field("One more.", keyboard, allowed, False, focus)
            self.assertEqual([call.args[0] for call in keyboard.paste.await_args_list],
                             ["I spoke,", " and wrote", "One more."])
            self.assertEqual(snapshots.await_count, 6)

    async def test_wayland_join_guard_retries_plain_before_any_keys_are_sent(self):
        with TemporaryDirectory() as directory:
            history = PortableDictationHistory(directory)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(), None, history)
            focus = (":1.8", "/field", 1)
            before = Field(focus, 4, 10, 11)
            field = dictation.field = SimpleNamespace(focus=focus, snapshot=AsyncMock(return_value=before),
                                                     matches=lambda _field: False)
            dictation.last_insertion = (before, "I spoke,")

            async def paste(_text, _terminal, can_send):
                return PASTE_SENT if can_send() else PASTE_FALLBACK

            keyboard = SimpleNamespace(paste=AsyncMock(side_effect=paste))
            result = await dictation.paste_in_field("And wrote", keyboard, lambda: True, False, focus)
            self.assertEqual(result, PASTE_SENT)
            self.assertEqual([call.args[0] for call in keyboard.paste.await_args_list], [" and wrote", "And wrote"])
            dictation.last_insertion = (before, "I spoke,")
            keyboard.paste.reset_mock()

            async def changed_focus(text, terminal, can_send):
                field.focus = (":1.8", "/other_field", 2)
                return await paste(text, terminal, can_send)

            keyboard.paste.side_effect = changed_focus
            self.assertEqual(await dictation.paste_in_field("And wrote", keyboard, lambda: True, False, focus),
                             PASTE_FALLBACK)
            keyboard.paste.assert_awaited_once()

    async def test_older_wayland_take_needs_a_verified_field_to_paste(self):
        with TemporaryDirectory() as directory:
            history = PortableDictationHistory(directory)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(), None, history)
            focus = (":1.8", "/field", 1)
            field = dictation.field = SimpleNamespace(focus=focus)
            field.snapshot = AsyncMock(side_effect=[Field(focus, 1, 2, 3), None, None])
            keyboard = dictation.keyboard = SimpleNamespace(paste=AsyncMock(return_value=PASTE_SENT))
            dictation.command_mode = True
            dictation.take_field = focus
            dictation.paste_target = keyboard
            older = dictation.prepare_delivery()
            dictation.watch_focus()
            self.assertEqual(await older("An earlier take"), PASTE_SENT)
            self.assertEqual(await older("Unverified field"), PASTE_FALLBACK)
            field.focus = (":1.8", "/field", 3)
            self.assertEqual(await older("Focus changed and returned"), PASTE_FALLBACK)
            keyboard.paste.assert_awaited_once()

    async def test_wayland_paste_does_not_redirect_a_take_when_an_observed_field_loses_focus(self):
        with TemporaryDirectory() as directory:
            history = PortableDictationHistory(directory)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(), None, history)
            initial_focus = (":1.8", "/before", 1)
            field = dictation.field = SimpleNamespace(focus=(":1.8", "/after", 2))
            keyboard = SimpleNamespace(paste=AsyncMock(return_value=PASTE_SENT))
            self.assertEqual(await dictation.paste_in_field("Known field", keyboard, lambda: True, False, initial_focus),
                             PASTE_FALLBACK)
            keyboard.paste.assert_not_awaited()
            field.focus = initial_focus

            async def query():
                field.focus = (":1.8", "/after", 2)
                return None

            field.snapshot = query
            self.assertEqual(await dictation.paste_in_field("Changed during query", keyboard, lambda: True, False, initial_focus),
                             PASTE_FALLBACK)
            keyboard.paste.assert_not_awaited()
            self.assertEqual(await dictation.paste_in_field("No observed target", keyboard, lambda: True, False, None), PASTE_SENT)
            keyboard.paste.assert_awaited_once()

    async def test_accessibility_snapshot_skips_password_caret_and_text_queries(self):
        field = WaylandField()
        field.bus = SimpleNamespace(connected=True)
        field.change_focus((":1.8", "/field"))
        with patch("wayland_field.call", new=AsyncMock(side_effect=[[[EDITABLE_AND_FOCUSED]], ["password text"]])) as query:
            self.assertIsNone(await field.snapshot())
        self.assertEqual([call.args[4] for call in query.await_args_list], ["GetState", "GetRoleName"])

    async def test_accessibility_snapshot_accepts_a_focused_gtk_text_box(self):
        field = WaylandField()
        field.bus = SimpleNamespace(connected=True)
        field.change_focus((":1.8", "/field"))
        for role in ("text box", "section", "document web"):
            replies = [[[EDITABLE_AND_FOCUSED]], [role], [0], [Variant("i", 2)], [Variant("i", 3)]]
            with patch("wayland_field.call", new=AsyncMock(side_effect=replies)):
                self.assertEqual(await field.snapshot(), Field(field.focus, field.revision, 2, 3))

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
            while (KEY_V, 0) not in self.desktop.key_events:
                await asyncio.sleep(.01)

    def dictation_workspace(self):
        from portable_dictation_history import PortableDictationHistory
        from wayland_dictation import WaylandDictation

        storage = TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        history = PortableDictationHistory(Path(storage.name))
        history.initialize()
        state = SimpleNamespace(draining=False, status="ready", portable_shortcut_ready=asyncio.Event())
        state.portable_shortcut_ready.set()

        def reopen():
            dictation = state.portable_dictation = WaylandDictation(state, None, history)
            dictation.command_mode = True
            self.addAsyncCleanup(dictation.close)
            return dictation

        return state, history, reopen

    async def test_wayland_paste_consent_restores_rotates_and_is_forgotten(self):
        from server import restore_portable_shortcut

        state, history, reopen = self.dictation_workspace()
        first = reopen()
        await restore_portable_shortcut(state)
        await first.keyboard_task
        self.assertEqual(self.desktop.started, 0)

        self.desktop.renewal = "fixture-approved-first"
        first.allow_paste()
        await first.keyboard_task
        self.assertTrue(first.keyboard_allowed)
        self.assertEqual(self.desktop.select_options[-1]["persist_mode"], 2)
        self.assertNotIn("restore_token", self.desktop.select_options[-1])
        await first.close()

        self.desktop.renewal = "fixture-approved-second"
        second = reopen()
        await restore_portable_shortcut(state)
        await second.keyboard_task
        self.assertTrue(second.keyboard_allowed)
        self.assertEqual(self.desktop.select_options[-1]["restore_token"], "fixture-approved-first")
        self.assertNotIn(self.desktop.renewal, str(second.snapshot()))
        second.text = "Paste after restart."
        paste = asyncio.create_task(second.run_command("paste-last"))
        await self.key_sent()
        self.assertEqual(await self.desktop.read_owned("text/plain;charset=utf-8"), second.text.encode())
        self.assertEqual(await paste, "pasted")
        for mime, content in self.desktop.original.items():
            self.assertEqual(await self.desktop.read_owned(mime), content)
        await second.close()

        self.desktop.renewal = "fixture-approved-third"
        third = reopen()
        await restore_portable_shortcut(state)
        await third.keyboard_task
        self.assertEqual(self.desktop.select_options[-1]["restore_token"], "fixture-approved-second")
        await third.disable_paste()
        await third.close()
        self.assertEqual(history.consume_paste_grant(), "")

        fourth = reopen()
        await restore_portable_shortcut(state)
        await fourth.keyboard_task
        self.assertFalse(fourth.keyboard_allowed)
        self.assertEqual(self.desktop.started, 3)

    async def test_denied_or_revoked_grant_is_not_reused(self):
        from server import restore_portable_shortcut

        state, history, reopen = self.dictation_workspace()
        history.save_paste_grant("expired")
        self.desktop.renewal = "must-not-save"
        self.desktop.permissions = False
        dictation = reopen()
        await restore_portable_shortcut(state)
        await dictation.keyboard_task
        self.assertEqual(self.desktop.select_options[-1]["restore_token"], "expired")
        self.assertFalse(dictation.keyboard_allowed)
        self.assertEqual(history.consume_paste_grant(), "")
        self.assertEqual(self.desktop.key_events, [])

        self.desktop.renewal = "granted"
        self.desktop.permissions = True
        dictation.allow_paste()
        await dictation.keyboard_task
        self.assertTrue(dictation.keyboard_allowed)
        self.assertNotIn("restore_token", self.desktop.select_options[-1])
        self.desktop.send(self.desktop.session, SESSION, "Closed", "a{sv}", [{}])
        async with asyncio.timeout(3):
            while dictation.keyboard_allowed:
                await asyncio.sleep(.01)
        await asyncio.gather(*dictation.paste_grant_tasks)
        self.assertEqual(history.consume_paste_grant(), "")
        self.assertIn("Copy last", dictation.feedback)

    async def test_older_desktop_can_grant_a_session_without_persistence(self):
        self.desktop.remote_version = 1
        await self.paste.approve()
        self.assertTrue(self.paste.active)
        self.assertNotIn("persist_mode", self.desktop.select_options[-1])
        self.assertEqual(self.paste.restore_token, "")

    async def test_paste_uses_keyboard_and_restores_all_formats(self):
        await self.paste.approve()
        self.assertEqual(self.desktop.devices, KEYBOARD)
        self.assertTrue(self.desktop.requested_clipboard)
        text = "The notebook is on the table. دفتر"
        delivery = asyncio.create_task(self.paste.paste(text, True, lambda: True))
        await self.key_sent()
        self.assertEqual(await self.desktop.read_owned("text/plain;charset=utf-8"), text.encode())
        self.assertEqual(await delivery, "Paste shortcut sent.")
        self.assertEqual(self.desktop.key_events, [(KEY_LEFTCTRL, 1), (KEY_LEFTSHIFT, 1), (KEY_V, 1),
                                                   (KEY_V, 0), (KEY_LEFTSHIFT, 0), (KEY_LEFTCTRL, 0)])
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
        with patch("wayland_paste.clipboard_is_empty", new=AsyncMock(return_value=False)):
            await self.paste.approve()
        self.assertIn("Copy last", await self.paste.paste("Available", False, lambda: True))
        self.assertEqual(self.desktop.owners, [])
        self.assertEqual(self.desktop.key_events, [])

    async def test_confirmed_empty_clipboard_pastes_and_clears_again(self):
        self.desktop.notify_initial = False
        self.desktop.selection = {}
        with patch("wayland_paste.clipboard_is_empty", new=AsyncMock(return_value=True)) as probe:
            await self.paste.approve()
        probe.assert_awaited_once()
        self.assertEqual(self.paste.formats, ())
        delivery = asyncio.create_task(self.paste.paste("First dictation.", False, lambda: True))
        await self.key_sent()
        self.assertEqual(await self.desktop.read_owned("text/plain"), b"First dictation.")
        self.assertEqual(await delivery, PASTE_SENT)
        self.assertEqual(self.desktop.owners[-1], [])

    async def test_gnome_clears_the_cached_transcript_if_the_clipboard_was_empty(self):
        self.desktop.selection = {}
        await self.paste.approve()
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            self.assertEqual(await self.paste.paste("First dictation.", False, lambda: True), PASTE_SENT)
        self.assertEqual(self.desktop.owners[-2:], [[EMPTY_RESTORE_FORMAT], []])

    async def test_gnome_empty_restore_preserves_a_new_clipboard_owner(self):
        self.desktop.selection = {}
        await self.paste.approve()

        def copy_during_restore(formats):
            if formats == [EMPTY_RESTORE_FORMAT]:
                self.desktop.foreign_copy({"text/plain": b"New copy"})

        self.desktop.on_set_selection = copy_during_restore
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertEqual(await self.paste.paste("First dictation.", False, lambda: True), PASTE_SENT)
        self.assertEqual(len(self.desktop.owners), 2)
        self.assertEqual(await self.paste.snapshot(), {"text/plain": b"New copy"})

    async def test_new_clipboard_owner_overrides_an_empty_probe(self):
        self.desktop.notify_initial = False

        async def probe():
            self.desktop.foreign_copy({"text/html": b"<b>Keep this</b>"})
            async with asyncio.timeout(1):
                while self.paste.selection_revision == 0:
                    await asyncio.sleep(.001)
            return True

        with patch("wayland_paste.clipboard_is_empty", new=probe):
            await self.paste.approve()
        self.assertEqual(self.paste.formats, ("text/html",))
        self.assertEqual(await self.paste.snapshot(), {"text/html": b"<b>Keep this</b>"})

    async def test_background_restore_probes_empty_clipboard_lazily_on_paste(self):
        self.desktop.notify_initial = False
        self.desktop.selection = {}
        with patch("wayland_paste.clipboard_is_empty", new=AsyncMock(return_value=True)) as probe:
            await self.paste.approve("existing-grant")
            probe.assert_not_awaited()
            self.assertIsNone(self.paste.formats)
            delivery = asyncio.create_task(self.paste.paste("After restart.", False, lambda: True))
            await self.key_sent()
            self.assertEqual(await self.desktop.read_owned("text/plain"), b"After restart.")
            self.assertEqual(await delivery, PASTE_SENT)
            probe.assert_awaited_once()

    async def test_copy_during_modifier_press_prevents_pasting_foreign_text(self):
        await self.paste.approve()
        def replace_on_control(key, pressed):
            if (key, pressed) == (KEY_LEFTCTRL, 1):
                self.desktop.foreign_copy({"text/plain": b"New content"})

        self.desktop.on_key = replace_on_control
        self.assertIn("Copy last", await self.paste.paste("Available", False, lambda: True))
        self.assertEqual(self.desktop.key_events, [(KEY_LEFTCTRL, 1), (KEY_LEFTCTRL, 0)])
        self.assertEqual(len(self.desktop.owners), 1)
        self.assertEqual(self.desktop.selection["text/plain"], b"New content")

    async def test_revocation_during_shortcut_does_not_send_more_keys(self):
        await self.paste.approve()
        def revoke_on_control(key, pressed):
            if (key, pressed) == (KEY_LEFTCTRL, 1):
                self.desktop.send(self.desktop.session, SESSION, "Closed", "a{sv}", [{}])

        self.desktop.on_key = revoke_on_control
        self.assertIn("Copy last", await self.paste.paste("Available", False, lambda: True))
        self.assertFalse(self.paste.active)
        self.assertEqual(self.desktop.key_events, [(KEY_LEFTCTRL, 1)])
        self.assertEqual(self.paste.data, {})
