from types import SimpleNamespace
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from portable_dictation_history import DEFAULT, PortableDictationHistory
from portable_capture import CaptureError
from portable_dictation import OutputContext
from windows_dictation import (
    COPY_LAST, ELEVATED_COPY_LAST, FOCUS_REQUEST, PASTE_CONTINUATION_CHANGED, PASTE_REQUEST, PASTE_TARGET_ELEVATED, PICKER_HEADER,
    WindowsDictation, join_dictation, windows_shortcut_parts,
)


class WindowsDictationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        history = Mock()
        history.status.return_value = (DEFAULT.copy(), True)
        history.voice_input.return_value = {"noiseCleanup": False, "boostMicrophone": False}
        history.output_preferences.return_value = ([], "", "")
        self.dictation = WindowsDictation(SimpleNamespace(draining=False), AsyncMock(), history)
        self.dictation.schedule = Mock()
        self.dictation.cancel_capture = AsyncMock()

    async def test_release_site_uses_only_the_original_foreground_window(self):
        browser = (1, 2, "chrome", "Fixture tab")
        moved = (1, 2, "chrome", "Another tab")
        payload = b"U\t0\t0\thttps://scope.example.test/private?token=fixture\n"
        with (patch("windows_dictation.foreground_application", return_value=browser) as foreground,
              patch("windows_capture.capture_inventory", new=AsyncMock(return_value=payload)) as probe):
            self.assertEqual(await self.dictation.output_context(False), OutputContext("chrome"))
            probe.assert_not_awaited()
            self.assertEqual(await self.dictation.output_context(True), OutputContext("chrome", "scope.example.test"))
            probe.assert_awaited_once_with("foreground-page", "1", "2", timeout=.35)
            from portable_annotation_origin import observed_annotation_origin

            origin = await observed_annotation_origin(self.dictation)
            self.assertEqual(origin["siteUrl"], "https://scope.example.test/private")
            foreground.side_effect = [browser, moved]
            self.assertEqual(await self.dictation.output_context(True, annotation=True), OutputContext("chrome"))
            foreground.side_effect = None
            probe.side_effect = CaptureError("Accessibility timed out.")
            self.assertEqual(await self.dictation.output_context(True), OutputContext("chrome"))

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

    async def test_command_target_discards_stale_replies_and_tracks_the_original_field(self):
        controller = self.dictation
        listener = controller.listener = Mock()
        listener.stdin.drain = AsyncMock()
        controller.state.status = "ready"
        controller.state.portable_recording = None
        request = asyncio.create_task(controller.run_command("start", raw=True))
        await asyncio.sleep(0)
        self.assertEqual(FOCUS_REQUEST.unpack(listener.stdin.write.call_args.args[0]), (b"f", 1))
        await controller.handle_event("command-focus 0 99 98 97 Wrong.exe")
        self.assertFalse(request.done())
        self.assertIsNone(controller.focus)
        await controller.handle_event("command-focus 1 12 34 56 External Editor.exe")
        self.assertEqual(await request, {"status": "starting"})
        controller.schedule.assert_called_once_with(raw=True)
        self.assertEqual(controller.application, "External Editor")
        self.assertEqual(controller.focus, ("12", "34", "56"))
        delivery = controller.prepare_delivery(raw=True)
        await controller.handle_event("focus-lost")
        controller.paste_attempt = AsyncMock()
        self.assertEqual(await delivery("Do not paste into a later field."), COPY_LAST)
        controller.paste_attempt.assert_not_awaited()

    async def test_stop_print_uses_the_queued_take_without_paste_or_clipboard(self):
        controller = self.dictation
        controller.listener = SimpleNamespace(stdin=Mock())
        controller.phase = "recording"
        controller.capture_raw = True
        controller.transcribe.return_value = {"text": "  Words as heard.  ", "inferenceMs": 42}
        controller.retain = AsyncMock(return_value="")
        controller.paste_attempt = AsyncMock()
        self.assertEqual(await controller.run_command("toggle"), {"status": "recording"})
        controller.schedule.assert_not_called()
        request = asyncio.create_task(controller.run_command("stop", printed=True))
        await asyncio.sleep(0)
        reply = controller.schedule.call_args.kwargs["printed"]
        with TemporaryDirectory() as folder:
            paste = AsyncMock()
            job = SimpleNamespace(output=([], "", None), raw=True, path=Path(folder) / "voice.wav",
                                  languages=(), voice={}, sound=controller.sound, delivery=paste, printed=reply,
                                  stopped=0, microphone_fallback=False, interrupted=False)
            controller.phase = "idle"
            await controller.process_job(job)
            self.assertEqual(await request, {"status": "transcript", "text": "Words as heard."})
            paste.assert_not_awaited()
        controller.paste_attempt.assert_not_awaited()
        with self.assertRaisesRegex(CaptureError, "no active dictation"):
            await controller.run_command("stop", printed=True)

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

    async def test_picker_previews_recent_text_without_pasting_and_commits_exact_selection(self):
        controller = self.dictation
        controller.listener = SimpleNamespace(stdin=Mock())
        controller.listener.stdin.drain = AsyncMock()
        controller.last_id = "new"
        controller.text = "Newest dictation."
        controller.history.recent_paste_entries.return_value = [("new", controller.text), ("old", "Previous\nline.")]
        controller.paste_to = AsyncMock(return_value="Paste shortcut sent.")

        await controller.handle_event("paste-picker 25 12 34 56")
        await controller.picker_load
        packet = controller.listener.stdin.write.call_args.args[0]
        self.assertEqual(PICKER_HEADER.unpack_from(packet), (b"k", 25, 2))
        self.assertEqual(packet[PICKER_HEADER.size + 8:], b"Newest dictation.Previous\r\nline.")
        controller.paste_to.assert_not_awaited()
        await controller.handle_event("picker-paste 24 1")
        self.assertIsNone(controller.action)
        await controller.handle_event("picker-paste 25 1")
        await controller.action
        self.assertEqual(controller.paste_to.await_args.args[:2], ("Previous\nline.", ("12", "34", "56")))
        self.assertTrue(controller.paste_to.await_args.kwargs["raw"])
        self.assertEqual(controller.feedback, "Paste shortcut sent.")

    async def test_picker_rechecks_deleted_entries_and_cancels_when_focus_changes(self):
        controller = self.dictation
        controller.listener = SimpleNamespace(stdin=Mock())
        controller.listener.stdin.drain = AsyncMock()
        controller.history.recent_paste_entries.return_value = [("old", "Older text")]
        controller.paste_to = AsyncMock()

        for sequence, event in ((12, "picker-paste 12 0"), (13, "picker-copy 13 0")):
            with self.subTest(action=event):
                controller.history.recent_paste_entries.return_value = [("old", "Older text")]
                await controller.handle_event(f"paste-picker {sequence} 12 34 56")
                await controller.picker_load
                with patch("windows_dictation.copy_text", new_callable=AsyncMock) as copy:
                    if sequence == 12:
                        controller.history.recent_paste_entries.return_value = []
                    else:
                        await controller.handle_event("paste-focus-lost")
                    await controller.handle_event(event)
                    if controller.action is not None:
                        await controller.action
                    controller.paste_to.assert_not_awaited()
                    copy.assert_not_awaited()

    async def test_picker_copy_only_copies_selected_retained_text(self):
        controller = self.dictation
        controller.listener = SimpleNamespace(stdin=Mock())
        controller.listener.stdin.drain = AsyncMock()
        controller.history.recent_paste_entries.return_value = [("saved", "Older text")]
        controller.paste_to = AsyncMock()
        await controller.handle_event("paste-picker 14 12 34 56")
        await controller.picker_load
        with patch("windows_dictation.copy_text", new_callable=AsyncMock) as copy:
            await controller.handle_event("picker-copy 14 0")
            await controller.action
            copy.assert_awaited_once_with("Older text")
        controller.paste_to.assert_not_awaited()
        controller.history.record.assert_not_called()

    def test_picker_history_excludes_printed_expired_and_disabled_entries(self):
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(Path(folder))
            history.initialize()
            now = time()
            for index, age, kind in ((1, 0, "printed"), (2, 1, "raw"), (3, 2, "dictation"),
                                     (4, 8 * 86_400, "dictation")):
                history.record({"id": str(index), "time": now - age, "kind": kind,
                                "text": f"Text {index}", "app": "", "seconds": 1,
                                "inferenceMs": 1, "totalMs": 2})
            self.assertEqual(history.recent_paste_entries(8), [("2", "Text 2"), ("3", "Text 3")])
            history.configure(retention=0)
            self.assertEqual(history.recent_paste_entries(8), [])

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

    async def test_elevated_paste_preserves_text_for_recovery_in_another_app(self):
        controller = self.dictation
        controller.text = "A quiet dictation."
        worker = Mock(returncode=None)
        worker.stdin.drain = AsyncMock()
        worker.stdout.readexactly = AsyncMock(side_effect=[bytes([PASTE_TARGET_ELEVATED]), b"\x00"])
        controller.spawn = AsyncMock(return_value=worker)
        controller.activity_channel = "b" * 32
        controller.last_insertion = (("12", "34", "56"), 0, "We talked,")

        await controller.handle_event("paste-last 12 34 56")
        await controller.action
        self.assertEqual(controller.feedback, ELEVATED_COPY_LAST)
        self.assertEqual(controller.text, "A quiet dictation.")
        self.assertIsNone(controller.last_insertion)
        worker.stdin.write.assert_called_once()

        await controller.handle_event("paste-last 123 456 789")
        await controller.action
        self.assertEqual(controller.feedback, "Paste shortcut sent.")
        self.assertEqual(worker.stdin.write.call_args.args[0][PASTE_REQUEST.size:], b"A quiet dictation.")
        controller.spawn.assert_awaited_once_with("paste-server")
        controller.history.record.assert_not_called()

    async def test_recovery_focus_loss_does_not_use_the_previous_capture_target(self):
        self.dictation.text = "Do not paste"
        self.dictation.terminal = True
        self.dictation.focus = ("12", "34", "56")
        process = Mock()

        async def spawn(*args):
            self.assertEqual(args, ("paste-server",))
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

    async def test_paste_status_releases_delivery_while_the_same_worker_stays_open(self):
        controller = self.dictation
        worker = Mock(returncode=None)
        worker.stdin.drain = AsyncMock()
        worker.stdout.readexactly = AsyncMock(side_effect=[b"\x00", b"\x00"])
        controller.spawn = AsyncMock(return_value=worker)
        controller.activity_channel = "b" * 32
        await controller.handle_event("start 12 34 56 Editor.exe")
        paste = controller.prepare_delivery()
        self.assertEqual(await paste("First take."), "Paste shortcut sent.")
        self.assertEqual(await paste("Second take.\nQuiet words."), "Paste shortcut sent.")
        controller.spawn.assert_awaited_once_with("paste-server")
        requests = [call.args[0] for call in worker.stdin.write.call_args_list]
        header, payload = requests[1][:PASTE_REQUEST.size], requests[1][PASTE_REQUEST.size:]
        self.assertEqual(PASTE_REQUEST.unpack(header), (12, 34, 56, 0, 0, len(payload), b"b" * 32))
        self.assertEqual(payload, b" Second take.\r\nQuiet words.")
        worker.wait.assert_not_called()
        with patch("windows_dictation.stop_process", new_callable=AsyncMock) as stop:
            await controller.disable()
            stop.assert_awaited_once_with(worker)
        self.assertIsNone(controller.paste_worker)

    async def test_worker_disconnect_does_not_resend_an_unacknowledged_paste(self):
        controller = self.dictation
        worker = Mock(returncode=None)
        worker.stdin.drain = AsyncMock()
        worker.stdout.readexactly = AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 1))
        controller.spawn = AsyncMock(return_value=worker)
        await controller.handle_event("start 12 34 56 Editor.exe")
        with patch("windows_dictation.stop_process", new_callable=AsyncMock) as stop:
            self.assertEqual(await controller.prepare_delivery()("One attempt."), COPY_LAST)
            stop.assert_awaited_once_with(worker)
        controller.spawn.assert_awaited_once_with("paste-server")
        worker.stdin.write.assert_called_once()
        self.assertIsNone(controller.paste_worker)

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

    def test_shortcut_syntax_and_reserved_shortcuts(self):
        from portable_capture import CaptureError

        self.assertEqual(windows_shortcut_parts(" left alt "), ("Left Alt", ()))
        self.assertEqual(windows_shortcut_parts("Shift+Ctrl+PageUp"), ("Ctrl+Shift+PageUp", ("33", "6")))
        self.assertEqual(windows_shortcut_parts("F12"), ("F12", ("123", "0")))
        for value in ("Right Alt", "A", "Ctrl+Alt+V", "Ctrl+Left Alt+V", "Ctrl+Alt+Delete", "Super+L"):
            with self.subTest(value=value), self.assertRaises(CaptureError):
                windows_shortcut_parts(value)

    async def test_listener_uses_saved_shortcut_arguments(self):
        history = self.dictation.history
        history.desktop_shortcut.side_effect = lambda *, windows, raw=False: "Ctrl+F8" if raw else "Ctrl+PageUp"
        controller = WindowsDictation(self.dictation.state, None, history)
        helper = Mock()
        helper.stdout.readline = AsyncMock(return_value=b"ready\n")
        controller.spawn = AsyncMock(return_value=helper)
        controller.read_events = AsyncMock()
        await controller.enable()
        await controller.events
        self.assertEqual(controller.spawn.await_args.args[:2], ("listen", "hold"))
        self.assertEqual(controller.spawn.await_args.args[3:], ("33", "2", "--raw", "119", "2"))
        self.assertEqual(controller.snapshot()["defaultShortcut"], "Left Alt")
        self.assertEqual(controller.snapshot()["shortcut"], "Ctrl+PageUp")
        self.assertEqual(controller.snapshot()["rawShortcut"], "Ctrl+F8")

    async def test_raw_shortcut_does_not_share_normal_or_paste_last(self):
        from portable_capture import CaptureError

        controller = self.dictation
        await controller.change_shortcut("Ctrl+F8", raw=True)
        self.assertEqual(controller.snapshot()["rawShortcut"], "Ctrl+F8")
        with self.assertRaisesRegex(CaptureError, "different shortcuts"):
            await controller.change_shortcut("ctrl+f8")
        with self.assertRaisesRegex(CaptureError, "reserved"):
            await controller.change_shortcut("Ctrl+Alt+V", raw=True)
        await controller.change_shortcut("", raw=True)
        self.assertEqual(controller.snapshot()["rawShortcut"], "")
        controller.history.save_desktop_shortcut.assert_called_with("", windows=True, raw=True)

    async def test_raw_take_and_paste_last_skip_continuation(self):
        controller = self.dictation
        await controller.handle_event("start-raw 12 34 56 Editor.exe")
        controller.schedule.assert_called_once_with(raw=True)
        controller.gesture_accepted = False
        controller.text = "And raw."
        controller.last_raw = True
        controller.activity_channel = "b" * 32
        controller.last_insertion = (("12", "34", "56"), 0, "First,")
        controller.paste_attempt = AsyncMock(return_value=0)
        await controller.handle_event("paste-last 12 34 56")
        await controller.action
        self.assertEqual(controller.paste_attempt.await_args.args[0], "And raw.")
        self.assertIsNone(controller.last_insertion)

    async def test_shortcut_save_failure_restores_listener_and_fences_new_takes(self):
        from portable_capture import CaptureError

        controller = self.dictation
        controller.listener = Mock()
        started = []

        async def enable():
            started.append(controller.shortcut)
            controller.listener = Mock()
            await controller.handle_event("start 12 34 56 Editor.exe")

        async def disable():
            controller.listener = None

        controller.enable = enable
        controller.disable = disable
        controller.history.save_desktop_shortcut.side_effect = OSError("read only")
        with self.assertRaisesRegex(CaptureError, "Could not change the Windows dictation shortcut"):
            await controller.change_shortcut("Ctrl+F9")
        self.assertEqual(started, ["Ctrl+F9", "Left Alt"])
        self.assertTrue(controller.enabled)
        self.assertEqual(controller.shortcut, "Left Alt")
        controller.schedule.assert_not_called()
        controller.history.save_desktop_shortcut.assert_called_once_with("Ctrl+F9", windows=True)

        controller.history.save_desktop_shortcut.side_effect = None
        await controller.disable()
        await controller.change_shortcut("F9")
        self.assertFalse(controller.enabled)
        self.assertEqual(controller.shortcut, "F9")
        controller.history.save_desktop_shortcut.assert_called_with("F9", windows=True)

    async def test_native_gesture_resets_if_start_is_declined_or_capture_ends_by_itself(self):
        listener = self.dictation.listener = Mock()
        self.dictation.phase = "transcribing"
        await self.dictation.handle_event("start 12 34 56 External Editor.exe")
        self.assertEqual([call.args[0] for call in listener.stdin.write.call_args_list], [b"c", b"r"])
        self.dictation.schedule.assert_not_called()
        self.dictation.phase = "idle"
        await self.dictation.handle_event("start 12 34 56 External Editor.exe")
        self.assertTrue(self.dictation.gesture_accepted)
        await self.dictation.clear_capture()
        self.assertFalse(self.dictation.gesture_accepted)
        self.assertIsNone(self.dictation.focus)
        self.assertEqual([call.args[0] for call in listener.stdin.write.call_args_list], [b"c", b"r", b"i", b"r"])

    async def test_escape_cancels_inference_after_the_native_gesture_has_ended(self):
        controller = self.dictation
        listener = controller.listener = Mock()
        controller.gesture_accepted = True
        controller.phase = "recording"
        await controller.handle_event("finish")
        self.assertFalse(controller.gesture_accepted)
        job = Mock()
        controller.jobs.append(job)
        controller.phase = "idle"
        self.assertEqual(controller.phase, "transcribing")
        self.assertTrue(controller.cancel_shortcut_armed)
        controller.output_task = asyncio.create_task(asyncio.Event().wait())
        try:
            # A different shortcut interrupted a refused take. Only Escape owns queued cancellation.
            await controller.handle_event("cancel")
            self.assertEqual(list(controller.jobs), [job])
            await controller.handle_event("escape")
            self.assertFalse(controller.jobs)
            self.assertEqual(controller.feedback, "Dictation canceled.")
            job.folder.cleanup.assert_called_once()
            self.assertEqual([call.args[0] for call in listener.stdin.write.call_args_list], [b"c", b"i"])
        finally:
            if controller.output_task is not None:
                controller.output_task.cancel()
                await asyncio.gather(controller.output_task, return_exceptions=True)

    async def test_escape_preserves_committing_paste_and_can_drop_the_next_take(self):
        controller = self.dictation
        listener = controller.listener = Mock()
        first, second = Mock(), Mock()
        controller.jobs.extend([first, second])
        controller.committing = True
        controller.update_cancel_shortcut()
        await controller.handle_event("escape")
        self.assertEqual(list(controller.jobs), [first])
        self.assertEqual(controller.phase, "delivering")
        second.folder.cleanup.assert_called_once()
        first.folder.cleanup.assert_not_called()
        await controller.handle_event("escape")
        self.assertEqual(list(controller.jobs), [first])
        self.assertEqual([call.args[0] for call in listener.stdin.write.call_args_list], [b"c", b"i"])

    async def test_uwp_window_without_hwnd_focus_keeps_destination_and_delivers(self):
        self.dictation.paste_attempt = AsyncMock(return_value=0)
        await self.dictation.handle_event("start 12 0 56 WindowsTerminal.exe")
        self.assertEqual(self.dictation.focus, ("12", "0", "56"))
        self.assertEqual(self.dictation.application, "WindowsTerminal")
        self.assertEqual(await self.dictation.prepare_delivery()("Hello terminal."), "Paste shortcut sent.")
        self.dictation.paste_attempt.assert_awaited_once()
