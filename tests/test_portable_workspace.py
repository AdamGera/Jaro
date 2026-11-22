import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
import wave
from uuid import uuid4

import httpx
from fastapi import FastAPI

from portable_workspace import BOOTSTRAP, configure_workspace
from linux_capture import default_computer_audio, default_microphone
from portable_capture import CaptureError, draft_frames, draft_tracks, private_wav, split_audio
from native_recordings import source_tracks
from windows_capture import WindowsRecording


class PortableWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_linux_capture_requires_a_microphone_instead_of_a_playback_monitor(self):
        sources = '[{"name":"mic","properties":{}},{"name":"speaker.monitor","properties":{"device.class":"monitor"}}]'
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["mic\n", sources])):
            self.assertEqual(await default_microphone(), "mic")
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["speaker.monitor\n", sources])):
            with self.assertRaisesRegex(CaptureError, "Select a microphone"):
                await default_microphone()

    async def test_computer_audio_uses_only_the_default_outputs_reported_monitor(self):
        sinks = '[{"name":"usb","monitor_source":"USB.output-monitor"},{"name":"hdmi","monitor_source":"hdmi.monitor"}]'
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["usb\n", sinks])) as pulse:
            self.assertEqual(await default_computer_audio(), "USB.output-monitor")
            pulse.assert_any_await("get-default-sink")
            pulse.assert_any_await("--format=json", "list", "sinks")
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["missing\n", sinks])):
            with self.assertRaisesRegex(CaptureError, "Select an audio output"):
                await default_computer_audio()

    def test_dual_capture_preserves_sources_and_averages_the_mix(self):
        mixed, mic, system, levels = split_audio(struct.pack("<6h", 32_000, 32_000, 100, -100, -20_000, 0))
        self.assertEqual(struct.unpack("<3h", mixed), (32_000, 0, -10_000))
        self.assertEqual(struct.unpack("<3h", mic), (32_000, 100, -20_000))
        self.assertEqual(struct.unpack("<3h", system), (32_000, -100, 0))
        self.assertEqual(levels, (1, 1))

    def test_recovery_trims_uncommitted_track_frames_and_keeps_valid_sources(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "capture.wav"
            tracks = source_tracks(path)
            for target, frames in ((path, 2), (tracks["mic"], 3), (tracks["system"], 1)):
                with private_wav(target) as (writer, _):
                    writer.writeframes(b"\x01\x00" * frames)
            self.assertEqual(draft_tracks(path, draft_frames(path)), {"mic": tracks["mic"]})
            with wave.open(str(tracks["mic"]), "rb") as mic:
                self.assertEqual(mic.getnframes(), 2)
                self.assertEqual(mic.readframes(3), b"\x01\x00" * 2)

    @unittest.skipUnless(os.name == "posix", "requires an executable script for the capture helper fixture")
    async def test_windows_adapter_drains_partial_audio_when_its_control_pipe_closes(self):
        with TemporaryDirectory() as folder:
            helper = Path(folder) / "capture-fixture"
            helper.write_text("""#!/usr/bin/env python3
import struct
import sys
assert sys.argv[1:] == ["both", "120"]
frame = struct.pack("<2h", 1200, -500)
sys.stdout.buffer.write(frame * 1600)
sys.stdout.buffer.flush()
assert sys.stdin.buffer.read() == b""
sys.stdout.buffer.write(frame * 31)
""")
            helper.chmod(0o700)
            path = Path(folder) / "capture.wav"
            capture = WindowsRecording(uuid4(), path, "both", seconds=120)
            with (patch("windows_capture.helper_path", return_value=helper),
                  patch("windows_capture.subprocess.CREATE_NO_WINDOW", 0, create=True)):
                await capture.start()
                try:
                    self.assertEqual(capture.frames, 1600)
                finally:
                    await capture.stop()
            self.assertEqual(capture.frames, 1631)
            for audio, sample in ((path, 350), (source_tracks(path)["mic"], 1200),
                                  (source_tracks(path)["system"], -500)):
                self.assertEqual(draft_frames(audio), 1631)
                with wave.open(str(audio), "rb") as stream:
                    self.assertEqual(stream.readframes(1631), struct.pack("<h", sample) * 1631)

    async def test_owner_key_unlocks_only_its_loopback_workspace(self):
        origin = "http://127.0.0.1:43721"
        app = FastAPI()

        @app.get("/api/sessions")
        def sessions():
            return ["Private recording"]

        record = {"port": 43721}
        configure_workspace(app, record, origin)
        auth = {"X-Jaro-Workspace-Key": record["workspaceKey"], "Origin": origin}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as webview:
            self.assertEqual((await webview.get("/api/sessions")).status_code, 403)
            page = await webview.get(BOOTSTRAP)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(record["workspaceKey"], page.text)
            self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
            self.assertEqual((await webview.post(BOOTSTRAP, headers={
                **auth, "Origin": "http://127.0.0.1:43722",
            })).status_code, 403)
            self.assertEqual((await webview.post(BOOTSTRAP, headers={
                **auth, "X-Jaro-Workspace-Key": "a" * 64,
            })).status_code, 403)
            connected = await webview.post(BOOTSTRAP, headers=auth)
            self.assertEqual(connected.status_code, 204)
            self.assertIn("HttpOnly", connected.headers["set-cookie"])
            self.assertIn("SameSite=strict", connected.headers["set-cookie"])
            self.assertEqual((await webview.get("/api/sessions")).json(), ["Private recording"])
            self.assertEqual((await webview.get("/api/sessions", headers={
                "Origin": "http://127.0.0.1:43722",
            })).status_code, 403)
            self.assertEqual((await webview.get("/api/sessions", headers={
                "Host": "alternate.localhost:43721",
            })).status_code, 403)
            self.assertEqual((await webview.get("/api/sessions", headers={
                "Cookie": "jaro-workspace-43722=" + record["workspaceKey"],
            })).status_code, 403)
