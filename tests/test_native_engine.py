"""The Mac app and browser share one engine, one library and accepted jobs."""
import asyncio
from contextlib import aclosing
import json
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import HTTPException

from annotation_assets import staged_annotation_paths
from audio_file import audio_blocks, open_audio_pcm
from dictation_input import CleanupFailed, MIC_GAIN, boost_microphone, replace_mixed_microphone
from engine_lifecycle import ClientLeases, engine_proof, library_id, workspace_key
from managed_preferences import ANNOTATION_MANAGED, MEETINGS_MANAGED, annotate_managed, meetings_managed
import meeting_brief
from meeting_rolling import append_rolling, read_rolling
from native_recordings import recording_marker, source_tracks
from personal_notes import NoteEdit, save
from portable_workspace import BOOTSTRAP, WorkspaceAccess
from server import URL, app, connect_browser, process_mixed_audio, quiesce_update, renew_client
from session_store import RECOVERY_LIMIT, SHUTDOWN_INTERRUPTION, SessionStore


class NativeEngineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"RUMBLE_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)

        async def load(app):
            app.state.status = "ready"
            app.state.model = SimpleNamespace(transcribe=Mock(return_value=("Hello Jard", "en", [])))

        fake = patch("server.load_model", load)
        fake.start()
        self.addCleanup(fake.stop)
        # No model downloads, paid services or GPU are needed by this test.
        notes = patch("insights.Insights.preview_job", new_callable=AsyncMock, side_effect=RuntimeError("offline test"))
        notes.start()
        self.addCleanup(notes.stop)
        self.client = self.enterContext(TestClient(app, base_url=URL))
        self.secret = (self.root / ".native-token").read_text().strip()
        self.header = {"X-Jard-Token": self.secret}

    def new_stage(self):
        value = self.client.post("/api/native/staging", headers=self.header).raise_for_status().json()
        path = Path(value["path"])
        self.assertTrue(path.is_relative_to(self.root))
        with wave.open(str(path), "wb") as dest:
            dest.setparams((1, 2, 48_000, 0, "NONE", ""))
            dest.writeframes(bytes(2 * 48_000))
        return f"/api/native/staging/{value['id']}", value["id"]

    def test_system_dictation_voice_input_and_original_audio_fallback(self):
        stage = self.client.post("/api/native/staging?dictation=true", headers=self.header).raise_for_status().json()
        endpoint = f"/api/native/staging/{stage['id']}/dictate"
        original = struct.pack("<80f", *([0.01] * 79 + [0.9]))
        enhanced = struct.pack("<80f", *([0.02] * 80))

        async def blocks(*_args, **_kwargs):
            yield original

        cleanup = SimpleNamespace(clean=AsyncMock(return_value=enhanced))
        with (patch("server.audio_blocks", blocks),
              patch("server.dictation_has_signal", return_value=True) as signal,
              patch("server.stupase_cleanup", return_value=cleanup) as processor,
              patch("server.decode_pcm", new_callable=AsyncMock,
                    return_value={"text": "Quiet words", "inferenceNs": 1000, "speechQueueNs": 0}) as decode):
            result = self.client.post(endpoint, headers=self.header)
            self.assertEqual(result.raise_for_status().json()["text"], "Quiet words")
            self.assertEqual(decode.call_args.args[1], original)
            processor.assert_not_called()
            for noise, boost, expected in (
                (False, False, original), (True, False, enhanced),
                (False, True, boost_microphone(original)), (True, True, boost_microphone(enhanced)),
            ):
                with self.subTest(noise=noise, boost=boost):
                    processor.reset_mock()
                    result = self.client.post(endpoint, headers=self.header,
                                              params={"noise_cleanup": noise, "boost_microphone_input": boost})
                    self.assertEqual(result.raise_for_status().json()["text"], "Quiet words")
                    self.assertEqual(decode.call_args.args[1], expected)
                    self.assertEqual(processor.called, noise)
                    self.assertEqual(signal.call_args.args[-1], MIC_GAIN if boost else 1.0)
            boosted = struct.unpack("<80f", boost_microphone(original))
            self.assertLessEqual(max(boosted), 0.98 + 1e-6)
            self.assertAlmostEqual(boosted[0], 0.01 * MIC_GAIN * 0.98 / (0.9 * MIC_GAIN), places=6)
            decode.reset_mock()
            decode.side_effect = [{"text": text, "inferenceNs": 1000, "speechQueueNs": 0} for text in ("", "Quiet words")]
            enabled = {"noise_cleanup": True, "boost_microphone_input": True}
            self.assertEqual(self.client.post(endpoint, headers=self.header, params=enabled).json()["text"], "Quiet words")
            self.assertEqual([call.args[1] for call in decode.call_args_list], [boost_microphone(enhanced), boost_microphone(original)])
            cleanup.clean.side_effect = CleanupFailed()
            decode.side_effect = None
            decode.reset_mock()
            self.assertEqual(self.client.post(endpoint, headers=self.header, params=enabled).json()["text"], "Quiet words")
            self.assertEqual([call.args[1] for call in decode.call_args_list], [boost_microphone(original)])

    def test_dictation_keeps_each_cleaned_window_and_retries_missing_speech_once(self):
        stage = self.client.post("/api/native/staging?dictation=true", headers=self.header).raise_for_status().json()
        endpoint = f"/api/native/staging/{stage['id']}/dictate"
        original = [struct.pack("<80f", *([level] * 80)) for level in (.1, .2, .3, .4)]
        cleaned = [struct.pack("<80f", *([level] * 80)) for level in (.01, .02)]

        async def blocks(*_args, **_kwargs):
            for pcm in original:
                yield pcm

        cleanup = SimpleNamespace(clean=AsyncMock(side_effect=[*cleaned, CleanupFailed()]))
        decoded = [{"text": text, "inferenceNs": 1_000_000, "speechQueueNs": 3_000_000}
                   for text in ("First", "", "Second", "Third", "Fourth")]
        with (patch("server.audio_blocks", blocks),
              patch("server.dictation_has_signal", return_value=True),
              patch("server.stupase_cleanup", return_value=cleanup),
              patch("server.decode_pcm", new_callable=AsyncMock, side_effect=decoded) as decode):
            result = self.client.post(endpoint, headers=self.header,
                                      params={"noise_cleanup": True, "boost_microphone_input": True}).raise_for_status().json()
        self.assertEqual((result["text"], result["inferenceMs"], result["speechTiming"]["speechQueueMs"]),
                         ("First Second Third Fourth", 5, 15))
        self.assertGreaterEqual(result["speechTiming"]["voiceCleanupMs"], 0)
        self.assertEqual([call.args[0] for call in cleanup.clean.await_args_list], original[:3])
        self.assertEqual([call.args[1] for call in decode.await_args_list],
                         [boost_microphone(block) for block in (*cleaned, *original[1:])])

    def test_meeting_voice_input_reaches_both_models_and_preserves_source(self):
        source = self.root / "source.wav"
        path = source.with_name("mic.wav")
        with wave.open(str(path), "wb") as capture:
            capture.setparams((1, 2, 48_000, 0, "NONE", ""))
            capture.writeframes(struct.pack("<h", 840) * 240)
        original = path.read_bytes()
        async def decode_mic():
            return b"".join([block async for block in audio_blocks(path)])

        mic = asyncio.run(decode_mic())
        mixed = struct.pack("<80f", *([0.15] * 80))
        cleaned = struct.pack("<80f", *([0.03] * 80))
        boosted = replace_mixed_microphone(mixed, mic, boost_microphone(mic))
        cleanup = SimpleNamespace(clean=AsyncMock(return_value=cleaned))
        speakers = Mock()
        state = SimpleNamespace(diarizer=Mock(), library=Mock())
        state.diarizer.session.return_value = speakers

        async def blocks(*_args, **_kwargs):
            yield mixed

        def run(voice, expected, transcripts=("Meeting words",), *, meeting=True, mic_used=True):
            with (patch("server.audio_blocks", blocks),
                  patch("server.open_audio_pcm", wraps=open_audio_pcm) as open_mic,
                  patch("server.stupase_cleanup", return_value=cleanup),
                  patch("server.run_inference", new_callable=AsyncMock) as diarization,
                  patch("server.decode_pcm", AsyncMock(side_effect=[
                      {"text": text, "segments": []} for text in transcripts
                  ])) as recognize):
                asyncio.run(process_mixed_audio(
                    state, "fixture", source, {"languageHints": ["en"], "meeting": meeting, "recording": True,
                        "diarization": True, "meetingVoiceInput": voice}, 1,
                    checkpoint=0, progress_only=True, emit=lambda **_: None, vocabulary=(),
                ))
                self.assertEqual([call.args[1] for call in recognize.call_args_list], expected)
                diarization.assert_awaited_once_with(state, speakers.push, expected[0])
                if mic_used:
                    open_mic.assert_called_once_with(path)
                else:
                    open_mic.assert_not_called()
                self.assertEqual(state.library.append.call_args.args[1]["text"], "Meeting words")
                self.assertEqual(path.read_bytes(), original)

        run({}, [mixed], mic_used=False)
        run({"noiseCleanup": False, "boostMicrophone": False}, [mixed], mic_used=False)
        run({"boostMicrophone": True}, [mixed], meeting=False, mic_used=False)
        run({"boostMicrophone": True}, [boosted])
        run({"noiseCleanup": True}, [replace_mixed_microphone(mixed, mic, cleaned)])
        both = {"noiseCleanup": True, "boostMicrophone": True}
        improved = replace_mixed_microphone(mixed, mic, boost_microphone(cleaned))
        run(both, [improved])
        run(both, [improved, boosted], ("", "Meeting words"))
        cleanup.clean.side_effect = CleanupFailed()
        run(both, [boosted])

        with wave.open(str(path), "wb") as capture:
            capture.setparams((1, 2, 16_000, 0, "NONE", ""))
            capture.writeframes(struct.pack("<h", 840) * 80)
        original = path.read_bytes()
        mic = asyncio.run(decode_mic())
        run({"boostMicrophone": True}, [replace_mixed_microphone(mixed, mic, boost_microphone(mic))])

        # A 48 kHz take can end between output sample boundaries.
        with wave.open(str(path), "wb") as capture:
            capture.setparams((1, 2, 48_000, 0, "NONE", ""))
            capture.writeframes(struct.pack("<h", 840) * 242)
        original = path.read_bytes()
        mic = asyncio.run(decode_mic())
        mixed += struct.pack("<f", 0.15)
        self.assertEqual(len(mic), len(mixed))
        run({"boostMicrophone": True}, [replace_mixed_microphone(mixed, mic, boost_microphone(mic))])

        path.write_bytes(b"invalid audio")
        original = path.read_bytes()
        run({"boostMicrophone": True}, [mixed])

    @patch("server.stupase_cleanup")
    def test_meeting_voice_settings_are_pinned_in_the_meeting_stage(self, cleanup):
        stage = self.client.post("/api/native/staging", headers=self.header, params={
            "recording": "true", "meeting": "true",
        }).raise_for_status().json()
        self.assertNotIn("meetingVoiceInput", json.loads(recording_marker(Path(stage["path"])).read_text()))
        cleanup.assert_not_called()

        flags = {"noise_cleanup": "true", "boost_microphone_input": "true"}
        stage = self.client.post("/api/native/staging", headers=self.header, params={
            "recording": "true", "meeting": "true", **flags,
        }).raise_for_status().json()
        marker = recording_marker(Path(stage["path"]))
        self.assertEqual(json.loads(marker.read_text())["meetingVoiceInput"],
                         {"noiseCleanup": True, "boostMicrophone": True})
        stage = self.client.post("/api/native/staging", headers=self.header, params={
            "recording": "true", **flags,
        }).raise_for_status().json()
        self.assertEqual(recording_marker(Path(stage["path"])).read_bytes(), b"")
        cleanup.return_value.prepare.assert_called_once_with()

    def test_annotation_retention_requires_local_changes_and_keeps_a_valid_policy(self):
        endpoint = "/api/annotations/retention"
        default = {"mediaDays": 7, "recordDays": 30, "includeExisting": False}
        custom = {"mediaDays": 30, "recordDays": 90, "includeExisting": True}
        self.assertEqual(self.client.get(endpoint).json(), default)
        self.assertEqual(self.client.put(endpoint, headers={"Origin": "https://another.example"}, json=custom).status_code, 403)
        self.assertEqual(self.client.put(endpoint, json={**custom, "mediaDays": True}).status_code, 422)
        self.assertEqual(self.client.put(endpoint, json={**custom, "recordDays": 6}).status_code, 400)
        self.assertEqual(self.client.get(endpoint).json(), default)
        with patch("managed_preferences.annotate_managed", return_value=True):
            self.assertEqual(self.client.put(endpoint, json=custom).json()["code"], "managed")
        self.assertEqual(self.client.put(endpoint, json=custom).json(), custom)
        self.assertEqual(self.client.get(endpoint).json(), custom)

    def test_annotation_folder_requires_native_auth_and_tracks_a_disconnected_folder(self):
        endpoint = "/api/native/annotation-storage"
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "Captures"
            folder.mkdir()
            value = {"folder": str(folder)}
            self.assertEqual(self.client.get(endpoint).status_code, 403)
            self.assertEqual(self.client.put(endpoint, json=value).status_code, 403)
            self.assertEqual(self.client.put(endpoint, json={"folder": str(self.root)}, headers=self.header).status_code, 400)
            with patch("managed_preferences.annotate_managed", return_value=True):
                self.assertEqual(self.client.put(endpoint, json=value, headers=self.header).json()["code"], "managed")
            saved = self.client.put(endpoint, json=value, headers=self.header).raise_for_status().json()
            self.assertEqual(saved["folder"], str(folder.resolve()))
            self.assertTrue(saved["custom"] and saved["available"])
            folder.rename(folder.with_name("Detached"))
            self.assertEqual(self.client.get(endpoint, headers=self.header).json(), {**saved, "available": False})
            reset = self.client.put(endpoint, json={"folder": None}, headers=self.header).raise_for_status().json()
            self.assertEqual(reset["folder"], str(self.root / "audio"))
            self.assertTrue(reset["available"] and not reset["custom"])

    def test_engine_proves_library_and_native_operations_require_local_token(self):
        challenge = "01" * 16
        info = self.client.get("/api/status", params={"challenge": challenge}).json()
        self.assertEqual(info["proof"], engine_proof(self.secret, challenge))
        self.assertEqual(info["libraryId"], library_id(self.root.resolve()))
        self.assertEqual((self.root / ".native-token").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.client.post("/api/native/staging").status_code, 403)
        self.assertEqual(self.client.post("/api/native/staging", headers={**self.header, "Origin":"https://example.net"}).status_code, 403)
        ep, _ = self.new_stage()
        self.assertEqual(self.client.delete(ep, headers=self.header).status_code, 204)
        self.assertEqual(self.client.post(ep + "/submit", headers=self.header).status_code, 404)

    def test_workspace_reports_unavailable_after_its_bundle_moves(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>Jaro fixture</html>")
        with patch("server.DIST", dist):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Jaro fixture", response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            dist.rename(self.root / "Former dist")
            for path in ("/", "/index.html"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertIn("workspace is unavailable", response.text)
            self.assertEqual(self.client.get("/api/status").json()["status"], "ready")

    def test_shared_workspace_requires_authentication_and_keeps_native_credentials_separate(self):
        wrapped = WorkspaceAccess(app, origin=URL, port=int(URL.rsplit(":", 1)[1]), shared_state=app.state)
        client = TestClient(wrapped, base_url=URL)
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/sessions").status_code, 403)
        self.assertEqual(client.get("/").status_code, 403)
        self.assertEqual(client.get("/api/sessions", headers=self.header).status_code, 200)
        challenge = "a" * 32
        identity = client.get("/api/status", params={"challenge": challenge}).raise_for_status().json()
        self.assertEqual(identity["proof"], engine_proof(self.secret, challenge))
        self.assertIsNone(client.get("/api/status", params={"challenge": "jaro:workspace:v1"}).json()["proof"])

        shell = client.get(BOOTSTRAP).raise_for_status()
        self.assertEqual(shell.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", shell.headers["Content-Security-Policy"])
        key_header = {"X-Jaro-Workspace-Key": workspace_key(self.secret)}
        self.assertEqual(client.post(BOOTSTRAP, headers=key_header).status_code, 403)
        self.assertEqual(client.post(BOOTSTRAP, headers={**key_header, "Origin": "http://127.0.0.1:9"}).status_code, 403)
        invalid = client.post(BOOTSTRAP, headers={"Origin": URL, "X-Jaro-Workspace-Key": self.secret})
        self.assertEqual(invalid.status_code, 403)
        response = client.post(BOOTSTRAP, headers={**key_header, "Origin": URL})
        self.assertEqual(response.status_code, 204)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(client.get("/api/sessions").status_code, 200)
        self.assertEqual(client.get("/api/sessions", headers={"Host": "subdomain.example"}).status_code, 403)
        self.assertEqual(client.get("/api/sessions", headers={"Origin": "https://example.com"}).status_code, 403)
        self.assertEqual(client.post("/api/native/staging", headers={"Origin": URL}).status_code, 403)

    def test_live_preview_uses_written_range_and_submit_is_idempotent(self):
        ep, key = self.new_stage()
        self.assertEqual(self.client.post(ep + "/preview?start=48000&frames=240", headers=self.header).status_code, 409)
        response = self.client.post(ep + "/preview?start=0&frames=48000&language=en", headers=self.header)
        self.assertEqual(response.raise_for_status().json()["text"], "Hello Jard")
        self.assertEqual(len(app.state.model.transcribe.call_args.args[0]), 16_000 * 4)

        opts = {"recording": "true", "name": "Call", "language": "en"}
        session = self.client.post(ep+"/submit", params=opts, headers=self.header).raise_for_status().json()["id"]
        self.assertEqual(session, key)
        self.assertEqual(self.client.post(ep+"/submit", params=opts, headers=self.header).json(), {"id":key})
        self.client.portal.call(self.wait_for_jobs)
        saved = self.client.get(f"/api/sessions/{key}").raise_for_status().json()
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["transcript"]["text"], "Hello Jard")
        audio = self.client.get(saved["audioUrl"], headers={"Range": "bytes=0-3"})
        self.assertEqual((audio.status_code, audio.content), (206, b"RIFF"))
        self.assertEqual(audio.headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(self.client.delete(f"/api/sessions/{key}", headers={"Origin": URL}).status_code, 204)
        self.assertEqual(self.client.get(f"/api/sessions/{key}").status_code, 404)

    def test_ordinary_session_preview_persists_rolling_transcript_through_finalization(self):
        stage = self.client.post("/api/native/staging?recording=true", headers=self.header).raise_for_status().json()
        path = Path(stage["path"])
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 48_000, 0, "NONE", ""))
            wav.writeframes(struct.pack("<h", 4096) * 48_000)
        endpoint = f"/api/native/staging/{stage['id']}"
        preview = self.client.post(f"{endpoint}/preview?start=0&frames=48000&language=en", headers=self.header).raise_for_status().json()
        self.assertEqual(preview["text"], "Hello Jard")
        self.assertEqual(preview["meetingSpeakerMode"], "mixed")
        self.assertEqual(len(preview["segments"]), 1)
        self.assertIsNone(preview["segments"][0]["speaker"])
        append_rolling(path, 0, 48_000, preview)

        gate = Event()
        original_process = app.state.library.finish
        def hold_finish(key, *args, **kwargs):
            gate.wait(5)
            return original_process(key, *args, **kwargs)

        with patch.object(app.state.library, "finish", side_effect=hold_finish):
            submitted = self.client.post(f"{endpoint}/submit", params={
                "recording": "true", "recover": "true", "live": "true", "name": "Session", "language": "en",
            }, headers=self.header).raise_for_status().json()
            self.assertEqual(submitted["id"], stage["id"])
            during = self.client.get(f"/api/sessions/{stage['id']}").raise_for_status().json()
            self.assertEqual(during["status"], "processing")
            self.assertFalse(during["meeting"])
            self.assertEqual(during["transcript"]["text"], "Hello Jard")
            self.assertEqual(during["transcript"]["transcriptSource"], "rolling")
            self.assertNotIn("speakerNames", during["transcript"])
            gate.set()
            self.client.portal.call(self.wait_for_jobs)

        finished = self.client.get(f"/api/sessions/{stage['id']}").raise_for_status().json()
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["transcript"]["text"], "Hello Jard")
        self.assertNotIn("speakerNames", finished["transcript"])
        self.assertNotIn("transcriptSource", finished["transcript"])
        with app.state.library.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM meeting_rolling WHERE session_id=?", (stage["id"],)).fetchone()[0], 0)

    def test_live_recording_notes_auto_attach_and_survive_non_discard_abandon(self):
        ep, key = self.new_stage()
        doc = lambda text: {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
        self.assertEqual(
            self.client.put(f"/api/personal-note-drafts/{key}", json={"document": doc("First live thought"), "revision": 0},
                            headers={"Origin": URL}).raise_for_status().json(),
            {"revision": 1, "promoted": False},
        )
        self.assertEqual(
            self.client.put(f"/api/personal-note-drafts/{key}", json={"document": doc("Final live note"), "revision": 1},
                            headers={"Origin": URL}).raise_for_status().json(),
            {"revision": 1, "promoted": False},
        )
        submitted = self.client.post(
            ep + "/submit", params={"recording": "true", "name": "Call", "language": "en"}, headers=self.header
        ).raise_for_status().json()
        self.assertEqual(submitted["id"], key)
        self.client.portal.call(self.wait_for_jobs)
        saved = self.client.get(f"/api/sessions/{key}").raise_for_status().json()
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["personalNotes"], {"document": doc("Final live note"), "revision": 1})
        with app.state.library.connection() as db:
            self.assertEqual(
                db.execute("SELECT queued_personal FROM overviews WHERE id=?", (key,)).fetchone()[0],
                "Final live note",
            )

        # Non-discard abandon (e.g. 0-byte capture error) keeps the notes draft; explicit discard deletes it.
        ep2, key2 = self.new_stage()
        self.client.put(f"/api/personal-note-drafts/{key2}", json={"document": doc("Keep on mic error"), "revision": 0},
                        headers={"Origin": URL}).raise_for_status()
        self.assertEqual(self.client.delete(ep2, headers=self.header).status_code, 204)
        self.assertEqual(
            self.client.get(f"/api/personal-note-drafts/{key2}").raise_for_status().json()["document"],
            doc("Keep on mic error"),
        )
        ep3, key3 = self.new_stage()
        self.client.put(f"/api/personal-note-drafts/{key3}", json={"document": doc("Explicit discard"), "revision": 0},
                        headers={"Origin": URL}).raise_for_status()
        self.assertEqual(self.client.delete(ep3, params={"discard": "true"}, headers=self.header).status_code, 204)
        self.assertEqual(
            self.client.get(f"/api/personal-note-drafts/{key3}").raise_for_status().json()["revision"],
            0,
        )

    @staticmethod
    async def wait_for_jobs():
        await asyncio.wait_for(asyncio.gather(*app.state.jobs), timeout=10)

    def test_meeting_diarizes_the_mix_and_keeps_the_internal_mic_track(self):
        stage = self.client.post("/api/native/staging?recording=true&meeting=true", headers=self.header).raise_for_status().json()
        path = Path(stage["path"])
        tracks = source_tracks(path)
        for destination in (path, *tracks.values()):
            with wave.open(str(destination), "wb") as wav:
                wav.setparams((1, 2, 48_000, 0, "NONE", ""))
                wav.writeframes(struct.pack("<h", 8192 if destination != tracks["system"] else 0) * 48_000)
        original = {name: track.read_bytes() for name, track in tracks.items()}
        turns = [{"speaker": 1, "start": 0, "end": .4, "text": "Hello"},
                 {"speaker": 3, "start": .5, "end": .9, "text": "Jard"}]
        speaker_session = Mock()
        speaker_session.label.return_value = (turns, [])
        speaker_session.intervals.return_value = [{key: value for key, value in turn.items() if key != "text"} for turn in turns]
        diarizer = Mock()
        diarizer.session.return_value = speaker_session
        endpoint = f"/api/native/staging/{stage['id']}"
        preview = f"{endpoint}/preview?meeting=true&start=0&frames=48000"
        unlabeled = self.client.post(preview, headers=self.header).raise_for_status().json()
        self.assertIsNone(unlabeled["segments"][0]["speaker"])
        append_rolling(path, 0, 48_000, unlabeled)
        self.assertEqual(list(read_rolling(path, 48_000))[0][2], unlabeled)
        app.state.model.transcribe.reset_mock()
        with patch.object(app.state, "diarizer", diarizer):
            live = self.client.post(preview, headers=self.header).raise_for_status().json()
            self.assertEqual(live["segments"], turns)
            self.client.post(f"{endpoint}/submit?recording=true&recover=true", headers=self.header).raise_for_status()
            self.client.portal.call(self.wait_for_jobs)
        self.assertEqual([call.kwargs for call in diarizer.session.call_args_list], [{"live": True}, {}])
        self.assertEqual(app.state.model.transcribe.call_count, 2)
        for call in app.state.model.transcribe.call_args_list:
            self.assertAlmostEqual(struct.unpack_from("<f", call.args[0], 8000 * 4)[0], .25, places=3)
        session = self.client.get(f"/api/sessions/{stage['id']}").raise_for_status().json()
        self.assertEqual(session["status"], "complete")
        self.assertTrue(session["diarization"])
        self.assertEqual(session["transcript"]["segments"], turns)
        self.assertNotIn("speakerNames", session["transcript"])
        self.assertEqual({row["speaker"] for row in session["transcript"]["speakerActivity"]}, {1, 3})
        for name, url in session["recordingTracks"].items():
            self.assertEqual(self.client.get(url).raise_for_status().content, original[name])

    def test_workspace_reads_original_meeting_turns_behind_cached_segments(self):
        source = self.root / "meeting-turns.wav"
        source.write_bytes(b"audio fixture")
        library = app.state.library
        options = {"meeting": True, "sourceSpeakerTurns": True, "recording": True, "diarization": False}
        key, _ = library.create(source, "Meeting fixture", "audio/wav", options, 40)
        turns = [{"speaker": 1, "start": 0, "end": 2, "text": "Before the pause."},
                 {"speaker": 1, "start": 4, "end": 5, "text": "Same turn."},
                 {"speaker": 1, "start": 37, "end": 39, "text": "After the pause."}]
        for turn in turns:
            library.append(key, {"text": turn["text"], "words": [], "segments": [turn]}, turn["end"])
        url = f"/api/sessions/{key}/transcript-clipboard"
        waiting = self.client.get(url)
        self.assertEqual(waiting.status_code, 409)
        self.assertIn("finish transcribing", waiting.json()["detail"])
        library.finish(key)
        cached = library.get(key)["transcript"]
        self.assertEqual(len(cached["segments"]), 1)
        saved = self.client.get(f"/api/sessions/{key}").raise_for_status().json()
        self.assertEqual(saved["meetingTurns"], turns)
        self.assertEqual(saved["transcript"], cached)
        self.assertEqual(library.get(key)["transcript"], cached)
        with library.connection() as db:
            db.execute("DELETE FROM overviews WHERE id=?", (key,))
            db.execute("DELETE FROM notes WHERE id=?", (key,))
        markdown = "**00:00 · You**  \nBefore the pause. Same turn.\n\n**00:37 · You**  \nAfter the pause."
        copied = self.client.get(url).raise_for_status()
        self.assertEqual(copied.text, markdown)
        self.assertEqual(copied.headers["cache-control"], "no-store")
        with library.connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM notes WHERE id=?", (key,)).fetchone())
        library.remove_meeting_audio(key, trash=lambda path: path.rename(self.root / "audio-trash"))
        self.assertEqual(self.client.get(url).raise_for_status().text, markdown)
        self.assertEqual(self.client.get(f"/api/sessions/{uuid4()}/transcript-clipboard").status_code, 404)

        source.write_bytes(b"silent audio fixture")
        silent, _ = library.create(source, "Silent", "audio/wav", options, 1)
        library.finish(silent)
        error = self.client.get(f"/api/sessions/{silent}/transcript-clipboard")
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.json()["detail"], "This meeting has no transcript to copy.")

    def test_meeting_delete_reports_and_retries_a_committed_trash_archive(self):
        source = self.root / "trash-fixture.wav"
        source.write_bytes(b"audio fixture")
        library = app.state.library
        options = {"meeting": True, "recording": True, "diarization": False}
        key, _ = library.create(source, "Meeting fixture", "audio/wav", options, 1)
        library.finish(key)
        url = f"/api/sessions/{key}"
        self.assertEqual(self.client.delete(url, headers={"Origin": "https://example.net"}).status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertEqual(self.client.delete(url + "/audio").status_code, 403)
        with patch.object(library, "_deliver_meeting_archive", side_effect=OSError("Trash unavailable")):
            response = self.client.delete(url, headers={"Origin": URL})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"trashPending": True})
        self.assertEqual(self.client.get(url).status_code, 404)

        deliver = library._deliver_meeting_archive
        with patch.object(library, "_deliver_meeting_archive", side_effect=lambda key, _: deliver(key, lambda path: path.rename(self.root / "Fixture Trash"))):
            self.assertEqual(self.client.delete(url, headers={"Origin": URL}).status_code, 204)
        self.assertEqual(self.client.delete(url, headers={"Origin": URL}).status_code, 404)
        self.assertTrue((self.root / "Fixture Trash" / "Meeting.json").is_file())

    def test_native_meeting_trash_refuses_other_sessions_and_pending_work(self):
        library = app.state.library
        source = self.root / "native-trash.wav"
        source.write_bytes(b"ordinary recording")
        options = {"recording": True, "diarization": False}
        ordinary, _ = library.create(source, "Ordinary recording", "audio/wav", options, 1)
        library.finish(ordinary)
        source.write_bytes(b"meeting recording")
        meeting, _ = library.create(source, "Meeting fixture", "audio/wav", dict(options, meeting=True), 1)
        route = f"/api/native/meetings/{meeting}"
        headers = {**self.header, "Origin": URL}
        revision = "/api/sessions/revision"
        before = self.client.get(revision).raise_for_status().json()["revision"]
        self.assertEqual(self.client.get(revision, headers={"Origin": "https://example.net"}).status_code, 403)
        self.assertEqual(self.client.delete(route, headers={"Origin": URL}).status_code, 403)
        self.assertEqual(self.client.delete(route, headers={**headers, "Origin": "https://example.net"}).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/native/meetings/{ordinary}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/api/sessions/{ordinary}").status_code, 200)
        self.assertEqual(self.client.delete(route, headers=headers).status_code, 409)
        self.assertEqual(self.client.get(revision).json()["revision"], before)
        library.finish(meeting)
        deliver = library._deliver_meeting_archive
        with patch.object(library, "_deliver_meeting_archive", side_effect=OSError("Trash unavailable")):
            response = self.client.delete(route, headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"trashPending": True})
        self.assertNotEqual(self.client.get(revision).json()["revision"], before)
        with patch.object(library, "_deliver_meeting_archive",
                          side_effect=lambda key, _: deliver(key, lambda path: path.rename(self.root / "Native Trash"))):
            self.assertEqual(self.client.delete(route, headers=headers).status_code, 204)
        self.assertTrue((self.root / "Native Trash" / "Meeting.json").is_file())
        self.assertEqual(self.client.delete(route, headers=headers).status_code, 404)

    def test_meeting_transcript_edit_requires_opt_in_origin_and_current_revision(self):
        path = "/api/meeting-transcript-edits"
        self.assertEqual(self.client.get(path).json(), {"enabled": False})
        self.assertEqual(self.client.put(path, json={"enabled": True}).status_code, 403)
        origin = {"Origin": URL}
        self.assertEqual(self.client.put(path, headers=origin, json={"enabled": True}).json(), {"enabled": True})
        source = self.root / "edit.wav"
        source.write_bytes(b"original recording")
        store = app.state.library
        key, _ = store.create(source, "Meeting.wav", "audio/wav",
                              {"meeting": True, "recording": True, "diarization": True, "languageHints": ["en"]}, 2)
        turns = [{"text": "first", "speaker": 1, "start": 0, "end": 1},
                 {"text": "second", "speaker": 2, "start": 1, "end": 2}]
        store.append(key, {"text": "first second", "segments": turns, "words": []}, 2)
        store.finish(key)
        with store.connection() as db:
            db.execute("UPDATE overviews SET status='error' WHERE id=?", (key,))
            db.execute("UPDATE meeting_briefs SET status='error' WHERE id=?", (key,))
        url = f"/api/sessions/{key}"
        payload = {"revision": self.client.get(url).json()["transcriptEditRevision"], "lines": [1]}
        self.assertEqual(self.client.post(url + "/delete-transcript-lines", json=payload).status_code, 403)
        response = self.client.post(url + "/delete-transcript-lines", headers=origin, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["transcript"]["text"], "first")
        self.assertEqual(self.client.get(url).json()["meetingTurns"], turns[:1])
        self.assertEqual(self.client.put(path, headers=origin, json={"enabled": False}).json(), {"enabled": False})
        self.assertEqual(self.client.post(url + "/delete-transcript-lines", headers=origin, json=payload).status_code, 409)

    def test_settled_meeting_draft_can_be_copied_revealed_and_archived(self):
        library = app.state.library
        source, mic, system = (self.root / name for name in ("draft.wav", "draft-mic.wav", "draft-system.wav"))
        source.write_bytes(b"draft mix")
        mic.write_bytes(b"draft microphone")
        system.write_bytes(b"draft system audio")
        options = {"meeting": True, "sourceSpeakerTurns": True, "recording": True, "diarization": False,
                   "meetingEndedAt": "2000-01-01T00:00:00Z", "retentionApplies": 1}
        key, audio = library.create(source, "Draft meeting", "audio/wav", options, 12,
                                    tracks={"mic": mic, "system": system})
        text = "Keep the saved part."
        library.append(key, {"text": text, "words": [], "segments": [{"speaker": 1, "start": 4, "end": 8, "text": text}]}, 8)
        library.finish(key, SHUTDOWN_INTERRUPTION, closing=True)
        url = f"/api/sessions/{key}"
        copy = f"{url}/transcript-clipboard"
        reveal = f"/api/native/meetings/{key}/recording-files"
        self.assertEqual(self.client.get(copy).status_code, 409)
        self.assertEqual(self.client.get(reveal, headers=self.header).status_code, 404)
        self.assertEqual(self.client.delete(url, headers={"Origin": URL}).status_code, 409)

        with library.connection() as db:
            db.execute("UPDATE meeting_finalization SET attempts=? WHERE session_id=?", (RECOVERY_LIMIT, key))
            save(db, key, NoteEdit(revision=0, document={"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Draft notes to keep."}]}]}))
        self.assertIsNone(library.claim_recording_finalization())
        self.assertEqual(library.expire_meetings(), [])
        self.assertEqual([row["id"] for row in library.list("", meetings_only=True)], [key])
        self.assertEqual(self.client.get(copy).raise_for_status().text, f"**00:04 · You**  \n{text}")
        files = self.client.get(reveal, headers=self.header).raise_for_status().json()
        self.assertEqual(files, {"folder": str(audio.parent), "path": str(audio)})
        self.assertEqual(Path(files["path"]).read_bytes(), b"draft mix")

        archive = self.root / "Recovered meeting in Trash"
        deliver = library._deliver_meeting_archive
        with patch.object(library, "_deliver_meeting_archive", side_effect=lambda key, _: deliver(key, lambda path: path.rename(archive))):
            self.assertEqual(self.client.delete(url, headers={"Origin": URL}).status_code, 204)
        self.assertEqual(self.client.get(copy).status_code, 404)
        self.assertEqual(self.client.get(reveal, headers=self.header).status_code, 404)
        self.assertEqual(library.list("", meetings_only=True), [])
        self.assertEqual({path.name: path.read_bytes() for path in (archive / "Audio").iterdir()},
                         {"source.wav": b"draft mix", "mic.wav": b"draft microphone", "system.wav": b"draft system audio"})
        self.assertIn(f"**00:04 · You**  \n{text}", (archive / "Transcript.md").read_text())
        self.assertIn("Draft notes to keep.", (archive / "Notes.md").read_text())
        self.assertEqual(json.loads((archive / "Meeting.json").read_text())["tables"]["sessions"][0]["status"], "interrupted")

    def test_reveal_meeting_files_requires_a_finished_readable_meeting_and_native_authentication(self):
        library = app.state.library
        source = self.root / "recording-fixture.wav"
        source.write_bytes(b"audio fixture")
        options = {"recording": True, "diarization": False}
        key, _ = library.create(source, "Meeting fixture", "audio/wav", {**options, "meeting": True}, 1)
        url = f"/api/native/meetings/{key}/recording-files"
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url, headers={**self.header, "Origin": "https://example.net"}).status_code, 403)
        self.assertEqual(self.client.get(url, headers=self.header).status_code, 404)

        library.finish(key)
        path, _ = library.media(key)
        response = self.client.get(url, headers=self.header).raise_for_status()
        self.assertEqual(response.json(), {"folder": str(path.parent), "path": str(path)})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        elsewhere = self.root / "other-folder"
        path.parent.rename(elsewhere)
        path.parent.symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.client.get(url, headers=self.header).status_code, 404)
        path.parent.unlink()
        elsewhere.rename(path.parent)

        with library.connection() as db:
            db.execute("UPDATE sessions SET options=json_set(options,'$.audioRemovedAt', 'now') WHERE id=?", (key,))
        self.assertEqual(self.client.get(url, headers=self.header).status_code, 404)
        with library.connection() as db:
            db.execute("UPDATE sessions SET options=json_remove(options,'$.audioRemovedAt') WHERE id=?", (key,))
            db.execute("UPDATE sessions SET options=json_set(options,'$.retentionApplies',1,'$.meetingEndedAt','2020-01-01') WHERE id=?", (key,))
        self.assertEqual(self.client.get(url, headers=self.header).status_code, 404)

        source.write_bytes(b"ordinary recording fixture")
        ordinary, _ = library.create(source, "Ordinary recording", "audio/wav", options, 1)
        library.finish(ordinary)
        self.assertEqual(self.client.get(f"/api/native/meetings/{ordinary}/recording-files", headers=self.header).status_code, 404)

    def test_clients_can_detach_independently(self):
        a,b = (str(uuid4()) for _ in range(2))
        for key in (a,b):
            self.assertEqual(self.client.post(f"/api/clients/{key}").status_code, 204)
        self.assertEqual(self.client.delete(f"/api/clients/{a}").status_code, 204)
        self.assertIn(b, app.state.leases.deadlines)
        self.assertNotIn(a, app.state.leases.deadlines)
        self.assertEqual(self.client.post("/api/quit", headers={"Origin":"https://example.net","X-Jard-Action":"quit"}).status_code, 403)

    def test_local_rewrite_follows_spoken_enumeration_and_rejects_numeric_drift(self):
        response_text = "1. Order 1200 labels.\n2. Record −12.5%."
        messages = []

        async def generate(prompt, **options):
            messages.append(prompt)
            yield json.dumps({"text": response_text})

        model = SimpleNamespace(lock=asyncio.Lock(), ready=AsyncMock(), fit_prompt=AsyncMock(), generate=generate)
        route = "/api/native/dictation-rewrite"
        raw = "Point one, order 1200 labels. Point two, record −12.5%."
        payload = {"text": raw, "instruction": "Write a concise list.", "vocabulary": ["Jaro"]}
        with patch.object(app.state.insights, "model", model):
            response = self.client.post(route, json=payload, headers=self.header).raise_for_status()
            self.assertEqual(response.json()["text"], response_text)
            protocol, *examples, request = messages[-1]
            self.assertIn("Unless the writing style asks for another format", protocol["content"])
            self.assertIn("one per line", protocol["content"])
            self.assertIn("including signs and separators", protocol["content"])
            self.assertEqual([turn["role"] for turn in examples], ["user", "assistant", "user", "assistant"])
            self.assertEqual(json.loads(request["content"]), {
                "style": payload["instruction"], "vocabulary": ["Jaro"], "transcript": raw,
            })
            context = {**payload, "application": "Fixture Browser", "browserHost": "https://Example.com:443/private?q=secret"}
            self.client.post(route, json=context, headers=self.header).raise_for_status()
            request = json.loads(messages[-1][-1]["content"])
            self.assertEqual(request["destination"], {"application": "Fixture Browser", "browserHost": "example.com"})
            self.assertNotIn("private", messages[-1][-1]["content"])
            self.assertNotIn("secret", messages[-1][-1]["content"])
            response_text = "- Order 1200 labels.\n- Record −12.5%."
            payload["instruction"] = "Use bullets instead of numbered markers."
            response = self.client.post(route, json=payload, headers=self.header).raise_for_status()
            self.assertEqual(response.json()["text"], response_text)
            response_text = response_text.replace("1200", "1,200")
            refused = self.client.post(route, json=payload, headers=self.header)
            self.assertEqual(refused.status_code, 422)
            self.assertIn("The original wording was kept", refused.json()["detail"])
            self.assertEqual(app.state.insights.chat_count, 0)

    def test_managed_annotation_blocks_admission_but_keeps_saved_assets_readable(self):
        key = str(uuid4())
        video = self.root / "example.mp4"
        video.write_bytes(b"saved video")
        staged_annotation_paths(video)["manifest"].write_text("{}")
        options = {"recording": False, "diarization": False, "languageHints": []}
        app.state.library.create(video, "Saved annotation.mp4", "video/mp4", options, 1, key=key,
                                 annotation={"version": 1, "source": "display", "frames": [], "strokeCount": 0})
        app.state.library.finish(key)
        capture = str(uuid4())
        endpoint = "/api/native/staging"
        params = {"annotation_id": capture}

        annotate_managed.cache_clear()
        self.addCleanup(annotate_managed.cache_clear)
        with patch("managed_preferences.forced_boolean", side_effect=lambda key: key == "DisableAnnotate"):
            refused = {"detail": ANNOTATION_MANAGED, "code": "managed"}
            self.assertNotIn("code", self.client.post(endpoint, params=params).json())
            denied = self.client.post(endpoint, headers=self.header, params=params)
            self.assertEqual((denied.status_code, denied.json()), (403, refused))
            self.assertFalse((app.state.staging / capture).exists())

            stage, draft = self.new_stage()
            denied = self.client.post(stage + "/submit", headers=self.header,
                                      params={"annotation": "true", "mime": "video/mp4"})
            self.assertEqual((denied.status_code, denied.json()), (403, refused))
            self.assertTrue((app.state.staging / draft).exists())
            self.assertEqual(self.client.delete(stage, headers=self.header).status_code, 204)
            saved = self.client.get(f"/api/sessions/{key}").raise_for_status().json()
            self.assertEqual(saved["annotation"]["source"], "display")
            self.assertEqual(self.client.get(saved["audioUrl"]).content, b"saved video")
            manifest = self.client.get(saved["annotation"]["manifestUrl"])
            self.assertEqual(manifest.json(), {})
            self.assertEqual(manifest.headers["Cross-Origin-Resource-Policy"], "same-origin")

        annotate_managed.cache_clear()
        allowed = self.client.post(endpoint, headers=self.header, params=params)
        self.assertEqual((allowed.status_code, allowed.json()["id"]), (201, capture))


