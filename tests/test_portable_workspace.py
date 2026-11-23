import asyncio
from importlib.util import find_spec
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave
from uuid import uuid4

import httpx
import numpy as np
from fastapi import FastAPI

from portable_workspace import BOOTSTRAP, configure_workspace
from server import begin_screencast, discard_screencast, portable_screenshot, screencast_status, screenshot_targets
from server import append_screencast_ink, screencast_pen, screencast_preview, screencast_targets, set_screencast_region, start_model_setup, start_workspace_speech
from linux_capture import capture_args, default_computer_audio, default_microphone, microphone_inputs
from portable_capture import CaptureError, PortableRecording, SAMPLE_RATE, draft_frames, draft_tracks, private_wav, release_cutoff, split_audio
from native_recordings import source_tracks
from windows_capture import parse_microphones, WindowsMeetingRecording, WindowsRecording


class PortableWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_dictation_records_while_processing_and_delivers_in_order(self):
        from portable_dictation import PASTE_SENT, PortableDictation
        from portable_dictation_history import PortableDictationHistory

        class Dictation(PortableDictation):
            enabled = True

        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = PortableDictationHistory(root)
            history.initialize()
            paths = []

            def recorder(_key, path, _mode, **_options):
                paths.append(path)

                async def start():
                    with private_wav(path) as (wav, _):
                        wav.writeframes(b"\x01\x02" * 1600)

                return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False,
                                       start=start, stop=AsyncMock())

            first_started, release_first = asyncio.Event(), asyncio.Event()

            async def transcribe(_state, path, _languages, **_options):
                take = paths.index(path) + 1
                if take in (1, 3):
                    first_started.set()
                    await release_first.wait()
                return {"text": f"Take {take}.", "inferenceMs": 5}

            state = SimpleNamespace(status="ready", draining=False, library=SimpleNamespace(root=root),
                                    portable_recording=None, portable_recording_lock=asyncio.Lock(),
                                    portable_recorder=recorder)
            dictation = Dictation(state, transcribe, history)
            dictation.deliver = AsyncMock(return_value=PASTE_SENT)
            self.addAsyncCleanup(dictation.cancel_capture, include_pending=True)

            async def toggle():
                self.assertTrue(dictation.schedule())
                await dictation.action

            await toggle()
            await toggle()
            await asyncio.wait_for(first_started.wait(), 2)
            self.assertEqual(dictation.snapshot()["pendingJobs"], 1)
            with patch("portable_dictation.MAX_PENDING", 1):
                self.assertFalse(dictation.schedule())
                self.assertTrue(dictation.snapshot()["queueFull"])
                self.assertEqual(len(paths), 1)
            await toggle()
            self.assertEqual(dictation.phase, "recording")
            release_first.set()
            await asyncio.sleep(.02)
            dictation.deliver.assert_not_awaited()
            self.assertEqual(dictation.phase, "recording")
            await toggle()
            await dictation.output_task
            self.assertEqual([call.args[0] for call in dictation.deliver.await_args_list], ["Take 1.", "Take 2."])
            self.assertEqual([entry["text"] for entry in history.page(legacy=True)["entries"]], ["Take 2.", "Take 1."])
            self.assertEqual(dictation.phase, "idle")
            self.assertFalse(list(root.glob(".dictation-*")))

            first_started.clear()
            release_first.clear()
            await toggle()
            await toggle()
            await asyncio.wait_for(first_started.wait(), 2)
            await toggle()
            await toggle()
            self.assertEqual(dictation.snapshot()["pendingJobs"], 2)
            self.assertTrue(await dictation.cancel_dictation())
            self.assertFalse(paths[3].parent.exists())
            release_first.set()
            await dictation.output_task
            self.assertEqual([call.args[0] for call in dictation.deliver.await_args_list],
                             ["Take 1.", "Take 2.", "Take 3."])

    async def test_lost_dictation_is_retained_without_pasting_into_the_focused_app(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory

        class Dictation(PortableDictation):
            enabled = True

        for mode in ("eof", "stall", "silent"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                class Recorder(PortableRecording):
                    async def spawn(self):
                        return await asyncio.create_subprocess_exec(
                            sys.executable, "-c",
                            "import sys; sys.stdout.buffer.write(b'\\x11\\x22' * 6400); sys.stdout.flush(); "
                            "sys.stdin.buffer.read()",
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        )

                root = Path(directory)
                history = PortableDictationHistory(root)
                history.initialize()
                state = SimpleNamespace(draining=False, status="ready", library=SimpleNamespace(root=root),
                                        portable_recording=None, portable_recording_lock=asyncio.Lock(),
                                        portable_recorder=Recorder)
                signal = "" if mode == "silent" else "Keep these words"
                transcribe = AsyncMock(return_value={"text": signal, "inferenceMs": 10})
                dictation = Dictation(state, transcribe, history)
                dictation.deliver = AsyncMock()
                dictation.text = "Previous dictation"
                with (patch("portable_capture.DICTATION_STALL", .15),
                      patch("portable_dictation.copy_text", new_callable=AsyncMock) as clipboard):
                    if mode == "stall":
                        clipboard.side_effect = OSError("Clipboard unavailable")
                    await dictation.toggle()
                    self.assertEqual(dictation.phase, "recording")
                    capture = dictation.capture
                    if mode != "stall":
                        capture.process.stdin.close()
                    await capture.worker
                    if dictation.action:
                        await dictation.action
                    await dictation.output_task
                    self.assertTrue(capture.interrupted)
                    self.assertEqual(dictation.phase, "idle")
                    dictation.deliver.assert_not_called()
                    transcribe.assert_awaited_once()
                    rows = history.page(legacy=True, include_audio=True)["entries"]
                    if signal:
                        self.assertEqual(dictation.snapshot()["text"], signal)
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["text"], signal)
                        self.assertIn("audioExpiresAt", rows[0])
                        clipboard.assert_awaited_once_with(signal)
                        self.assertIn("Copy last" if mode == "stall" else "was copied", dictation.feedback)
                    else:
                        self.assertEqual(dictation.snapshot()["text"], "Previous dictation")
                        self.assertFalse(rows)
                        clipboard.assert_not_called()

    @staticmethod
    def tail_audio(voiced, through_ms):
        frame = np.arange(through_ms * SAMPLE_RATE // 1000)
        audio = np.zeros(frame.size, dtype="<i2")
        for start, end in voiced:
            part = (start * SAMPLE_RATE // 1000 <= frame) & (frame < end * SAMPLE_RATE // 1000)
            audio[part] = (200 * np.sin(frame[part] * (2 * np.pi * 250 / SAMPLE_RATE))).astype("<i2")
        return audio.tobytes()

    def test_dictation_tail_keeps_quiet_words_and_ignores_an_isolated_click(self):
        release = SAMPLE_RATE
        for audio, heard, end in (
            ([(940, 1080)], 1199, None),
            ([(940, 1080)], 1200, 1200),
            ([(1180, 1200)], 1200, 1200),
            ([(940, 1280)], 1300, None),
            ([(940, 1280)], 1450, 1440),
            ([(940, 1700)], 1700, 1600),
        ):
            with self.subTest(audio=audio, heard=heard):
                cutoff = release_cutoff(0, self.tail_audio(audio, heard), release)
                self.assertEqual(cutoff, end * SAMPLE_RATE // 1000 if end else None)

    async def test_dictation_release_trims_only_after_recorder_finishes(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "voice.wav"
            capture = PortableRecording(uuid4(), path, "mic")
            captured = self.tail_audio([(940, 1280), (1500, 1650)], 1700)
            capture.pcm_epoch = 9
            capture.frames = len(captured) // 2
            capture.blocks.append((0, captured))
            capture.stop = AsyncMock()
            with private_wav(path) as (wav, _):
                wav.writeframes(captured)
            await capture.finish_dictation(10)
            capture.stop.assert_awaited_once()
            self.assertEqual(capture.frames, 1440 * SAMPLE_RATE // 1000)
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnframes(), capture.frames)
                self.assertEqual(wav.readframes(capture.frames), captured[:capture.frames * 2])

    def test_meeting_monitors_capture_hardware_once_and_use_default_as_fallback(self):
        from linux_meeting_audio import meeting_monitors

        virtual = {"name": "effects", "monitor_source": "effects.monitor", "properties": {}}
        headset = {"name": "headset", "monitor_source": "headset.monitor", "properties": {"device.api": "bluez5"}}
        laptop = {"name": "laptop", "monitor_source": "laptop.monitor", "properties": {"device.api": "alsa"}}
        self.assertEqual(meeting_monitors([virtual, headset, laptop, headset], "effects"),
                         ("headset.monitor", "laptop.monitor"))
        self.assertEqual(meeting_monitors([virtual], "effects"), ("effects.monitor",))
        self.assertEqual(meeting_monitors([virtual], "missing"), ())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires POSIX no-follow file access")
    def test_launch_at_login_respects_the_users_autostart_entries(self):
        from portable_login import LinuxLogin

        with TemporaryDirectory() as folder:
            entry = Path(folder) / "autostart" / "jaro.desktop"
            login = LinuxLogin(entry)
            self.assertEqual(login.set_state(None), {"state": "disabled"})
            self.assertFalse(entry.exists())
            self.assertEqual(login.set_state(True), {"state": "enabled"})
            self.assertIn("Exec=/usr/bin/jaro --background", entry.read_text())
            with entry.open("a") as config:
                config.write("Hidden=true\n")
            self.assertEqual(login.set_state(None), {"state": "disabled"})
            self.assertEqual(login.set_state(True), {"state": "enabled"})
            self.assertEqual(login.set_state(False), {"state": "disabled"})
            self.assertFalse(entry.exists())

            external = Path(folder) / "user-entry.desktop"
            external.write_text("[Desktop Entry]\nExec=/usr/bin/jaro --background\n")
            entry.symlink_to(external)
            for enabled in (True, False):
                self.assertEqual(login.set_state(enabled)["state"], "unknown")
            self.assertTrue(entry.is_symlink())
            self.assertNotIn("X-Jaro-Managed", external.read_text())

    async def test_capture_power_releases_a_late_grant_and_skips_a_canceled_tap(self):
        from recording_power import keep_awake_until

        loop = asyncio.get_running_loop()
        stopped = loop.create_future()
        requested = asyncio.Event()
        grant = loop.create_future()
        release = Mock()

        async def inhibit(_reason):
            requested.set()
            return await grant

        with patch("recording_power.sys.platform", "linux"), patch("recording_power.inhibit_linux", side_effect=inhibit) as start:
            guard = keep_awake_until(stopped, "Recording")
            await requested.wait()
            stopped.set_result(None)
            grant.set_result(release)
            await guard
            release.assert_called_once_with()

            stopped = loop.create_future()
            guard = keep_awake_until(stopped, "Tap", delay=10)
            stopped.cancel()
            await guard
            start.assert_awaited_once_with("Recording")

    async def test_desktop_pen_keeps_final_stroke_before_stopping(self):
        from desktop_pen import PenProcess
        from screencast_ink import RecordingDrawing

        with TemporaryDirectory() as folder:
            pen = PenProcess(RecordingDrawing(Path(folder) / "ink.jsonl"))
            seen = []
            child = "import sys; print('READY', flush=True); sys.stdin.read(); print('{\"released\":12}', flush=True)"
            self.assertTrue(await pen.start([sys.executable, "-c", child], seen.append))
            self.assertTrue(pen.active)
            await pen.close()
            self.assertFalse(pen.active)
            self.assertEqual(seen, [{"released": 12}])

    async def test_drawing_preview_reads_ffmpeg_and_gstreamer_headers(self):
        from screencast_ink import BOUNDARY_LINE, RecordingDrawing

        reader = asyncio.StreamReader()
        drawing = RecordingDrawing(Path("unused-ink.jsonl"))
        worker = asyncio.create_task(drawing.read_frames(reader))
        view = drawing.stream(lambda: True)
        try:
            for media, length, pixel in ((b"Content-type", b"Content-length", b"j"),
                                         (b"Content-Type", b"Content-Length", b"p")):
                frame = BOUNDARY_LINE + media + b": image/jpeg\r\n" + length + b": 1\r\n\r\n" + pixel + b"\r\n"
                reader.feed_data(frame)
                self.assertEqual(await asyncio.wait_for(anext(view), 1), frame)
            drawing.close_view()
            with self.assertRaises(StopAsyncIteration):
                await anext(view)
        finally:
            reader.feed_eof()
            await worker
            await view.aclose()

    async def test_windows_screen_picker_rejects_external_and_stale_targets(self):
        from windows_screencast import capture_targets

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screencast"
        app.add_api_route(endpoint, begin_screencast, methods=["POST"], status_code=202)
        app.add_api_route(endpoint + "/targets", screencast_targets, methods=["GET"])
        app.state.draining = False
        app.state.portable_recording = app.state.portable_dictation = app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        app.state.jobs = set()
        configure_workspace(app, record, origin)
        listed = capture_targets("1a\t12345\tAperçu\r\n2b\t0\tDisplay\r\ninvalid\t1\tSkip".encode(), "window")
        self.assertEqual(listed, [{"id": "1a:12345", "label": "Aperçu"}])
        opener = AsyncMock(return_value=listed)
        headers = {"Origin": origin}
        with (TemporaryDirectory() as staging, patch("server.URL", origin),
              patch("server.sys", SimpleNamespace(platform="win32")),
              patch("windows_screencast.targets", opener),
              patch("windows_screencast.WindowsScreenRecording.record", new_callable=AsyncMock) as record_screen):
            app.state.screen_draft_dir = Path(staging)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                list_windows = endpoint + "/targets?source=window"
                start = endpoint + "?source=window&target=1a%3A12345"
                self.assertEqual((await browser.get(list_windows)).status_code, 403)
                self.assertEqual((await browser.post(start, headers=headers)).status_code, 403)
                opener.assert_not_awaited()
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(list_windows)).json(), {"targets": listed})
                self.assertEqual((await browser.post(start)).status_code, 403)
                self.assertEqual((await browser.post(endpoint + "?source=area", headers=headers)).status_code, 422)
                self.assertEqual((await browser.post(endpoint + "?source=window&target=1a%3A8", headers=headers)).status_code, 409)
                result = await browser.post(start, headers=headers)
                self.assertEqual(result.status_code, 202)
                self.assertEqual(result.json()["phase"], "starting")
                self.assertEqual(app.state.portable_screencast.target, "1a:12345")
                await app.state.portable_screencast.task
                record_screen.assert_awaited_once()

    async def test_windows_area_uses_native_pixels_and_expires_the_preview(self):
        from windows_screencast import Snapshot, WindowsScreenRecording

        picture = Snapshot(b"preview", (1920, 1080), (3840, 2160))
        with TemporaryDirectory() as folder, patch("windows_screencast.snapshot", new=AsyncMock(return_value=picture)):
            take = WindowsScreenRecording(Path(folder), "area", "1a:0")
            choose = asyncio.create_task(take.choose_region())
            await asyncio.sleep(0)
            self.assertEqual(take.status()["preview"]["width"], 1920)
            self.assertEqual(take.preview, b"preview")
            with self.assertRaises(ValueError):
                take.select_region((1900, 0, 30, 20))
            take.select_region((1451, 879, 469, 201))
            self.assertEqual(await choose, (2902, 1758, 938, 402, 3840, 2160))
            self.assertEqual(take.phase, "starting")
            self.assertIsNone(take.preview_id)
            self.assertIsNone(take.preview)
            self.assertFalse(list(Path(folder).iterdir()))

    async def test_windows_desktop_pen_uses_selected_display_area_and_keeps_renderable_strokes(self):
        from screencast_ink import load_events
        from windows_screencast import WindowsScreenRecording

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        path = "/api/portable/screencast/{key}"
        app.add_api_route(path + "/pen", screencast_pen, methods=["POST", "DELETE"])
        app.add_api_route("/api/portable/screencast", discard_screencast, methods=["DELETE"], status_code=204)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with (TemporaryDirectory() as folder, patch("server.URL", origin),
              patch("server.sys", SimpleNamespace(platform="win32")),
              patch("windows_screencast.helper_path", return_value=Path("jaro-screen.exe")),
              patch("windows_screencast.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True)):
            take = WindowsScreenRecording(Path(folder), "area", "dad:0")
            window = WindowsScreenRecording(Path(folder), "window", "dad:1")
            self.assertIsNone(window.pen.binary)
            take.draft.create()
            app.state.portable_screencast = take
            take.region = (100, 70, 500, 400, 1920, 1080)
            take.phase, take.started = "recording", 5
            take.frame_bytes, take.bytes_encoded = 100, 4800
            take.pen.video_clock = 1000
            endpoint = path.format(key=take.draft.key) + "/pen"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.post(endpoint, headers=headers)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                with patch.object(take.pen, "start", new=AsyncMock(return_value=True)) as start:
                    self.assertTrue((await browser.post(endpoint, headers=headers)).json()["desktopDrawing"])
                    start.assert_awaited_once_with(
                        (Path("jaro-screen.exe"), "pen", "display", "dad", "0", "100", "70", "500", "400", "1920", "1080"),
                        take.pen.keep_stroke, creationflags=0x08000000,
                    )
                take.pen.keep_stroke({"released": 1_001_700_000,
                                      "points": [[.2, .25, 1_001_200_000], [.5, .25, 1_001_600_000]]})
                initial, final = load_events(take.draft.ink)
                self.assertEqual((initial["time"], final["liftedAt"]), (1.2, 1.7))
                self.assertEqual((final["from"], final["to"]), ([.2, .75], [.5, .75]))
                self.assertNotIn("captured", final)
                with patch.object(take.pen, "start", new=AsyncMock(return_value=False)):
                    self.assertEqual((await browser.post(endpoint, headers=headers)).status_code, 409)
                    self.assertFalse(take.status()["desktopDrawing"])
                self.assertEqual((await browser.delete("/api/portable/screencast", headers=headers)).status_code, 204)
                self.assertFalse(take.draft.folder.exists())

    async def test_windows_screenshot_requires_a_current_target_and_workspace_origin(self):
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screenshots"
        app.add_api_route(endpoint, portable_screenshot, methods=["POST"])
        app.state.draining = False
        app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        configure_workspace(app, record, origin)
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0002 0003 01 01 1100 ffd9")
        command = AsyncMock(return_value=struct.pack("<IIII", 3, 2, 6, 4) + jpeg)
        headers = {"Origin": origin}
        with (TemporaryDirectory() as directory, patch("server.URL", origin),
              patch("server.sys", SimpleNamespace(platform="win32")),
              patch("windows_screencast.targets", new=AsyncMock(return_value=[{"id": "ab:42"}])),
              patch("windows_screencast.output", command)):
            app.state.screen_draft_dir = Path(directory)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                path = endpoint + "?source=window&target=ab%3A42"
                self.assertEqual((await browser.post(path, headers=headers)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(path)).status_code, 403)
                self.assertEqual((await browser.post(path.replace("target=ab", "target=ff"), headers=headers)).status_code, 409)
                command.assert_not_awaited()
                result = await browser.post(path, headers=headers)
                self.assertEqual(result.content, jpeg)
                self.assertEqual(result.headers["cache-control"], "no-store")
                self.assertEqual(result.headers["content-type"], "image/jpeg")
                command.assert_awaited_once_with("snapshot", "window", "ab", "42", timeout=10)
            self.assertFalse(app.state.screenshot_pending)
            self.assertFalse(list(Path(directory).iterdir()))

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_live_ink_is_owned_and_recovers_completed_strokes(self):
        from time import monotonic
        from linux_screencast import ScreenRecording
        from screencast_ink import load_events

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        path = "/api/portable/screencast/{key}/ink"
        app.add_api_route(path, append_screencast_ink, methods=["POST"], status_code=204)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            take = ScreenRecording(Path(directory), "window", False)
            app.state.portable_screencast = take
            take.draft.create()
            take.phase, take.started = "recording", monotonic() - 2
            endpoint = path.format(key=take.draft.key)
            stroke = {"time": 1.5, "points": [[.1, .2, 1], [.3, .4, 1.25]]}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.post(endpoint, headers=headers, json=stroke)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(endpoint, headers=headers, json=stroke)).status_code, 204)
                self.assertEqual((await browser.post(endpoint, headers=headers, json=stroke)).status_code, 422)
                self.assertEqual((await browser.post(endpoint, headers=headers, json={
                    "time": 1.8, "points": [],
                })).status_code, 204)
            with take.draft.ink.open("ab") as file:
                file.write(b'[{"time":')
            first, second, clear = load_events(take.draft.ink)
            self.assertEqual(first["from"], [.1, .8])
            self.assertEqual(second["to"], [.3, .6])
            self.assertEqual(second["liftedAt"], 1.5)
            self.assertEqual(clear, {"time": 1.8, "clear": True})

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_desktop_pen_uses_workspace_origin_and_marks_captured_strokes(self):
        from time import monotonic
        from dbus_fast import Variant
        from linux_pen import monitor_arguments
        from linux_screencast import ScreenRecording
        from screencast_ink import load_events

        self.assertEqual(monitor_arguments({"mapping_id": Variant("s", "DP-1"),
                                            "position": Variant("(ii)", [-1280, 160]),
                                            "size": Variant("(ii)", [1280, 720])}), ("DP-1", "-1280,160,1280,720"))
        self.assertEqual(monitor_arguments({"size": Variant("(ii)", [1280, 720])}), ("", ""))
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        path = "/api/portable/screencast/{key}"
        app.add_api_route(path + "/pen", screencast_pen, methods=["POST", "DELETE"])
        app.add_api_route(path + "/ink", append_screencast_ink, methods=["POST"], status_code=204)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            take = ScreenRecording(Path(directory), "display", False)
            app.state.portable_screencast = take
            take.draft.create()
            take.phase, take.started = "recording", monotonic() - 2
            take.pen.monitor = ("DP-1", "-1280,160,1280,720")
            endpoint = path.format(key=take.draft.key)
            with patch.object(take.pen, "open", new_callable=AsyncMock) as start:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                    self.assertEqual((await browser.post(endpoint + "/pen", headers=headers)).status_code, 403)
                    start.assert_not_awaited()
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertTrue((await browser.post(endpoint + "/pen", headers=headers)).json()["desktopDrawing"])
                    start.assert_awaited_once_with(take.started, None)
                    take.pen.task = asyncio.create_task(asyncio.sleep(30))
                    try:
                        self.assertEqual((await browser.post(endpoint + "/ink", headers=headers, json={
                            "time": 1.5, "points": [[.1, .2, 1.2]],
                        })).status_code, 409)
                    finally:
                        take.pen.task.cancel()
                        await asyncio.gather(take.pen.task, return_exceptions=True)
                    with patch.object(take.pen, "close", new_callable=AsyncMock) as close:
                        self.assertEqual((await browser.delete(endpoint + "/pen", headers=headers)).status_code, 200)
                        close.assert_awaited_once()
            take.pen.keep_stroke({
                "released": (take.started + 1.5) * 1_000_000,
                "points": [[.1, .2, (take.started + 1.2) * 1_000_000]],
            }, take.started)
            self.assertEqual(load_events(take.draft.ink), [{
                "time": 1.2, "stroke": 0, "from": [.1, .8], "to": [.1, .8], "liftedAt": 1.5, "captured": True,
            }])

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_screen_recording_dismissal_releases_the_workspace(self):
        from linux_desktop_portal import PortalResponseError, ScreenCastPortal

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screencast"
        app.add_api_route(endpoint, begin_screencast, methods=["POST"], status_code=202)
        app.add_api_route(endpoint, screencast_status, methods=["GET"])
        app.add_api_route(endpoint, discard_screencast, methods=["DELETE"], status_code=204)
        app.state.draining = False
        app.state.portable_recording = app.state.portable_dictation = app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        app.state.jobs = set()
        configure_workspace(app, record, origin)
        portal = AsyncMock(spec=ScreenCastPortal)
        portal.select.side_effect = PortalResponseError(1, {})
        headers = {"Origin": origin}
        with (TemporaryDirectory() as staging, patch("server.URL", origin),
              patch("linux_screencast.supported", return_value=True),
              patch("linux_screencast.ScreenCastPortal", return_value=portal)):
            app.state.screen_draft_dir = Path(staging)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                start = endpoint + "?source=display"
                self.assertEqual((await browser.post(start, headers=headers)).status_code, 403)
                portal.connect.assert_not_called()
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(start)).status_code, 403)
                started = await browser.post(start, headers=headers)
                self.assertEqual(started.status_code, 202)
                self.assertEqual(started.json()["levels"], {})
                await app.state.portable_screencast.task
                self.assertEqual((await browser.get(endpoint)).json()["phase"], "cancelled")
                self.assertEqual((await browser.post(start, headers=headers)).status_code, 409)
                portal.close.assert_awaited_once()
                self.assertEqual((await browser.delete(endpoint, headers=headers)).status_code, 204)
                self.assertEqual((await browser.get(endpoint)).json(), {"phase": "idle"})
                self.assertFalse(list(Path(staging).iterdir()))

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_screen_preview_is_private_and_expires_when_refreshed(self):
        from linux_screencast import ScreenRecording

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screencast/area"
        app.add_api_route(endpoint + "/{key}", screencast_preview, methods=["GET"])
        app.add_api_route(endpoint + "/{key}", set_screencast_region, methods=["POST"], status_code=202)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            take = ScreenRecording(Path(directory), "area", False)
            app.state.portable_screencast = take
            take.phase = "selecting"
            take.preview, take.preview_size = b"preview", (800, 600)
            take.preview_id = str(uuid4())
            take.selection = asyncio.get_running_loop().create_future()
            first = endpoint + "/" + take.preview_id
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.get(first)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                response = await browser.get(first)
                self.assertEqual(response.content, b"preview")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual((await browser.post(first, headers=headers, json={
                    "x": 700, "y": 0, "width": 101, "height": 100,
                })).status_code, 422)
                self.assertFalse(take.selection.done())
                self.assertEqual((await browser.post(first, headers=headers)).json()["phase"], "previewing")
                self.assertIsNone(await take.selection)
                take.phase = "selecting"
                take.preview_id = str(uuid4())
                take.selection = asyncio.get_running_loop().create_future()
                self.assertEqual((await browser.get(first)).status_code, 404)
                self.assertEqual((await browser.post(first, headers=headers)).status_code, 409)
                accepted = await browser.post(endpoint + "/" + take.preview_id, headers=headers,
                                               json={"x": 3, "y": 5, "width": 160, "height": 100})
                self.assertEqual(accepted.json()["phase"], "starting")
                self.assertEqual((await take.selection).width, 160)
            self.assertFalse(list(Path(directory).iterdir()))

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    def test_screen_region_cannot_include_pixels_outside_the_preview(self):
        from linux_screencast import Region

        region = Region(101, 99, 180, 102, 800, 600)
        self.assertIn("right=519", region.crop())
        self.assertIn("bottom=399", region.crop())
        self.assertIn("video/x-raw,format=RGBx,width=800,height=600", region.crop())
        for bounds in ((-1, 0, 2, 2), (0, 0, 1, 2), (799, 0, 2, 2), (0, 598, 2, 3)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                Region(*bounds, 800, 600)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    def test_screen_draft_can_be_opened_only_after_its_recorder_releases_it(self):
        from linux_screencast import ScreenDraft, writer_lock

        with TemporaryDirectory() as staging:
            folder = Path(staging) / str(uuid4())
            draft = ScreenDraft(folder, "area", ("mic", "system"))
            draft.create()
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertIsNone(ScreenDraft.read(folder))
            with writer_lock(folder, create=True):
                draft.movie.write_bytes(b"fragment fixture")
                self.assertIsNone(ScreenDraft.read(folder))
            self.assertEqual(ScreenDraft.read(folder), draft)
            draft.movie.unlink()
            outside = Path(staging) / "outside.mp4"
            outside.write_bytes(b"other file")
            draft.movie.symlink_to(outside)
            self.assertIsNone(ScreenDraft.read(folder))
            draft.discard()
            self.assertFalse(folder.exists())
            self.assertTrue(outside.is_file())

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_screen_recording_meters_select_the_loudest_channel(self):
        from linux_screencast import input_level

        prefix = b'Got message #42 from element "system_level" (element): level, endtime=(guint64)200000000, '
        self.assertEqual(input_level(prefix + b"peak=(GValueArray)< -699, -699 >;"), ("system", 0))
        source, peak = input_level(prefix + b"peak=(GValueArray)< -50, -6.020599913279624 >;")
        self.assertEqual(source, "system")
        self.assertAlmostEqual(peak, 0.5)
        self.assertIsNone(input_level(prefix + b"peak=(GValueArray)< invalid >;"))
        self.assertIsNone(input_level(b'Got message #11 from element "filesink0" (state-changed): '))

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux screenshot portal client")
    async def test_screenshot_picker_requires_its_workspace_and_handles_cancel(self):
        from linux_desktop_portal import PortalResponseError, ScreenshotPortal

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route("/api/portable/screenshots", screenshot_targets, methods=["GET"])
        app.add_api_route("/api/portable/screenshots", portable_screenshot, methods=["POST"])
        app.state.draining = False
        configure_workspace(app, record, origin)
        endpoint = "/api/portable/screenshots"
        portal = AsyncMock(spec=ScreenshotPortal)
        portal.targets.return_value = ["picker"]
        image = b"\x89PNG\r\n\x1a\nfixture"
        with (patch("server.URL", origin), patch("linux_desktop_portal.ScreenshotPortal", return_value=portal),
              patch("linux_desktop_portal.read_screenshot", return_value=(image, "image/png")) as load):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.post(endpoint + "?source=picker", headers={"Origin": origin})).status_code, 403)
                portal.connect.assert_not_called()
                await browser.post(BOOTSTRAP, headers={"Origin": origin, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(endpoint)).json(), {"targets": ["picker"]})
                self.assertEqual((await browser.post(endpoint + "?source=picker")).status_code, 403)
                portal.screenshot.side_effect = PortalResponseError(1, {})
                approved = {"Origin": origin}
                self.assertEqual((await browser.post(endpoint + "?source=picker", headers=approved)).status_code, 204)
                load.assert_not_called()
                portal.screenshot.side_effect = None
                portal.screenshot.return_value = "file:///fixture.png"
                response = await browser.post(endpoint + "?source=picker", headers=approved)
                self.assertEqual(response.content, image)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertFalse(app.state.screenshot_pending)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux screenshot portal client")
    async def test_screenshot_targets_and_untrusted_file_uris(self):
        from linux_desktop_portal import SCREENSHOT, ScreenshotPortal, read_screenshot

        portal = ScreenshotPortal()
        with patch.object(portal, "property", side_effect=[2, 3, 5, 3, 5]):
            self.assertEqual(await portal.targets(), ["picker"])
            self.assertEqual(await portal.targets(), ["area", "display"])
            with patch.object(portal, "request", new_callable=AsyncMock, return_value={}) as screenshot:
                with self.assertRaisesRegex(CaptureError, "did not return"):
                    await portal.screenshot("area")
                args = screenshot.call_args.args
                self.assertEqual(args[:2], (SCREENSHOT, "Screenshot"))
                self.assertTrue(args[-1]["interactive"].value)
                self.assertEqual(args[-1]["target"].value, 4)

        with TemporaryDirectory() as folder:
            image = Path(folder) / "screen shot.png"
            data = b"\x89PNG\r\n\x1a\nfixture"
            image.write_bytes(data)
            self.assertEqual(read_screenshot(image.as_uri()), (data, "image/png"))
            for uri in ("https://example.invalid/file.png", "file://remotehost/a.png", "file:///tmp/%00a"):
                with self.subTest(uri=uri), self.assertRaises(CaptureError):
                    read_screenshot(uri)
            link = image.with_name("symlink.png")
            link.symlink_to(image)
            with self.assertRaises(OSError):
                read_screenshot(link.as_uri())

    async def test_only_workspace_owner_can_start_one_resumable_model_install(self):
        origin = "http://127.0.0.1:43721"
        app = FastAPI()
        app.add_api_route("/api/portable/models", start_model_setup, methods=["POST"], status_code=202)
        app.state.jobs = set()
        app.state.draining = False
        app.state.model_setup = {"phase": "needed", "total": 100}
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        installing = asyncio.Event()
        finish = asyncio.Event()

        async def install(report):
            report(25, 100)
            installing.set()
            await finish.wait()
            raise RuntimeError("Interrupted download.")

        async def resume(report):
            report(100, 100)

        async def load(owner):
            self.assertEqual(owner.state.model_setup["phase"], "starting")
            owner.state.status = "ready"

        with (patch("server.URL", origin), patch("server.install_workspace_models", side_effect=install) as download,
              patch("server.load_speech", side_effect=load), patch("server.logger.exception")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as webview:
                endpoint = "/api/portable/models"
                headers = {"Origin": origin}
                self.assertEqual((await webview.post(endpoint, headers=headers)).status_code, 403)
                await webview.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await webview.post(endpoint)).status_code, 403)
                self.assertEqual((await webview.post(endpoint, headers={"Origin": "https://other.example"})).status_code, 403)
                response = await webview.post(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["modelSetup"]["phase"], "downloading")
                await asyncio.wait_for(installing.wait(), 1)
                again = await webview.post(endpoint, headers=headers)
                self.assertEqual(again.json()["modelSetup"]["completed"], 25)
                download.assert_awaited_once()
                finish.set()
                await asyncio.gather(*app.state.jobs)
                self.assertEqual(app.state.model_setup["phase"], "download-error")
                download.side_effect = resume
                response = await webview.post(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202)
                await asyncio.gather(*app.state.jobs)
                self.assertIsNone(app.state.model_setup)
                self.assertEqual(app.state.status, "ready")
                self.assertEqual(download.await_count, 2)
                app.state.model_setup = {"phase": "load-error", "total": 100}
                response = await webview.post(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202)
                await asyncio.gather(*app.state.jobs)
                self.assertIsNone(app.state.model_setup)
                self.assertEqual(download.await_count, 2)

    async def test_model_setup_distinguishes_missing_models_from_failed_startup(self):
        app = FastAPI()
        app.state.model_setup = {"phase": "checking"}

        async def load(owner):
            owner.state.status = status

        with (patch("server.load_speech", side_effect=load), patch("server.model_status") as installed):
            status = "ready"
            await start_workspace_speech(app)
            self.assertIsNone(app.state.model_setup)
            installed.assert_not_called()

            status = "error"
            installed.return_value = {"ready": False, "bytes": 4100}
            await start_workspace_speech(app)
            self.assertEqual(app.state.model_setup, {"phase": "needed", "total": 4100})

            installed.return_value = {"ready": True, "bytes": 4100}
            await start_workspace_speech(app)
            self.assertEqual(app.state.model_setup, {"phase": "load-error", "total": 4100})

    async def test_linux_capture_requires_a_microphone_instead_of_a_playback_monitor(self):
        sources = '[{"name":"mic","properties":{}},{"name":"speaker.monitor","properties":{"device.class":"monitor"}}]'
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["mic\n", sources])):
            self.assertEqual(await default_microphone(), "mic")
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["speaker.monitor\n", sources])):
            with self.assertRaisesRegex(CaptureError, "Select a microphone"):
                await default_microphone()

    async def test_dictation_microphones_list_and_capture_only_input_sources(self):
        sources = ('[{"name":"builtin","description":"Built-in microphone"},'
                   '{"name":"usb","description":"USB microphone"},'
                   '{"name":"speaker.monitor"},'
                   '{"name":"playback","properties":{"device.class":"monitor"}},'
                   '{"name":"speaker_return","monitor_of_sink":12}]')
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=[sources, "builtin\n"])):
            self.assertEqual(await microphone_inputs(), [
                {"id": "builtin", "label": "Built-in microphone", "default": True},
                {"id": "usb", "label": "USB microphone", "default": False},
            ])
        with patch("linux_capture.pulse_info", new=AsyncMock(return_value=sources)):
            self.assertEqual((await capture_args("mic", "usb"))[:4], ("-f", "pulse", "-i", "usb"))
            with self.assertRaisesRegex(CaptureError, "unavailable"):
                await capture_args("mic", "speaker.monitor")
        self.assertEqual(parse_microphones("1\t{endpoint-a}\tIntégré\n0\t{endpoint-b}\tUSB\n".encode()), [
            {"id": "{endpoint-a}", "label": "Intégré", "default": True},
            {"id": "{endpoint-b}", "label": "USB", "default": False},
        ])

    async def test_dictation_microphone_persists_and_falls_back_without_losing_the_selection(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_microphone, portable_microphone

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation/microphone"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(endpoint, portable_microphone, methods=["GET"])
        app.add_api_route(endpoint, change_portable_microphone, methods=["POST"])
        configure_workspace(app, record, origin)

        class Dictation(PortableDictation):
            platform = "x11"
            enabled = True
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        with TemporaryDirectory() as folder:
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.draining = False
            state.status = "ready"
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recording = None
            history = PortableDictationHistory(folder)
            history.initialize()
            with history.connection(write=True) as db, db:
                db.execute("ALTER TABLE preferences DROP COLUMN microphone")
            history.initialize()
            self.assertEqual(history.microphone(), "")
            transcribe = AsyncMock(return_value={"text": "Fixture speech", "inferenceMs": 80})
            state.portable_dictation = Dictation(state, transcribe, history)
            inputs = AsyncMock(return_value=[{"id": "usb", "label": "USB", "default": False}])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                with patch("server.URL", origin), patch("linux_capture.microphone_inputs", inputs):
                    self.assertEqual((await browser.get(endpoint)).status_code, 403)
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await browser.get(endpoint)).json()["selected"], "")
                    self.assertEqual((await browser.post(endpoint, json={"selected": "usb"})).status_code, 403)
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": "stale"})).status_code, 400)
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": "usb"})).status_code, 200)
                    state.portable_dictation = Dictation(state, transcribe, history)
                    self.assertEqual(state.portable_dictation.microphone, "usb")
                    inputs.side_effect = CaptureError("Input service unavailable.")
                    self.assertEqual((await browser.get(endpoint)).json(),
                                     {"selected": "usb", "inputs": [], "available": False})

                    attempted = []
                    worker = asyncio.Future()

                    def recorder(_key, path, mode, *, seconds, microphone):
                        self.assertEqual((mode, seconds), ("mic", 120))
                        attempted.append(microphone)
                        async def start():
                            if microphone:
                                raise CaptureError("USB disconnected.")
                            with private_wav(path) as (wav, _):
                                wav.writeframes(b"\x01\x02" * 1600)
                        return SimpleNamespace(
                            path=path, worker=worker, frames=1600, interrupted=False, stop=AsyncMock(), start=start,
                        )

                    state.portable_recorder = recorder
                    await state.portable_dictation.toggle()
                    self.assertEqual(state.portable_dictation.phase, "recording")
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": ""})).status_code, 409)
                    await state.portable_dictation.toggle()
                    self.assertEqual(attempted, ["usb", ""])
                    self.assertEqual(transcribe.await_args.args[1].name, "fallback.wav")
                    state.portable_dictation.deliver.assert_awaited_once_with("Fixture speech")
                    self.assertIn("Used the system microphone", state.portable_dictation.feedback)
                    self.assertEqual(history.microphone(), "usb")
                    row, = history.page(legacy=True, include_audio=True)["entries"]
                    self.assertEqual(row["text"], "Fixture speech")
                    self.assertIn("audioExpiresAt", row)
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": ""})).status_code, 200)
                    self.assertEqual(history.microphone(), "")

    async def test_dictation_audio_uses_the_private_workspace_and_supports_webkit_ranges(self):
        from time import time
        from portable_dictation_history import SECONDS_PER_DAY, PortableDictationHistory
        from server import portable_history, portable_history_audio

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation/history"
        identifier = str(uuid4())
        url = f"{endpoint}/{identifier}/audio"
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(endpoint, portable_history, methods=["GET"])
        app.add_api_route(endpoint + "/{key}/audio", portable_history_audio, methods=["GET"])
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            app.state.portable_dictation = SimpleNamespace(history=history)
            app.state.portable_recording_dir = Path(folder)
            source = Path(folder) / "original.wav"
            with private_wav(source) as (wav, _):
                wav.writeframes(b"\x02\x01" * 1600)
            original = source.read_bytes()
            created = time()
            history.record({"id": identifier, "time": created, "text": "Original audio", "app": "",
                            "seconds": .1, "inferenceMs": 40, "totalMs": 100}, source)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.get(url)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={"Origin": origin, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(url, headers={"Origin": "https://elsewhere.test"})).status_code, 403)
                response = await browser.get(url)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.content, original)
                response = await browser.get(url, headers={"Range": "bytes=0-1"})
                self.assertEqual((response.status_code, response.content), (206, b"RI"))
                self.assertEqual(response.headers["content-range"], f"bytes 0-1/{len(original)}")
                self.assertEqual((await browser.get(url, headers={"Range": "bytes=-4"})).content, original[-4:])
                self.assertEqual((await browser.get(url, headers={"Range": "bytes=100000-"})).status_code, 416)
                with patch("portable_dictation_history.time", return_value=created + SECONDS_PER_DAY):
                    self.assertEqual((await browser.get(url)).status_code, 404)
                    row, = (await browser.get(endpoint)).json()["entries"]
                    self.assertEqual(row["text"], "Original audio")
                    self.assertNotIn("audioExpiresAt", row)

    async def test_restart_restores_only_an_explicitly_enabled_shortcut_when_speech_is_ready(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_dictation, portable_dictation_status, restore_portable_shortcut

        class Dictation(PortableDictation):
            platform = "windows"
            enabled = False
            refuse = False

            async def enable(self):
                if self.refuse:
                    raise CaptureError("The shortcut could not register.")
                self.enabled = True

            async def disable(self):
                self.enabled = False

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation"
        app = FastAPI()
        state = app.state
        state.status = "ready"
        state.draining = False
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/{action}", change_portable_dictation, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            state.portable_recording_dir = Path(folder)
            history = PortableDictationHistory(folder)
            history.initialize()

            def reopen():
                state.portable_dictation = Dictation(state, None, history)
                state.portable_shortcut_ready = asyncio.Event()
                return state.portable_dictation

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                dictation = reopen()
                self.assertEqual((await browser.post(endpoint + "/enable", headers=headers)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                state.portable_shortcut_ready.set()
                await restore_portable_shortcut(state)
                self.assertFalse(dictation.enabled)
                response = await browser.post(endpoint + "/enable", headers=headers)
                self.assertTrue(response.raise_for_status().json()["shortcutRequested"])
                await dictation.disable()  # Runtime shutdown keeps the preference.

                dictation = reopen()
                restore = asyncio.create_task(restore_portable_shortcut(state))
                await asyncio.sleep(0)
                self.assertFalse(dictation.enabled)
                state.portable_shortcut_ready.set()
                await restore
                self.assertTrue(dictation.enabled)

                dictation = reopen()
                dictation.refuse = True
                state.portable_shortcut_ready.set()
                await restore_portable_shortcut(state)
                self.assertFalse(dictation.enabled)
                self.assertEqual(dictation.feedback, "The shortcut could not register.")
                self.assertTrue(dictation.shortcut_requested)

                response = await browser.post(endpoint + "/disable", headers=headers)
                self.assertFalse(response.raise_for_status().json()["shortcutRequested"])
                dictation = reopen()
                state.portable_shortcut_ready.set()
                await restore_portable_shortcut(state)
                self.assertFalse(dictation.enabled)

    async def test_portable_language_and_voice_input_are_saved_and_used_for_each_take(self):
        from dictation_input import CleanupFailed
        from linux_dictation import LinuxDictation
        from portable_dictation_history import PortableDictationHistory
        from server import (change_portable_language, change_portable_shortcut_mode, change_portable_voice_input,
                            portable_dictation_status, transcribe_portable_dictation)

        class Dictation(LinuxDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        async def capture_fixture(path):
            with private_wav(path) as (wav, _):
                wav.writeframes(b"\x01\x02" * 1600)

        def recorder(_key, path, _mode, **_options):
            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, stop=AsyncMock(),
                                   start=lambda: capture_fixture(path))

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/language", change_portable_language, methods=["POST"])
        app.add_api_route(endpoint + "/voice-input", change_portable_voice_input, methods=["POST"])
        app.add_api_route(endpoint + "/shortcut-mode", change_portable_shortcut_mode, methods=["POST"])
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder:
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder = recorder
            state.portable_recording = None
            state.draining = False
            state.status = "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
            transcript = {"text": "Bonjour, ceci est un essai.", "inferenceMs": 80}
            recognized = AsyncMock(return_value=transcript)
            cleanup = state.stupase_cleanup = Mock()
            cleanup.status.return_value = "downloading"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                with patch("server.URL", origin), patch("server.transcribe_dictation", recognized):
                    url = endpoint + "/language"
                    voice_url = endpoint + "/voice-input"
                    mode_url = endpoint + "/shortcut-mode"
                    boost = {"setting": "boostMicrophone", "enabled": True}
                    noise = {"setting": "noiseCleanup", "enabled": True}
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    defaults = (await browser.get(endpoint)).json()
                    self.assertEqual(defaults["shortcutMode"], "tap")
                    self.assertEqual((defaults["language"], defaults["noiseCleanup"], defaults["boostMicrophone"]),
                                     ("", False, False))
                    self.assertEqual((await browser.post(voice_url, json=noise)).status_code, 403)
                    self.assertEqual((await browser.post(voice_url, headers=headers,
                                                         json={**boost, "enabled": "false"})).status_code, 422)
                    self.assertEqual((await browser.post(url, json={"language": "fr"})).status_code, 403)
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": "never"})).status_code, 400)
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": "fr"})).json()["language"], "fr")
                    self.assertEqual((await browser.post(mode_url, headers=headers, json={"mode": "hold"})).json()["shortcutMode"], "hold")
                    dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                    self.assertEqual(dictation.language, "fr")
                    self.assertEqual(dictation.shortcut_mode, "hold")
                    await dictation.toggle()
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": "es"})).status_code, 409)
                    self.assertEqual((await browser.post(voice_url, headers=headers, json=boost)).status_code, 409)
                    self.assertEqual((await browser.post(mode_url, headers=headers, json={"mode": "tap"})).status_code, 409)
                    await dictation.toggle()
                    self.assertEqual(recognized.await_args.args[2], ("fr",))
                    self.assertTrue(recognized.await_args.kwargs["screen_nonspeech"])
                    self.assertFalse(recognized.await_args.kwargs["boost"])
                    self.assertNotIn("cleanup", recognized.await_args.kwargs)
                    self.assertEqual(history.page(legacy=True)["entries"][0]["text"], "Bonjour, ceci est un essai.")
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": ""})).json()["language"], "")
                    await dictation.toggle()
                    await dictation.toggle()
                    self.assertEqual(recognized.await_args.args[2], ())
                    self.assertEqual(history.language(), "")
                    status = (await browser.post(voice_url, headers=headers, json=boost)).json()
                    self.assertTrue(status["boostMicrophone"])
                    self.assertFalse(status["noiseCleanup"])
                    cleanup.prepare.assert_not_called()
                    status = (await browser.post(voice_url, headers=headers, json=noise)).json()
                    self.assertTrue(status["boostMicrophone"])
                    self.assertTrue(status["noiseCleanup"])
                    self.assertEqual(status["cleanupStatus"], "downloading")
                    cleanup.prepare.assert_called_once_with(retry=True)
                    dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                    for unusable in (CleanupFailed(), {"text": "", "inferenceMs": 80}):
                        recognized.reset_mock()
                        recognized.side_effect = [unusable, transcript]
                        await dictation.toggle()
                        await dictation.toggle()
                        first, fallback = recognized.await_args_list
                        self.assertIs(first.kwargs["cleanup"], cleanup)
                        self.assertNotIn("cleanup", fallback.kwargs)
                        self.assertTrue(first.kwargs["boost"] and fallback.kwargs["boost"])
                        self.assertEqual(first.args[1], fallback.args[1])
                        dictation.deliver.assert_awaited_with(transcript["text"])
                    status = (await browser.post(voice_url, headers=headers, json={**noise, "enabled": False})).json()
                    self.assertFalse(status["noiseCleanup"])
                    self.assertTrue(status["boostMicrophone"])

    async def test_portable_dictation_applies_saved_vocabulary_and_replacements_to_paste_and_history(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import (change_portable_profiles, change_portable_replacements, change_portable_vocabulary,
                            describe_portable_profiles, portable_dictation_status, preview_portable_profiles,
                            transcribe_portable_dictation)

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        pcm = b"\x01\x02" * 1600

        def recorder(_key, path, _mode, **_options):
            async def start():
                with private_wav(path) as (wav, _):
                    wav.writeframes(pcm)
            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, stop=AsyncMock(), start=start)

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        endpoint = "/api/portable/dictation"
        app = FastAPI()
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/vocabulary", change_portable_vocabulary, methods=["POST"])
        app.add_api_route(endpoint + "/replacements", change_portable_replacements, methods=["POST"])
        app.add_api_route(endpoint + "/output-profiles", change_portable_profiles, methods=["POST"])
        app.add_api_route(endpoint + "/output-profiles/describe", describe_portable_profiles, methods=["POST"])
        app.add_api_route(endpoint + "/output-profiles/preview", preview_portable_profiles, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
            vocabulary = [{"term": "Jaro Notebook", "soundsLike": ["red notebook"]}]
            utterance = "The red notebook and a blue wheel. red notebooks."
            expected = "The Jaro Notebook and a Bluewheel. red notebooks."
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                request = {"base": [], "entries": vocabulary}
                self.assertEqual((await browser.post(endpoint + "/vocabulary", headers=headers, json=request)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                async def set_rules(rules):
                    return await browser.post(endpoint + "/replacements", headers=headers, json={"text": rules})
                saved = await browser.post(endpoint + "/vocabulary", headers=headers, json=request)
                self.assertEqual(saved.raise_for_status().json()["vocabularyEntries"], vocabulary)
                self.assertEqual((await browser.post(endpoint + "/vocabulary", headers=headers, json=request)).status_code, 409)
                self.assertEqual((await set_rules("incorrect syntax")).status_code, 400)
                (await set_rules("blue => Azure\nblue wheel => Bluewheel\nBluewheel => Chained")).raise_for_status()
                dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                with patch("server.transcribe_dictation", new_callable=AsyncMock,
                           return_value={"text": utterance, "inferenceMs": 42}) as recognize:
                    await dictation.toggle()
                    (await set_rules("blue wheel => Changed during take")).raise_for_status()
                    await dictation.toggle()
                    self.assertEqual(recognize.await_args.args[3], ("Jaro Notebook",))
                    dictation.deliver.assert_awaited_with(expected)
                    saved = history.page(legacy=True, include_audio=True)["entries"][0]
                    self.assertEqual(saved["text"], expected)
                    with history.open_audio(saved["id"]) as (handle, _size), wave.open(handle, "rb") as clip:
                        self.assertEqual(clip.readframes(1600), pcm)

                (await set_rules("blue wheel => Bluewheel")).raise_for_status()
                async def set_profiles(rows, base=""):
                    profiles = json.dumps(rows)
                    return await browser.post(endpoint + "/output-profiles", headers=headers,
                                              json={"profiles": profiles, "base": base})

                profiles = [{"target": "default", "steps": ["lowercase"]},
                            {"target": "app:Example", "steps": ["rewrite", "lowercase"],
                             "vocabulary": ["Example project"], "corrections": "Bluewheel => Cog",
                             "instruction": "Remove filler while keeping all numbers."}]
                self.assertEqual((await set_profiles([{"target": "default", "steps": ["script"]}])).status_code, 400)
                self.assertEqual((await set_profiles([{"target": "site:example.net", "steps": ["lowercase"]}])).status_code, 400)
                self.assertEqual((await set_profiles([{"target": "default", "steps": ["rewrite"]}])).status_code, 400)
                (await set_profiles(profiles)).raise_for_status()
                self.assertEqual((await set_profiles([])).status_code, 409)
                preview = {"profiles": json.dumps(profiles), "app": "example", "text": "The blue wheel, 12 times."}
                simulated = await browser.post(endpoint + "/output-profiles/preview", headers=headers, json=preview)
                self.assertEqual(simulated.raise_for_status().json(), {
                    "profile": "app:Example", "steps": ["rewrite", "lowercase"], "skipped": ["rewrite"],
                    "text": "the cog, 12 times.", "warnings": [], "vocabulary": ["Example project", "Jaro Notebook"],
                })
                dictation.platform = "wayland"
                simulated = await browser.post(endpoint + "/output-profiles/preview", headers=headers, json=preview)
                self.assertEqual(simulated.raise_for_status().json()["profile"], "default")

                async def generate(*_args, **_kwargs):
                    yield json.dumps({"text": rewritten})

                model = SimpleNamespace(lock=asyncio.Lock(), ready=AsyncMock(), fit_prompt=AsyncMock(), generate=generate)
                state.insights = SimpleNamespace(chat_count=0, model=model)
                dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                capture_text = "The blue wheel, 12 times."
                with patch("server.transcribe_dictation", new_callable=AsyncMock,
                           return_value={"text": capture_text, "inferenceMs": 42}) as recognize:
                    for rewritten, delivered in (("Polished 12 times.", "polished 12 times."),
                                                ("Polished 13 times.", "the cog, 12 times.")):
                        dictation.application = "Example"
                        await dictation.toggle()
                        await dictation.toggle()
                        self.assertEqual(recognize.await_args.args[3], ("Example project", "Jaro Notebook"))
                        dictation.deliver.assert_awaited_with(delivered)
                        self.assertEqual(history.page(legacy=True)["entries"][0]["text"], delivered)
                    self.assertIn("changed a number", dictation.feedback)
                    self.assertEqual(state.insights.chat_count, 0)
                    self.assertEqual(model.ready.await_count, 2)
                    await dictation.toggle()
                    await dictation.toggle()
                    dictation.deliver.assert_awaited_with("the bluewheel, 12 times.")
                    self.assertEqual(model.ready.await_count, 2)

    @unittest.skipUnless(os.name == "posix", "uses an executable indicator fixture")
    async def test_dictation_indicator_observes_the_take_and_cannot_prevent_paste(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_indicator, portable_dictation_status

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Pasted.")

        def recorder(_key, path, _mode, **_options):
            worker = asyncio.get_running_loop().create_future()

            async def stop():
                if not worker.done():
                    worker.set_result(None)

            return SimpleNamespace(path=path, levels=(.60,), worker=worker, frames=1600, interrupted=False, stop=stop, start=AsyncMock())

        async def wait_for_line(path, line):
            async with asyncio.timeout(3):
                while not path.is_file() or line not in path.read_text().splitlines():
                    await asyncio.sleep(.02)

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        endpoint = "/api/portable/dictation"
        app = FastAPI()
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/indicator", change_portable_indicator, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            root = Path(folder)
            helper, activity = root / "indicator", root / "activity"
            helper.write_text("#!/usr/bin/env python3\n"
                              "from pathlib import Path\nimport sys\n"
                              f"with Path({str(activity)!r}).open('a', buffering=1) as log:\n"
                              "    print(sys.argv[1], file=log)\n"
                              "    for line in sys.stdin:\n"
                              "        print(line.strip(), file=log)\n"
                              "    print('closed', file=log)\n")
            helper.chmod(0o700)
            state = app.state
            state.library = SimpleNamespace(root=root)
            state.portable_recording_dir = root
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(root)
            history.initialize()
            history.configure(retention=0)
            processing = asyncio.Event()

            async def recognize(*_args, **_kwargs):
                await processing.wait()
                return {"text": "One take.", "inferenceMs": 42}

            with patch.dict(os.environ, {"JARO_DICTATION_INDICATOR": str(helper)}):
                dictation = state.portable_dictation = Dictation(state, recognize, history)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                async def position(value):
                    return await browser.post(endpoint + "/indicator", headers=headers, json={"position": value})

                self.assertEqual((await position("bottom")).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await position("cursor")).status_code, 422)
                self.assertTrue((await position("bottom")).raise_for_status().json()["indicatorAvailable"])
                await dictation.toggle()
                self.assertTrue(dictation.capture.measure_level)
                await wait_for_line(activity, "R60")
                self.assertEqual((await position("off")).status_code, 409)
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(dictation.toggle())
                    try:
                        await wait_for_line(activity, "P")
                    finally:
                        processing.set()
                self.assertEqual(activity.read_text().splitlines(), ["bottom", "R60", "P", "closed"])
                dictation.deliver.assert_awaited_once_with("One take.")
                self.assertEqual((await position("off")).raise_for_status().json()["indicatorPosition"], "off")
                await dictation.toggle()
                self.assertFalse(dictation.capture.measure_level)
                self.assertIsNone(dictation.indicator_task)
                await dictation.cancel_capture()
                (await position("top")).raise_for_status()
                helper.unlink()
                await dictation.toggle()
                await dictation.indicator_task
                self.assertFalse(dictation.snapshot()["indicatorAvailable"])
                await dictation.toggle()
                self.assertEqual(dictation.deliver.await_count, 2)

    async def test_dictation_sound_lifecycle_and_workspace_preferences(self):
        from portable_dictation import PASTE_SENT, PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from portable_dictation_sounds import SoundPreferences
        from server import change_portable_sound, preview_portable_sound

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value=PASTE_SENT)

        def recorder(_key, path, _mode, **_options):
            worker = asyncio.get_running_loop().create_future()

            async def stop():
                if not worker.done():
                    worker.set_result(None)

            return SimpleNamespace(path=path, worker=worker, frames=1600, interrupted=False, stop=stop, start=AsyncMock())

        events = []
        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        endpoint = "/api/portable/dictation/sound"
        app = FastAPI()
        app.add_api_route(endpoint, change_portable_sound, methods=["POST"])
        app.add_api_route(endpoint + "/preview", preview_portable_sound, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin), patch("portable_dictation.DONE_DELAY", 0):
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = state.library.root
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            history.configure(retention=0)
            async def transcribe(*_args, **_options):
                await asyncio.sleep(0)
                return {"text": "Hello.", "inferenceMs": 42}

            dictation = state.portable_dictation = Dictation(state, transcribe, history)
            self.assertEqual(dictation.sound, SoundPreferences())
            dictation.sounds.available = True

            async def play(cue, settings, delay, while_valid):
                if while_valid is None or while_valid():
                    events.append((cue, settings.volume, dictation.capture is None or dictation.capture.worker.done()))
                return True

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                with patch.object(dictation.sounds, "_play", side_effect=play):
                    async def change(values):
                        return await browser.post(endpoint, headers=headers, json=values)

                    self.assertEqual((await change({"style": "wood"})).status_code, 403)
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await change({"style": "../other"})).status_code, 422)
                    self.assertEqual((await change({"volume": 101})).status_code, 422)
                    (await change({"style": "wood", "volume": 41})).raise_for_status()
                    self.assertEqual(history.sound_preferences(), SoundPreferences(True, "wood", 41))
                    (await browser.post(endpoint + "/preview", headers=headers)).raise_for_status()
                    await dictation.toggle()
                    await dictation.sounds.task
                    self.assertEqual((await browser.post(endpoint + "/preview", headers=headers)).status_code, 409)
                    self.assertEqual((await change({"enabled": False})).status_code, 409)
                    await dictation.toggle()
                    await dictation.sounds.task
                    self.assertEqual(events, [("start", 41, True), ("start", 41, False),
                                              ("stop", 41, True), ("done", 41, True)])
                    await dictation.toggle()
                    await dictation.sounds.task
                    await dictation.cancel_capture(audible=True)
                    await dictation.sounds.task
                    self.assertEqual(events[-1], ("cancel", 41, True))
                    previous = len(events)
                    (await change({"enabled": False})).raise_for_status()
                    await dictation.toggle()
                    await dictation.toggle()
                    self.assertEqual(len(events), previous)
                    self.assertEqual(dictation.deliver.await_count, 2)
                    (await change({"enabled": True})).raise_for_status()
                with patch.object(dictation.sounds, "_play", return_value=False):
                    await dictation.toggle()
                    self.assertFalse(await dictation.sounds.task)
                    await dictation.toggle()
                    await dictation.sounds.task
                    self.assertEqual(dictation.deliver.await_count, 3)

    async def test_cancel_discards_a_processing_take_and_never_interrupts_delivery(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_dictation

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        microphone_open = asyncio.Event()
        inference = asyncio.Event()
        release_inference = asyncio.Event()
        delivery = asyncio.Event()
        release_delivery = asyncio.Event()

        def recorder(_key, path, _mode, **_options):
            async def start():
                await microphone_open.wait()

            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, start=start, stop=AsyncMock())

        async def transcribe(*_args, **_kwargs):
            inference.set()
            await release_inference.wait()
            return {"text": "Keep this text.", "inferenceMs": 20}

        async def retain(*_args):
            delivery.set()
            await release_delivery.wait()
            return ""

        async def wait_until(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        app = FastAPI()
        app.add_api_route("/api/portable/dictation/{action}", change_portable_dictation, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = state.portable_dictation = Dictation(state, transcribe, history)
            dictation.retain = AsyncMock(side_effect=retain)
            dictation.text = "Previous dictation."
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                async def cancel():
                    return await browser.post("/api/portable/dictation/cancel", headers=headers)

                self.assertEqual((await cancel()).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await cancel()).status_code, 409)
                dictation.schedule()
                await wait_until(lambda: dictation.phase == "starting")
                self.assertTrue(dictation.can_cancel)
                stopped = (await cancel()).raise_for_status().json()
                self.assertEqual(stopped["phase"], "idle")
                self.assertFalse(stopped["canCancel"])
                self.assertEqual(stopped["message"], "Dictation canceled.")
                self.assertIsNone(dictation.capture)
                self.assertFalse(list(Path(folder).glob(".dictation-*")))

                microphone_open.set()
                dictation.schedule()
                await dictation.action
                dictation.schedule()
                await asyncio.wait_for(inference.wait(), 3)
                self.assertEqual(dictation.phase, "transcribing")
                (await cancel()).raise_for_status()
                dictation.retain.assert_not_awaited()
                dictation.deliver.assert_not_awaited()
                self.assertEqual(dictation.text, "Previous dictation.")

                release_inference.set()
                dictation.schedule()
                await dictation.action
                dictation.schedule()
                try:
                    await asyncio.wait_for(delivery.wait(), 3)
                    self.assertEqual(dictation.phase, "delivering")
                    self.assertFalse(dictation.can_cancel)
                    self.assertEqual((await cancel()).status_code, 409)
                finally:
                    release_delivery.set()
                    await dictation.output_task
                dictation.retain.assert_awaited_once()
                dictation.deliver.assert_awaited_once_with("Keep this text.")

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

    async def test_only_detected_meeting_passes_call_executables_to_windows_recorder(self):
        helper = Path("jaro-capture.exe")
        path = Path("capture.wav")
        with (patch("windows_capture.helper_path", return_value=helper),
              patch("windows_capture.subprocess.CREATE_NO_WINDOW", 0, create=True),
              patch("windows_capture.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn):
            await WindowsMeetingRecording(uuid4(), path, "both", seconds=120).spawn()
            await WindowsMeetingRecording(uuid4(), path, "both", seconds=120,
                                          call_binaries=("chrome", "google-chrome")).spawn()
        self.assertEqual([call.args for call in spawn.await_args_list], [
            (helper, "meeting", "120"),
            (helper, "meeting", "120", "call", "chrome.exe", "google-chrome.exe"),
        ])

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

    async def test_stupase_worker_passes_silent_chunks_and_guards_stale_retire(self):
        from dictation_input import StuPASECleanup
        from stupase_runner import HEADER

        with TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup = StuPASECleanup(root)
            worker_script = (
                "import struct, sys\n"
                "h = struct.Struct('<I')\n"
                "sys.stdout.buffer.write(h.pack(0)); sys.stdout.buffer.flush()\n"
                "while hdr := sys.stdin.buffer.read(4):\n"
                "    n, = h.unpack(hdr)\n"
                "    data = sys.stdin.buffer.read(n)\n"
                "    sys.stdout.buffer.write(h.pack(n) + data); sys.stdout.buffer.flush()\n"
            )
            fake_runner = root / "fake_runner.py"
            fake_runner.write_text(worker_script, encoding="utf-8")
            pcm = np.zeros(160, dtype="<f4").tobytes()
            with patch("dictation_input.checkpoints_ready", return_value=True), patch("dictation_input.RUNNER", fake_runner):
                try:
                    self.assertEqual(await cleanup.clean(pcm), pcm)
                    first_pid = cleanup.worker.pid
                    stale_gen = cleanup.worker_generation - 1
                    await cleanup.retire_worker(stale_gen)
                    self.assertIsNotNone(cleanup.worker)
                    self.assertEqual(cleanup.worker.pid, first_pid)
                finally:
                    await cleanup.close()

    async def test_linux_meeting_starts_with_silent_playback_monitor(self):
        from linux_meeting_audio import LinuxMeetingRecording, Source

        with TemporaryDirectory() as directory:
            recording = LinuxMeetingRecording(uuid4(), Path(directory) / "meeting.wav", "both", seconds=10)
            recording.epoch = asyncio.get_running_loop().time()

            class FakeSource:
                def __init__(self, name, *, received=False, optional=False):
                    self.name = name
                    self.optional = optional
                    self.received = received
                    self.task = asyncio.create_task(asyncio.sleep(10))

                def add(self, target, _start):
                    if not self.optional:
                        target[:] = 100

            async def watch_routes():
                recording.mic = FakeSource("mic", received=True, optional=False)
                recording.outputs["monitor"] = FakeSource("monitor", received=False, optional=True)
                await asyncio.sleep(10)

            recording.watch_routes = watch_routes
            blocks = recording.capture_blocks(640, 4)
            try:
                block = await asyncio.wait_for(anext(blocks), 1)
                self.assertEqual(len(block), 640)
            finally:
                recording.end_frame = 0
                await blocks.aclose()

    def test_tray_quit_focuses_workspace_while_busy_and_windows_encoder_falls_back(self):
        from portable_tray import MeetingTray
        from screen_draft import windows_h264_encoder

        with TemporaryDirectory() as directory:
            tray_dir = Path(directory)
            shutdown = Mock()
            state = SimpleNamespace(request_shutdown=shutdown)
            busy_flag = True
            tray = MeetingTray(state, tray_dir, lambda: busy_flag, lambda: None, lambda _: "", AsyncMock())
            tray.submit("quit")
            shutdown.assert_not_called()
            self.assertTrue((tray_dir / "open").is_file())
            busy_flag = False
            tray.submit("quit")
            shutdown.assert_called_once()

        with patch("screen_draft.subprocess.check_output", return_value=" V..... libopenh264 Cisco OpenH264\n"):
            self.assertEqual(windows_h264_encoder(), "libopenh264")
            self.assertEqual(windows_h264_encoder(prefer_software=True), "libopenh264")
