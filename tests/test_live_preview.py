"""Tests for Stage 1 engine hardening and Stage 2 shared pause-aware live preview."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4
import wave

import httpx
from fastapi import FastAPI
import numpy as np

import audio_file
import desktop.mac_bridge as mac_bridge
from live_preview import (
    CommitAction,
    LiveWindowPlanner,
    SkipAction,
    TentativeAction,
    WaitAction,
    finish_live_preview,
    read_wav_i16_slice,
    trim_boundary_overlap,
)
from meeting_rolling import read_rolling
from portable_capture import SAMPLE_RATE, private_wav
from rumble_model import RumbleModel
from server import (
    URL,
    SpeechInference,
    decode_pcm,
    finish_portable_capture,
    portable_recording_preview,
    run_inference,
    set_portable_recording_preview,
    start_portable_recording,
    stopped_portable_recording,
)
from session_store import SessionStore


def _tone_i16(seconds: float, rate: int, *, amp: float = 0.25, freq: float = 440.0) -> np.ndarray:
    n = int(round(seconds * rate))
    t = np.arange(n, dtype=np.float32) / rate
    return (np.sin(2 * np.pi * freq * t) * amp * 32767.0).astype("<i2")


def _silence_i16(seconds: float, rate: int) -> np.ndarray:
    return np.zeros(int(round(seconds * rate)), dtype="<i2")


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes(samples.astype("<i2").tobytes())


class Stage1HardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_duration_fast_path_skips_ffprobe_for_valid_pcm_wav_and_falls_back_on_truncated(self):
        with TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.wav"
            _write_wav(valid, _silence_i16(3.5, 48_000), 48_000)
            with patch("audio_file.asyncio.create_subprocess_exec", side_effect=AssertionError("ffprobe should not run")):
                self.assertAlmostEqual(await audio_file.audio_duration(valid), 3.5, places=3)

            # Truncated WAV must fall back to ffprobe rather than trusting the header.
            truncated = Path(directory) / "truncated.wav"
            truncated.write_bytes(valid.read_bytes()[:100])
            self.assertIsNone(audio_file._fast_wav_duration(truncated))

    def test_single_pass_screen_nonspeech_still_produces_ctc_word_timestamps(self):
        import torch
        from transformers.feature_extraction_utils import BatchFeature

        engine = RumbleModel.__new__(RumbleModel)
        engine.np = np
        engine.torch = torch
        engine.reported_screen_failure = False
        engine.last_run = 0.0

        ids = torch.tensor([[1, 0, 2]])
        attention = torch.ones_like(ids)
        inputs = BatchFeature({
            "input_ids": ids,
            "attention_mask": attention,
            "input_features": torch.zeros(1),
            "input_features_mask": torch.ones(1, dtype=torch.long),
        })
        hidden = torch.ones(4, 2)
        features = SimpleNamespace(last_hidden_state=hidden, pooler_output=torch.tensor([[2.0, 3.0]]))
        captured_states = []

        @asynccontextmanager
        async def _unused():
            yield

        class FakeAligner:
            def capture(self):
                from contextlib import contextmanager

                @contextmanager
                def _ctx():
                    yield captured_states

                return _ctx()

            def confidently_blank(self, states):
                return False

            def words(self, text, state):
                self.seen_state = state
                return [(0, (0.1, 0.5))]

        def fake_get_audio_features(*_args, **_kwargs):
            # Simulate audio_tower.ln_post forward hook firing once during _screened_inputs.
            captured_states.append(hidden)
            return features

        engine.aligner = FakeAligner()
        engine.processor = SimpleNamespace(
            apply_transcription_request=Mock(return_value=inputs),
            decode=Mock(return_value=[{"transcription": "Hello.", "language": "English"}]),
        )
        engine.model = SimpleNamespace(
            device="cpu",
            dtype=torch.float32,
            get_audio_features=Mock(side_effect=fake_get_audio_features),
            get_input_embeddings=lambda: lambda tokens: torch.ones(1, 3, 2),
            model=SimpleNamespace(get_placeholder_mask=lambda *args: (ids == 0).unsqueeze(-1).expand(1, 3, 2)),
            generate=Mock(return_value=torch.tensor([[1, 0, 2, 5]])),
        )

        pcm = np.full(1600, 0.2, dtype="<f4").tobytes()
        text, lang, words = engine.transcribe(pcm, ("en",), timed=True, screen_nonspeech=True)
        self.assertEqual((text, lang, words), ("Hello.", "English", [(0, (0.1, 0.5))]))
        engine.model.get_audio_features.assert_called_once()
        self.assertNotIn("input_features", engine.model.generate.call_args.kwargs)
        self.assertIs(engine.aligner.seen_state, hidden)

    async def test_live_decode_uses_static_cache_and_refreshes_diarizer_last_used(self):
        calls = []

        class StubModel:
            def transcribe(self, pcm, languages, *, timed=False, cache_implementation=None, **options):
                calls.append((timed, cache_implementation, options))
                return "Live hello.", "English", []

            def release_device_cache(self):
                pass

        speakers = SimpleNamespace(
            push=Mock(),
            label=Mock(return_value=([{"speaker": 1, "start": 0.0, "end": 1.0, "text": "Live hello."}], [])),
        )
        state = SimpleNamespace(
            model=StubModel(),
            inference=SpeechInference(),
            draining=False,
            diarizer_last_used=0.0,
        )
        pcm = np.full(SAMPLE_RATE, 0.1, dtype="<f4").tobytes()
        result = await decode_pcm(state, pcm, ("en",), speakers, 0, live=True, timed_output=True, screen_nonspeech=True)
        self.assertEqual(result["text"], "Live hello.")
        self.assertEqual(calls, [(True, "static", {"screen_nonspeech": True})])
        self.assertGreater(state.diarizer_last_used, 0.0)


class LiveWindowPlannerTests(unittest.TestCase):
    def test_tentative_pause_commit_and_exact_48k_alignment(self):
        rate = 48_000
        # 2.8 s speech + 0.6 s pause
        audio = np.concatenate([_tone_i16(2.8, rate), _silence_i16(0.6, rate)])
        raw = audio.tobytes()

        def reader(start, frames):
            chunk = raw[start * 2 : (start + frames) * 2]
            return chunk if len(chunk) == frames * 2 else None

        planner = LiveWindowPlanner(rate)
        # Before 1.6 s -> wait
        self.assertIsInstance(planner.plan(int(1.2 * rate), reader, show_live=True), WaitAction)
        # At 2.0 s of active speech -> tentative preview
        act = planner.plan(int(2.0 * rate), reader, show_live=True)
        self.assertIsInstance(act, TentativeAction)
        assert isinstance(act, TentativeAction)
        self.assertEqual(act.start % 3, 0)
        self.assertEqual(act.frames % 3, 0)
        planner.mark_tentative(act.start + act.frames)

        # Under load (e.g. dictation waiting) -> suppress tentative
        self.assertIsInstance(planner.plan(int(2.6 * rate), reader, show_live=True, under_load=True), WaitAction)
        # Hidden Live panel -> suppress tentative
        self.assertIsInstance(planner.plan(int(2.6 * rate), reader, show_live=False), WaitAction)

        # After 0.6 s pause closes the utterance -> pause-aware CommitAction (not forced)
        commit = planner.plan(len(audio), reader, show_live=True)
        self.assertIsInstance(commit, CommitAction)
        assert isinstance(commit, CommitAction)
        self.assertFalse(commit.forced)
        self.assertEqual(commit.start, 0)
        self.assertEqual(commit.frames % 3, 0)
        self.assertGreaterEqual(commit.frames, int(2.7 * rate))
        self.assertLessEqual(commit.frames, len(audio))

    def test_continuous_speech_forces_bounded_commit_and_backlog_skips(self):
        rate = 16_000
        audio = _tone_i16(18.0, rate)
        raw = audio.tobytes()
        reader = lambda s, n: raw[s * 2 : (s + n) * 2] if len(raw) >= (s + n) * 2 else None

        planner = LiveWindowPlanner(rate)
        # Continuous 8.5 s speech -> bounded <= 8 s forced commit so dictation stays responsive.
        act = planner.plan(int(8.5 * rate), reader, show_live=True)
        self.assertIsInstance(act, CommitAction)
        assert isinstance(act, CommitAction)
        self.assertTrue(act.forced)
        self.assertLessEqual(act.frames, int(8.0 * rate))

        # Severe backlog (> 16 s) -> SkipAction jumps forward without blocking capture.
        skip = planner.plan(len(audio), reader, show_live=True)
        self.assertIsInstance(skip, SkipAction)
        assert isinstance(skip, SkipAction)
        self.assertGreater(skip.cursor, 0)

    def test_trim_boundary_overlap_multilingual(self):
        self.assertEqual(trim_boundary_overlap("we are reviewing the audio", "the audio pipeline works"), "pipeline works")
        self.assertEqual(trim_boundary_overlap("שלום לכולם היום", "היום אנחנו בודקים"), "אנחנו בודקים")
        self.assertEqual(trim_boundary_overlap("مرحبا بالجميع اليوم", "اليوم نختبر الجودة"), "نختبر الجودة")
        self.assertEqual(trim_boundary_overlap("no overlap here", "completely fresh words"), "completely fresh words")


class SharedLiveAndStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_cleanup_after_deadline_does_not_block_stop(self):
        started = asyncio.Event()
        cleanup = asyncio.Event()

        async def preview():
            try:
                started.set()
                await asyncio.Future()
            finally:
                await cleanup.wait()

        task = asyncio.create_task(preview())
        await started.wait()
        try:
            await asyncio.wait_for(finish_live_preview(task, 0.01, logging.getLogger(__name__), "test"), 0.3)
            self.assertFalse(task.done())
        finally:
            cleanup.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_portable_tentative_preview_and_save_first_stop_on_slow_preview(self):
        @asynccontextmanager
        async def audio_priority():
            yield

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.post("/api/portable/recording", status_code=201)(start_portable_recording)
            app.get("/api/portable/recording/{key}/preview")(portable_recording_preview)
            app.put("/api/portable/recording/{key}/preview")(set_portable_recording_preview)
            state = app.state
            state.library = SessionStore(Path(directory))
            state.portable_workspace = True
            state.draining = False
            state.status = "ready"
            state.inference = SpeechInference()
            state.live_speakers = {}
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.screen_draft_dir = None
            state.portable_recording_dir = Path(directory) / "microphone-drafts"
            state.portable_recording_dir.mkdir()
            state.portable_recording_lock = asyncio.Lock()
            state.jobs = set()
            state.diarizer = None
            state.insights = SimpleNamespace(model=SimpleNamespace(audio_priority=audio_priority))

            # 2.0 s of active speech -> triggers TentativeAction (not yet a CommitAction).
            voiced = _tone_i16(2.0, SAMPLE_RATE)

            def recorder(key, path, mode):
                worker = asyncio.Future()

                async def start():
                    with private_wav(path) as (audio, _):
                        audio.writeframes(voiced.tobytes())

                async def stop():
                    if not worker.done():
                        worker.set_result(None)

                return SimpleNamespace(key=key, path=path, frames=len(voiced), worker=worker, start=start, stop=stop)

            state.portable_recorder = state.portable_meeting_recorder = recorder
            decode_calls = []

            async def fake_decode(_state, _pcm, _langs, speakers, _offset, **kwargs):
                decode_calls.append((speakers, kwargs.get("timed_output"), kwargs.get("screen_nonspeech")))
                return {"text": "Tentative live words", "language": "English", "segments": [], "words": []}

            with (
                patch("recording_power.keep_awake_until"),
                patch("server.schedule_prewarm") as prewarm,
                patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
                patch("server.decode_pcm", side_effect=fake_decode),
            ):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Origin": URL}) as client:
                    started = await client.post("/api/portable/recording", params={"mode": "mic", "live": "true", "language": "en"})
                    self.assertEqual(started.status_code, 201)
                    prewarm.assert_called_once()
                    capture_id = started.json()["id"]
                    preview_url = f"/api/portable/recording/{capture_id}/preview"

                    async with asyncio.timeout(3):
                        while True:
                            resp = await client.get(preview_url, params={"after": 0, "rev": 0})
                            if resp.status_code == 200:
                                break
                            await asyncio.sleep(0.05)
                    body = resp.json()
                    self.assertEqual(body["text"], "")
                    self.assertEqual(body["tentative"], "Tentative live words")
                    self.assertEqual(body["through"], 0)
                    self.assertGreater(body["rev"], 0)
                    # Tentative preview must NOT run diarizer or timed alignment, and MUST screen nonspeech.
                    self.assertEqual(decode_calls[0], (None, False, True))
                    # Subsequent poll with same rev returns 204.
                    self.assertEqual((await client.get(preview_url, params={"after": 0, "rev": body["rev"]})).status_code, 204)
                    # Tentative words must NOT be written to .rolling.ndjson.
                    self.assertEqual(list(read_rolling(state.portable_recording.path, len(voiced) * 3)), [])

                    # Verify save-first Stop does NOT hang when preview_task is stuck.
                    capture = state.portable_recording
                    capture.preview_task.cancel()
                    try:
                        await capture.preview_task
                    except asyncio.CancelledError:
                        pass
                    capture.preview_task = asyncio.create_task(asyncio.sleep(10))
                    state.live_speakers[capture.key] = (0.0, object())
                    t0 = asyncio.get_running_loop().time()
                    async with stopped_portable_recording(state, capture.key):
                        elapsed = asyncio.get_running_loop().time() - t0
                    self.assertLess(elapsed, 1.0)
                    self.assertNotIn(capture.key, state.live_speakers)

    async def test_mac_bridge_tentative_then_pause_commit_and_hidden_live_meeting_skips_tentative(self):
        with TemporaryDirectory() as temp:
            wav = Path(temp) / "meeting.wav"
            # 2.0 s of speech -> should emit tentative when live=True, skip tentative when live=False.
            _write_wav(wav, _tone_i16(2.0, 48_000), 48_000)

            bridge = mac_bridge.MacBridge(Path(temp), SimpleNamespace())
            meeting_id = str(uuid4())
            copy_state = {"through": 0, "changed": asyncio.Event()}
            take = mac_bridge.Capture(
                child=None,
                stage={"id": "stage-live", "path": str(wav)},
                marker=None,
                settings={"language": ["en"]},
                state={"live": True, "meeting": meeting_id, "meetingTurns": [], "text": "", "tentative": "", "segments": []},
                global_dictation=False,
                started=True,
                copy_state=copy_state,
            )

            api_calls = []
            emitted = []

            async def fake_api(method, path, **kwargs):
                api_calls.append(kwargs.get("params", {}))
                if kwargs.get("params", {}).get("tentative") == "true":
                    return {"text": "Fast tentative", "tentative": True, "segments": [], "words": [], "activity": []}
                return {
                    "text": "Fast tentative committed.",
                    "segments": [{"speaker": 1, "text": "Fast tentative committed.", "start": 0.0, "end": 3.0}],
                    "words": [],
                    "activity": [],
                }

            step = 0

            async def stepped_sleep(_delay):
                nonlocal step
                step += 1
                if step == 2:
                    # Append 1.0 s more speech + 0.6 s pause to trigger pause commit.
                    _write_wav(wav, np.concatenate([_tone_i16(3.0, 48_000), _silence_i16(0.6, 48_000)]), 48_000)
                elif step >= 3:
                    take.finished = True

            with (
                patch.object(bridge, "api", side_effect=fake_api),
                patch.object(mac_bridge, "emit", side_effect=lambda *a, **kw: emitted.append((a, kw))),
                patch.object(mac_bridge, "append_rolling") as append_mock,
                patch.object(mac_bridge.asyncio, "sleep", side_effect=stepped_sleep),
            ):
                await bridge.live_preview(take)

            self.assertEqual(len(api_calls), 2)
            self.assertEqual(api_calls[0].get("tentative"), "true")
            self.assertNotIn("tentative", api_calls[1])
            append_mock.assert_called_once()
            self.assertEqual(take.state["text"], "Fast tentative committed.")
            self.assertEqual(take.state["tentative"], "")
            self.assertEqual(emitted[0][1]["tentative"], "Fast tentative")
            self.assertEqual(emitted[1][1]["tentative"], "")

    async def test_portable_stop_keeps_inference_serialized_until_cancelled_preview_worker_exits(self):
        @asynccontextmanager
        async def audio_priority():
            yield

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.post("/api/portable/recording", status_code=201)(start_portable_recording)
            state = app.state
            state.library = SessionStore(Path(directory))
            state.portable_workspace = True
            state.draining = False
            state.status = "ready"
            state.inference = SpeechInference()
            state.live_speakers = {}
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.screen_draft_dir = None
            state.portable_recording_dir = Path(directory) / "microphone-drafts"
            state.portable_recording_dir.mkdir()
            state.portable_recording_lock = asyncio.Lock()
            state.jobs = set()
            state.diarizer = None
            state.insights = SimpleNamespace(model=SimpleNamespace(audio_priority=audio_priority))

            worker_started = threading.Event()
            worker_release = threading.Event()
            dictation_started = threading.Event()
            concurrency_lock = threading.Lock()
            in_flight = 0
            max_in_flight = 0

            class BlockingModel:
                def transcribe(self, pcm, languages, *, timed=False, cache_implementation=None, **options):
                    nonlocal in_flight, max_in_flight
                    with concurrency_lock:
                        in_flight += 1
                        max_in_flight = max(max_in_flight, in_flight)
                    try:
                        worker_started.set()
                        worker_release.wait(timeout=5.0)
                        return "stale preview", "English", []
                    finally:
                        with concurrency_lock:
                            in_flight -= 1

                def release_device_cache(self):
                    pass

            state.model = BlockingModel()
            voiced = _tone_i16(2.0, SAMPLE_RATE)

            def recorder(key, path, mode):
                worker = asyncio.Future()

                async def start():
                    with private_wav(path) as (audio, _):
                        audio.writeframes(voiced.tobytes())

                async def stop():
                    if not worker.done():
                        worker.set_result(None)

                return SimpleNamespace(key=key, path=path, frames=len(voiced), worker=worker, start=start, stop=stop)

            state.portable_recorder = state.portable_meeting_recorder = recorder

            def dictation_inference():
                nonlocal in_flight, max_in_flight
                dictation_started.set()
                with concurrency_lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                try:
                    return "dictation-ok"
                finally:
                    with concurrency_lock:
                        in_flight -= 1

            with (
                patch("recording_power.keep_awake_until"),
                patch("server.schedule_prewarm"),
                patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
            ):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Origin": URL}) as client:
                    started = await client.post("/api/portable/recording", params={"mode": "mic", "live": "true", "language": "en"})
                    self.assertEqual(started.status_code, 201)
                    capture = state.portable_recording
                    # Wait until the live preview thread is actively running inside BlockingModel.
                    async with asyncio.timeout(3):
                        while not worker_started.is_set():
                            await asyncio.sleep(0.02)

                    # Stop must return promptly (~0.35 s) even while the preview thread is still blocked.
                    t0 = asyncio.get_running_loop().time()
                    async with stopped_portable_recording(state, capture.key):
                        stop_elapsed = asyncio.get_running_loop().time() - t0
                    self.assertLess(stop_elapsed, 1.0)
                    self.assertTrue(state.inference.active)

                    # Immediately launch a dictation: it MUST wait until the old preview thread exits.
                    dictation_task = asyncio.create_task(run_inference(state, dictation_inference, dictation=True))
                    await asyncio.sleep(0.05)
                    self.assertFalse(dictation_started.is_set())
                    self.assertEqual(max_in_flight, 1)

                    # Release the old preview worker thread -> lock releases -> dictation runs cleanly.
                    worker_release.set()
                    self.assertEqual(await asyncio.wait_for(dictation_task, 2.0), "dictation-ok")
                    if state.jobs:
                        await asyncio.gather(*list(state.jobs))
                    self.assertEqual(max_in_flight, 1)
                    self.assertFalse(state.inference.active)
                    self.assertEqual(len(capture.preview_chunks), 0)
                    self.assertEqual(list(read_rolling(capture.path, len(voiced) * 3)), [])

    async def test_portable_real_save_path_preserves_phrase_spoken_just_before_stop(self):
        @asynccontextmanager
        async def audio_priority(**_kwargs):
            yield

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.post("/api/portable/recording", status_code=201)(start_portable_recording)
            state = app.state
            state.library = SessionStore(Path(directory))
            state.portable_workspace = True
            state.draining = False
            state.status = "ready"
            state.inference = SpeechInference()
            state.live_speakers = {}
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.screen_draft_dir = None
            state.portable_recording_dir = Path(directory) / "microphone-drafts"
            state.portable_recording_dir.mkdir()
            state.portable_recording_lock = asyncio.Lock()
            state.jobs = set()
            state.diarizer = None
            state.diarizer_lazy = False
            state.insights = SimpleNamespace(
                model=SimpleNamespace(audio_priority=audio_priority),
                wake=asyncio.Event(),
            )

            class StubFinalModel:
                def transcribe(self, pcm, languages, *, timed=False, cache_implementation=None, **options):
                    seconds = len(pcm) / (4 * SAMPLE_RATE)
                    if seconds >= 4.0:
                        return "Opening phrase. Final phrase just before stop.", "English", []
                    return "Opening phrase.", "English", []

                def release_device_cache(self):
                    pass

            state.model = StubFinalModel()
            initial = np.concatenate([_tone_i16(2.8, SAMPLE_RATE), _silence_i16(0.6, SAMPLE_RATE)])
            tail = _tone_i16(1.2, SAMPLE_RATE)
            capture_ref = {}

            def recorder(key, path, mode):
                worker = asyncio.Future()

                async def start():
                    with private_wav(path) as (audio, _):
                        audio.writeframes(initial.tobytes())

                async def stop():
                    # Simulate a trailing phrase captured right before Stop (never seen by live preview).
                    full = np.concatenate([initial, tail])
                    _write_wav(path, full, SAMPLE_RATE)
                    capture_ref["take"].frames = len(full)
                    if not worker.done():
                        worker.set_result(None)

                take = SimpleNamespace(key=key, path=path, frames=len(initial), worker=worker, start=start, stop=stop)
                capture_ref["take"] = take
                return take

            state.portable_recorder = state.portable_meeting_recorder = recorder

            with (
                patch("recording_power.keep_awake_until"),
                patch("server.schedule_prewarm"),
                patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
            ):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Origin": URL}) as client:
                    started = await client.post("/api/portable/recording", params={"mode": "mic", "live": "true", "language": "en"})
                    self.assertEqual(started.status_code, 201)
                    capture_id = started.json()["id"]
                    capture = state.portable_recording
                    async with asyncio.timeout(3):
                        while not capture.preview_processed:
                            await asyncio.sleep(0.03)

                accepted = await finish_portable_capture(
                    state, capture.key, ("en",), name="Session", diarization=False, live=True,
                )
                self.assertEqual(accepted.status_code, 202)
                if state.jobs:
                    await asyncio.gather(*list(state.jobs))

                saved = state.library.get(capture_id)
                self.assertEqual(saved["status"], "complete")
                self.assertIn("Final phrase just before stop.", saved["transcript"]["text"])

    async def test_macos_stop_saves_promptly_without_waiting_for_pending_copy_request(self):
        with TemporaryDirectory() as temp:
            wav = Path(temp) / "meeting.wav"
            _write_wav(wav, _tone_i16(2.0, 48_000), 48_000)
            marker = wav.with_suffix(".recording")
            marker.write_bytes(b"{}")

            bridge = mac_bridge.MacBridge(Path(temp), SimpleNamespace())
            meeting_id = str(uuid4())
            events_queue: asyncio.Queue = asyncio.Queue()

            class FakeChild:
                returncode = 0

                async def wait(self):
                    return 0

            async def fake_command(cmd):
                if cmd.startswith("copy "):
                    req = cmd.split(" ", 1)[1]
                    await events_queue.put({"event": "meetingCopy", "request": req, "frame": 2 * 48_000})

            take = mac_bridge.Capture(
                child=FakeChild(),
                stage={"id": str(uuid4()), "path": str(wav)},
                marker=marker.open("r+b"),
                settings={"language": ["en"]},
                state={
                    "mode": "both",
                    "live": True,
                    "warm": True,
                    "meeting": meeting_id,
                    "meetingLabel": "Meeting",
                    "meetingTurns": [],
                    "text": "Already committed.",
                    "tentative": "",
                    "segments": [],
                },
                global_dictation=False,
                warm_events=events_queue,
            )
            take.command = fake_command
            bridge.captures["recording"] = take
            submit_called = asyncio.Event()

            async def fake_submit(*_args, **_kwargs):
                submit_called.set()
                return {"id": take.stage["id"]}

            async def idle_preview(_take):
                while not _take.finished:
                    await asyncio.sleep(0.05)

            with (
                patch.object(bridge, "live_preview", side_effect=idle_preview),
                patch.object(bridge, "submit", side_effect=fake_submit),
                patch.object(bridge, "reconcile_warm", new_callable=AsyncMock),
                patch.object(mac_bridge, "emit"),
                patch.object(mac_bridge, "write_event"),
            ):
                record_task = asyncio.create_task(bridge.record_events("recording", take))
                await events_queue.put({"event": "started", "microphone": "Mic"})
                await asyncio.sleep(0.02)

                # Start a Copy New / Copy All request while the meeting is recording.
                copy_task = asyncio.create_task(bridge.copy_meeting_through_now({"meeting": meeting_id}))
                await asyncio.sleep(0.05)
                self.assertIsNotNone(take.copy_state.get("cut"))

                # Trigger Stop while Copy is still waiting: must NOT wait for Copy's 3-second timeout!
                t0 = asyncio.get_running_loop().time()
                await events_queue.put({"event": "finished", "reason": "Stopped"})
                await events_queue.put({"event": "takeEnded"})
                await asyncio.wait_for(record_task, 1.0)
                stop_elapsed = asyncio.get_running_loop().time() - t0
                copy_result = await asyncio.wait_for(copy_task, 1.0)

            self.assertTrue(submit_called.is_set())
            self.assertLess(stop_elapsed, 0.6)
            self.assertEqual(copy_result, {"through": 0, "complete": False})

    async def test_portable_stop_logs_preview_exception_commits_recording_and_preserves_stop_cancel(self):
        @asynccontextmanager
        async def audio_priority(**_kwargs):
            yield

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.post("/api/portable/recording", status_code=201)(start_portable_recording)
            state = app.state
            state.library = SessionStore(Path(directory))
            state.portable_workspace = True
            state.draining = False
            state.status = "ready"
            state.inference = SpeechInference()
            state.live_speakers = {}
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.screen_draft_dir = None
            state.portable_recording_dir = Path(directory) / "microphone-drafts"
            state.portable_recording_dir.mkdir()
            state.portable_recording_lock = asyncio.Lock()
            state.jobs = set()
            state.diarizer = None
            state.diarizer_lazy = False
            state.insights = SimpleNamespace(
                model=SimpleNamespace(audio_priority=audio_priority),
                wake=asyncio.Event(),
            )

            class StubModel:
                def transcribe(self, pcm, languages, *, timed=False, cache_implementation=None, **options):
                    return "Committed after preview crash.", "English", []

                def release_device_cache(self):
                    pass

            state.model = StubModel()
            voiced = _tone_i16(2.0, SAMPLE_RATE)

            def recorder(key, path, mode):
                worker = asyncio.Future()

                async def start():
                    with private_wav(path) as (audio, _):
                        audio.writeframes(voiced.tobytes())

                async def stop():
                    if not worker.done():
                        worker.set_result(None)

                return SimpleNamespace(key=key, path=path, frames=len(voiced), worker=worker, start=start, stop=stop)

            state.portable_recorder = state.portable_meeting_recorder = recorder

            async def failing_preview(_state, capture, _languages, _diarization):
                while _state.portable_recording is capture and not capture.worker.done():
                    await asyncio.sleep(0.02)
                raise RuntimeError("unexpected portable preview boom")

            with (
                patch("recording_power.keep_awake_until"),
                patch("server.schedule_prewarm"),
                patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
                patch("server.preview_portable_recording", side_effect=failing_preview),
            ):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Origin": URL}) as client:
                    started = await client.post("/api/portable/recording", params={"mode": "mic", "live": "true", "language": "en"})
                    self.assertEqual(started.status_code, 201)
                    capture_id = started.json()["id"]
                    capture = state.portable_recording
                    await asyncio.sleep(0.03)

                with self.assertLogs("uvicorn.error", level="ERROR") as captured_logs:
                    accepted = await finish_portable_capture(
                        state, capture.key, ("en",), name="Session", diarization=False, live=True,
                    )
                self.assertEqual(accepted.status_code, 202)
                self.assertTrue(any("unexpected portable preview boom" in msg for msg in captured_logs.output))
                if state.jobs:
                    await asyncio.gather(*list(state.jobs))

                saved = state.library.get(capture_id)
                self.assertEqual(saved["status"], "complete")
                self.assertEqual(saved["transcript"]["text"], "Committed after preview crash.")

            # Verify cancelling the Stop request itself is preserved and still holds the inference lock
            # until an in-flight preview worker thread exits.
            worker_started = threading.Event()
            worker_release = threading.Event()

            def blocking_worker():
                worker_started.set()
                worker_release.wait(timeout=5.0)
                return "done"

            cancel_key = uuid4()
            stop_done = asyncio.Event()

            async def fake_stop():
                stop_done.set()

            preview_task = asyncio.create_task(run_inference(state, blocking_worker))
            async with asyncio.timeout(3):
                while not worker_started.is_set():
                    await asyncio.sleep(0.01)

            state.portable_recording = SimpleNamespace(
                key=cancel_key,
                stop=fake_stop,
                preview_task=preview_task,
                meeting_claim=None,
            )

            async def run_stop():
                async with stopped_portable_recording(state, cancel_key):
                    pass

            stop_task = asyncio.create_task(run_stop())
            await asyncio.wait_for(stop_done.wait(), 1.0)
            await asyncio.sleep(0.02)
            stop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task
            await asyncio.sleep(0.02)
            self.assertTrue(preview_task.cancelled())
            self.assertTrue(state.inference.active)
            worker_release.set()
            if state.jobs:
                await asyncio.gather(*list(state.jobs))
            self.assertFalse(state.inference.active)

    async def test_macos_stop_logs_preview_exception_commits_recording_and_preserves_stop_cancel(self):
        with TemporaryDirectory() as temp:
            wav = Path(temp) / "meeting.wav"
            _write_wav(wav, _tone_i16(2.0, 48_000), 48_000)
            marker = wav.with_suffix(".recording")
            marker.write_bytes(b"{}")

            bridge = mac_bridge.MacBridge(Path(temp), SimpleNamespace())
            meeting_id = str(uuid4())
            events_queue: asyncio.Queue = asyncio.Queue()

            class FakeChild:
                returncode = 0

                async def wait(self):
                    return 0

            take = mac_bridge.Capture(
                child=FakeChild(),
                stage={"id": str(uuid4()), "path": str(wav)},
                marker=marker.open("r+b"),
                settings={"language": ["en"]},
                state={
                    "mode": "both",
                    "live": True,
                    "warm": True,
                    "meeting": meeting_id,
                    "meetingLabel": "Meeting",
                    "meetingTurns": [],
                    "text": "",
                    "tentative": "",
                    "segments": [],
                },
                global_dictation=False,
                warm_events=events_queue,
            )
            bridge.captures["recording"] = take
            submitted = []
            emitted = []

            async def fake_submit(stage_id, *_args, **_kwargs):
                submitted.append(stage_id)
                return {"id": stage_id}

            async def failing_preview(_take):
                while not _take.finished:
                    await asyncio.sleep(0.02)
                raise RuntimeError("unexpected mac preview boom")

            with (
                patch.object(bridge, "live_preview", side_effect=failing_preview),
                patch.object(bridge, "submit", side_effect=fake_submit),
                patch.object(bridge, "reconcile_warm", new_callable=AsyncMock),
                patch.object(mac_bridge, "emit", side_effect=lambda *a, **kw: emitted.append((a, kw))),
                patch.object(mac_bridge, "write_event"),
            ):
                with self.assertLogs(mac_bridge.logger, level="ERROR") as captured_logs:
                    record_task = asyncio.create_task(bridge.record_events("recording", take))
                    await events_queue.put({"event": "started", "microphone": "Mic"})
                    await asyncio.sleep(0.03)
                    await events_queue.put({"event": "finished", "reason": "Stopped"})
                    await events_queue.put({"event": "takeEnded"})
                    await asyncio.wait_for(record_task, 1.0)

            self.assertEqual(submitted, [take.stage["id"]])
            self.assertTrue(any(args == ("accepted",) and kwargs.get("id") == take.stage["id"] for args, kwargs in emitted))
            self.assertTrue(any("unexpected mac preview boom" in msg for msg in captured_logs.output))

            # Verify cancelling record_events during Stop preview wait preserves CancelledError and cancels preview.
            marker2 = (Path(temp) / "meeting2.recording")
            marker2.write_bytes(b"{}")
            events_queue2: asyncio.Queue = asyncio.Queue()
            preview_waiting = asyncio.Event()
            preview_cancelled = asyncio.Event()

            async def slow_preview(_take):
                try:
                    while not _take.finished:
                        await asyncio.sleep(0.01)
                    preview_waiting.set()
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    preview_cancelled.set()
                    raise

            take2 = mac_bridge.Capture(
                child=FakeChild(),
                stage={"id": str(uuid4()), "path": str(wav)},
                marker=marker2.open("r+b"),
                settings={"language": ["en"]},
                state={
                    "mode": "both",
                    "live": True,
                    "warm": True,
                    "meeting": str(uuid4()),
                    "meetingLabel": "Meeting",
                    "meetingTurns": [],
                    "text": "",
                    "tentative": "",
                    "segments": [],
                },
                global_dictation=False,
                warm_events=events_queue2,
            )
            bridge.captures["recording"] = take2

            with (
                patch.object(bridge, "live_preview", side_effect=slow_preview),
                patch.object(bridge, "submit", side_effect=fake_submit),
                patch.object(bridge, "reconcile_warm", new_callable=AsyncMock),
                patch.object(mac_bridge, "emit"),
                patch.object(mac_bridge, "write_event"),
            ):
                record_task2 = asyncio.create_task(bridge.record_events("recording", take2))
                await events_queue2.put({"event": "started", "microphone": "Mic"})
                await events_queue2.put({"event": "finished", "reason": "Stopped"})
                await events_queue2.put({"event": "takeEnded"})
                await asyncio.wait_for(preview_waiting.wait(), 1.0)
                await asyncio.sleep(0.02)
                record_task2.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await record_task2
                await asyncio.wait_for(preview_cancelled.wait(), 1.0)
                take2.marker.close()


if __name__ == "__main__":
    unittest.main()
