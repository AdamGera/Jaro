"""Exercise clipboard ownership and keyboard delivery against an isolated X server."""
import asyncio
from contextlib import suppress
import os
import select
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
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
        self.assertEqual(await self.current(), data)

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
