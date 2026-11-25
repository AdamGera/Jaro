"""Exercise the shortcut protocol on an isolated D-Bus session."""
import asyncio
from importlib.util import find_spec
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4


if sys.platform == "linux" and find_spec("dbus_fast"):
    from dbus_fast import Message, MessageType, Variant
    from dbus_fast.aio import MessageBus
    from linux_capture import CaptureError
    from linux_desktop_portal import APP_ID, DBUS, DESKTOP, PATH, REGISTRY, REQUEST, SESSION
    from portable_dictation_history import PortableDictationHistory
    from wayland_dictation import ANNOTATE, CANCEL, PASTE_LAST, RAW, SHORTCUT, SHORTCUTS, GlobalShortcuts, WaylandDictation


PRIMARY_LABEL = "Super+D"
RECOVERY_LABEL = "Ctrl+Alt+V"
CANCEL_LABEL = "Ctrl+Alt+Shift+X"
RAW_LABEL = "Ctrl+Alt+Shift+J"


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
        self.paste_label = RECOVERY_LABEL
        self.cancel_label = None
        self.annotation_label = None
        self.raw_label = None
        self.listed_raw = None
        self.reject_raw = False
        self.listed_annotation = None
        self.listed_shortcut = PRIMARY_LABEL
        self.listed_paste = RECOVERY_LABEL
        self.list_count = 0
        self.configuration = 0
        self.configure_failure = None
        self.primary_label = PRIMARY_LABEL
        self.was_closed = False
        self.request_closed = False

    async def open(self, address=None):
        self.bus = await MessageBus(bus_address=address).connect()
        await self.bus.request_name(DESKTOP)
        self.bus.add_message_handler(self.route)

    @staticmethod
    def labels(primary, recovery=None, cancel=None, annotate=None, raw=None):
        return [[key, {"trigger_description": Variant("s", label)}]
                for key, label in ((SHORTCUT, primary), (PASTE_LAST, recovery), (CANCEL, cancel),
                                   (ANNOTATE, annotate), (RAW, raw)) if label is not None]

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
                          [self.session, self.labels(self.shortcut_changed, self.paste_label)])
                self.notify("Activated")
            code = self.bind_failure if self.bind_failure is not None else (
                None if self.approve is None else int(not self.approve))
            if self.reject_raw and any(key == RAW for key, _ in self.bound):
                code = 1
            return self.response(message, code,
                                 {"shortcuts": Variant("a(sa{sv})", self.labels(
                                     self.primary_label, self.paste_label, self.cancel_label,
                                     self.annotation_label, self.raw_label))})
        if message.interface == SHORTCUTS and message.member == "ListShortcuts":
            self.list_count += 1
            return self.response(message, 0, {"shortcuts": Variant("a(sa{sv})", self.labels(
                self.listed_shortcut, self.listed_paste, annotate=self.listed_annotation, raw=self.listed_raw))})
        if message.interface == SHORTCUTS and message.member == "ConfigureShortcuts":
            self.configuration += 1
            if self.configure_failure:
                return Message.new_error(message, self.configure_failure, "Settings cannot open")
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
        self.portal = GlobalShortcuts(lambda key, pressed: self.events.append((key, pressed)), self.events.append,
                                      lambda: self.events.append("closed"))
        self.addAsyncCleanup(self.portal.close)

    async def wait_for(self, count):
        async with asyncio.timeout(3):
            while len(self.events) < count:
                await asyncio.sleep(.01)

    async def test_annotation_only_assignment_restores_and_scopes_tap_and_hold(self):
        from portable_annotation_shortcut import AnnotationShortcut

        self.desktop.primary_label = self.desktop.listed_shortcut = None
        self.desktop.paste_label = self.desktop.listed_paste = None
        self.desktop.annotation_label = self.desktop.listed_annotation = "Ctrl+Alt+Shift+S"
        state = SimpleNamespace(draining=False, portable_screencast=None)
        pen = SimpleNamespace(active=False, open=AsyncMock(), close=AsyncMock())
        take = SimpleNamespace(phase="recording", saving=False, stopped=False, started=1, pen=pen, region=None,
                               draft=SimpleNamespace(key=str(uuid4())))

        async def started(_):
            state.portable_screencast = take

        async def finished(_):
            state.portable_screencast = None

        start, finish = AsyncMock(side_effect=started), AsyncMock(side_effect=finished)
        hotkey = state.portable_annotation_shortcut = AnnotationShortcut(state, start, finish)
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(state, None, history)
            self.addAsyncCleanup(dictation.close)
            dictation.enable(restoring=True)
            await dictation.approval
            self.assertEqual(dictation.annotation_shortcut, self.desktop.annotation_label)
            self.assertFalse(dictation.enabled)
            self.assertEqual([key for key, _ in self.desktop.bound], [ANNOTATE])

            async def wait_until(condition):
                async with asyncio.timeout(2):
                    while not condition():
                        await asyncio.sleep(.005)

            async def press():
                self.desktop.notify("Activated", shortcut=ANNOTATE)
                await wait_until(lambda: hotkey.pressed is not None)

            async def release():
                self.desktop.notify("Deactivated", shortcut=ANNOTATE)
                await wait_until(lambda: hotkey.pressed is None and hotkey.action is not None and hotkey.action.done())

            await press()
            self.desktop.notify("Activated", shortcut=ANNOTATE)  # Physical auto-repeat.
            await release()
            start.assert_awaited_once_with(None)

            await press()
            await wait_until(lambda: pen.open.await_count == 1)
            await release()
            pen.close.assert_awaited_once()
            finish.assert_not_awaited()

            await press()
            replacement = SimpleNamespace(**vars(take))
            state.portable_screencast = replacement
            await release()
            finish.assert_not_awaited()

            # Revoking a binding while its drawing surface is up must end drawing without saving.
            await press()
            await wait_until(lambda: pen.open.await_count == 2)
            self.desktop.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})", [self.desktop.session, []])
            await wait_until(lambda: pen.close.await_count == 2)
            self.assertEqual(dictation.annotation_shortcut, "")
            finish.assert_not_awaited()
            self.desktop.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})",
                              [self.desktop.session, self.desktop.labels(None, annotate=self.desktop.annotation_label)])
            await wait_until(lambda: bool(dictation.annotation_shortcut))
            await press()
            await release()
            finish.assert_awaited_once_with(replacement)
            self.assertEqual(hotkey.saved, take.draft.key)
            dismissed = SimpleNamespace(**{**vars(take), "phase": "cancelled"})
            state.portable_screencast = dismissed
            await press()
            await release()
            start.assert_awaited_with(dismissed)
            await hotkey.close()

    async def test_gnome_annotate_toggles_only_when_added_and_a_fresh_key_is_pressed(self):
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            annotation = Mock()
            state = SimpleNamespace(draining=False, portable_annotation_shortcut=annotation)
            dictation = WaylandDictation(state, None, history)
            self.addAsyncCleanup(dictation.close)
            with self.assertRaisesRegex(CaptureError, "Enable desktop shortcut commands"):
                await dictation.run_command("gnome-annotate")
            dictation.command_mode = dictation.gnome_enabled = True
            with patch("linux_dictation.monotonic", return_value=1):
                with self.assertRaisesRegex(CaptureError, "Update Jaro"):
                    await dictation.run_command("gnome-annotate")
            annotation.tap.assert_not_called()
            dictation.gnome_annotation = True
            with patch("linux_dictation.monotonic", side_effect=[3, 3.45, 3.49, 5, 7]):
                self.assertEqual(await dictation.run_command("gnome-annotate"), "queued")
                await dictation.run_command("gnome-annotate")
                await dictation.run_command("gnome-annotate")
                annotation.tap.assert_called_once_with()
                self.assertEqual(await dictation.run_command("gnome-annotate"), "queued")
                dictation.gnome_enabled = False
                with self.assertRaisesRegex(CaptureError, "Add GNOME shortcuts"):
                    await dictation.run_command("gnome-annotate")
                self.assertEqual(annotation.tap.call_count, 2)

    async def test_new_take_invalidates_earlier_portal_paste_destination(self):
        from wayland_paste import PASTE_FALLBACK

        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
            keyboard = SimpleNamespace(paste=AsyncMock(), active=True)
            dictation.keyboard = dictation.paste_target = keyboard
            dictation.command_mode = True
            earlier = dictation.prepare_delivery()
            dictation.watch_focus()
            dictation.paste_target = keyboard
            self.assertEqual(await earlier("Earlier result"), PASTE_FALLBACK)
            keyboard.paste.assert_not_called()

    async def test_paste_last_keeps_the_field_focused_at_shortcut_press(self):
        from portable_dictation import PASTE_SENT
        from wayland_paste import PASTE_FALLBACK

        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
            dictation.portal = SimpleNamespace(held={PASTE_LAST})
            keyboard = dictation.keyboard = SimpleNamespace(paste=AsyncMock(return_value=PASTE_SENT), active=True)
            field = dictation.field = SimpleNamespace(focus=(":1.8", "/first", 1), snapshot=AsyncMock(return_value=None))
            dictation.text = "Retained dictation."
            dictation.on_shortcut(PASTE_LAST, True)
            field.focus = (":1.8", "/second", 2)
            dictation.portal.held.clear()
            dictation.on_shortcut(PASTE_LAST, False)
            await dictation.action
            self.assertEqual(dictation.feedback, PASTE_FALLBACK)
            keyboard.paste.assert_not_awaited()

            dictation.portal.held.add(PASTE_LAST)
            dictation.on_shortcut(PASTE_LAST, True)
            dictation.portal.held.clear()
            dictation.on_shortcut(PASTE_LAST, False)
            await dictation.action
            self.assertEqual(dictation.feedback, PASTE_SENT)
            self.assertEqual(keyboard.paste.await_args.args[0], dictation.text)

    async def test_bind_updates_activation_and_close(self):
        self.assertEqual(await self.portal.bind(), {SHORTCUT: PRIMARY_LABEL, PASTE_LAST: RECOVERY_LABEL})
        self.assertEqual(self.desktop.registered, APP_ID)
        self.assertEqual({key: props["preferred_trigger"].value for key, props in self.desktop.bound},
                         {SHORTCUT: "CTRL+ALT+SHIFT+d", PASTE_LAST: "CTRL+ALT+v", CANCEL: "CTRL+ALT+SHIFT+x"})
        self.desktop.notify("Activated", shortcut="another")
        self.desktop.notify("Activated", session=PATH + "/session/another")
        self.desktop.notify("Activated")
        self.desktop.notify("Activated")
        self.desktop.notify("Activated", shortcut=PASTE_LAST)
        self.desktop.notify("Activated", shortcut=PASTE_LAST)
        self.desktop.notify("Deactivated", shortcut=PASTE_LAST)
        self.desktop.notify("Deactivated")
        self.desktop.notify("Activated")
        self.desktop.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})",
                          [self.desktop.session, self.desktop.labels("Alt+D")])
        self.desktop.notify("Activated", shortcut=PASTE_LAST)
        self.desktop.notify("Deactivated", shortcut=PASTE_LAST)
        await self.wait_for(6)
        self.assertEqual(self.events, [(SHORTCUT, True), (PASTE_LAST, True), (PASTE_LAST, False),
                                       (SHORTCUT, False), (SHORTCUT, True), {SHORTCUT: "Alt+D"}])
        self.assertFalse(self.portal.held)
        await self.portal.call(SHORTCUTS, "ConfigureShortcuts", "osa{sv}", [self.portal.session, "", {}])
        self.assertEqual(self.desktop.configuration, 1)
        await self.portal.close()
        self.assertTrue(self.desktop.was_closed)

    async def test_configure_can_retry_temporary_failure_and_keeps_assigned_shortcut(self):
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
            self.addAsyncCleanup(dictation.close_portal)
            dictation.restore_paste = Mock()
            dictation.enable()
            await dictation.approval
            self.assertTrue(dictation.enabled)
            self.assertTrue(dictation.snapshot()["configurable"])

            self.desktop.configure_failure = DBUS + ".Error.NoReply"
            with self.assertRaisesRegex(CaptureError, "Could not open"):
                await dictation.configure_shortcut()
            self.assertTrue(dictation.snapshot()["configurable"])
            self.desktop.configure_failure = None
            await dictation.configure_shortcut()

            self.desktop.configure_failure = DBUS + ".Error.UnknownMethod"
            with self.assertRaisesRegex(CaptureError, "Open your desktop's settings"):
                await dictation.configure_shortcut()
            self.assertFalse(dictation.snapshot()["configurable"])
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertFalse(self.desktop.was_closed)
            with self.assertRaises(CaptureError):
                await dictation.configure_shortcut()
            self.assertEqual(self.desktop.configuration, 3)

    async def test_raw_shortcut_rebind_preserves_approved_bindings(self):
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
            self.addAsyncCleanup(dictation.close_portal)
            dictation.restore_paste = Mock()
            dictation.enable()
            await dictation.approval
            self.assertFalse(dictation.snapshot()["portalRawRequested"])
            self.assertNotIn(RAW, dict(self.desktop.bound))
            keyboard = dictation.keyboard = SimpleNamespace(active=True)

            self.desktop.reject_raw = True
            await dictation.change_raw_shortcut(True)
            await dictation.approval
            self.assertTrue(history.portal_raw_shortcut())
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertEqual(dictation.recovery_shortcut, RECOVERY_LABEL)
            self.assertEqual(dictation.raw_shortcut, "")
            self.assertIs(dictation.keyboard, keyboard)
            self.assertNotIn(RAW, dict(self.desktop.bound))

            self.desktop.reject_raw = False
            self.desktop.raw_label = RAW_LABEL
            await dictation.change_raw_shortcut(True)
            await dictation.approval
            self.assertEqual(dictation.raw_shortcut, RAW_LABEL)
            self.assertEqual(dict(self.desktop.bound)[RAW]["preferred_trigger"].value, "CTRL+ALT+SHIFT+j")
            self.assertNotIn("preferred_trigger", dict(self.desktop.bound)[SHORTCUT])
            self.assertNotIn("preferred_trigger", dict(self.desktop.bound)[PASTE_LAST])
            self.assertIs(dictation.keyboard, keyboard)

            self.desktop.listed_raw = RAW_LABEL
            await dictation.close_portal()
            dictation.enable(restoring=True)
            await dictation.approval
            self.assertEqual(dictation.raw_shortcut, RAW_LABEL)
            self.assertNotIn("preferred_trigger", dict(self.desktop.bound)[RAW])
            self.desktop.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})",
                              [self.desktop.session, self.desktop.labels(PRIMARY_LABEL, RECOVERY_LABEL, raw=PRIMARY_LABEL)])
            async with asyncio.timeout(2):
                while dictation.raw_shortcut:
                    await asyncio.sleep(.01)
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertIn("different desktop shortcut", dictation.feedback)

            await dictation.change_raw_shortcut(False)
            await dictation.approval
            self.desktop.notify("Activated", shortcut=RAW)
            await asyncio.sleep(.02)
            self.assertEqual(dictation.raw_shortcut, "")
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertNotIn(RAW, dictation.portal.held)
            self.assertFalse(history.portal_raw_shortcut())

    async def test_gnome_reconfigure_keeps_current_assignments_when_list_is_empty(self):
        saved = {SHORTCUT, PASTE_LAST}
        with (TemporaryDirectory() as folder, patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}),
              patch("gnome_shortcuts.portal_shortcut_ids", return_value=saved) as saved_shortcuts):
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
            self.addAsyncCleanup(dictation.close_portal)
            dictation.restore_paste = Mock()
            dictation.enable()
            await dictation.approval
            self.desktop.listed_shortcut = self.desktop.listed_paste = None
            self.desktop.reject_raw = True
            await dictation.change_raw_shortcut(True)
            await dictation.approval
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertEqual(dictation.recovery_shortcut, RECOVERY_LABEL)
            self.assertNotIn(RAW, dict(self.desktop.bound))

            self.desktop.raw_label = RAW_LABEL
            self.desktop.reject_raw = False
            await dictation.change_raw_shortcut(True)
            await dictation.approval
            self.assertEqual(dictation.raw_shortcut, RAW_LABEL)
            self.assertNotIn("preferred_trigger", dict(self.desktop.bound)[SHORTCUT])
            saved.add(RAW)
            await dictation.close_portal()
            dictation.enable(restoring=True)
            await dictation.approval
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertEqual(dictation.raw_shortcut, RAW_LABEL)
            self.assertTrue(all("preferred_trigger" not in options for _, options in self.desktop.bound))
            saved_shortcuts.assert_called_with(APP_ID)

            await dictation.close_portal()
            saved.remove(RAW)
            dictation.enable(restoring=True)
            await dictation.approval
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertEqual(dictation.raw_shortcut, "")
            self.assertNotIn(RAW, dict(self.desktop.bound))

            saved_shortcuts.return_value = None
            await dictation.change_raw_shortcut(False)
            await dictation.approval
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertEqual(dictation.raw_shortcut, "")
            self.assertNotIn(RAW, dict(self.desktop.bound))

            await dictation.close_portal()
            saved.clear()
            saved_shortcuts.return_value = saved
            self.desktop.bound = None
            dictation.enable(restoring=True)
            await dictation.approval
            self.assertIsNone(self.desktop.bound)
            self.assertFalse(dictation.enabled)
            self.assertIn("no saved dictation", dictation.feedback)

    async def test_raw_portal_gesture_and_desktop_command_keep_capture_flavor(self):
        from portable_capture import private_wav
        from portable_dictation import PASTE_SENT
        from wayland_dictation_commands import dispatch, listen

        def recorder(_key, path, _mode, **_options):
            async def start():
                with private_wav(path) as (wav, _):
                    wav.writeframes(b"\x11\x22" * 1600)

            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False,
                                   start=start, stop=AsyncMock(), finish_dictation=AsyncMock())

        async def wait_until(condition):
            async with asyncio.timeout(2):
                while not condition():
                    await asyncio.sleep(.005)

        with TemporaryDirectory() as folder:
            root = Path(folder)
            history = PortableDictationHistory(root)
            history.initialize()
            history.save_portal_raw_shortcut(True)
            history.configure(linux_shortcut_mode="doubleTap")
            state = SimpleNamespace(draining=False, library=SimpleNamespace(root=root), status="ready",
                                    portable_recording=None, portable_recording_lock=asyncio.Lock(),
                                    portable_recorder=recorder)
            transcribe = AsyncMock(return_value={"text": "original speech", "inferenceMs": 10})
            dictation = WaylandDictation(state, transcribe, history)
            self.addAsyncCleanup(dictation.close)
            keyboard = dictation.keyboard = SimpleNamespace(active=True, paste=AsyncMock(return_value=PASTE_SENT),
                                                            close=AsyncMock())
            dictation.replacements = "original => updated"
            dictation.vocabulary_entries = [{"term": "Unrelated", "soundsLike": []}]
            self.desktop.raw_label = RAW_LABEL
            dictation.enable()
            await dictation.approval

            async def tap(key):
                self.desktop.notify("Activated", shortcut=key)
                await wait_until(lambda: key in dictation.portal.held)
                self.desktop.notify("Deactivated", shortcut=key)
                await wait_until(lambda: key not in dictation.portal.held)

            await tap(RAW)
            await tap(SHORTCUT)
            self.assertEqual(dictation.phase, "idle")
            await tap(RAW)
            await wait_until(lambda: dictation.phase == "recording")
            self.assertTrue(dictation.capture_raw)
            await tap(SHORTCUT)
            self.assertEqual(dictation.phase, "recording")
            await tap(RAW)
            await wait_until(lambda: dictation.phase == "idle" and not dictation.action_pending())
            self.assertEqual(transcribe.await_args.kwargs["vocabulary"], ())
            self.assertEqual(keyboard.paste.await_args.args[0], "original speech")
            self.assertEqual(history.page()["entries"][0]["kind"], "raw")

            keyboard.active = False
            self.assertFalse(dictation.command_mode)
            self.assertEqual(await dictation.run_command("start-raw"), "starting")
            await dictation.action
            transcribe.return_value = {"text": "Printed words as heard.", "inferenceMs": 10}
            self.assertEqual(await dictation.run_command("stop-print"),
                             {"status": "transcript", "text": "Printed words as heard."})
            keyboard.paste.assert_awaited_once()
            self.assertEqual(dictation.snapshot()["text"], "original speech")
            self.assertEqual(history.page()["entries"][0]["kind"], "printed")
            keyboard.active = True
            transcribe.return_value = {"text": "original speech", "inferenceMs": 10}

            await dictation.close_portal()
            dictation.command_mode = True
            self.assertEqual(await dictation.run_command("start-raw"), "starting")
            await dictation.action
            self.assertTrue(dictation.capture_raw)
            self.assertEqual(await dictation.run_command("toggle"), "recording")
            self.assertEqual(await dictation.run_command("stop"), "transcribing")
            await wait_until(lambda: dictation.phase == "idle" and not dictation.action_pending())
            self.assertEqual(keyboard.paste.await_args.args[0], "original speech")
            self.assertEqual([entry["kind"] for entry in history.page()["entries"]], ["raw", "printed", "raw"])

            submitted = asyncio.Event()

            async def wait_for_inference(*_args, **_kwargs):
                submitted.set()
                await asyncio.Future()

            transcribe.side_effect = wait_for_inference
            listener = await listen(root, dictation.run_command)
            try:
                self.assertEqual(await dictation.run_command("start"), "starting")
                await dictation.action
                printing = asyncio.create_task(asyncio.to_thread(dispatch, root, "stop", printed=True))
                await asyncio.wait_for(submitted.wait(), 2)
                listener.close()
                await dictation.close()
                with self.assertRaisesRegex(RuntimeError, "Dictation canceled"):
                    await printing
                self.assertEqual([entry["kind"] for entry in history.page()["entries"]], ["raw", "printed", "raw"])
            finally:
                listener.close()
                await listener.wait_closed()

    async def test_unsupported_configure_releases_an_unassigned_portal_for_retry(self):
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
            self.addAsyncCleanup(dictation.close_portal)
            self.desktop.primary_label = None
            self.desktop.configure_failure = DBUS + ".Error.NotSupported"
            dictation.enable()
            await dictation.approval
            self.assertFalse(dictation.enabled)
            self.assertTrue(dictation.snapshot()["configurable"])

            with self.assertRaisesRegex(CaptureError, "Open your desktop's settings"):
                await dictation.configure_shortcut()
            self.assertFalse(dictation.snapshot()["configurable"])
            self.assertTrue(self.desktop.was_closed)
            self.assertIsNone(dictation.portal)

            self.desktop.primary_label = PRIMARY_LABEL
            dictation.restore_paste = Mock()
            dictation.enable()
            await dictation.approval
            self.assertEqual(dictation.shortcut, PRIMARY_LABEL)
            self.assertTrue(dictation.enabled)

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

    async def test_restore_uses_only_previous_portal_assignments(self):
        self.desktop.listed_paste = None
        self.desktop.paste_label = None
        self.assertEqual(await self.portal.bind(restoring=True), {SHORTCUT: PRIMARY_LABEL})
        self.assertEqual(self.desktop.list_count, 1)
        self.assertEqual([key for key, _options in self.desktop.bound], [SHORTCUT])
        self.assertNotIn("preferred_trigger", self.desktop.bound[0][1])
        await self.portal.close()

        self.desktop.listed_shortcut = ""
        self.desktop.bound = None
        revoked = GlobalShortcuts(Mock(), Mock(), Mock())
        self.addAsyncCleanup(revoked.close)
        with self.assertRaisesRegex(CaptureError, "Choose Enable shortcut"):
            await revoked.bind(restoring=True)
        self.assertEqual(self.desktop.list_count, 2)
        self.assertIsNone(self.desktop.bound)
        self.assertFalse(revoked.confirmed)

    async def test_gnome_can_confirm_assignment_despite_bind_status(self):
        self.desktop.bind_failure = 2
        self.desktop.shortcut_changed = PRIMARY_LABEL
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}):
            self.assertEqual(await self.portal.bind(), {SHORTCUT: PRIMARY_LABEL, PASTE_LAST: RECOVERY_LABEL})
        self.assertEqual(self.desktop.list_count, 1)
        self.assertEqual(self.events, [])
        self.desktop.notify("Activated")
        await self.wait_for(1)
        self.assertEqual(self.events, [(SHORTCUT, True)])

    async def test_gnome_keeps_primary_when_only_it_can_be_confirmed(self):
        self.desktop.bind_failure = 2
        self.desktop.shortcut_changed = PRIMARY_LABEL
        self.desktop.listed_paste = None
        with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.assertEqual(await self.portal.bind(), {SHORTCUT: PRIMARY_LABEL})
        self.desktop.notify("Activated", shortcut=PASTE_LAST)
        self.desktop.notify("Activated")
        await self.wait_for(1)
        self.assertEqual(self.events, [(SHORTCUT, True)])

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

    async def test_paste_last_waits_for_release_and_obeys_permission_and_history(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        history = PortableDictationHistory(folder.name)
        history.initialize()
        key = str(uuid4())
        phrase = "Paste this transcript."
        saved, epoch = history.record({"id": key, "text": phrase, "time": time(), "app": "",
                                       "seconds": 1, "inferenceMs": 120, "totalMs": 300})
        self.assertTrue(saved)
        dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
        self.addAsyncCleanup(dictation.close)
        dictation.text, dictation.last_id, dictation.last_clear_epoch = phrase, key, epoch
        dictation.enable()
        await dictation.approval
        self.assertEqual(dictation.snapshot()["recoveryShortcut"], RECOVERY_LABEL)
        inserted = []

        class Keyboard:
            active = True

            async def paste(self, text, terminal, allowed):
                if allowed():
                    inserted.append((text, terminal))
                    return "Paste shortcut sent."
                return "Use Copy last."

            async def close(self):
                self.active = False

        async def wait_for(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)

        def press():
            self.desktop.notify("Activated", shortcut=PASTE_LAST)

        def release():
            self.desktop.notify("Deactivated", shortcut=PASTE_LAST)

        press()
        release()
        await wait_for(lambda: "Allow automatic paste" in dictation.feedback)
        dictation.keyboard = Keyboard()
        dictation.terminal = True
        press()
        await wait_for(lambda: dictation.phase == "pasting")
        press()
        self.assertEqual(inserted, [])
        release()
        await wait_for(lambda: dictation.phase == "idle")
        self.assertEqual(inserted, [(phrase, True)])

        press()
        await wait_for(lambda: dictation.phase == "pasting")
        await dictation.disable_paste()
        release()
        await wait_for(lambda: dictation.phase == "idle")
        self.assertEqual(inserted, [(phrase, True)])

        dictation.keyboard = Keyboard()
        history.remove()
        press()
        release()
        await wait_for(lambda: "no last dictation" in dictation.feedback)
        self.assertEqual(inserted, [(phrase, True)])

    async def test_deduplicated_portal_edges_drive_holds_and_double_taps(self):
        from portable_capture import private_wav

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        history = PortableDictationHistory(root)
        history.initialize()
        startup = asyncio.Event()
        startup.set()
        take_count = 0

        def recorder(_key, path, _mode, **_options):
            async def start():
                await startup.wait()
                with private_wav(path) as (wav, _):
                    wav.writeframes(b"\x11\x22" * 1600)

            async def finish_dictation(_released_at):
                await asyncio.sleep(.2)

            nonlocal take_count
            take_count += 1
            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, start=start, stop=AsyncMock(),
                                   finish_dictation=finish_dictation)

        state = SimpleNamespace(draining=False, library=SimpleNamespace(root=root), status="ready",
                                portable_recording=None, portable_recording_lock=asyncio.Lock(),
                                portable_recorder=recorder)
        transcribe = AsyncMock(return_value={"text": "Finished dictation", "inferenceMs": 10})
        dictation = WaylandDictation(state, transcribe, history)
        self.addAsyncCleanup(dictation.close)
        self.desktop.cancel_label = CANCEL_LABEL
        dictation.enable()
        await dictation.approval
        self.assertEqual(dictation.snapshot()["cancelShortcut"], CANCEL_LABEL)

        async def wait_for(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)

        async def edge(pressed):
            self.desktop.notify("Activated" if pressed else "Deactivated")
            await wait_for(lambda: (SHORTCUT in dictation.portal.held) == pressed)

        async def finished(phase):
            await wait_for(lambda: dictation.phase == phase and not dictation.action_pending())

        await dictation.change_mode("hold")
        startup.clear()
        await edge(True)
        await wait_for(lambda: dictation.phase == "starting")
        await edge(False)
        await finished("idle")
        transcribe.assert_not_called()

        startup.set()
        await edge(True)
        await finished("recording")
        await edge(True)
        self.assertEqual(dictation.phase, "recording")
        await edge(False)
        await wait_for(lambda: dictation.phase == "transcribing")
        transcribe.assert_not_called()  # The release tail is still recording.
        await finished("idle")
        transcribe.assert_awaited_once()

        await dictation.change_mode("holdDouble")
        await edge(True)
        await finished("recording")
        await edge(False)
        await edge(True)
        await edge(False)
        await finished("recording")
        self.assertEqual(take_count, 3)
        await edge(True)
        await edge(False)
        await finished("idle")
        self.assertEqual(transcribe.await_count, 2)

        await dictation.change_mode("doubleTap")
        await edge(True)
        await edge(False)
        self.assertEqual(take_count, 3)
        await edge(True)
        await edge(False)
        await finished("recording")
        self.desktop.notify("Activated", shortcut=CANCEL)
        self.desktop.notify("Activated", shortcut=CANCEL)
        await finished("idle")
        self.assertEqual(dictation.feedback, "Dictation canceled.")
        self.assertEqual(transcribe.await_count, 2)
        self.desktop.notify("Deactivated", shortcut=CANCEL)

        await edge(True)
        await edge(False)
        await edge(True)
        await edge(False)
        await finished("recording")
        self.desktop.send(PATH, SHORTCUTS, "ShortcutsChanged", "oa(sa{sv})",
                          [self.desktop.session, self.desktop.labels("", RECOVERY_LABEL)])
        await wait_for(lambda: not dictation.shortcut)
        await finished("idle")
        self.assertEqual(transcribe.await_count, 2)
        self.assertEqual(history.linux_shortcut_mode(), "doubleTap")

    async def test_gnome_registration_rolls_back_and_cannot_compete_with_the_portal(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        history = PortableDictationHistory(folder.name)
        history.initialize()
        dictation = WaylandDictation(SimpleNamespace(draining=False), None, history)
        self.addAsyncCleanup(dictation.close)
        dictation.command_prefix = "jardctl dictation"
        dictation.gnome = Mock()

        dictation.portal = self.portal
        with self.assertRaisesRegex(CaptureError, "Disable the portal"):
            await dictation.add_gnome_shortcuts()
        dictation.gnome.install.assert_not_called()
        dictation.portal = None

        dictation.gnome.install.side_effect = CaptureError("Settings write failed.")
        with self.assertRaisesRegex(CaptureError, "Settings write failed"):
            await dictation.add_gnome_shortcuts()
        dictation.gnome.remove.assert_called_once()
        self.assertFalse(dictation.command_mode)
        self.assertFalse(history.commands_enabled())

        dictation.gnome.install.side_effect = None
        dictation.gnome.install.return_value = False
        with patch("gnome_shortcuts.repeat_window", return_value=1):
            await dictation.add_gnome_shortcuts()
        self.assertTrue(dictation.snapshot()["gnomeShortcuts"])
        with self.assertRaisesRegex(CaptureError, "Allow automatic paste"):
            await dictation.run_command("paste-last")
        self.assertIn("Allow automatic paste", dictation.feedback)
        dictation.text = "Retained dictation"
        async def paste(text, terminal, allowed):
            self.assertEqual(text, "Retained dictation")
            self.assertTrue(allowed())
            return "Paste shortcut sent."
        dictation.keyboard = SimpleNamespace(active=True, paste=paste, close=AsyncMock())
        self.assertEqual(await dictation.run_command("gnome-paste-last"), "pasted")
        self.assertEqual(dictation.feedback, "Paste shortcut sent.")
        await dictation.remove_gnome_shortcuts()
        self.assertTrue(history.commands_enabled())
        self.assertFalse(dictation.snapshot()["gnomeShortcuts"])

    async def test_gnome_key_repeats_keep_the_current_take_recording(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        history = PortableDictationHistory(folder.name)
        history.initialize()
        state = SimpleNamespace(draining=False, status="ready", portable_recording=None)
        dictation = WaylandDictation(state, None, history)
        self.addAsyncCleanup(dictation.close)
        dictation.command_mode = dictation.gnome_enabled = True
        dictation.schedule = Mock()
        with patch("linux_dictation.monotonic", side_effect=[1, 1.65, 2.25, 3.3, 3.4]):
            self.assertEqual(await dictation.run_command("gnome-toggle"), "starting")
            dictation.phase = "recording"
            self.assertEqual(await dictation.run_command("gnome-toggle"), "recording")
            self.assertEqual(await dictation.run_command("gnome-toggle"), "recording")
            self.assertEqual(await dictation.run_command("gnome-toggle"), "transcribing")
            self.assertEqual(await dictation.run_command("toggle"), "transcribing")
        self.assertEqual(dictation.schedule.call_count, 3)
