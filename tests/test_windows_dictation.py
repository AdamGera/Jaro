from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from portable_dictation_history import DEFAULT
from windows_dictation import COPY_LAST, WindowsDictation


class WindowsDictationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        history = Mock()
        history.status.return_value = (DEFAULT.copy(), True)
        self.dictation = WindowsDictation(SimpleNamespace(draining=False), AsyncMock(), history)
        self.dictation.schedule = Mock()
        self.dictation.cancel_capture = AsyncMock()

    async def test_finish_keeps_destination_through_transcription(self):
        await self.dictation.handle_event("start 12 34 56 External Editor.exe")
        self.assertEqual(self.dictation.application, "External Editor")
        self.dictation.phase = "recording"
        await self.dictation.handle_event("finish")
        self.assertEqual(self.dictation.schedule.call_count, 2)
        self.assertEqual(self.dictation.focus, ("12", "34", "56"))
        self.dictation.cancel_capture.assert_not_awaited()

    async def test_interrupted_hold_and_release_during_startup_cancel_capture(self):
        for event, phase in (("cancel", "recording"), ("finish", "starting")):
            with self.subTest(event=event):
                self.dictation.phase = "idle"
                self.dictation.schedule.reset_mock()
                self.dictation.cancel_capture.reset_mock()
                await self.dictation.handle_event("start 12 34 56 Editor.exe")
                self.dictation.phase = phase
                await self.dictation.handle_event(event)
                self.dictation.schedule.assert_called_once()
                self.dictation.cancel_capture.assert_awaited_once()

    async def test_focus_lost_while_helper_starts_does_not_send_transcript(self):
        await self.dictation.handle_event("start 12 34 56 Editor.exe")
        process = Mock()

        async def spawn(*_):
            await self.dictation.handle_event("focus-lost")
            return process

        self.dictation.spawn = spawn
        self.dictation.send_text = AsyncMock()
        with patch("windows_dictation.stop_process", new_callable=AsyncMock) as stop:
            self.assertEqual(await self.dictation.deliver("Fixture speech"), COPY_LAST)
            stop.assert_awaited_once_with(process)
        self.dictation.send_text.assert_not_awaited()

    async def test_paste_last_reuses_text_without_recording_or_writing_history(self):
        self.dictation.text = "A recovered dictation"
        self.dictation.deliver = AsyncMock(return_value="Paste shortcut sent.")
        await self.dictation.handle_event("paste-last 123 456 789")
        self.assertEqual(self.dictation.phase, "pasting")
        await self.dictation.action
        self.assertEqual(self.dictation.phase, "idle")
        self.assertEqual(self.dictation.feedback, "Paste shortcut sent.")
        self.dictation.deliver.assert_awaited_once_with("A recovered dictation", recovery=True)
        self.dictation.history.record.assert_not_called()
        self.dictation.schedule.assert_not_called()

    async def test_paste_last_respects_history_clears(self):
        self.dictation.text = "Removed text"
        self.dictation.last_clear_epoch = 0
        self.dictation.history.status.return_value = ({**DEFAULT, "clearEpoch": 1}, True)
        self.dictation.deliver = AsyncMock()
        await self.dictation.handle_event("paste-last 123 456 789")
        await self.dictation.action
        self.assertEqual(self.dictation.feedback, "There’s no last dictation to paste.")
        self.dictation.deliver.assert_not_awaited()

    async def test_recovery_focus_loss_does_not_use_the_previous_capture_target(self):
        self.dictation.text = "Do not paste"
        self.dictation.terminal = True
        self.dictation.focus = ("12", "34", "56")
        process = Mock()

        async def spawn(*args):
            self.assertEqual(args, ("paste", "123", "456", "789", "terminal"))
            await self.dictation.handle_event("paste-focus-lost")
            return process

        self.dictation.spawn = spawn
        self.dictation.send_text = AsyncMock()
        with patch("windows_dictation.stop_process", new_callable=AsyncMock) as stop:
            await self.dictation.handle_event("paste-last 123 456 789")
            await self.dictation.action
            stop.assert_awaited_once_with(process)
        self.assertEqual(self.dictation.feedback, COPY_LAST)
        self.assertEqual(self.dictation.focus, ("12", "34", "56"))
        self.dictation.send_text.assert_not_awaited()
