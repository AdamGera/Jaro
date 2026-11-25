import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from portable_capture import SAMPLE_RATE, private_wav
from server import (URL, begin_portable_recording, finish_portable_capture, portable_recording_preview,
                    set_portable_recording_preview)
from session_store import SessionStore


class RecordingPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_microphone_session_can_enable_live_and_finalize_its_rolling_transcript(self):
        @asynccontextmanager
        async def audio_priority():
            yield

        def recorder(key, path, mode):
            frames = 4 * SAMPLE_RATE
            worker = asyncio.Future()

            async def start():
                with private_wav(path) as (audio, _):
                    audio.writeframes(b"\x01\x02" * frames)

            async def stop():
                worker.set_result(None)

            return SimpleNamespace(key=key, path=path, frames=frames, worker=worker, start=start, stop=stop)

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.get("/recording/{key}/preview")(portable_recording_preview)
            app.put("/recording/{key}/preview")(set_portable_recording_preview)
            state = app.state
            state.library = SessionStore(Path(directory))
            state.portable_workspace = True
            state.draining = False
            state.status = "ready"
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.screen_draft_dir = None
            state.portable_recording_dir = Path(directory) / "microphone-drafts"
            state.portable_recording_dir.mkdir()
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder = recorder
            state.jobs = set()
            state.diarizer = None
            state.insights = SimpleNamespace(model=SimpleNamespace(audio_priority=audio_priority))
            finish = asyncio.Event()
            provisional = {"text": "The provisional recording.", "words": [], "segments": []}

            async def finalize(_state, key, *_args, **_options):
                await finish.wait()
                state.library.append(key, {"text": "The final recording.", "words": [], "segments": []}, 4)
                state.library.finish(key)

            with (patch("recording_power.keep_awake_until"), patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
                  patch("server.decode_pcm", new_callable=AsyncMock, return_value=provisional) as decode,
                  patch("server.process_saved_audio", side_effect=finalize), patch("server.audio_duration", return_value=4)):
                capture_id = (await begin_portable_recording(state, "mic", False, ("en",)))["id"]
                try:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Origin": URL}) as client:
                        path = f"/recording/{capture_id}/preview"
                        await asyncio.sleep(.4)
                        self.assertEqual((await client.get(path)).status_code, 204)
                        decode.assert_not_awaited()
                        self.assertEqual((await client.put(path, json={"live": True})).status_code, 204)
                        async with asyncio.timeout(3):
                            while (preview := await client.get(path)).status_code == 204:
                                await asyncio.sleep(.05)
                        self.assertEqual(preview.json()["text"], provisional["text"])
                        self.assertEqual((await client.put(path, json={"live": False})).status_code, 204)
                    await finish_portable_capture(state, capture_id, ("en",), name="Microphone session", live=True)
                    during = state.library.get(str(capture_id))
                    self.assertFalse(during["meeting"])
                    self.assertEqual(during["transcript"]["transcriptSource"], "rolling")
                    self.assertEqual(during["transcript"]["text"], provisional["text"])
                    self.assertNotIn("speakerNames", during["transcript"])
                finally:
                    finish.set()
                    if capture := state.portable_recording:
                        await capture.stop()
                    await asyncio.gather(*state.jobs)
                self.assertEqual(state.library.get(str(capture_id))["transcript"]["text"], "The final recording.")
