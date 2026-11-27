import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from linux_dictation import LinuxDictation
from portable_audio_control import AudioPreferences
from portable_dictation import PortableDictation, recording_system_audio
from portable_tray import MeetingTray
from saved_meetings import resolve_speaker_label
from screen_draft import ScreenDraft
from server import (begin_portable_recording, portable_capture_busy, portable_recording_busy,
                    run_portable_meeting_command, start_screen_take)
from windows_dictation import WindowsDictation


class _FakeHistory:
    def shortcut_enabled(self, *, portal=False):
        return True

    def status(self, key=None, *, required=False):
        return {"terminal": False, "clearEpoch": 0, "retention": 7, "revision": 1}, True

    def microphone(self):
        return ""

    def language(self):
        return ""

    def voice_input(self):
        return {"noiseCleanup": False, "boostMicrophone": False}

    def output_preferences(self):
        return [], "", ""

    def indicator_position(self):
        return "off"

    def sound_preferences(self):
        return None

    def audio_preferences(self):
        return AudioPreferences(behavior="mute", percent=20)

    def linux_shortcut_mode(self):
        return "tap"

    def desktop_shortcut(self, *, windows=False, raw=False):
        return None

    def windows_shortcut_mode(self):
        return "hold"


class _FakeAudioControl:
    modes = ["none", "mute", "lower", "pause"]

    def __init__(self):
        self.begun = []
        self.while_valids = []
        self.released = 0
        self.settled = 0
        self.task = None

    def begin(self, settings, *, delay=0, while_valid=None):
        self.begun.append(settings.behavior)
        self.while_valids.append(while_valid)
        return None

    def release(self):
        self.released += 1
        return None

    async def settle(self):
        self.settled += 1
        self.released += 1
        return None


class _FakeSounds:
    available = False

    async def stop(self):
        return None

    def play(self, *args, **kwargs):
        return None


class _FakeCapture:
    def __init__(self, path, mode="mic"):
        self.path = path
        self.mode = mode
        self.frames = 16000
        self.interrupted = False
        self.levels = (0.2,)
        self.measure_level = False
        self.dictation = False
        self.worker = asyncio.get_running_loop().create_future()

    async def start(self):
        return None

    async def stop(self):
        if not self.worker.done():
            self.worker.set_result(None)


class _FakeStdin:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(data.decode("utf-8"))

    async def drain(self):
        return None

    def close(self):
        return None


