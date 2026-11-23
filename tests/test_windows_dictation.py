from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from portable_dictation_history import DEFAULT
from windows_dictation import COPY_LAST, PASTE_CONTINUATION_CHANGED, WindowsDictation, join_dictation


class WindowsDictationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        history = Mock()
        history.status.return_value = (DEFAULT.copy(), True)
        history.voice_input.return_value = {"noiseCleanup": False, "boostMicrophone": False}
        history.output_preferences.return_value = ([], "", "")
        self.dictation = WindowsDictation(SimpleNamespace(draining=False), AsyncMock(), history)
        self.dictation.schedule = Mock()
        self.dictation.cancel_capture = AsyncMock()

    async def test_finish_keeps_destination_through_transcription(self):
        await self.dictation.handle_event("start 12 34 56 External Editor.exe")
        self.assertEqual(self.dictation.application, "External Editor")
        self.dictation.phase = "recording"
        with patch("windows_dictation.monotonic", return_value=12.5):
            await self.dictation.handle_event("finish")
        self.assertEqual(self.dictation.schedule.call_count, 2)
        self.dictation.schedule.assert_called_with(released_at=12.5)
        self.assertEqual(self.dictation.focus, ("12", "34", "56"))
        self.dictation.cancel_capture.assert_not_awaited()

    async def test_queued_destination_stays_invalid_after_focus_returns(self):
        await self.dictation.handle_event("start 12 34 56 Editor.exe")
        deliver = self.dictation.prepare_delivery()
        await self.dictation.handle_event("focus-lost")
        await self.dictation.handle_event("start 12 34 56 Editor.exe")
        self.dictation.spawn = AsyncMock()
        self.assertEqual(await deliver("Earlier dictation"), COPY_LAST)
        self.dictation.spawn.assert_not_called()

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

    def test_continuation_respects_existing_spaces_punctuation_and_names(self):
        self.assertEqual(join_dictation("We talked,", "And listened."), " and listened.")
        self.assertEqual(join_dictation("We talked.", "and listened."), " And listened.")
        self.assertEqual(join_dictation("We talked to", "Anya."), " Anya.")
        self.assertEqual(join_dictation("We talked (", "briefly)."), "briefly).")
        self.assertEqual(join_dictation("We talked ", ", and listened."), ", and listened.")
        self.assertEqual(join_dictation("We talked!", "élise listened."), " Élise listened.")

    async def test_consecutive_takes_and_paste_last_continue_until_real_input(self):
        controller = self.dictation
        channel = controller.activity_channel = "b" * 32
        attempt = controller.paste_attempt = AsyncMock(return_value=0)
        await controller.handle_event("start 12 34 56 Editor.exe")
        first = controller.prepare_delivery()
        second = controller.prepare_delivery()
        await first("We talked,")
        await second("And listened.")
        focus = ("12", "34", "56")
        self.assertEqual([call.args[0] for call in attempt.await_args_list], ["We talked,", " and listened."])
        self.assertEqual(attempt.await_args.args[4], (channel, "0"))
        controller.clear_focus()  # End of the completed queue leaves a successful paste available to join.
        controller.text = "We agreed."
        await controller.handle_event("paste-last 12 34 56")
        await controller.action
        self.assertEqual(attempt.await_args.args[0], " We agreed.")
        self.assertEqual(controller.last_insertion, (focus, 0, " We agreed."))
        controller.history.record.assert_not_called()
        await controller.handle_event("paste-focus-lost")  # The completed recovery watch is being replaced.
        await controller.handle_event("paste-last 12 34 56")
        await controller.action
        self.assertEqual(attempt.await_args.args[0], " We agreed.")
        await controller.handle_event("activity 1")
        await controller.handle_event("start 12 34 56 Editor.exe")
        await controller.prepare_delivery()("A new take.")
        self.assertEqual(attempt.await_args.args[0], "A new take.")
        self.assertEqual(attempt.await_args.args[4], ())

    async def test_activity_between_processing_and_native_paste_retries_plain(self):
        controller = self.dictation
        controller.activity_channel = "b" * 32
        attempt = controller.paste_attempt = AsyncMock(side_effect=[0, PASTE_CONTINUATION_CHANGED, 0])
        await controller.handle_event("start 12 34 56 Editor.exe")
        paste = controller.prepare_delivery()
        await paste("We talked,")
        self.assertEqual(await paste("And listened."), "Paste shortcut sent.")
        self.assertEqual([call.args[0] for call in attempt.await_args_list],
                         ["We talked,", " and listened.", "And listened."])
        self.assertIsNone(controller.last_insertion)

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

    async def test_mode_change_reopens_listener_and_rolls_back_on_failure(self):
        from portable_capture import CaptureError

        self.dictation.listener = Mock()
        started = []

        async def enable():
            started.append(self.dictation.mode)
            if self.dictation.mode == "doubleTap":
                raise CaptureError("Could not start listener.")
            self.dictation.listener = Mock()

        async def disable():
            self.dictation.listener = None

        self.dictation.enable = enable
        self.dictation.disable = disable
        await self.dictation.change_mode("tap")
        self.dictation.history.configure.assert_called_once_with(windows_shortcut_mode="tap")
        self.assertEqual(self.dictation.snapshot()["shortcutMode"], "tap")
        with self.assertRaisesRegex(CaptureError, "Could not change"):
            await self.dictation.change_mode("doubleTap")
        self.assertEqual(started, ["tap", "doubleTap", "tap"])
        self.assertTrue(self.dictation.enabled)
        self.assertEqual(self.dictation.mode, "tap")
        self.dictation.history.configure.assert_called_once_with(windows_shortcut_mode="tap")
        self.dictation.phase = "recording"
        with self.assertRaisesRegex(CaptureError, "Finish the current dictation"):
            await self.dictation.change_mode("holdDouble")

    async def test_native_gesture_resets_if_start_is_declined_or_capture_ends_by_itself(self):
        listener = self.dictation.listener = Mock()
        self.dictation.phase = "transcribing"
        await self.dictation.handle_event("start 12 34 56 External Editor.exe")
        listener.stdin.write.assert_called_once_with(b"r")
        self.dictation.schedule.assert_not_called()
        self.dictation.phase = "idle"
        await self.dictation.handle_event("start 12 34 56 External Editor.exe")
        self.assertTrue(self.dictation.gesture_accepted)
        await self.dictation.clear_capture()
        self.assertFalse(self.dictation.gesture_accepted)
        self.assertIsNone(self.dictation.focus)
        self.assertEqual(listener.stdin.write.call_count, 2)
