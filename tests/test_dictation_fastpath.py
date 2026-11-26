import math
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

import numpy as np

import audio_file
from rumble_model import MAX_SAMPLES, RumbleModel, SAMPLE_RATE
import server


def _write_pcm16_wav(path: Path, samples: np.ndarray, *, rate: int, channels: int = 1):
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm.tobytes())


class DictationFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_decode_pcm_with_speakers_preserves_word_times_when_timed_output_true(self):
        captured = {}

        class FakeSpeakers:
            def push(self, pcm, offset):
                captured["pushed"] = (len(pcm), offset)

            def label(self, text, words, offset, duration, *, include_word_times=True):
                captured["include_word_times"] = include_word_times
                return ([{"speaker": 0, "text": text, "start": 0.0, "end": duration}],
                        [{"word": "hello", "start": 0.1, "end": 0.4}] if include_word_times else [])

        class FakeModel:
            def transcribe(self, pcm, languages, *, timed=False, cache_implementation=None, **options):
                captured["timed"] = timed
                captured["cache"] = cache_implementation
                return "hello", "en", [("hello", 0.1, 0.4)]

        state = SimpleNamespace(model=FakeModel())

        async def run_direct(_state, fn, *, dictation=False):
            return fn()

        pcm = np.full(SAMPLE_RATE, 0.1, dtype="<f4").tobytes()
        with patch("server.run_inference", side_effect=run_direct):
            result = await server.decode_pcm(
                state, pcm, ("en",), speakers=FakeSpeakers(), live=True, timed_output=True,
            )

        self.assertTrue(captured["timed"])
        self.assertEqual(captured["cache"], "static")
        self.assertTrue(captured["include_word_times"])
        self.assertEqual(result["words"], [{"word": "hello", "start": 0.1, "end": 0.4}])

    async def test_dictation_decode_pcm_uses_static_cache_and_falls_back_on_oom(self):
        calls = []
        released = []

        class FakeModel:
            def transcribe(self, pcm, languages, *, timed=False, cache_implementation=None, **options):
                calls.append(cache_implementation)
                if cache_implementation == "static":
                    raise RuntimeError("MPS backend out of memory")
                return "ok", "en", []

            def release_device_cache(self):
                released.append(True)

        state = SimpleNamespace(model=FakeModel())

        async def run_direct(_state, fn, *, dictation=False):
            return fn()

        pcm = np.full(SAMPLE_RATE, 0.1, dtype="<f4").tobytes()
        with patch("server.run_inference", side_effect=run_direct):
            result = await server.decode_pcm(state, pcm, ("en",), live=True, dictation=True)

        self.assertEqual(calls, ["static", None])
        self.assertEqual(released, [True])
        self.assertEqual(result["text"], "ok")

    def test_rumble_decode_reuses_single_encoder_pass_when_tail_is_silent(self):
        import torch

        engine = RumbleModel.__new__(RumbleModel)
        engine.torch = torch
        engine.reported_screen_failure = False
        audio_feature_calls = 0

        class FakeAligner:
            def confidently_blank(self, hidden):
                return False

        class FakeModel:
            def generate(self, **kwargs):
                self.last_generate = kwargs
                return torch.tensor([[1, 2, 3]])

        engine.aligner = FakeAligner()
        engine.model = FakeModel()
        engine.processor = SimpleNamespace(
            decode=lambda generated, return_format="parsed": [{"transcription": "hi", "language": "en"}]
        )
        engine._transcription_inputs = lambda audio, prompt=None: {
            "input_ids": torch.tensor([[1, 2]]),
            "attention_mask": torch.tensor([[1, 1]]),
            "input_features": torch.zeros((1, 2, 2)),
            "input_features_mask": torch.ones((1, 2)),
        }

        def fake_screened(inputs):
            nonlocal audio_feature_calls
            audio_feature_calls += 1
            return {"input_ids": inputs["input_ids"], "attention_mask": inputs["attention_mask"], "inputs_embeds": "reused"}

        def fake_audio_features(inputs):
            nonlocal audio_feature_calls
            audio_feature_calls += 1
            return SimpleNamespace(last_hidden_state=torch.zeros((1, 2, 2)))

        engine._screened_inputs = fake_screened
        engine._audio_features = fake_audio_features

        # Silent post-release tail -> single pass via _screened_inputs
        silent_tail = np.concatenate([np.full(100, 0.2, dtype=np.float32), np.zeros(50, dtype=np.float32)])
        text, _ = engine._decode(silent_tail, screen_nonspeech=True, speech_prefix_samples=100)
        self.assertEqual(text, "hi")
        self.assertEqual(audio_feature_calls, 1)
        self.assertEqual(engine.model.last_generate["inputs_embeds"], "reused")

        # Non-silent post-release tail -> keeps separate prefix screen to avoid leakage
        audio_feature_calls = 0
        voiced_tail = np.full(150, 0.2, dtype=np.float32)
        text, _ = engine._decode(voiced_tail, screen_nonspeech=True, speech_prefix_samples=100)
        self.assertEqual(text, "hi")
        self.assertEqual(audio_feature_calls, 1)
        self.assertNotIn("inputs_embeds", engine.model.last_generate)

    async def test_audio_blocks_fast_wav_16k_and_48k_without_ffmpeg_and_fallback_on_stereo(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            # 16 kHz mono 1s sine wave
            t16 = np.arange(SAMPLE_RATE, dtype=np.float32) / SAMPLE_RATE
            wav16 = root / "mono16.wav"
            _write_pcm16_wav(wav16, 0.5 * np.sin(2 * math.pi * 440 * t16), rate=SAMPLE_RATE)

            # 48 kHz mono 1.5s sine wave
            t48 = np.arange(72_000, dtype=np.float32) / 48_000
            wav48 = root / "mono48.wav"
            _write_pcm16_wav(wav48, 0.5 * np.sin(2 * math.pi * 440 * t48), rate=48_000)

            with patch("audio_file.open_audio_pcm", side_effect=AssertionError("ffmpeg should not run")):
                blocks16 = [part async for part in audio_file.audio_blocks(wav16, pause_aware=True, fast_wav=True)]
                blocks48 = [part async for part in audio_file.audio_blocks(wav48, pause_aware=True, fast_wav=True)]

            self.assertEqual(len(blocks16), 1)
            self.assertEqual(len(blocks16[0]), SAMPLE_RATE * 4)
            self.assertEqual(len(blocks48), 1)
            self.assertEqual(len(blocks48[0]), int(1.5 * SAMPLE_RATE) * 4)

            # Pause-aware multi-window WAV (>25s) splits cleanly without ffmpeg
            wav30 = root / "long16.wav"
            samples30 = np.full(SAMPLE_RATE * 30, 0.2, dtype=np.float32)
            samples30[SAMPLE_RATE * 22:SAMPLE_RATE * 23] = 0.0
            _write_pcm16_wav(wav30, samples30, rate=SAMPLE_RATE)
            with patch("audio_file.open_audio_pcm", side_effect=AssertionError("ffmpeg should not run")):
                blocks30 = [part async for part in audio_file.audio_blocks(wav30, pause_aware=True, fast_wav=True)]
            self.assertEqual(len(blocks30), 2)
            self.assertEqual(sum(len(b) for b in blocks30), SAMPLE_RATE * 30 * 4)
            self.assertLess(len(blocks30[0]), MAX_SAMPLES * 4)

            # Stereo WAV falls back to ffmpeg
            stereo = root / "stereo.wav"
            _write_pcm16_wav(stereo, np.zeros(SAMPLE_RATE * 2, dtype=np.float32), rate=SAMPLE_RATE, channels=2)
            self.assertIsNone(audio_file._read_fast_wav_pcm(stereo))

    def test_rumble_prewarm_skips_when_recent_and_warms_when_stale(self):
        import time

        engine = RumbleModel.__new__(RumbleModel)
        engine.np = np
        engine.last_run = 0.0
        decode_calls = []
        screen_calls = []
        released = []

        fail_first_static = True

        def fake_decode(audio, *, max_new_tokens=512, cache_implementation=None, **_kwargs):
            nonlocal fail_first_static
            decode_calls.append((len(audio), max_new_tokens, cache_implementation))
            if cache_implementation == "static" and fail_first_static:
                fail_first_static = False
                raise RuntimeError("MPS backend out of memory")
            engine.last_run = time.monotonic()
            return "", None

        engine._decode = fake_decode
        engine.release_device_cache = lambda: released.append(True)
        engine.aligner = SimpleNamespace(warm_screen=lambda: screen_calls.append(True))

        # Cold model: runs silent prewarm (falling back to dynamic cache on OOM) + warms aligner screen
        self.assertTrue(engine.prewarm())
        self.assertEqual(decode_calls, [(SAMPLE_RATE // 2, 1, "static"), (SAMPLE_RATE // 2, 1, None)])
        self.assertEqual(released, [True])
        self.assertEqual(screen_calls, [True])

        # Recently run: skips prewarm
        decode_calls.clear()
        screen_calls.clear()
        self.assertTrue(engine.is_warm())
        self.assertFalse(engine.prewarm())
        self.assertEqual(decode_calls, [])
        self.assertEqual(screen_calls, [])

        # Stale again: prewarms with static cache
        engine.last_run = time.monotonic() - 120.0
        self.assertFalse(engine.is_warm())
        self.assertTrue(engine.prewarm())
        self.assertEqual(decode_calls, [(SAMPLE_RATE // 2, 1, "static")])
        self.assertEqual(screen_calls, [True])

    async def test_schedule_prewarm_runs_when_idle_and_skips_when_warm_or_busy(self):
        import asyncio

        calls = []

        class FakeModel:
            warm = False

            def is_warm(self):
                return self.warm

            def prewarm(self):
                calls.append(True)
                self.warm = True
                return True

        state = SimpleNamespace(
            status="ready", draining=False, model=FakeModel(),
            inference=server.SpeechInference(), prewarm_task=None,
        )
        self.assertTrue(server.schedule_prewarm(state))
        task = state.prewarm_task
        self.assertIsNotNone(task)
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(calls, [True])
        # Now warm -> skipped
        self.assertFalse(server.schedule_prewarm(state))
        # Stale but busy -> skipped
        state.model.warm = False
        state.inference.active = True
        self.assertFalse(server.schedule_prewarm(state))

    async def test_lazy_diarizer_defers_load_until_needed_and_unloads_when_idle(self):
        import asyncio
        import sys
        import types

        loaded = []

        class FakeDiarizer:
            def __init__(self, device):
                loaded.append(device)

        fake_module = types.ModuleType("diarization")
        fake_module.Diarizer = FakeDiarizer
        released = []
        state = SimpleNamespace(
            diarizer=None, diarizer_lazy=False, diarizer_lock=asyncio.Lock(), diarizer_last_used=0.0,
            draining=False, jobs=set(), submitting=set(), live_speakers={}, portable_recording=None,
            inference=server.SpeechInference(),
            model=SimpleNamespace(device="mps", aligner=object(), release_device_cache=lambda: released.append(True)),
        )
        with patch("server.diarization_installed", return_value=True), patch.dict(sys.modules, {"diarization": fake_module}):
            await server.load_speaker_tools(state)
            self.assertIsNone(state.diarizer)
            self.assertTrue(state.diarizer_lazy)
            self.assertTrue(server.diarization_ready(state))
            self.assertEqual(loaded, [])

            diarizer = await server.ensure_diarizer(state)
            self.assertIsInstance(diarizer, FakeDiarizer)
            self.assertEqual(loaded, ["mps"])

            # Idle past threshold -> unloads diarizer and trims MPS cache
            state.diarizer_last_used -= server.IDLE_DIARIZER_UNLOAD_SECONDS + 1
            await server.maybe_unload_idle_diarizer(state)
            self.assertIsNone(state.diarizer)
            self.assertTrue(server.diarization_ready(state))
            self.assertEqual(released, [True])


if __name__ == "__main__":
    unittest.main()
