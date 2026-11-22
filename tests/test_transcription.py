import asyncio
from contextlib import contextmanager, nullcontext
from pathlib import Path
import os
import plistlib
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch
from uuid import uuid4
import wave

import numpy as np

from audio_file import PAUSE_AWARE_CHUNKING, audio_blocks, dictation_has_signal, quiet_sample_boundary, split_at_pauses
from diarization import SpeakerSession
from rumble_model import MAX_SAMPLES, SAMPLE_RATE
from transcription_pieces import wav_piece


def write_mic_fixture(path, samples):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
        stream.writeframes(np.asarray(samples, dtype="<i2").tobytes())


class TranscriptionTests(unittest.TestCase):
    def test_speech_backend_keeps_metal_default_and_supports_explicit_cpu(self):
        from rumble_model import RumbleModel, speech_backend

        torch = Mock()
        torch.backends.mps.is_available.return_value = True
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "platform", "darwin"):
            self.assertEqual(speech_backend(torch), ("mps", torch.bfloat16))

        torch.backends.mps.is_available.reset_mock()
        torch.backends.mps.is_available.return_value = False
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "platform", "linux"):
            self.assertEqual(speech_backend(torch), ("cpu", torch.float32))
        with patch.dict(os.environ, {"JARD_INFERENCE_DEVICE": "cpu"}, clear=True):
            self.assertEqual(speech_backend(torch), ("cpu", torch.float32))
            with patch.dict(os.environ, {"JARD_INFERENCE_DTYPE": "bfloat16"}):
                self.assertEqual(speech_backend(torch), ("cpu", torch.bfloat16))
            with patch.dict(os.environ, {"JARD_INFERENCE_DTYPE": "unknown"}):
                with self.assertRaisesRegex(ValueError, "JARD_INFERENCE_DTYPE"):
                    speech_backend(torch)
        torch.backends.mps.is_available.assert_not_called()

        model = object.__new__(RumbleModel)
        model.torch = torch
        model.device = "cpu"
        model.release_device_cache()
        torch.mps.empty_cache.assert_not_called()
        model.device = "mps"
        model.release_device_cache()
        torch.mps.empty_cache.assert_called_once_with()

    def test_shared_vocabulary_uses_private_preferences_and_the_model_limit(self):
        from saved_dictations import shared_vocabulary

        with TemporaryDirectory() as directory, patch("saved_dictations.preferences_path") as preferences:
            file = Path(directory) / "preferences.plist"
            preferences.return_value = file
            self.assertEqual(shared_vocabulary(), ())
            lines = ["  Café  ", "CAFE\u0301", "x" * 81, "אדם", *[f"Name {index}" for index in range(35)]]
            file.write_bytes(plistlib.dumps({"JardSystemDictation": {"vocabulary": "\r\n".join(lines)}}))
            file.chmod(0o600)
            self.assertEqual(shared_vocabulary(), ("Café", "אדם", *(f"Name {index}" for index in range(30))))
            file.chmod(0o644)
            self.assertEqual(shared_vocabulary(), ())
            file.chmod(0o600)
            file.write_bytes(b"unreadable plist")
            self.assertEqual(shared_vocabulary(), ())

    def test_vocab_echo_retries_with_only_the_language_hint_and_new_alignment(self):
        from rumble_model import RumbleModel, language_prompt

        @contextmanager
        def capture():
            yield [next(frames)]

        frames = iter(("prompted", "unprompted"))
        engine = RumbleModel.__new__(RumbleModel)
        engine.np = np
        engine.aligner = SimpleNamespace(capture=capture, words=Mock(return_value=["word times"]))
        engine._decode = Mock(side_effect=[("CAFÉ NOIR; אדם!", "French"), ("Café noir.", "French")])
        audio = np.full(1600, .3, dtype="<f4").tobytes()
        self.assertEqual(engine.transcribe(audio, ("fr",), timed=True, vocabulary=("Café noir", "אדם")),
                         ("Café noir.", "French", ["word times"]))
        first, retry = engine._decode.call_args_list
        self.assertIn("Possible names and terms", first.kwargs["prompt"])
        self.assertEqual(retry.kwargs["prompt"], language_prompt(("fr",)))
        engine.aligner.words.assert_called_once_with("Café noir.", "unprompted")

        engine._decode = Mock(return_value=("Café noir.", "French"))
        self.assertEqual(engine.transcribe(audio, ("fr",), vocabulary=("Café noir",)), ("Café noir.", "French", []))
        engine._decode.assert_called_once()

    def test_ctc_screen_passes_any_ambiguous_frame(self):
        import torch
        from speaker_alignment import CtcAligner

        head = CtcAligner.__new__(CtcAligner)
        head.weight = torch.tensor([[1.0], [0.0], [-1.0]])
        head.bias = torch.tensor([0.0, 2.5, 0.0])
        head.blank = 1
        frames = torch.zeros(33, 1)
        self.assertTrue(head.confidently_blank(frames))
        frames[-1] = 1
        self.assertFalse(head.confidently_blank(frames))
        frames[-1] = float("nan")
        self.assertFalse(head.confidently_blank(frames))
        self.assertFalse(head.confidently_blank(frames[:0]))

    def test_rumble_screen_skips_generation_or_reuses_the_encoder_output(self):
        import torch
        from transformers.feature_extraction_utils import BatchFeature
        from rumble_model import RumbleModel

        engine = RumbleModel.__new__(RumbleModel)
        engine.torch = torch
        engine.reported_screen_failure = False
        ids = torch.tensor([[1, 0, 2]])
        attention = torch.ones_like(ids)
        inputs = BatchFeature({"input_ids": ids, "attention_mask": attention, "input_features": torch.zeros(1),
                               "input_features_mask": torch.ones(1, dtype=torch.long)})
        features = SimpleNamespace(last_hidden_state=torch.ones(1, 2), pooler_output=torch.tensor([[2.0, 3.0]]))
        engine.processor = SimpleNamespace(apply_transcription_request=Mock(return_value=inputs),
            decode=Mock(return_value=[{"transcription": " Test.", "language": "English"}]))
        engine.model = SimpleNamespace(device="cpu", dtype=torch.float32,
            get_audio_features=Mock(return_value=features), get_input_embeddings=lambda: lambda tokens: torch.ones(1, 3, 2),
            model=SimpleNamespace(get_placeholder_mask=lambda *args: (ids == 0).unsqueeze(-1).expand(1, 3, 2)),
            generate=Mock(return_value=torch.tensor([[1, 0, 2, 5]])))
        engine.aligner = SimpleNamespace(confidently_blank=Mock(return_value=True))

        self.assertEqual(engine._decode(np.zeros(1), screen_nonspeech=True), ("", None))
        engine.model.generate.assert_not_called()
        engine.aligner.confidently_blank.return_value = False
        self.assertEqual(engine._decode(np.zeros(1), screen_nonspeech=True), ("Test.", "English"))
        options = engine.model.generate.call_args.kwargs
        self.assertNotIn("input_features", options)
        torch.testing.assert_close(options["inputs_embeds"], torch.tensor([[[1., 1.], [2., 3.], [1., 1.]]]))
        torch.testing.assert_close(engine.processor.decode.call_args.args[0], torch.tensor([[5]]))

        # A crossing block screens a separate prefix and only decodes the full
        # recording if that prefix is admitted.
        audio = np.arange(12, dtype=np.float32)
        engine.model.generate.reset_mock()
        engine.aligner.confidently_blank.return_value = True
        self.assertEqual(engine._decode(audio, screen_nonspeech=True, speech_prefix_samples=6), ("", None))
        np.testing.assert_array_equal(engine.processor.apply_transcription_request.call_args.kwargs["audio"], audio[:6])
        engine.model.generate.assert_not_called()
        engine.aligner.confidently_blank.return_value = False
        self.assertEqual(engine._decode(audio, screen_nonspeech=True, speech_prefix_samples=6), ("Test.", "English"))
        np.testing.assert_array_equal(engine.processor.apply_transcription_request.call_args_list[-2].kwargs["audio"], audio)
        self.assertIn("input_features", engine.model.generate.call_args.kwargs)

        engine.aligner.confidently_blank.side_effect = RuntimeError("fixture projection failed")
        with self.assertLogs("uvicorn.error", level="WARNING"):
            self.assertEqual(engine._decode(np.zeros(1), screen_nonspeech=True), ("Test.", "English"))
        self.assertIn("input_features", engine.model.generate.call_args.kwargs)
        engine.aligner = None
        engine.model.get_audio_features.reset_mock()
        self.assertEqual(engine._decode(np.zeros(1), screen_nonspeech=True), ("Test.", "English"))
        engine.model.get_audio_features.assert_not_called()

    def test_dictation_signal_gate_preserves_sustained_quiet_audio(self):
        window = 1440
        noise = np.tile([4, -4], window // 2)
        quiet = np.tile([45, -45], window // 2)
        audible = np.tile([150, -150], window // 2)
        samples = (
            ("silent", np.zeros(window * 10), False),
            ("DC offset", np.full(window * 10, 4000), False),
            ("constant room noise", np.tile(noise, 10), False),
            ("isolated sound", np.concatenate((np.tile(noise, 8), quiet)), False),
            ("sustained quiet sound", np.concatenate((np.tile(noise, 4), np.tile(quiet, 6))), True),
            ("one quiet gap", np.concatenate((noise, np.tile(quiet, 3), noise, np.tile(quiet, 3))), True),
            ("two quiet gaps", np.concatenate((noise, np.tile(quiet, 3), np.tile(noise, 2), np.tile(quiet, 3))), False),
            ("quiet sound crosses a read boundary", np.concatenate((np.tile(noise, 62), np.tile(quiet, 6))), True),
            ("loud final partial window", audible[:window // 2], True),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mic.wav"
            for name, audio, expected in samples:
                with self.subTest(name=name):
                    write_mic_fixture(path, audio)
                    self.assertEqual(dictation_has_signal(path), expected)

    def test_multilingual_pause_preserves_every_sample(self):
        phrase = np.full(4 * SAMPLE_RATE, 0.15, dtype="<f4")
        gap = np.zeros(SAMPLE_RATE // 2, dtype="<f4")
        pcm = np.concatenate((phrase, gap, phrase)).tobytes()
        parts = list(split_at_pauses(pcm))
        self.assertEqual(len(parts), 2)
        self.assertEqual(b"".join(parts), pcm)
        self.assertEqual(len(parts[0]), len(pcm) // 2)

    def test_untimed_words_follow_the_current_voice_then_carry_across_silence(self):
        processor = SimpleNamespace(feature_extractor=SimpleNamespace(hop_length=160))
        session = SpeakerSession(None, processor, None)
        frames = np.zeros((300, 8))
        frames[:100, 0] = 0.9
        frames[100:200, 1] = 0.9
        session.blocks.append((0, frames))
        for second, expected in enumerate([1, 2, 2]):
            segments, words = session.label("Two words.", [], second * SAMPLE_RATE, 1, include_word_times=True)
            self.assertEqual([(s["text"], s["speaker"]) for s in segments], [("Two words.", expected)])
            self.assertEqual([w["speaker"] for w in words], [expected, expected])
            self.assertTrue(all(w["start"] is None and w["end"] is None for w in words))


class DecoderCacheTests(unittest.IsolatedAsyncioTestCase):

    async def test_saved_jobs_reuse_terms_for_all_blocks_and_meeting_tracks(self):
        from server import process_saved_audio

        pcm = np.full(1600, .1, dtype="<f4").tobytes()

        async def blocks(*args, **kwargs):
            yield pcm
            yield pcm

        terms = ("Project Atlas", "Café Noir")
        with TemporaryDirectory() as directory:
            file = Path(directory) / "mixed.wav"
            # The separate tracks path calculates the length from the committed WAV.
            file.write_bytes(bytes(44 + (25 * 48_000 + 960) * 2))
            library = Mock()
            model = SimpleNamespace(audio_priority=lambda **_: nullcontext())
            state = SimpleNamespace(library=library, diarizer=None, insights=SimpleNamespace(model=model, wake=asyncio.Event()),
                                    meeting_subscriber=None)
            options = {"languageHints": ["en"], "recording": True, "diarization": False}
            for tracks in (None, {"mic": file, "system": file}):
                with (
                    self.subTest(tracks=bool(tracks)),
                    patch("server.shared_vocabulary", return_value=terms) as preferences,
                    patch("server.audio_blocks", side_effect=blocks),
                    patch("server.read_window", return_value=({"mic": b"a", "system": b"b"}, None)),
                    patch("server.resample_capture", new_callable=AsyncMock, return_value=pcm),
                    patch("server.decode_pcm", new_callable=AsyncMock,
                          return_value={"text": "", "segments": [], "words": []}) as decode,
                ):
                    await process_saved_audio(state, "capture", file, options, 25.02, tracks=tracks)
                    preferences.assert_called_once_with()
                    self.assertEqual(decode.await_count, 4 if tracks else 2)
                    self.assertTrue(all(block.kwargs["vocabulary"] is terms for block in decode.await_args_list))
                    library.finish.assert_called_once_with("capture")
                    library.reset_mock()

    async def test_sdk_file_uses_shared_or_embedded_snapshot_across_blocks(self):
        from starlette.datastructures import QueryParams
        from server import sdk_transcribe

        async def blocks(*args, **kwargs):
            yield bytes(1600 * 4)
            yield bytes(1600 * 4)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = SimpleNamespace(sdk={"token": "fixture"}, status="ready", draining=False, sdk_pending=0,
                                    library=SimpleNamespace(root=root),
                                    insights=SimpleNamespace(model=SimpleNamespace(audio_priority=nullcontext)),
                                    sdk_upload_root=root)
            request = SimpleNamespace(app=SimpleNamespace(state=state),
                                      headers={"authorization": "Bearer fixture", "content-type": "audio/wav"},
                                      query_params=QueryParams("language=en"), is_disconnected=AsyncMock(return_value=False))
            with (
                patch("server.shared_vocabulary", return_value=("Shared name",)) as preferences,
                patch("server.save_upload", new_callable=AsyncMock),
                patch("server.audio_blocks", side_effect=blocks),
                patch("server.decode_pcm", new_callable=AsyncMock, return_value={"text": "Spoken"}) as decode,
            ):
                self.assertEqual(await sdk_transcribe(request), {"transcript": "Spoken Spoken", "durationMs": 200})
                self.assertEqual([block.kwargs["vocabulary"] for block in decode.await_args_list], [("Shared name",)] * 2)
                preferences.assert_called_once_with()
                state.sdk_embedded = True
                state.embedded_vocabulary = ("Startup name",)
                decode.reset_mock()
                await sdk_transcribe(request)
                self.assertEqual([block.kwargs["vocabulary"] for block in decode.await_args_list], [("Startup name",)] * 2)
                preferences.assert_called_once_with()
    @unittest.skipUnless(shutil.which("ffmpeg"), "Requires the local FFmpeg decoder")
    async def test_audio_windows_follow_quiet_boundaries_without_losing_lookahead(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "long.wav"
            samples = np.full(MAX_SAMPLES * 2 + 1, 8192, dtype="<i2")
            pause = MAX_SAMPLES - 2 * SAMPLE_RATE
            samples[pause:pause + SAMPLE_RATE // 10] = 0
            # The language split sees this earlier pause inside the first window.
            samples[5 * SAMPLE_RATE:5 * SAMPLE_RATE + SAMPLE_RATE // 2] = 0
            path.write_bytes(wav_piece(samples.tobytes()))
            blocks = [part async for part in audio_blocks(path, pause_aware=True)]
            self.assertEqual(len(blocks[0]) // 4, pause + SAMPLE_RATE // 20)
            self.assertTrue(all(0 < len(part) <= MAX_SAMPLES * 4 for part in blocks))
            expected = (samples.astype("<f4") / np.float32(32768)).tobytes()
            self.assertEqual(b"".join(blocks), expected)
            multilingual = [part async for part in audio_blocks(path, multilingual=True, pause_aware=True)]
            self.assertEqual(len(multilingual[0]) // 4, 5 * SAMPLE_RATE + SAMPLE_RATE // 4)
            self.assertGreater(len(multilingual), len(blocks))
            self.assertEqual(b"".join(multilingual), expected)
            constant = np.full(MAX_SAMPLES, 0.15, dtype="<f4")
            self.assertEqual(quiet_sample_boundary(constant), MAX_SAMPLES - SAMPLE_RATE // 20)
            for count in (MAX_SAMPLES, MAX_SAMPLES + 1):
                with self.subTest(samples=count):
                    path.write_bytes(wav_piece(samples[:count].tobytes()))
                    parts = [part async for part in audio_blocks(path, pause_aware=True)]
                    self.assertEqual(len(parts), 1 if count == MAX_SAMPLES else 2)
                    self.assertEqual(b"".join(parts), expected[:count * 4])

    @unittest.skipUnless(shutil.which("ffmpeg"), "Requires the local FFmpeg decoder")
    async def test_mixed_meeting_resumes_its_persisted_chunking_policy(self):
        from server import process_mixed_audio
        from session_store import SessionStore

        samples = np.full(MAX_SAMPLES + SAMPLE_RATE, 8192, dtype="<i2")
        pause = MAX_SAMPLES - 2 * SAMPLE_RATE
        samples[pause:pause + SAMPLE_RATE // 10] = 0
        for chunking, checkpoint in ((None, MAX_SAMPLES), (PAUSE_AWARE_CHUNKING, pause + SAMPLE_RATE // 20)):
            with self.subTest(chunking=chunking), TemporaryDirectory() as directory:
                root = Path(directory)
                audio = root / "input.wav"
                audio.write_bytes(wav_piece(samples.tobytes()))
                options = {"meeting": True, "recording": True, "diarization": False, "languageHints": ["en"]}
                if chunking:
                    options["audioChunking"] = chunking
                duration = len(samples) / SAMPLE_RATE
                store = SessionStore(root)
                key, source = store.create(audio, "Mixed meeting", "audio/wav", options, duration)
                store.append(key, {"text": "Before restart.", "segments": [], "words": []},
                             checkpoint / SAMPLE_RATE, next_frame=checkpoint * 3)
                reopened = SessionStore(root)
                saved = reopened.claim_meeting_finalization()
                self.assertEqual(saved["id"], key)
                self.assertEqual(saved["next_frame"], checkpoint * 3)
                state = SimpleNamespace(library=reopened, diarizer=None)
                with patch("server.decode_pcm", new_callable=AsyncMock,
                           return_value={"text": "After restart.", "segments": [], "words": []}) as decode:
                    actual = await process_mixed_audio(state, key, source, saved["options"], duration,
                                                       checkpoint=saved["next_frame"], progress_only=True,
                                                       emit=lambda **kwargs: None, vocabulary=())
                    decode.assert_awaited_once()
                self.assertEqual(decode.await_args.args[-1], checkpoint)
                self.assertEqual(actual, len(samples))
                self.assertEqual(reopened.get(key)["transcript"]["text"], "Before restart. After restart.")

    async def test_near_silent_native_take_never_calls_model(self):
        from starlette.datastructures import QueryParams
        from server import native_dictation

        pcm = bytes(1600 * 4)

        async def blocks(*args, **kwargs):
            yield pcm

        key = uuid4()
        with (
            TemporaryDirectory() as directory,
            patch("server.shared_vocabulary", return_value=("Shared name",)) as preferences,
            patch("server.audio_blocks", side_effect=blocks) as decode,
            patch("server.decode_pcm", new_callable=AsyncMock, return_value={"text": "One take", "inferenceNs": 12_000_000}) as transcribe,
        ):
            path = Path(directory) / str(key)
            write_mic_fixture(path, np.full(4800, 1000))
            path.with_suffix(".dictation").touch()
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(staging=path.parent, native_token="fixture", status="ready",
                    insights=SimpleNamespace(model=SimpleNamespace(audio_priority=nullcontext)))),
                headers={"x-jard-token": "fixture"}, query_params=QueryParams("language=en"),
            )
            self.assertEqual(await native_dictation(request, key), {"text": "", "inferenceMs": 0})
            preferences.assert_not_called()
            decode.assert_not_called()
            transcribe.assert_not_awaited()
            write_mic_fixture(path, np.tile([200, -200], 2400))
            self.assertEqual(await native_dictation(request, key), {"text": "One take", "inferenceMs": 12})
            transcribe.assert_awaited_once_with(request.app.state, pcm, ("en",),
                                               live=True, vocabulary=(), dictation=True, measure_inference=True,
                                               screen_nonspeech=True)
            decode.reset_mock()
            transcribe.reset_mock()
            write_mic_fixture(path, np.concatenate((np.zeros(2400), np.tile([200, -200], 1200))))
            self.assertEqual(await native_dictation(request, key, release_frame=2400), {"text": "", "inferenceMs": 0})
            decode.assert_not_called()
            transcribe.assert_not_awaited()
            write_mic_fixture(path, np.tile([200, -200], 2400))
            self.assertEqual(await native_dictation(request, key, release_frame=2400), {"text": "One take", "inferenceMs": 12})
            transcribe.assert_awaited_once_with(request.app.state, pcm, ("en",),
                                               live=True, vocabulary=(), dictation=True, measure_inference=True,
                                               screen_nonspeech=True, speech_prefix_samples=800)

    async def test_dictation_rewrite_deadline_includes_waiting_for_the_model(self):
        from fastapi import HTTPException
        from server import rewrite_dictation

        async def generate(*args, **kwargs):
            yield '{"text":"Edited 12 words."}'

        model = SimpleNamespace(lock=asyncio.Lock(), ready=AsyncMock(), fit_prompt=AsyncMock(), generate=generate)
        insights = SimpleNamespace(chat_count=0, model=model)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(native_token="fixture", insights=insights)),
            headers={"x-jard-token": "fixture"},
            json=AsyncMock(return_value={"text": "Raw 12 words.", "instruction": "Edit lightly.", "deadline": 1}),
        )
        async with model.lock:
            with self.assertRaisesRegex(HTTPException, "1-second deadline"):
                await asyncio.wait_for(rewrite_dictation(request), 3)
            model.ready.assert_not_awaited()
            self.assertEqual(insights.chat_count, 0)

        stream_closed = asyncio.Event()

        async def stalled(*args, **kwargs):
            try:
                yield '{"text":'
                await asyncio.Event().wait()
            finally:
                stream_closed.set()

        model.generate = stalled
        with self.assertRaisesRegex(HTTPException, "1-second deadline"):
            await asyncio.wait_for(rewrite_dictation(request), 3)
        self.assertTrue(stream_closed.is_set())
        self.assertFalse(model.lock.locked())
        self.assertEqual(insights.chat_count, 0)

        model.generate = generate
        self.assertEqual(await rewrite_dictation(request), {"text": "Edited 12 words."})
        self.assertEqual(model.ready.await_count, 2)
        self.assertEqual(insights.chat_count, 0)

    async def test_final_pass_retries_same_block_with_dynamic_cache_on_memory_error(self):
        from server import SpeechInference, decode_pcm

        model = Mock()
        model.transcribe.side_effect = [RuntimeError("MPS backend out of memory"), ("", None, [])]
        state = SimpleNamespace(model=model, inference=SpeechInference())
        pcm = np.zeros(SAMPLE_RATE, dtype="<f4").tobytes()
        await decode_pcm(state, pcm, ("en",))
        self.assertEqual(model.transcribe.call_args_list, [
            call(pcm, ("en",), timed=True, cache_implementation="static"),
            call(pcm, ("en",), timed=True, cache_implementation=None),
        ])
        model.release_device_cache.assert_called_once_with()

    async def test_dictation_passes_waiting_meeting_chunks_and_cancel_releases_priority(self):
        from server import SpeechInference

        gate = SpeechInference()
        entered = []

        async def decode(name, *, dictation=False):
            async with gate.turn(dictation=dictation):
                entered.append(name)

        async with gate.turn():
            meeting = asyncio.create_task(decode("meeting"))
            cancelled = asyncio.create_task(decode("cancelled", dictation=True))
            spoken = asyncio.create_task(decode("dictation", dictation=True))
            await asyncio.sleep(0)
            self.assertEqual(entered, [])
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled

        await asyncio.wait_for(asyncio.gather(meeting, spoken), 1)
        self.assertEqual(entered, ["dictation", "meeting"])

    async def test_meeting_recovery_job_arbitrates_restart_to_update(self):
        from fastapi import HTTPException
        from server import quiesce_update, start_job

        state = SimpleNamespace(jobs=set(), native_token="fixture", draining=False, submitting=set(), status="ready",
                                insights=SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0)),
                                leases=SimpleNamespace(streams=set(), deadlines={}), request_shutdown=Mock())
        request = SimpleNamespace(app=SimpleNamespace(state=state), headers={"x-jard-token": "fixture"})
        gate = asyncio.Event()
        job = start_job(state, gate.wait())
        with self.assertRaises(HTTPException) as busy:
            await quiesce_update(request, uuid4())
        self.assertEqual(busy.exception.status_code, 409)
        self.assertFalse(state.draining)
        gate.set()
        await job
        response = await quiesce_update(request, uuid4())
        self.assertEqual(response.status_code, 202)
        self.assertTrue(state.draining)
        state.request_shutdown.assert_not_called()
