import asyncio
from contextlib import suppress
import json
from pathlib import Path
import shlex
import socket
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


if sys.platform == "linux":
    from linux_capture import CaptureError
    from wayland_dictation_commands import address, command_prefix, dispatch, listen


@unittest.skipUnless(sys.platform == "linux", "requires Linux Unix socket peer credentials")
class WaylandDictationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = TemporaryDirectory(prefix="Jaro tests '")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.actions = []
        self.server = await listen(self.root, self.perform)
        self.addAsyncCleanup(self.close)

    async def perform(self, action):
        if action == "paste-last":
            raise CaptureError("Allow automatic paste first.")
        self.actions.append(action)
        if action == "stop-print":
            return {"status": "transcript", "text": "Printed résumé. " * 300}
        return "recording"

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def raw(self, message):
        reader, writer = await asyncio.open_unix_connection(address(self.root))
        try:
            writer.write(message)
            writer.write_eof()
            return await asyncio.wait_for(reader.read(), 3)
        except ConnectionError:
            return b""
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def test_requires_active_workspace_for_library(self):
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "start"), {"status": "recording"})
        with self.assertRaisesRegex(RuntimeError, "Allow automatic paste"):
            await asyncio.to_thread(dispatch, self.root, "paste-last")
        with self.assertRaisesRegex(RuntimeError, "Open Jaro for this library"):
            await asyncio.to_thread(dispatch, self.root / "other", "start")
        await self.close()
        with self.assertRaisesRegex(RuntimeError, "Open Jaro for this library"):
            await asyncio.to_thread(dispatch, self.root, "start")
        self.assertEqual(self.actions, ["start"])

    async def test_invalid_and_unauthorized_requests_have_no_side_effect(self):
        self.assertEqual(await self.raw(b"start"), b"")
        self.assertEqual(await self.raw(b"remove\n"), b"")
        with patch("wayland_dictation_commands.same_user", return_value=False):
            self.assertEqual(await self.raw(b"start\n"), b"")
        self.assertEqual(self.actions, [])
        self.assertEqual(json.loads(await self.raw(b"stop\n")), {"status": "recording"})

    async def test_command_preserves_library_path(self):
        args = shlex.split(command_prefix(self.root))
        self.assertEqual(args[1], f"RUMBLE_DATA_DIR={self.root}")
        self.assertEqual(args[-1], "dictation")

    async def test_raw_commands_reach_the_current_library(self):
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "start", raw=True), {"status": "recording"})
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "toggle", raw=True), {"status": "recording"})
        with self.assertRaisesRegex(ValueError, "start or toggle"):
            await asyncio.to_thread(dispatch, self.root, "stop", raw=True)
        self.assertEqual(self.actions, ["start-raw", "toggle-raw"])

    async def test_gnome_commands_are_identifiable_to_the_workspace(self):
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "toggle", gnome=True), {"status": "recording"})
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "cancel", gnome=True), {"status": "recording"})
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "annotate", gnome=True), {"status": "recording"})
        with self.assertRaisesRegex(ValueError, "shortcut command"):
            await asyncio.to_thread(dispatch, self.root, "annotate")
        self.assertEqual(self.actions, ["gnome-toggle", "gnome-cancel", "gnome-annotate"])

    async def test_print_keeps_the_full_transcript_on_the_library_socket(self):
        with self.assertRaisesRegex(ValueError, "print with stop"):
            await asyncio.to_thread(dispatch, self.root, "start", printed=True)
        with self.assertRaisesRegex(ValueError, "print with stop"):
            await asyncio.to_thread(dispatch, self.root, "stop", gnome=True, printed=True)
        with self.assertRaisesRegex(RuntimeError, "Open Jaro for this library"):
            await asyncio.to_thread(dispatch, self.root / "other", "stop", printed=True)
        with patch("wayland_dictation_commands.same_user", return_value=False):
            self.assertEqual(await self.raw(b"stop-print\n"), b"")
        self.assertEqual(await asyncio.to_thread(dispatch, self.root, "stop", printed=True),
                         {"status": "transcript", "text": "Printed résumé. " * 300})
        self.assertEqual(self.actions, ["stop-print"])
