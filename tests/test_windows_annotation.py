import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from portable_capture import CaptureError
from portable_dictation_history import PortableDictationHistory
from windows_annotation import DEFAULT_SHORTCUT, WindowsAnnotationBinding
from windows_screencast import WindowsScreenRecording, cursor_display


class WindowsAnnotationTests(IsolatedAsyncioTestCase):
    async def test_conflicts_keep_the_active_shortcut_and_persisted_preference(self):
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(Path(folder))
            history.initialize()
            active_events = asyncio.Queue()
            active_events.put_nowait(b"ready\n")
            active = SimpleNamespace(stdout=SimpleNamespace(readline=active_events.get))
            conflict = SimpleNamespace(stdout=AsyncMock())
            conflict.stdout.readline.return_value = b"conflict\n"
            dictation = SimpleNamespace(shortcut="Ctrl+Shift+P", raw_shortcut="Ctrl+F8", history=history,
                                       spawn=AsyncMock(side_effect=[active, conflict]))
            shortcut = WindowsAnnotationBinding(SimpleNamespace(release=lambda **_: None), dictation)
            with patch("windows_annotation.stop_process", new_callable=AsyncMock) as stop:
                try:
                    await shortcut.configure(enabled=True)
                    with self.assertRaisesRegex(CaptureError, "already registered"):
                        await shortcut.configure(shortcut="Ctrl+Shift+F9")
                    self.assertEqual(history.annotation_shortcut(windows=True), (DEFAULT_SHORTCUT, True))
                    self.assertEqual(history.annotation_shortcut(), ("", False))
                    self.assertEqual(shortcut.shortcut, DEFAULT_SHORTCUT)
                    self.assertTrue(shortcut.status()["enabled"])
                    with self.assertRaisesRegex(CaptureError, "used for Dictation"):
                        await shortcut.configure(shortcut="Ctrl+Shift+P")
                    with self.assertRaisesRegex(CaptureError, "dictation without processing"):
                        await shortcut.configure(shortcut="Ctrl+F8")
                    with self.assertRaisesRegex(CaptureError, "used for Annotate"):
                        shortcut.check_dictation(DEFAULT_SHORTCUT)
                    self.assertIn(conflict, [call.args[0] for call in stop.await_args_list])
                finally:
                    await shortcut.close()

    async def test_notice_failure_prevents_encoder_and_draft(self):
        with TemporaryDirectory() as folder:
            notice = SimpleNamespace(show=AsyncMock(side_effect=CaptureError("Could not show control.")),
                                     close=AsyncMock())
            take = WindowsScreenRecording(Path(folder), "display", "20002:0", True, notice=notice)
            take.run_encoder = AsyncMock()
            with patch("windows_screencast.helper_path", return_value=Path("fixture")):
                await take.record()
            self.assertEqual(take.phase, "error")
            self.assertEqual(take.error, "Could not show control.")
            take.run_encoder.assert_not_awaited()
            self.assertFalse(take.draft.folder.exists())
            notice.close.assert_awaited_once()

    async def test_shortcut_target_is_a_display(self):
        with patch("windows_screencast.output", return_value=b"20002:0\r\n") as reply:
            self.assertEqual(await cursor_display(), "20002:0")
            reply.assert_awaited_once_with("cursor-display")
        with patch("windows_screencast.output", return_value=b"20002:12345\r\n"):
            with self.assertRaisesRegex(CaptureError, "display under the pointer"):
                await cursor_display()