class ManagedMeetingsEngineTests(unittest.TestCase):
    def test_managed_launch_defers_capture_recovery_and_writing(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"RUMBLE_DATA_DIR": folder}))
        forced = self.enterContext(patch("managed_preferences.forced_boolean",
                                         side_effect=lambda key: key == "DisableMeetings"))
        meetings_managed.cache_clear()
        self.addCleanup(meetings_managed.cache_clear)

        async def ready(app):
            app.state.status = "ready"

        self.enterContext(patch("server.load_model", ready))
        store = SessionStore(folder)
        options = {"meeting": True, "recording": True, "diarization": False, "languageHints": []}
        saved, interrupted = str(uuid4()), str(uuid4())
        passage = {"text": "The next planning meeting is Friday.", "language": "English", "words": [], "segments": []}
        for key in (saved, interrupted):
            wave_path = store.root / f"{key}.wav"
            wave_path.write_bytes(b"fixture audio")
            store.create(wave_path, "Planning", "audio/wav", options, 1, key=key)
            store.append(key, passage, .5, next_frame=24000)
        store.finish(saved)
        store.finish(interrupted, SHUTDOWN_INTERRUPTION, closing=True)
        staged = store.root / "staging" / str(uuid4())
        staged.parent.mkdir()
        staged.write_bytes(b"retained draft")
        recording_marker(staged).write_text(json.dumps({"meetingProvider": "Fixture"}))
        with store.connection() as db:
            meeting_brief.settle(db, saved, 1, {"title": "Planning notes", "summary": "Meet on Friday."})
            meeting_brief.enqueue(db, saved, automatic=True)
            overview = dict(language="en", title="Previous overview", summary="Planning is on Friday.", topics=[])
            db.execute("UPDATE overviews SET body=? WHERE id=?", (json.dumps(overview), saved))
            db.execute("UPDATE notes SET displayed=base WHERE id=?", (saved,))
            checkpoint = db.execute("SELECT next_frame,attempts FROM meeting_finalization WHERE session_id=?",
                                    (interrupted,)).fetchone()
            self.assertIsNotNone(checkpoint)
            retry_budget = tuple(checkpoint)

        managed_writing = self.enterContext(patch("insights.Insights.write_meeting_brief", new_callable=AsyncMock))
        managed_overview = self.enterContext(patch("insights.Insights.preview_job", new_callable=AsyncMock))
        with TestClient(app, base_url=URL) as client:
            header = {"X-Jard-Token": (store.root / ".native-token").read_text().strip()}
            refused = {"detail": MEETINGS_MANAGED, "code": "managed"}
            stage = f"/api/native/staging/{staged.name}"
            session = f"/api/sessions/{saved}"
            for target, parameters in [
                ("/api/native/staging", {"recording": True, "meeting": True}),
                (stage + "/submit", {"recording": True, "recover": True}),
                (stage + "/preview", {"start": 0, "frames": 256}),
                (stage + "/meeting-title", {"through": 256}),
                (session + "/meeting-brief", {}),
            ]:
                with self.subTest(target=target):
                    response = client.post(target, headers=header, params=parameters)
                    self.assertEqual((response.status_code, response.json()), (403, refused))
            overview_request = client.post(session + "/overview", json={"language": "auto", "regenerate": True})
            self.assertEqual((overview_request.status_code, overview_request.json()), (403, refused))
            self.assertEqual(client.get("/api/meetings/automatic-notes").json(), {"enabled": False, "managed": True})
            change = client.put("/api/meetings/automatic-notes", headers={"Origin": URL}, json={"enabled": False})
            self.assertEqual((change.status_code, change.json()), (403, refused))

            detail = client.get(session).raise_for_status().json()
            self.assertTrue(detail["meetingsManaged"])
            self.assertFalse(app.state.startup_recovery)
            self.assertEqual(detail["transcript"]["text"], passage["text"])
            self.assertEqual(detail["overview"]["version"]["summary"], overview["summary"])
            self.assertEqual(client.get(session + "/meeting-brief").json()["summary"], "Meet on Friday.")
            self.assertIn("Meet on Friday.", client.get(session + "/export?kind=md").raise_for_status().text)
            self.assertEqual(staged.read_bytes(), b"retained draft")
            self.assertIsNone(app.state.insights.next_job())
            managed_writing.assert_not_awaited()
            managed_overview.assert_not_awaited()
            with store.connection() as db:
                self.assertEqual(tuple(db.execute("SELECT next_frame,attempts FROM meeting_finalization WHERE session_id=?",
                                                  (interrupted,)).fetchone()), retry_budget)
                self.assertEqual(meeting_brief.read(db, saved)["status"], "queued")
                self.assertEqual(meeting_brief.automatic_policy(db), (True, 0))

        forced.side_effect = None
        forced.return_value = False
        meetings_managed.cache_clear()
        resumed = Event()
        claims = []

        async def observe_recovery(state, stranded):
            claims.append(state.library.claim_recording_finalization())
            resumed.set()
            return False

        with (patch("insights.Insights.run", new_callable=AsyncMock),
              patch("server.recover_pending_recording", side_effect=observe_recovery),
              TestClient(app, base_url=URL) as client):
            self.assertTrue(app.state.startup_recovery)
            self.assertEqual(client.get("/api/meetings/automatic-notes").json(), {"enabled": True})
            self.assertEqual(app.state.insights.next_job()[0], saved)
            header = {"X-Jard-Token": (store.root / ".native-token").read_text().strip()}
            self.assertEqual(client.post("/api/native/staging", params={"recording": True, "meeting": True},
                                         headers=header).status_code, 201)
            self.assertTrue(resumed.wait(5), "The engine did not resume its interrupted meeting.")
            self.assertEqual((claims[0]["id"], claims[0]["next_frame"]), (interrupted, retry_budget[0]))


