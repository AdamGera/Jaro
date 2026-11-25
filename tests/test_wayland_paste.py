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
    from linux_capture import CaptureError
    from linux_desktop_portal import DBUS, DESKTOP, PATH, SESSION
    from portable_dictation import PASTE_SENT
    from portable_dictation_history import PortableDictationHistory
    from wayland_dictation import WaylandDictation
    from wayland_field import (ACCESSIBLE, ACTIVE, EDITABLE_AND_FOCUSED, PROPERTIES, REGISTRY, REGISTRY_ROOT,
                               Field, FocusWindow, SwayFocus, WaylandField)
    from wayland_paste import (
        CLIPBOARD, CONTROL_L, EMPTY_RESTORE_FORMAT, KEYBOARD, LATIN_V, PASTE_FALLBACK, REMOTE, SHIFT_L,
        WaylandPaste, read_clipboard, write_clipboard,
    )

KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V = 29, 42, 47


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
        self.grant_access = True
        self.clipboard_access = True
        self.notify_initial = True
        self.gnome_formats = False
        self.key_events = []
        self.key_methods = []
        self.on_key = None
        self.on_set_selection = None
        self.empty_restore_delay = 0
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
                grants = {"devices": Variant("u", KEYBOARD if self.grant_access else 0),
                          "clipboard_enabled": Variant("b", self.grant_access and self.clipboard_access)}
                if self.renewal:
                    grants["restore_token"] = Variant("s", self.renewal)
                return self.response(message, 0, grants)
            if message.member in ("NotifyKeyboardKeysym", "NotifyKeyboardKeycode"):
                self.key_events.append(tuple(message.body[-2:]))
                self.key_methods.append(message.member)
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
                if not formats and self.empty_restore_delay:
                    asyncio.get_running_loop().call_later(self.empty_restore_delay, self.owner, [], False)
                else:
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
    async def test_sway_primes_focus_from_the_compositors_window_and_only_reads_metadata(self):
        field = WaylandField()
        field.bus = SimpleNamespace(connected=True)
        window = FocusWindow(14, 716, "External editor")
        root = (REGISTRY, REGISTRY_ROOT)
        app = (":1.8", REGISTRY_ROOT)
        wrong_app = (":1.9", REGISTRY_ROOT)
        active = (app[0], "/window")
        stale = (app[0], "/old_window")
        editor = (app[0], "/field")
        children = {root: [wrong_app, app], app: [stale, active], active: [editor], editor: []}
        states = {active: ACTIVE, stale: ACTIVE, editor: EDITABLE_AND_FOCUSED}

        async def query(_bus, name, path, interface, member, signature="", body=None):
            target = (name, path)
            if interface == DBUS and member == "GetConnectionUnixProcessID":
                return [window.pid if body == [app[0]] else 901]
            if interface == ACCESSIBLE and member == "GetChildren":
                return [children[target]]
            if interface == ACCESSIBLE and member == "GetState":
                return [[states[target], 0]]
            if interface == PROPERTIES and member == "Get" and body == [ACCESSIBLE, "Name"]:
                return [Variant("s", window.title if target == active else "Inactive window")]
            self.fail(f"Unexpected accessibility query {interface}.{member}")

        with (patch("wayland_field.call", new=query),
              patch("wayland_field.sway_window", return_value=window) as current):
            await field.seed_focus()
            self.assertEqual(field.focus, (*editor, 1))
            self.assertTrue(await field.confirm_seeded_focus(field.focus))
            current.return_value = FocusWindow(15, 716, "External editor")
            self.assertFalse(await field.confirm_seeded_focus(field.focus))
            self.assertIsNone(field.focus)

    async def test_sway_discards_a_seed_if_a_focus_event_arrives_during_the_lookup(self):
        field = WaylandField()
        field.bus = SimpleNamespace(connected=True)
        window = FocusWindow(16, 716, "External editor")
        target = (":1.8", "/initial")

        async def search(*_):
            field.signal(SimpleNamespace(message_type=MessageType.SIGNAL, sender=":1.8", path="/moved",
                                         interface="org.a11y.atspi.Event.Object", member="StateChanged",
                                         body=["focused", 1]))
            return target

        with (patch("wayland_field.sway_window", return_value=window),
              patch("wayland_field.focused_editor", side_effect=search)):
            await field.seed_focus()
        self.assertEqual(field.focus, (":1.8", "/moved", 1))
        self.assertIsNone(field.seeded_window)

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

    async def test_sway_pastes_queued_plain_text_and_rejects_a_window_that_lost_focus(self):
        directory = self.enterContext(TemporaryDirectory())
        history = PortableDictationHistory(directory)
        history.initialize()
        dictation = WaylandDictation(SimpleNamespace(), None, history)
        dictation.command_mode = True
        sway = dictation.sway = SwayFocus()
        sway.change((1, 100))

        async def paste(text, terminal, allowed, *, confirm):
            return PASTE_SENT if allowed() and await confirm() else PASTE_FALLBACK

        keyboard = dictation.keyboard = SimpleNamespace(paste=AsyncMock(side_effect=paste))
        dictation.watch_focus()
        dictation.paste_target = keyboard
        older = dictation.prepare_delivery()
        dictation.watch_focus()
        dictation.paste_target = keyboard
        newer = dictation.prepare_delivery()

        with patch.object(sway, "refresh", AsyncMock(return_value=True)):
            self.assertEqual(await older("First words."), PASTE_SENT)
            self.assertEqual(await newer("Second words."), PASTE_SENT)
            self.assertIsNone(dictation.last_insertion)
            self.assertEqual([call.args[0] for call in keyboard.paste.await_args_list], ["First words.", "Second words."])
            sway.change((2, 200))
            sway.change((1, 100))
            self.assertEqual(await newer("An old destination."), PASTE_FALLBACK)
            self.assertEqual(keyboard.paste.await_count, 2)

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
        probe = patch("wayland_paste.paste_keycodes", return_value=(KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V, 135))
        self.paste_keycodes = probe.start()
        self.addCleanup(probe.stop)

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
        await second.keyboard.wait_for_restore()
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
        self.assertEqual(history.consume_paste_grant(), ("", False))

        fourth = reopen()
        await restore_portable_shortcut(state)
        await fourth.keyboard_task
        self.assertFalse(fourth.keyboard_allowed)
        self.assertEqual(self.desktop.started, 3)

    async def test_virtual_keyboard_consent_restores_only_when_supported(self):
        from server import restore_portable_shortcut

        state, history, reopen = self.dictation_workspace()

        async def restart():
            dictation = reopen()
            await restore_portable_shortcut(state)
            await dictation.keyboard_task
            return dictation

        with (patch("wayland_paste.clipboard_capabilities", return_value=(Path("/fixture/clipboard"), True)) as probe,
              patch("wayland_paste.shutil.which", return_value="/fixture/wtype")):
            first = await restart()
            self.assertFalse(first.keyboard_allowed)
            probe.assert_not_awaited()

            first.allow_paste()
            await first.keyboard_task
            self.assertTrue(first.keyboard_allowed)
            self.assertEqual(first.feedback, "Automatic paste is ready.")
            self.assertEqual(history.consume_paste_grant(), ("", True))
            await first.close()

            probe.return_value = (None, False)
            unsupported = await restart()
            self.assertFalse(unsupported.keyboard_allowed)
            self.assertTrue(unsupported.snapshot()["savedVirtualPaste"])
            self.assertIn("Choose Allow paste", unsupported.feedback)
            self.assertEqual(self.desktop.started, 0)
            await unsupported.close()

            probe.return_value = (Path("/fixture/clipboard"), True)
            restored = await restart()
            self.assertTrue(restored.keyboard_allowed)
            self.assertEqual(self.desktop.started, 0)
            await restored.disable_paste()
            await restored.close()

            disabled = await restart()
            self.assertFalse(disabled.keyboard_allowed)
            self.assertFalse(disabled.snapshot()["savedVirtualPaste"])
            self.assertEqual(history.consume_paste_grant(), ("", False))
            self.assertEqual(self.desktop.started, 0)

    async def test_explicit_portal_approval_replaces_saved_virtual_keyboard_choice(self):
        from server import restore_portable_shortcut

        state, history, reopen = self.dictation_workspace()
        history.save_paste_grant("", True)
        with patch("wayland_paste.clipboard_capabilities", return_value=(None, False)):
            dictation = reopen()
            await restore_portable_shortcut(state)
            await dictation.keyboard_task
            self.assertEqual(self.desktop.started, 0)

            self.desktop.renewal = "portal-approval"
            dictation.allow_paste()
            await dictation.keyboard_task
            self.assertTrue(dictation.keyboard_allowed)
            self.assertFalse(dictation.snapshot()["savedVirtualPaste"])
            self.assertEqual(history.consume_paste_grant(), ("portal-approval", False))

    async def test_denied_or_revoked_grant_is_not_reused(self):
        from server import restore_portable_shortcut

        state, history, reopen = self.dictation_workspace()
        history.save_paste_grant("expired")
        self.desktop.renewal = "must-not-save"
        self.desktop.grant_access = False
        dictation = reopen()
        await restore_portable_shortcut(state)
        await dictation.keyboard_task
        self.assertEqual(self.desktop.select_options[-1]["restore_token"], "expired")
        self.assertFalse(dictation.keyboard_allowed)
        self.assertEqual(history.consume_paste_grant(), ("", False))
        self.assertEqual(self.desktop.key_events, [])

        self.desktop.renewal = "granted"
        self.desktop.grant_access = True
        dictation.allow_paste()
        await dictation.keyboard_task
        self.assertTrue(dictation.keyboard_allowed)
        self.assertNotIn("restore_token", self.desktop.select_options[-1])
        self.desktop.send(self.desktop.session, SESSION, "Closed", "a{sv}", [{}])
        async with asyncio.timeout(3):
            while dictation.keyboard_allowed:
                await asyncio.sleep(.01)
        await asyncio.gather(*dictation.paste_grant_tasks)
        self.assertEqual(history.consume_paste_grant(), ("", False))
        self.assertIn("Copy last", dictation.feedback)

    async def test_virtual_keyboard_requires_the_compositor_protocol_and_clipboard(self):
        clipboard_owned = True

        lease = SimpleNamespace(owned=lambda: clipboard_owned, offer=AsyncMock(return_value=True), close=AsyncMock())

        with (patch("wayland_paste.clipboard_capabilities", return_value=(Path("/fixture/clipboard"), True)),
              patch("wayland_paste.shutil.which", return_value="/fixture/wtype"),
              patch("wayland_paste.ClipboardLease.start", return_value=lease) as start,
              patch("wayland_paste.run_tool", return_value=b"") as tools,
              patch("wayland_paste.RESTORE_DELAY", 0)):
            await self.paste.approve()
            self.assertTrue(self.paste.active)
            self.assertEqual(self.desktop.started, 0)
            self.assertEqual(await self.paste.paste("Dictated text", True, lambda: True), PASTE_SENT)
            start.assert_awaited_once_with(Path("/fixture/clipboard"))
            lease.offer.assert_awaited_once_with(b"Dictated text")
            tools.assert_awaited_once_with("/fixture/wtype", "-M", "ctrl", "-M", "shift", "-k", "v",
                                           "-m", "shift", "-m", "ctrl")
            tools.return_value = None
            self.assertEqual(await self.paste.paste("Dictated text", False, lambda: True), PASTE_FALLBACK)
            tools.reset_mock()
            confirm = AsyncMock(return_value=False)
            self.assertEqual(await self.paste.paste("Dictated text", False, lambda: True, confirm=confirm), PASTE_FALLBACK)
            confirm.assert_awaited_once()
            tools.assert_not_awaited()
            clipboard_owned = False
            tools.reset_mock()
            self.assertEqual(await self.paste.paste("Dictated text", False, lambda: True), PASTE_FALLBACK)
            tools.assert_not_awaited()

    async def test_portal_keyboard_and_data_control_are_independent_on_plasma(self):
        lease = SimpleNamespace(owned=lambda: True, offer=AsyncMock(return_value=True), close=AsyncMock())

        self.desktop.clipboard_access = False
        self.desktop.renewal = "plasma-token"
        with (patch("wayland_paste.clipboard_capabilities", return_value=(Path("/fixture/clipboard"), False)),
              patch("wayland_paste.ClipboardLease.start", return_value=lease),
              patch("wayland_paste.RESTORE_DELAY", 0),
              patch.dict(os.environ, XDG_CURRENT_DESKTOP="KDE")):
            await self.paste.approve()
            self.assertTrue(self.paste.active)
            self.assertFalse(self.desktop.requested_clipboard)
            self.assertEqual(self.paste.restore_token, "plasma-token")
            for terminal in (False, True):
                self.assertEqual(await self.paste.paste("Dictated text", terminal, lambda: True), PASTE_SENT)
            self.assertEqual(self.desktop.started, 1)
            self.assertEqual(self.desktop.key_events, [(KEY_LEFTCTRL, 1), (KEY_V, 1), (KEY_V, 0), (KEY_LEFTCTRL, 0),
                                                       (KEY_LEFTCTRL, 1), (KEY_LEFTSHIFT, 1), (KEY_V, 1),
                                                       (KEY_V, 0), (KEY_LEFTSHIFT, 0), (KEY_LEFTCTRL, 0)])

    async def test_missing_clipboard_grant_does_not_replace_the_selection(self):
        self.desktop.clipboard_access = False
        with patch("wayland_paste.clipboard_capabilities", return_value=(None, False)):
            with self.assertRaisesRegex(CaptureError, "Copy last"):
                await self.paste.approve()
            self.assertFalse(self.paste.active)
            self.assertEqual(await self.paste.paste("Dictated text", False, lambda: True), PASTE_FALLBACK)
            self.assertEqual(self.desktop.key_events, [])
            self.assertEqual(self.desktop.selection, self.desktop.original)

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
        await self.paste.wait_for_restore()
        self.assertEqual(self.desktop.key_events, [(KEY_LEFTCTRL, 1), (KEY_LEFTSHIFT, 1), (KEY_V, 1),
                                                   (KEY_V, 0), (KEY_LEFTSHIFT, 0), (KEY_LEFTCTRL, 0)])
        for mime, data in self.desktop.original.items():
            self.assertEqual(await self.desktop.read_owned(mime), data)

    async def test_rapid_portal_pastes_reuse_the_original_clipboard(self):
        await self.paste.approve()
        self.assertEqual(await asyncio.wait_for(self.paste.paste("First.", False, lambda: True), .4), PASTE_SENT)
        self.assertEqual(await self.desktop.read_owned("text/plain"), b"First.")
        self.assertIsNotNone(self.paste.restore_task)
        self.assertEqual(await self.paste.paste("Second.", False, lambda: True), PASTE_SENT)
        self.assertEqual(await self.desktop.read_owned("text/plain"), b"Second.")
        self.assertEqual(len(self.desktop.owners), 2)
        await self.paste.wait_for_restore()
        self.assertEqual(len(self.desktop.owners), 3)
        for mime, data in self.desktop.original.items():
            self.assertEqual(await self.desktop.read_owned(mime), data)

    async def test_rapid_portal_paste_adopts_new_external_copy(self):
        await self.paste.approve()
        self.assertEqual(await self.paste.paste("First.", False, lambda: True), PASTE_SENT)
        changed = self.paste.selection_revision
        self.desktop.foreign_copy({"text/plain": b"Newer", "text/html": b"<b>Newer</b>"})
        await self.paste.wait_for_selection(changed)
        self.assertEqual(await self.paste.paste("Second.", False, lambda: True), PASTE_SENT)
        await self.paste.wait_for_restore()
        self.assertEqual(await self.desktop.read_owned("text/plain"), b"Newer")
        self.assertEqual(await self.desktop.read_owned("text/html"), b"<b>Newer</b>")

    async def test_data_control_keeps_one_lease_for_rapid_pastes(self):
        lease = SimpleNamespace(owned=lambda: True, offer=AsyncMock(return_value=True), close=AsyncMock())
        with (patch("wayland_paste.clipboard_capabilities", return_value=(Path("/fixture/clipboard"), True)),
              patch("wayland_paste.shutil.which", return_value="/fixture/wtype"),
              patch("wayland_paste.ClipboardLease.start", return_value=lease) as start,
              patch("wayland_paste.run_tool", return_value=b"")):
            await self.paste.approve()
            self.assertEqual(await self.paste.paste("First.", False, lambda: True), PASTE_SENT)
            lease.close.assert_not_awaited()
            self.assertEqual(await self.paste.paste("Second.", False, lambda: True), PASTE_SENT)
            self.assertEqual([arg.args[0] for arg in lease.offer.await_args_list], [b"First.", b"Second."])
            start.assert_awaited_once()
            lease.close.assert_not_awaited()
            await self.paste.wait_for_restore()
            lease.close.assert_awaited_once()

    async def test_portal_uses_the_current_layout_with_symbol_fallback(self):
        self.paste_keycodes.side_effect = [(29, 42, 52, 135), None]
        await self.paste.approve()
        with patch("wayland_paste.RESTORE_DELAY", 0):
            self.assertEqual(await self.paste.paste("Dvorak.", False, lambda: True), PASTE_SENT)
            self.assertEqual(await self.paste.paste("Changed layout.", True, lambda: True), PASTE_SENT)
        self.assertEqual(self.desktop.key_events, [
            (29, 1), (52, 1), (52, 0), (29, 0),
            (CONTROL_L, 1), (SHIFT_L, 1), (LATIN_V, 1), (LATIN_V, 0), (SHIFT_L, 0), (CONTROL_L, 0),
        ])
        self.assertEqual(self.desktop.key_methods, ["NotifyKeyboardKeycode"] * 4 + ["NotifyKeyboardKeysym"] * 6)
        self.assertEqual(self.paste_keycodes.await_count, 2)

    async def test_plasma_uses_the_selected_layout_and_does_not_guess_a_physical_v(self):
        self.paste_keycodes.side_effect = [(29, 42, -1, 135), (29, 42, 52, 135), (29, 42, 0, 135)]
        await self.paste.approve()
        with (patch.dict(os.environ, XDG_CURRENT_DESKTOP="KDE"),
              patch.object(self.paste, "kde_layout", return_value=1) as active_group,
              patch("wayland_paste.RESTORE_DELAY", 0)):
            self.assertEqual(await self.paste.paste("Dvorak.", True, lambda: True), PASTE_SENT)
            self.assertEqual(await self.paste.paste("Russian.", False, lambda: True), PASTE_SENT)
        self.assertEqual(self.desktop.key_events, [(29, 1), (42, 1), (52, 1), (52, 0), (42, 0), (29, 0),
                                                   (135, 1), (135, 0)])
        self.assertEqual(self.desktop.key_methods, ["NotifyKeyboardKeycode"] * 8)
        self.paste_keycodes.assert_any_await(1)
        self.assertEqual(active_group.await_count, 2)

    async def test_layout_changes_before_delivery_leave_the_clipboard_preserved(self):
        self.paste_keycodes.side_effect = [(29, 42, -1, 0), (29, 42, 52, 0)]
        await self.paste.approve()
        with (patch.dict(os.environ, XDG_CURRENT_DESKTOP="KDE"),
              patch.object(self.paste, "kde_layout", side_effect=[0, 1])):
            self.assertEqual(await self.paste.paste("Use Copy last.", False, lambda: True), PASTE_FALLBACK)
        self.assertEqual(self.desktop.key_events, [])
        for mime, data in self.desktop.original.items():
            self.assertEqual(await self.desktop.read_owned(mime), data)

    async def test_new_copy_is_left_in_place(self):
        await self.paste.approve()
        delivery = asyncio.create_task(self.paste.paste("New dictation.", False, lambda: True))
        await self.key_sent()
        self.desktop.foreign_copy({"text/plain": b"New copy"})
        self.assertEqual(await delivery, "Paste shortcut sent.")
        await self.paste.wait_for_restore()
        self.assertEqual(len(self.desktop.owners), 1)
        self.assertEqual(self.desktop.selection["text/plain"], b"New copy")

    async def test_gnome_clipboard_format_structure_preserves_contents(self):
        self.desktop.gnome_formats = True
        await self.paste.approve()
        delivery = asyncio.create_task(self.paste.paste("Local dictation.", False, lambda: True))
        await self.key_sent()
        self.assertEqual(await self.desktop.read_owned("text/plain"), b"Local dictation.")
        self.assertEqual(await delivery, "Paste shortcut sent.")
        await self.paste.wait_for_restore()
        self.assertEqual(await self.desktop.read_owned("image/png"), self.desktop.original["image/png"])

    async def test_gnome_portal_keeps_the_restored_clipboard_and_grant_while_idle(self):
        self.desktop.renewal = "approved-once"
        with patch.dict(os.environ, XDG_CURRENT_DESKTOP="GNOME"):
            await self.paste.approve()
            session = self.paste.session
            self.assertEqual(await self.paste.paste("First take.", False, lambda: True), PASTE_SENT)
            # The old adapter closed the portal after 15 seconds, releasing the clipboard.
            await asyncio.sleep(15.1)
            self.assertEqual(self.paste.session, session)
            self.assertEqual(await self.desktop.read_owned("image/png"), self.desktop.original["image/png"])
            self.assertEqual(await self.paste.paste("Second take.", False, lambda: True), PASTE_SENT)
            self.assertEqual(self.paste.restore_token, "approved-once")
            self.assertEqual(self.desktop.started, 1)

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
        await self.paste.wait_for_restore()
        self.assertEqual(self.desktop.owners[-1], [])

    async def test_gnome_clears_the_cached_transcript_if_the_clipboard_was_empty(self):
        self.desktop.selection = {}
        self.desktop.empty_restore_delay = .04
        await self.paste.approve()
        with (patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}),
              patch("wayland_paste.RESTORE_DELAY", 0)):
            self.assertEqual(await self.paste.paste("First dictation.", False, lambda: True), PASTE_SENT)
            await self.paste.wait_for_restore()
            self.assertFalse(self.paste.owns_selection)
            self.assertEqual(self.paste.formats, ())
            self.assertEqual(await self.paste.paste("Second dictation.", True, lambda: True), PASTE_SENT)
            await self.paste.wait_for_restore()
            self.assertFalse(self.paste.owns_selection)
            self.assertEqual(self.paste.formats, ())
        self.assertEqual(self.desktop.owners[1::3], [[EMPTY_RESTORE_FORMAT], [EMPTY_RESTORE_FORMAT]])
        self.assertEqual(self.desktop.owners[2::3], [[], []])

    async def test_gnome_empty_restore_preserves_a_new_clipboard_owner(self):
        self.desktop.selection = {}
        await self.paste.approve()

        def copy_during_restore(formats):
            if formats == [EMPTY_RESTORE_FORMAT]:
                self.desktop.foreign_copy({"text/plain": b"New copy"})

        self.desktop.on_set_selection = copy_during_restore
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertEqual(await self.paste.paste("First dictation.", False, lambda: True), PASTE_SENT)
            await self.paste.wait_for_restore()
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
