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
            body_json = json.loads(request.content.decode()) if request.url.path == "/v1/streams" else None
            requests_seen.append((
                request.method,
                request.url.path,
                request.headers.get("x-jard-token"),
                dict(request.url.params),
                body_json,
            ))
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
                res = await client.transcribe_pcm(b"\x00\x00\x00\x00", languages=("he", "en"), vocabulary=("Jaro",))
                self.assertEqual(res["text"], "Hello from Rust")
                self.assertEqual(res["inferenceMs"], 42)

                stream = await client.open_stream(
                    sample_rate=48_000, sample_format="s16le", languages=("he",), partials=True,
                )
                await stream.feed(b"\x00\x00" * 160)
                seen = [event async for event in stream.events()]
                self.assertEqual(seen[0]["type"], "partial")
                self.assertEqual(seen[0]["stable"], "Hello")
                self.assertEqual(seen[0]["tentative"], " world")
                self.assertEqual(seen[1]["type"], "final")
                final = await stream.finish()
                self.assertEqual(final["text"], "Hello world")

        asyncio.run(run())
        self.assertTrue(all(token == "secret" for _, _, token, _, _ in requests_seen))
        transcribe_params = next(params for _, path, _, params, _ in requests_seen if path == "/v1/transcribe")
        self.assertEqual(transcribe_params["languages"], "he,en")
        self.assertIn("Hebrew, English", transcribe_params["prompt"])
        stream_body = next(body for _, path, _, _, body in requests_seen if path == "/v1/streams")
        self.assertEqual(stream_body["languages"], ["he"])
        self.assertIn("Hebrew", stream_body["prompt"])

    def test_schedule_prewarm_delegates_to_rust_speech_when_configured(self):
        async def run():
            rust = SimpleNamespace(prewarm=AsyncMock(return_value=True))
            state = SimpleNamespace(status="loading", draining=False, rust_speech=rust, rust_speech_checked=True)
            scheduled = server.schedule_prewarm(state)
            self.assertTrue(scheduled)
            await state.prewarm_task
            rust.prewarm.assert_awaited_once()

        asyncio.run(run())

    def test_native_speech_endpoint_reports_bundled_rust_helper_and_requires_token(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.get("/api/native/speech-endpoint")(server.native_speech_endpoint)
        app.state.native_token = "a" * 64
        app.state.rust_speech_checked = True
        app.state.rust_speech = rust_speech.RustSpeechClient("http://127.0.0.1:54321", token="tok123")

        client = TestClient(app)
        # Missing token -> rejected
        self.assertEqual(client.get("/api/native/speech-endpoint").status_code, 403)
        # Browser Origin -> rejected
        self.assertEqual(
            client.get(
                "/api/native/speech-endpoint",
                headers={"X-Jard-Token": "a" * 64, "Origin": "http://127.0.0.1:8765"},
            ).status_code,
            403,
        )
        # Valid native token -> returns bundled endpoint with no-store
        resp = client.get("/api/native/speech-endpoint", headers={"X-Jard-Token": "a" * 64})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        self.assertEqual(resp.json(), {"url": "http://127.0.0.1:54321", "token": "tok123"})

        # Inactive Rust speech -> nulls
        app.state.rust_speech = None
        resp2 = client.get("/api/native/speech-endpoint", headers={"X-Jard-Token": "a" * 64})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json(), {"url": None, "token": None})

    def test_mac_bridge_discovers_and_caches_bundled_speech_endpoint(self):
        import desktop.mac_bridge as mac_bridge

        async def run():
            with TemporaryDirectory() as temp:
                engine_http = httpx.AsyncClient(headers={"X-Jard-Token": "engine-secret"})
                bridge = mac_bridge.MacBridge(Path(temp), engine_http)
                api_mock = AsyncMock(return_value={"url": "http://127.0.0.1:55555", "token": "secret-tok"})
                try:
                    with patch.dict("os.environ", {"JARD_SPEECH_URL": ""}, clear=False), patch.object(
                        bridge, "api", api_mock
                    ):
                        client1 = await bridge._rust_speech_client()
                        client2 = await bridge._rust_speech_client()
                        self.assertIsNotNone(client1)
                        self.assertIsNotNone(client2)
                        self.assertEqual(client1.base_url, "http://127.0.0.1:55555")
                        self.assertEqual(client1.token, "secret-tok")
                        # Separate HTTP client ensures engine X-Jard-Token is never leaked to speech URLs.
                        self.assertIsNot(client1.http, engine_http)
                        self.assertNotIn("x-jard-token", {k.lower() for k in client1.http.headers})
                        # Second call reuses cache without hitting the engine again.
                        api_mock.assert_awaited_once_with("GET", "native/speech-endpoint")
                finally:
                    if bridge._speech_http is not None:
                        await bridge._speech_http.aclose()
                    await engine_http.aclose()

        asyncio.run(run())

    def test_tentative_meeting_preview_uses_rust_while_committed_turn_preserves_pytorch_diarization(self):
        from contextlib import asynccontextmanager
        import wave
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        @asynccontextmanager
        async def audio_priority():
            yield

        with TemporaryDirectory() as temp:
            root = Path(temp)
            stage_dir = root / "staging"
            stage_dir.mkdir()
            stage_id = "11111111-1111-4111-8111-111111111111"
            wav_path = stage_dir / stage_id
            with wave.open(str(wav_path), "wb") as stream:
                stream.setparams((1, 2, 48_000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x00\x00" * (48_000 * 2))

            app = FastAPI()
            app.post("/api/native/staging/{key}/preview")(server.preview_stage)
            app.state.status = "ready"
            app.state.native_token = "b" * 64
            app.state.staging = stage_dir
            app.state.live_speakers = {}
            app.state.rust_speech_checked = True
            rust_mock = SimpleNamespace(
                transcribe_pcm=AsyncMock(return_value={"text": "rust tentative", "language": "English"})
            )
            app.state.rust_speech = rust_mock
            app.state.insights = SimpleNamespace(model=SimpleNamespace(audio_priority=audio_priority))

            fake_speakers = object()
            pytorch_decode = AsyncMock(
                return_value={
                    "text": "committed pytorch turn",
                    "language": "English",
                    "segments": [{"speaker": 1, "start": 0.0, "end": 1.0, "text": "committed pytorch turn"}],
                    "words": [{"start": 0.1, "end": 0.4, "text": "committed"}],
                }
            )
            with (
                patch.object(server, "decode_pcm", pytorch_decode),
                patch.object(server, "live_speakers", AsyncMock(return_value=fake_speakers)),
                patch.object(server, "speech_vocabulary", AsyncMock(return_value=())),
            ):
                client = TestClient(app)
                # 1. Tentative single-language preview -> routes to Rust, skips PyTorch
                r_tent = client.post(
                    f"/api/native/staging/{stage_id}/preview",
                    params={"start": 0, "frames": 48_000, "tentative": "true", "meeting": "true", "language": "en"},
                    headers={"X-Jard-Token": "b" * 64},
                )
                self.assertEqual(r_tent.status_code, 200)
                self.assertEqual(r_tent.json()["text"], "rust tentative")
                self.assertTrue(r_tent.json()["tentative"])
                rust_mock.transcribe_pcm.assert_awaited_once()
                pytorch_decode.assert_not_awaited()

                # 2. Committed meeting turn (tentative=false) -> MUST stay on PyTorch with speakers + timed_output=True
                r_commit = client.post(
                    f"/api/native/staging/{stage_id}/preview",
                    params={"start": 0, "frames": 48_000, "meeting": "true", "diarization": "true", "language": "en"},
                    headers={"X-Jard-Token": "b" * 64},
                )
                self.assertEqual(r_commit.status_code, 200)
                body = r_commit.json()
                self.assertEqual(body["text"], "committed pytorch turn")
                self.assertEqual(body["segments"][0]["speaker"], 1)
                self.assertEqual(body["words"][0]["start"], 0.1)
                # Rust was NOT called again for the committed turn
                rust_mock.transcribe_pcm.assert_awaited_once()
                pytorch_decode.assert_awaited_once()
                call_args = pytorch_decode.await_args
                self.assertIs(call_args.args[3], fake_speakers)
                self.assertTrue(call_args.kwargs["timed_output"])


if __name__ == "__main__":
    unittest.main()