class LeaseTests(unittest.TestCase):
    @patch("engine_lifecycle.time.monotonic")
    def test_crashed_client_expires_and_accepted_work_finishes(self, clock):
        clock.return_value = 0
        leases = ClientLeases()
        leases.renew("desktop")
        clock.return_value = 60
        self.assertFalse(leases.idle(False))
        clock.return_value = 180
        self.assertFalse(leases.idle(True))
        self.assertFalse(leases.deadlines)
        clock.return_value = 211
        self.assertTrue(leases.idle(False))

    @patch("engine_lifecycle.time.monotonic")
    def test_browser_disconnect_starts_the_idle_grace_without_a_heartbeat(self, clock):
        clock.return_value = 0
        leases = ClientLeases()
        with leases.browser_connection():
            clock.return_value = 180
            self.assertFalse(leases.idle(False))
            self.assertFalse(leases.deadlines)
            clock.return_value = 200
        clock.return_value = 229
        self.assertFalse(leases.idle(False))
        clock.return_value = 230
        self.assertTrue(leases.idle(False))


class BrowserRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_ownership_and_late_connections_during_restart(self):
        leases = ClientLeases()
        desktop, tab = uuid4(), uuid4()
        leases.renew(str(desktop))
        insights = SimpleNamespace(active_job=None, chat_count=0, model=SimpleNamespace(audio_users=0))
        state = SimpleNamespace(native_token="fixture", leases=leases, draining=False, jobs=set(),
                                submitting=set(), insights=insights, status="ready", sdk_pending=0,
                                request_shutdown=Mock())
        request = SimpleNamespace(headers={"x-jard-token": "fixture"}, app=SimpleNamespace(state=state))

        async def start_stream():
            return (await connect_browser(tab, request)).body_iterator

        first, second, delayed = [await self.enterAsyncContext(aclosing(await start_stream())) for _ in range(3)]

        self.assertIn("Jaro connected", await anext(first))
        self.assertIn("Jaro connected", await anext(second))
        self.assertEqual(set(leases.deadlines), {str(desktop)})
        with self.assertRaisesRegex(HTTPException, "Close other"):
            await quiesce_update(request, desktop)

        await first.aclose()
        self.assertEqual(len(leases.streams), 1)
        with self.assertRaisesRegex(HTTPException, "Close other"):
            await quiesce_update(request, desktop)
        await second.aclose()

        # A native heartbeat remains independent even if it uses the same ID.
        self.assertEqual((await renew_client(tab, request)).status_code, 204)
        with self.assertRaisesRegex(HTTPException, "Close other"):
            await quiesce_update(request, desktop)
        leases.detach(str(tab))
        response = await quiesce_update(request, desktop)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(state.draining)

        for stream in (delayed, await start_stream()):
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        self.assertFalse(leases.streams)
        with self.assertRaises(HTTPException) as refused:
            await renew_client(tab, request)
        self.assertEqual(refused.exception.status_code, 409)
        self.assertEqual(set(leases.deadlines), {str(desktop)})


