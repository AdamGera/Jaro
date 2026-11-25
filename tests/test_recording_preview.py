import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from portable_capture import SAMPLE_RATE, private_wav, source_tracks
from server import (URL, portable_recording_preview, save_portable_recording, set_portable_recording_preview,
                    start_portable_recording)
from session_store import SessionStore


RECORDING = "/api/portable/recording"


class RecordingPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_microphone_session_can_enable_live_and_finalize_its_rolling_transcript(self):
        await self.check_recording("mic", meeting=False)

    async def test_dual_source_session_finalizes_its_rolling_transcript_and_keeps_both_tracks(self):
        await self.check_recording("both", meeting=False)

    async def test_meeting_keeps_its_rolling_transcript_and_both_tracks(self):
        await self.check_recording("both", meeting=True)

    async def check_recording(self, mode, *, meeting):
        @asynccontextmanager
        async def audio_priority():
            yield

        def recorder(key, path, input_mode):
            self.assertEqual(input_mode, mode)
            frames = 4 * SAMPLE_RATE
            worker = asyncio.Future()

            async def start():
                targets = {path: b"\x02\x02"}
                if input_mode == "both":
                    tracks = source_tracks(path)
                    targets.update({tracks["mic"]: b"\x01\x02", tracks["system"]: b"\x03\x02"})
                for target, sample in targets.items():
                    with private_wav(target) as (audio, _):
                        audio.writeframes(sample * frames)

            async def stop():
                worker.set_result(None)

            return SimpleNamespace(key=key, path=path, frames=frames, worker=worker, start=start, stop=stop)

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.post(RECORDING, status_code=201)(start_portable_recording)
            app.get(RECORDING + "/{key}/preview")(portable_recording_preview)
            app.put(RECORDING + "/{key}/preview")(set_portable_recording_preview)
            app.post(RECORDING + "/{key}/finish")(save_portable_recording)
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
            state.portable_recorder = state.portable_meeting_recorder = recorder
            state.jobs = set()
            state.diarizer = None
            state.insights = SimpleNamespace(model=SimpleNamespace(audio_priority=audio_priority))
            finish = asyncio.Event()
            provisional = {
                "text": "The provisional recording.",
                "words": [{"text": "The", "separator": " ", "start": 0.0, "end": 0.2},
                          {"text": "provisional", "separator": " ", "start": 0.2, "end": 0.8},
                          {"text": "recording.", "separator": "", "start": 0.8, "end": 1.3}],
                "segments": [],
            }

            async def finalize(_state, key, *_args, **_options):
                await finish.wait()
                state.library.append(key, {"text": "The final recording.", "words": [], "segments": []}, 4)
                state.library.finish(key)

            with (patch("recording_power.keep_awake_until"), patch("server.require_meetings"),
                  patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
                  patch("server.decode_pcm", new_callable=AsyncMock, return_value=provisional) as decode,
                  patch("server.process_saved_audio", side_effect=finalize) as process,
                  patch("server.audio_duration", return_value=4)):
                try:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL, headers={"Origin": URL}) as client:
                        started = await client.post(RECORDING, params={"mode": mode, "meeting": str(meeting), "language": "en"})
                        self.assertEqual(started.status_code, 201, started.text)
                        capture_id = started.json()["id"]
                        path = f"{RECORDING}/{capture_id}/preview"
                        if not meeting:
                            await asyncio.sleep(.4)
                            self.assertEqual((await client.get(path)).status_code, 204)
                            decode.assert_not_awaited()
                            self.assertEqual((await client.put(path, json={"live": True})).status_code, 204)
                        async with asyncio.timeout(3):
                            while (preview := await client.get(path)).status_code == 204:
                                await asyncio.sleep(.05)
                        self.assertEqual(preview.json()["text"], provisional["text"])
                        segment, = preview.json()["segments"]
                        self.assertEqual((segment["text"], segment["start"], segment["end"]),
                                         (provisional["text"], 0.0, 4.0))
                        self.assertEqual((await client.put(path, json={"live": False})).status_code, 204)
                        self.assertEqual(state.portable_recording.preview_enabled, meeting)
                        for _ in range(2):
                            accepted = await client.post(f"{RECORDING}/{capture_id}/finish", params={"name": "Recording", "live": "true"})
                            self.assertEqual(accepted.status_code, 202, accepted.text)
                            self.assertEqual(accepted.json(), {"id": capture_id})
                    during = state.library.get(capture_id)
                    self.assertEqual(during["meeting"], meeting)
                    self.assertEqual(during["transcript"]["transcriptSource"], "rolling")
                    self.assertEqual(during["transcript"]["text"], provisional["text"])
                    self.assertNotIn("speakerNames", during["transcript"])
                    mix, _ = state.library.media(capture_id)
                    self.assertEqual(mix.read_bytes()[44:46], b"\x02\x02")
                    if mode == "both":
                        for track, sample in (("mic", b"\x01\x02"), ("system", b"\x03\x02")):
                            original, _ = state.library.media(capture_id, track=track)
                            self.assertEqual(original.read_bytes()[44:46], sample)
                finally:
                    finish.set()
                    if capture := state.portable_recording:
                        await capture.stop()
                    await asyncio.gather(*state.jobs)
                saved = state.library.get(capture_id)
                self.assertEqual(saved["status"], "complete")
                self.assertEqual(saved["transcript"]["text"], "The final recording.")
                process.assert_awaited_once()
