import asyncio
import hashlib
import json
import math
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import audio_file
import rust_speech
import server


class RustSpeechTests(unittest.TestCase):
    def test_rumble_download_resumes_and_reuses_verified_model(self):
        data = b"pinned model"

        async def resume(*args):
            self.assertIn("--continue-at", args)
            partial = args[args.index("--output") + 1]
            with partial.open("ab") as stream:
                stream.write(data[stream.tell():])
            return SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

        with TemporaryDirectory() as directory, patch.multiple(
            rust_speech, RUMBLE_GGUF_BYTES=len(data), RUMBLE_GGUF_SHA256=hashlib.sha256(data).hexdigest(),
        ), patch.object(asyncio, "create_subprocess_exec", side_effect=resume) as curl:
            model = Path(directory) / rust_speech.RUMBLE_GGUF_NAME
            partial = model.with_suffix(".partial")
            partial.write_bytes(data[:4])
            self.assertEqual(asyncio.run(rust_speech.download_rumble_gguf(model.parent)), model)
            self.assertEqual(model.read_bytes(), data)
            self.assertFalse(partial.exists())
            self.assertEqual(asyncio.run(rust_speech.download_rumble_gguf(model.parent)), model)
            curl.assert_awaited_once()

    def test_rumble_download_discards_invalid_weights(self):
        async def download(*args):
            args[args.index("--output") + 1].write_bytes(b"bad")
            return SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

        with TemporaryDirectory() as directory, patch.multiple(
            rust_speech, RUMBLE_GGUF_BYTES=3, RUMBLE_GGUF_SHA256=hashlib.sha256(b"abc").hexdigest(),
        ), patch.object(asyncio, "create_subprocess_exec", side_effect=download):
            with self.assertRaisesRegex(ValueError, "failed verification"):
                asyncio.run(rust_speech.download_rumble_gguf(Path(directory)))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_decimate_pcm16_48k_to_16k_f32_preserves_length_and_dc(self):
        # 0.5 s of 48 kHz mono s16le constant value ~0.5
        frames = 24_000
        raw = struct.pack("<h", 16384) * frames
        out = audio_file.decimate_pcm16_to_16k_f32(raw, 48_000)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(len(out), 8_000 * 4)
        samples = struct.unpack(f"<{len(out) // 4}f", out)
        self.assertTrue(all(math.isclose(v, 0.5, abs_tol=1e-3) for v in samples))

    def test_decimate_pcm16_rejects_unsupported_rate_or_odd_bytes(self):
        self.assertIsNone(audio_file.decimate_pcm16_to_16k_f32(b"\x00\x00", 44_100))
        self.assertIsNone(audio_file.decimate_pcm16_to_16k_f32(b"\x00", 48_000))
        self.assertIsNone(audio_file.decimate_pcm16_to_16k_f32(b"", 16_000))

    def test_rust_speech_client_stream_and_transcribe_over_mock_transport(self):
        requests_seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append((request.method, request.url.path, request.headers.get("x-jard-token")))
            if request.url.path == "/v1/prewarm":
                return httpx.Response(202, json={"warming": True})
            if request.url.path == "/v1/transcribe":
                return httpx.Response(200, json={"text": "Hello from Rust", "inferenceMs": 42, "durationMs": 1200, "source": "offline"})
            if request.url.path == "/v1/streams":
                return httpx.Response(201, json={"id": 7, "ownerToken": "jaro_tok", "partials": True, "nativeStreaming": True, "leaseMs": 15000})
            if request.url.path == "/v1/streams/7/audio":
                return httpx.Response(204)
            if request.url.path == "/v1/streams/7/events":
                body = (
                    'data: {"type":"partial","stable":"Hello","tentative":" world"}\n\n'
                    'data: {"type":"final","text":"Hello world","inferenceMs":38,"durationMs":1000,"source":"stream"}\n\n'
                )
                return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
            if request.url.path == "/v1/streams/7/finish":
                return httpx.Response(200, json={"text": "Hello world", "inferenceMs": 38, "durationMs": 1000, "source": "stream"})
            return httpx.Response(404)

        async def run():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http:
                client = rust_speech.RustSpeechClient("http://127.0.0.1:8766", token="secret", http=http)
                self.assertTrue(await client.prewarm())
                res = await client.transcribe_pcm(b"\x00\x00\x00\x00", languages=("en",), vocabulary=("Jaro",))
                self.assertEqual(res["text"], "Hello from Rust")
                self.assertEqual(res["inferenceMs"], 42)

                stream = await client.open_stream(sample_rate=48_000, sample_format="s16le", partials=True)
                await stream.feed(b"\x00\x00" * 160)
                seen = [event async for event in stream.events()]
                self.assertEqual(seen[0]["type"], "partial")
                self.assertEqual(seen[0]["stable"], "Hello")
                self.assertEqual(seen[0]["tentative"], " world")
                self.assertEqual(seen[1]["type"], "final")
                final = await stream.finish()
                self.assertEqual(final["text"], "Hello world")

        asyncio.run(run())
        self.assertTrue(all(token == "secret" for _, _, token in requests_seen))

    def test_schedule_prewarm_delegates_to_rust_speech_when_configured(self):
        async def run():
            rust = SimpleNamespace(prewarm=AsyncMock(return_value=True))
            state = SimpleNamespace(status="loading", draining=False, rust_speech=rust, rust_speech_checked=True)
            scheduled = server.schedule_prewarm(state)
            self.assertTrue(scheduled)
            await state.prewarm_task
            rust.prewarm.assert_awaited_once()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