class RecoveryTests(unittest.TestCase):
    def test_active_recording_is_excluded_until_its_owner_releases_it(self):
        import fcntl
        from native_recordings import list_drafts, recording_marker

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / str(uuid4())
            path.write_bytes(bytes(46))
            with recording_marker(path).open("wb") as marker:
                fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(list_drafts(root), [])
            self.assertEqual([r["id"] for r in list_drafts(root)], [path.name])

    def test_draft_survives_restart_and_final_partial_frame_is_repaired(self):
        from native_recordings import list_drafts, recover_wav, recording_marker
        from session_store import open_library
        import struct

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with open_library(root):
                staging = root / "staging"
                staging.mkdir()
                path = staging / str(uuid4())
                with wave.open(str(path), "wb") as out:
                    out.setparams((1, 2, 48000, 0, "NONE", ""))
                    out.writeframes(bytes(96000))
                recording_marker(path).touch()
            with path.open("ab") as out:
                out.write(bytes(1025))  # a crash before checkpointing the header
            with open_library(root):
                self.assertEqual(list_drafts(staging)[0]["id"], path.name)
                recover_wav(path)
                header = path.read_bytes()[:44]
                self.assertEqual(struct.unpack_from("<I", header, 40)[0], 96000 + 1024)
                self.assertEqual(path.stat().st_size, 44 + 96000 + 1024)
