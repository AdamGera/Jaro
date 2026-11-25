"""Exercise clipboard ownership and keyboard delivery against an isolated X server."""
import asyncio
from contextlib import suppress
import os
import select
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4


@unittest.skipUnless(shutil.which("Xvfb"), "requires an isolated X11 server")
class X11PasteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from Xlib import X, XK, Xatom, display
            from x11_paste import X11Paste
        except ImportError:
            self.skipTest("requires python-xlib")
        self.X, self.XK, self.Xatom = X, XK, Xatom
        read_fd, write_fd = os.pipe()
        try:
            server = subprocess.Popen(["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "640x480x24",
                                       "-nolisten", "tcp"], pass_fds=(write_fd,), stdout=subprocess.DEVNULL)
            self.addCleanup(server.wait, 5)
            self.addCleanup(server.terminate)
            os.close(write_fd)
            write_fd = None
            ready, _, _ = select.select([read_fd], [], [], 5)
            self.assertTrue(ready, "Xvfb did not start")
            address = ":" + os.read(read_fd, 32).decode().strip()
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        self.address = address
        self.connections = [display.Display(address) for _ in range(3)]
        for connection in self.connections:
            self.addCleanup(connection.close)
        self.paster, self.source, self.reader = map(X11Paste, self.connections)
        self.root = self.reader.connection.screen().root
        self.target = self.root.create_window(0, 0, 640, 480, 0, X.CopyFromParent,
                                              event_mask=X.KeyPressMask)
        self.target.map()
        self.target.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.reader.connection.sync()
        self.target_focus = self.paster.connection.get_input_focus().focus
        self.keys = asyncio.Queue()
        pump = asyncio.create_task(self.pump_events())
        self.addAsyncCleanup(self.stop_task, pump)

    @staticmethod
    async def stop_task(task):
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def pump_events(self):
        while True:
            for client in (self.paster, self.source, self.reader):
                while client.connection.pending_events():
                    item = client.connection.next_event()
                    client.handle(item)
                    if client is self.reader and item.type == self.X.KeyPress:
                        self.keys.put_nowait(item)
            await asyncio.sleep(.003)

    async def set_clipboard(self, data):
        self.source.set_data(data, await self.source.server_time())
        self.source.connection.sync()

    async def receive(self, terminal=False):
        key = self.reader.connection.keysym_to_keycode(self.XK.string_to_keysym("v"))
        async with asyncio.timeout(2):
            while (pressed := await self.keys.get()).detail != key:
                pass
            self.assertTrue(pressed.state & self.X.ControlMask)
            self.assertEqual(bool(pressed.state & self.X.ShiftMask), terminal)
            return await self.reader.snapshot(self.reader.owner())

    async def current(self):
        async with asyncio.timeout(2):
            return await self.reader.snapshot(self.reader.owner())

    async def test_inserts_unicode_and_restores_all_clipboard_formats(self):
        text = self.reader.atoms["UTF8_STRING"]
        html = self.reader.connection.intern_atom("text/html")
        large = self.reader.connection.intern_atom("application/octet-stream")
        data = {text: (text, 8, b"previous"), html: (html, 8, b"<b>previous</b>"),
                large: (large, 8, b"0123456789" * 24_000)}
        await self.set_clipboard(data)
        delivery = asyncio.create_task(self.paster.paste("مرحبا بالعالم", self.target_focus, lambda: True))
        self.addAsyncCleanup(self.stop_task, delivery)
        pasted = await self.receive()
        self.assertEqual(pasted[text], (text, 8, "مرحبا بالعالم".encode()))
        self.assertEqual(await delivery, "Paste shortcut sent.")
        await self.paster.wait_for_restore()
        self.assertEqual(await self.current(), data)

    async def test_rapid_pastes_share_original_clipboard_and_finish_before_restore(self):
        text = self.reader.atoms["UTF8_STRING"]
        html = self.reader.connection.intern_atom("text/html")
        original = {text: (text, 8, b"original"), html: (html, 8, b"<b>original</b>")}
        await self.set_clipboard(original)
        first = asyncio.create_task(self.paster.paste("first", self.target_focus, lambda: True))
        self.assertEqual((await self.receive())[text][2], b"first")
        started = monotonic()
        self.assertEqual(await asyncio.wait_for(first, .35), "Paste shortcut sent.")
        self.assertLess(monotonic() - started, .4)
        self.assertEqual((await self.current())[text][2], b"first")
        second = asyncio.create_task(self.paster.paste("second", self.target_focus, lambda: True))
        self.assertEqual((await self.receive())[text][2], b"second")
        self.assertEqual(await second, "Paste shortcut sent.")
        await self.paster.wait_for_restore()
        self.assertEqual(await self.current(), original)

    async def test_rapid_paste_adopts_an_intervening_clipboard_copy(self):
        text = self.reader.atoms["UTF8_STRING"]
        original = {text: (text, 8, b"original")}
        newer = {text: (text, 8, b"newer user copy")}
        await self.set_clipboard(original)
        self.assertEqual(await self.paster.paste("first", self.target_focus, lambda: True), "Paste shortcut sent.")
        await self.set_clipboard(newer)
        self.assertEqual(await self.paster.paste("second", self.target_focus, lambda: True), "Paste shortcut sent.")
        await self.paster.wait_for_restore()
        self.assertEqual(await self.current(), newer)

    async def test_continuation_survives_queue_drain_and_checks_the_same_accessible_field(self):
        from portable_dictation_history import PortableDictationHistory
        from x11_dictation import X11Dictation

        with TemporaryDirectory() as workspace:
            history = PortableDictationHistory(workspace)
            history.initialize()
            dictation = X11Dictation(SimpleNamespace(draining=False), None, history)
            dictation.display = self.paster.connection
            dictation.paste = self.paster
            first = "We talked,"
            continuation = " and listened."
            focus = (":1.3", "/entry", 1)
            initial = SimpleNamespace(focus=focus, revision=1, caret=0, length=0)
            first_end = SimpleNamespace(focus=focus, revision=2, caret=len(first), length=len(first))
            second_end = SimpleNamespace(focus=focus, revision=3, caret=len(first + continuation),
                                         length=len(first + continuation))
            field = dictation.field = SimpleNamespace(
                focus=focus, snapshot=AsyncMock(side_effect=(initial, first_end, first_end, second_end)),
                matches=lambda before: before == first_end,
            )
            self.addAsyncCleanup(self.paster.wait_for_restore)
            text = self.reader.atoms["UTF8_STRING"]

            for phrase, expected in ((first, first), ("And listened.", continuation)):
                dictation.watch_focus()
                delivered = asyncio.create_task(dictation.prepare_delivery()(phrase))
                self.addAsyncCleanup(self.stop_task, delivered)
                self.assertEqual((await self.receive())[text][2], expected.encode())
                self.assertEqual(await delivered, "Paste shortcut sent.")
                dictation.clear_focus()

            dictation.watch_focus()
            deferred = dictation.prepare_delivery()
            field.focus = (":1.3", "/second-entry", 2)
            self.assertIn("Focus changed", await deferred("Do not insert here"))
            self.assertEqual(self.paster.connection.get_input_focus().focus, self.target_focus)

    async def test_preserves_new_copy_and_declines_paste_after_focus_lost(self):
        text = self.reader.atoms["UTF8_STRING"]
        await self.set_clipboard({text: (text, 8, b"original")})
        delivery = asyncio.create_task(self.paster.paste("dictation", self.target_focus, lambda: True, terminal=True))
        self.addAsyncCleanup(self.stop_task, delivery)
        self.assertEqual((await self.receive(terminal=True))[text][2], b"dictation")
        latest = {text: (text, 8, b"new copy")}
        await self.set_clipboard(latest)
        self.assertEqual(await delivery, "Paste shortcut sent.")
        self.assertEqual(await self.current(), latest)

        self.root.set_input_focus(self.X.RevertToNone, self.X.CurrentTime)
        self.reader.connection.sync()
        self.assertIn("Focus changed", await self.paster.paste("a second take", self.target_focus, lambda: True))
        self.assertEqual(await self.current(), latest)

    async def test_paste_last_shortcut_reuses_retained_text_and_honors_focus(self):
        from portable_dictation_history import PortableDictationHistory
        from x11_dictation import X11Dictation

        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        history = PortableDictationHistory(workspace.name)
        history.initialize()
        identifier = str(uuid4())
        phrase = "Dictated text"
        saved, epoch = history.record({"id": identifier, "text": phrase, "time": time(), "app": "Editor",
                                       "seconds": 1, "inferenceMs": 250, "totalMs": 400})
        self.assertTrue(saved)
        dictation = X11Dictation(SimpleNamespace(draining=False), None, history)
        self.addAsyncCleanup(dictation.close)
        with patch.dict(os.environ, DISPLAY=self.address):
            dictation.enable()
        dictation.text, dictation.last_id, dictation.last_clear_epoch = phrase, identifier, epoch
        self.assertEqual(dictation.snapshot()["recoveryShortcut"], "Ctrl+Alt+V")

        connection = self.reader.connection
        keys = [connection.keysym_to_keycode(self.XK.string_to_keysym(name))
                for name in ("Control_L", "Alt_L", "v")]

        def send(event, sequence):
            for key in sequence:
                connection.xtest_fake_input(event, key)
            connection.sync()

        async def press():
            send(self.X.KeyPress, keys)
            async with asyncio.timeout(2):
                while dictation.phase != "pasting":
                    await asyncio.sleep(.01)

        text = self.reader.atoms["UTF8_STRING"]
        original = {text: (text, 8, b"Clipboard before dictation")}
        await self.set_clipboard(original)
        await press()
        self.assertEqual(await self.current(), original)
        delivered = asyncio.create_task(self.receive())
        self.addAsyncCleanup(self.stop_task, delivered)
        send(self.X.KeyRelease, reversed(keys))
        self.assertEqual((await delivered)[text][2], phrase.encode())
        await dictation.action
        self.assertEqual(dictation.feedback, "Paste shortcut sent.")
        await dictation.paste.wait_for_restore()
        self.assertEqual(await self.current(), original)

        await press()
        self.root.set_input_focus(self.X.RevertToNone, self.X.CurrentTime)
        connection.sync()
        self.target.set_input_focus(self.X.RevertToParent, self.X.CurrentTime)
        connection.sync()
        send(self.X.KeyRelease, reversed(keys))
        await dictation.action
        self.assertIn("Focus changed", dictation.feedback)
        self.assertEqual(await self.current(), original)

        history.remove()
        send(self.X.KeyPress, keys)
        send(self.X.KeyRelease, reversed(keys))
        async with asyncio.timeout(2):
            while "no last dictation" not in dictation.feedback:
                await asyncio.sleep(.01)
        self.assertEqual(await self.current(), original)

        await dictation.disable()
        alt = next(1 << index for index, group in enumerate(connection.get_modifier_mapping()) if keys[1] in group)
        self.root.grab_key(keys[2], self.X.ControlMask | alt, False, self.X.GrabModeAsync, self.X.GrabModeAsync)
        connection.sync()
        with patch.dict(os.environ, DISPLAY=self.address):
            dictation.enable()
        self.assertTrue(dictation.enabled)
        self.assertIsNone(dictation.snapshot()["recoveryShortcut"])
        self.assertIn("Ctrl+Alt+V is unavailable", dictation.feedback)

    async def test_rebinding_keeps_the_working_shortcut_until_the_new_choice_is_saved(self):
        from portable_capture import CaptureError
        from portable_dictation_history import PortableDictationHistory
        from portable_shortcuts import shortcut_parts
        from x11_dictation import DEFAULT_SHORTCUT, X11Dictation

        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        history = PortableDictationHistory(workspace.name)
        history.initialize()
        state = SimpleNamespace(draining=False)
        dictation = X11Dictation(state, None, history)
        self.addAsyncCleanup(dictation.close)
        connection = self.reader.connection

        def key(name):
            return connection.keysym_to_keycode(self.XK.string_to_keysym(name))

        async def press(*names):
            for name in names:
                connection.xtest_fake_input(self.X.KeyPress, key(name))
            for name in reversed(names):
                connection.xtest_fake_input(self.X.KeyRelease, key(name))
            connection.sync()
            await asyncio.sleep(.15)

        self.assertEqual(shortcut_parts("shift+ctrl+PageUp"), ("Ctrl+Shift+PageUp", ["Ctrl", "Shift"], "Page_Up"))
        for invalid in ("A", "Ctrl+Ctrl+A", "Ctrl+", "Ctrl+F25", "Meta+Delete"):
            with self.subTest(invalid=invalid), self.assertRaises(CaptureError):
                shortcut_parts(invalid)

        with patch.dict(os.environ, DISPLAY=self.address), patch.object(dictation, "toggle_from_shortcut") as toggle:
            dictation.enable()
            previous = dictation.binding
            self.assertEqual(dictation.snapshot()["shortcut"], DEFAULT_SHORTCUT)
            self.assertEqual(dictation.snapshot()["defaultShortcut"], DEFAULT_SHORTCUT)
            await press("Control_L", "Alt_L", "Shift_L", "d")
            toggle.assert_called_once()

            # Holding just one Caps Lock variant should roll back the entire new grab.
            mask = self.X.ControlMask | self.X.LockMask
            self.root.grab_key(key("Page_Up"), mask, False, self.X.GrabModeAsync, self.X.GrabModeAsync)
            connection.sync()
            with self.assertRaisesRegex(CaptureError, "already in use"):
                await dictation.change_shortcut("Ctrl+PageUp")
            self.assertEqual(dictation.binding, previous)
            self.assertEqual(history.desktop_shortcut(), "")
            await press("Control_L", "Alt_L", "Shift_L", "d")
            self.assertEqual(toggle.call_count, 2)
            self.root.ungrab_key(key("Page_Up"), mask)
            connection.sync()

            with patch.object(history, "save_desktop_shortcut", side_effect=OSError("read only")):
                with self.assertRaisesRegex(CaptureError, "Could not save"):
                    await dictation.change_shortcut("Ctrl+PageUp")
            self.assertEqual(dictation.binding, previous)
            await press("Control_L", "Page_Up")
            self.assertEqual(toggle.call_count, 2)
            with self.assertRaisesRegex(CaptureError, "reserved for Paste last"):
                await dictation.change_shortcut("ctrl+alt+v")

            await dictation.change_shortcut("ctrl+pageup")
            self.assertEqual(history.desktop_shortcut(), "Ctrl+PageUp")
            self.assertEqual(dictation.snapshot()["recoveryShortcut"], "Ctrl+Alt+V")
            await press("Control_L", "Alt_L", "Shift_L", "d")
            self.assertEqual(toggle.call_count, 2)
            await press("Control_L", "Page_Up")
            self.assertEqual(toggle.call_count, 3)

            await dictation.remember_shortcut(True)
            await dictation.close()
            reopened = X11Dictation(state, None, history)
            self.addAsyncCleanup(reopened.close)
            self.assertEqual(reopened.snapshot()["shortcut"], "Ctrl+PageUp")
            self.assertTrue(reopened.shortcut_requested)
            with patch.object(reopened, "toggle_from_shortcut") as restored_toggle:
                reopened.enable()
                await press("Control_L", "Page_Up")
                restored_toggle.assert_called_once()

                await reopened.disable()
                await reopened.remember_shortcut(False)
                await reopened.change_shortcut("F9")
                self.assertFalse(reopened.enabled)
                self.assertFalse(reopened.shortcut_requested)
                reopened.enable()
                await press("Control_L", "Page_Up")
                self.assertEqual(restored_toggle.call_count, 1)
                await press("F9")
                self.assertEqual(restored_toggle.call_count, 2)

    async def test_raw_shortcut_preserves_text_and_only_stops_its_own_take(self):
        from pathlib import Path
        import wave

        from portable_capture import CaptureError
        from portable_dictation import PASTE_SENT
        from portable_dictation_history import PortableDictationHistory
        from x11_dictation import DEFAULT_SHORTCUT, X11Dictation

        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        history = PortableDictationHistory(root)
        history.initialize()
        history.save_desktop_shortcut("F9", raw=True)

        def recorder(_key, path, _mode, **_options):
            async def start():
                with path.open("xb") as audio:
                    path.chmod(0o600)
                    with wave.open(audio, "wb") as file:
                        file.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
                        file.writeframes(b"\x02\x01" * 1600)

            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False,
                                   start=start, stop=AsyncMock(), finish_dictation=AsyncMock())

        state = SimpleNamespace(draining=False, status="ready", library=SimpleNamespace(root=root),
                                portable_recording_lock=asyncio.Lock(), portable_recorder=recorder, portable_recording=None)
        transcribe = AsyncMock(return_value={"text": "original words", "inferenceMs": 123})
        dictation = X11Dictation(state, transcribe, history)
        dictation.sounds.play = lambda *_args, **_kwargs: None
        self.addAsyncCleanup(dictation.close)
        dictation.replacements = "original words => replaced words"
        dictation.vocabulary_entries = [{"term": "Vocabulary", "soundsLike": []}]
        connection = self.reader.connection
        original_clipboard = {self.reader.atoms["UTF8_STRING"]: (self.reader.atoms["UTF8_STRING"], 8, b"Existing copy")}
        await self.set_clipboard(original_clipboard)

        async def press(*names):
            keys = [connection.keysym_to_keycode(self.XK.string_to_keysym(name)) for name in names]
            for key in keys:
                connection.xtest_fake_input(self.X.KeyPress, key)
            for key in reversed(keys):
                connection.xtest_fake_input(self.X.KeyRelease, key)
            connection.sync()
            await asyncio.sleep(.13)

        async def receive_phrase(phrase):
            self.assertEqual((await self.receive())[self.reader.atoms["UTF8_STRING"]][2], phrase.encode())
            await dictation.output_task
            self.assertEqual(dictation.feedback, PASTE_SENT)
            await dictation.paste.wait_for_restore()
            self.assertEqual(await self.current(), original_clipboard)

        with patch.dict(os.environ, DISPLAY=self.address):
            dictation.enable()
            with self.assertRaisesRegex(CaptureError, "different shortcuts"):
                await dictation.change_shortcut(DEFAULT_SHORTCUT, raw=True)
            with self.assertRaisesRegex(CaptureError, "reserved for Paste last"):
                await dictation.change_shortcut("Ctrl+Alt+V", raw=True)
            regular = ("Control_L", "Alt_L", "Shift_L", "d")
            await press("F9")
            self.assertTrue(dictation.capture_raw)
            await press(*regular)
            self.assertEqual(dictation.phase, "recording")
            await press("F9")
            await receive_phrase("original words")
            self.assertEqual(transcribe.await_args.kwargs["vocabulary"], ())
            self.assertEqual(dictation.snapshot()["text"], "original words")
            self.assertEqual(history.page()["entries"][0]["kind"], "raw")
            await press("Control_L", "Alt_L", "v")
            await receive_phrase("original words")
            await dictation.action
            self.assertEqual(len(history.page()["entries"]), 1)

            await press(*regular)
            self.assertFalse(dictation.capture_raw)
            await press("F9")
            self.assertEqual(dictation.phase, "recording")
            await press(*regular)
            await receive_phrase("replaced words")
            self.assertEqual(transcribe.await_args.kwargs["vocabulary"], ("Vocabulary",))
            self.assertEqual([entry["kind"] for entry in history.page()["entries"]], ["dictation", "raw"])

            await dictation.change_shortcut("", raw=True)
            self.assertEqual(history.desktop_shortcut(raw=True), "")
            self.assertIsNone(dictation.raw_binding)
            await press("F9")
            self.assertEqual(dictation.phase, "idle")