class ConcurrentDictationTests(unittest.IsolatedAsyncioTestCase):
    async def _make_state(self, *, recording_mode=None, screencast_audio=None, hold_lock=False):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        recording = SimpleNamespace(mode=recording_mode, meeting=recording_mode == "both") if recording_mode else None
        screencast = (SimpleNamespace(draft=ScreenDraft(Path(temp.name) / "screen", "display", screencast_audio))
                      if screencast_audio is not None else None)
        state = SimpleNamespace(
            status="ready",
            draining=False,
            library=SimpleNamespace(root=Path(temp.name)),
            portable_recording=recording,
            portable_recording_dir=Path(temp.name),
            screen_draft_dir=Path(temp.name),
            portable_screencast=screencast,
            portable_recording_lock=asyncio.Lock(),
            portable_sdk_capture=SimpleNamespace(busy=False),
            portable_dictation=None,
            portable_recorder=lambda key, path, mode, **kw: _FakeCapture(path, mode),
            portable_meeting_recorder=lambda key, path, mode, **kw: _FakeCapture(path, mode),
            jobs=set(),
        )
        if hold_lock:
            await state.portable_recording_lock.acquire()
        return state

    async def _make_dictation(self, *, recording_mode=None, screencast_audio=None, hold_lock=False, cls=PortableDictation):
        state = await self._make_state(
            recording_mode=recording_mode, screencast_audio=screencast_audio, hold_lock=hold_lock
        )
        dictation = cls(state, lambda *a, **kw: None, _FakeHistory())
        dictation.audio_control = _FakeAudioControl()
        dictation.sounds = _FakeSounds()
        dictation.audio_preferences = AudioPreferences(behavior="mute", percent=20)
        state.portable_dictation = dictation
        return state, dictation

    async def test_dictation_starts_during_meeting_session_or_annotate_without_lock_wait(self):
        cases = (
            ("both", None, True, False),
            ("mic", None, False, True),
            ("system", None, True, False),
            (None, ("mic",), True, True),
            (None, ("mic", "system"), True, False),
        )
        for recording_mode, screencast_audio, hold_lock, expect_audio_control in cases:
            state, dictation = await self._make_dictation(
                recording_mode=recording_mode, screencast_audio=screencast_audio, hold_lock=hold_lock
            )
            await asyncio.wait_for(dictation.toggle(), timeout=0.5)
            self.assertEqual(dictation.phase, "recording")
            self.assertIsNotNone(dictation.capture)
            # Suppress ducking/muting/pausing whenever system audio is being recorded (meeting, session, or annotate).
            expected = ["mute"] if expect_audio_control else []
            self.assertEqual(dictation.audio_control.begun, expected)
            self.assertEqual(recording_system_audio(state), not expect_audio_control)
            await dictation.cancel_capture()

    async def test_computer_audio_capture_releases_existing_dictation_playback_effect(self):
        from unittest.mock import patch

        # 1. Dictation starts first (mutes playback), then a system/both Session starts -> releases playback effect.
        for mode, expect_settle in (("mic", 0), ("system", 1), ("both", 1)):
            state, dictation = await self._make_dictation()
            await dictation.toggle()
            self.assertEqual(dictation.audio_control.begun, ["mute"])
            self.assertTrue(dictation.audio_control.while_valids[-1]())
            settled_before = dictation.audio_control.settled
            with patch("server.start_job", side_effect=lambda s, coro: (coro.close(), None)[1]), \
                 patch("recording_power.keep_awake_until", lambda *a, **kw: None), \
                 patch("server.schedule_prewarm", lambda *a, **kw: None):
                await begin_portable_recording(state, mode, False, ())
            self.assertEqual(dictation.audio_control.settled - settled_before, expect_settle)
            if expect_settle:
                self.assertFalse(dictation.audio_control.while_valids[-1]())
            await dictation.cancel_capture()

        # 2. Dictation starts first (mutes playback), then Annotate starts with computer_audio=True -> releases effect.
        for computer_audio, expect_settle in ((False, 0), (True, 1)):
            state, dictation = await self._make_dictation()
            await dictation.toggle()
            self.assertEqual(dictation.audio_control.begun, ["mute"])
            settled_before = dictation.audio_control.settled

            class _FakeScreenTake:
                def __init__(self, staging, source, mic, comp, **kw):
                    audio = tuple(n for n, on in (("mic", mic), ("system", comp)) if on)
                    self.draft = ScreenDraft(Path(staging) / "screen", source, audio)

                async def record(self):
                    return None

                def status(self):
                    return {"phase": "recording"}

            fake_linux_screencast = SimpleNamespace(supported=lambda: True, ScreenRecording=_FakeScreenTake)
            fake_linux_x11 = SimpleNamespace(x11_session=lambda: True, recording_target=lambda area: area)
            with patch("server.require_annotation", lambda: None), \
                 patch("server.annotation_storage.capture_location", lambda db: None), \
                 patch("server.start_job", side_effect=lambda s, coro: (coro.close(), None)[1]), \
                 patch.dict(sys.modules, {"linux_screencast": fake_linux_screencast, "linux_x11": fake_linux_x11}), \
                 patch.object(state.library, "connection", create=True):
                await start_screen_take(state, "display", True, computer_audio, "")
            self.assertEqual(dictation.audio_control.settled - settled_before, expect_settle)
            if expect_settle:
                self.assertTrue(recording_system_audio(state))
                self.assertFalse(dictation.audio_control.while_valids[-1]())
            await dictation.cancel_capture()

    async def test_dictation_preserves_computer_audio_while_the_recorder_opens(self):
        state, dictation = await self._make_dictation()
        opening = asyncio.Event()

        class OpeningCapture(_FakeCapture):
            async def start(self):
                opening.set()
                await asyncio.Event().wait()

        state.portable_recorder = lambda key, path, mode, **kwargs: (
            OpeningCapture(path, mode) if mode == "both" else _FakeCapture(path, mode)
        )
        starting = asyncio.create_task(begin_portable_recording(state, "both", False, ()))
        try:
            await asyncio.wait_for(opening.wait(), 1)
            await asyncio.wait_for(dictation.toggle(), 1)
            self.assertEqual(dictation.phase, "recording")
            self.assertEqual(dictation.audio_control.begun, [])
        finally:
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await starting
            await dictation.cancel_capture()
        self.assertFalse(recording_system_audio(state))

    async def test_linux_command_starts_during_meeting_session_or_annotate(self):
        for recording_mode, screencast_audio in (("both", None), ("mic", None), ("system", None), (None, ("mic",))):
            _, dictation = await self._make_dictation(
                recording_mode=recording_mode, screencast_audio=screencast_audio, cls=LinuxDictation
            )
            dictation.enabled = True
            phase = await dictation.run_command("start")
            self.assertEqual(phase, "starting")
            await asyncio.wait_for(dictation.action, timeout=0.5)
            self.assertEqual(dictation.phase, "recording")
            await dictation.cancel_capture()

    async def test_windows_command_starts_during_meeting_session_or_annotate(self):
        for recording_mode, screencast_audio in (("both", None), ("mic", None), ("system", None), (None, ("mic",))):
            _, dictation = await self._make_dictation(
                recording_mode=recording_mode, screencast_audio=screencast_audio, cls=WindowsDictation
            )
            dictation.listener = SimpleNamespace(stdin=_FakeStdin())

            async def fake_focus(*, recovery=False):
                return None

            dictation.request_command_focus = fake_focus
            result = await dictation.run_command("start")
            self.assertEqual(result, {"status": "starting"})
            await asyncio.wait_for(dictation.action, timeout=0.5)
            self.assertEqual(dictation.phase, "recording")
            await dictation.cancel_capture()

    async def test_recording_busy_tray_and_meeting_status_allow_start_during_dictation(self):
        state, dictation = await self._make_dictation()
        await dictation.toggle()
        self.assertTrue(dictation.busy)
        self.assertFalse(portable_recording_busy(state))
        self.assertTrue(portable_capture_busy(state))

        state.portable_calls = SimpleNamespace(transition=asyncio.Lock(), active=None)
        status = await run_portable_meeting_command(state, SimpleNamespace(action="status", title=""))
        self.assertEqual(status, {"phase": "idle"})

        tray = MeetingTray(
            state,
            state.library.root,
            lambda: portable_recording_busy(state),
            lambda: None,
            lambda: None,
            shutdown_busy=lambda: portable_capture_busy(state),
        )
        phase, _ = tray.meeting_view()
        self.assertEqual(phase, "idle")
        self.assertTrue(tray.shutdown_busy())
        await dictation.cancel_capture()

    async def test_mac_bridge_unifies_sessions_meetings_and_dictation_on_warm_worker(self):
        desktop_dir = str(Path(__file__).resolve().parents[1] / "desktop")
        if desktop_dir not in sys.path:
            sys.path.insert(0, desktop_dir)
        import mac_bridge

        with TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = mac_bridge.MacBridge(Path("/tmp"), None)
            bridge.root = root
            created_stages = {}

            async def fake_api(method, path, **kwargs):
                if method == "GET" and path == "status":
                    return {"status": "ready"}
                if method == "POST" and path == "native/staging":
                    stage_id = str(uuid4())
                    wav_path = root / f"{stage_id}.wav"
                    wav_path.write_bytes(b"RIFF" + b"\x00" * 40)
                    params = kwargs.get("params") or {}
                    suffix = ".dictation" if params.get("dictation") else ".recording"
                    wav_path.with_suffix(suffix).write_bytes(b"")
                    created_stages[stage_id] = wav_path
                    return {"id": stage_id, "path": str(wav_path)}
                if method == "DELETE" and path.startswith("native/staging/"):
                    return {}
                raise AssertionError(f"Unexpected API call: {method} {path}")

            bridge.api = fake_api
            open_calls = []
            close_calls = []

            def make_worker(options=(), *, on_demand=False):
                child = SimpleNamespace(returncode=None, stdin=_FakeStdin(), terminate=lambda: None)
                worker = mac_bridge.WarmWorker(child, options, on_demand=on_demand)
                worker.reader = asyncio.get_running_loop().create_future()
                return worker

            async def fake_open_warm(options, *, on_demand=False):
                open_calls.append((options, on_demand))
                bridge.warm_worker = make_worker(options, on_demand=on_demand)
                return bridge.warm_worker

            async def fake_close_warm():
                close_calls.append(bridge.warm_worker)
                bridge.warm_worker = None

            async def fake_record_events(slot, take):
                return None

            bridge.open_warm = fake_open_warm
            bridge.close_warm = fake_close_warm
            bridge.record_events = fake_record_events

            # 1. With an already-warm worker (Keep Mic Ready ON), starting a mic or both Session reuses it with zero reopen.
            existing = bridge.warm_worker = make_worker((), on_demand=False)
            await bridge.record({"mode": "both", "live": True, "languages": []})
            self.assertEqual(open_calls, [])
            self.assertEqual(close_calls, [])
            rec_take = bridge.captures["recording"]
            self.assertIs(rec_take.child, existing.child)
            self.assertIsNotNone(rec_take.warm_events)
            self.assertIn("sharedRecording", rec_take.state)
            session_msg = json.loads(existing.child.stdin.messages[-1])
            self.assertEqual(session_msg["mode"], "both")
            self.assertFalse(session_msg["retainSources"])

            # 2. Global dictation during that active Session shares the exact same warm worker with pre-roll.
            await bridge.record({"dictation": str(uuid4()), "global": True, "languages": []})
            self.assertEqual(open_calls, [])
            self.assertEqual(close_calls, [])
            dict_take = bridge.captures["dictation"]
            self.assertIs(dict_take.child, existing.child)
            dict_msg = json.loads(existing.child.stdin.messages[-1])
            self.assertTrue(dict_msg["preRoll"])

            # Clean up both slots.
            for slot in list(bridge.captures):
                bridge.captures.pop(slot).marker.close()
            existing.meeting = None
            existing.events = None

            # 3. Starting dictation first, then a mic Session, shares the exact same warm worker too.
            await bridge.record({"dictation": str(uuid4()), "languages": []})
            await bridge.record({"mode": "mic", "live": True, "languages": []})
            self.assertEqual(open_calls, [])
            self.assertIs(bridge.captures["recording"].child, bridge.captures["dictation"].child)
            mic_msg = json.loads(existing.child.stdin.messages[-1])
            self.assertEqual(mic_msg["mode"], "mic")
            self.assertFalse(mic_msg["retainSources"])

            # Clean up.
            for slot in list(bridge.captures):
                bridge.captures.pop(slot).marker.close()

    def test_resolve_speaker_label_handles_corrupt_tables_gracefully(self):
        self.assertEqual(resolve_speaker_label(1, {"1": "  Priya   Nair  "}), "Priya Nair")
        self.assertEqual(resolve_speaker_label(2, {2: "Sam"}), "Sam")
        self.assertEqual(resolve_speaker_label(2, {"2": "   "}), "Speaker 2")
        self.assertEqual(resolve_speaker_label(3, {"3": 12345}), "Speaker 3")
        self.assertEqual(resolve_speaker_label(4, "not-a-dict"), "Speaker 4")
        self.assertIsNone(resolve_speaker_label(None, {"1": "Priya"}))


if __name__ == "__main__":
    unittest.main()
