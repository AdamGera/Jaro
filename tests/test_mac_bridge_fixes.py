import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import desktop.mac_bridge as mac_bridge


class MacBridgeLiveToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_meeting_live_preview_continues_when_live_display_toggled_off(self):
        with TemporaryDirectory() as temp:
            wav = Path(temp) / "capture.wav"
            # 44-byte WAV header + 4 seconds of 48 kHz mono 16-bit PCM
            wav.write_bytes(b"\x00" * (44 + 2 * 4 * 48_000))

            bridge = mac_bridge.MacBridge(Path(temp), SimpleNamespace())
            meeting_id = str(uuid4())
            copy_state = {"through": 0, "changed": asyncio.Event()}
            take = mac_bridge.Capture(
                child=None,
                stage={"id": "stage-1", "path": str(wav)},
                marker=None,
                settings={"languageHints": ["en"]},
                state={"live": False, "meeting": meeting_id, "meetingTurns": [], "text": "", "segments": []},
                global_dictation=False,
                started=True,
                copy_state=copy_state,
            )

            preview_response = {
                "text": "hello meeting",
                "segments": [{"speaker": 0, "text": "hello meeting", "start": 0.0, "end": 4.0}],
            }
            emitted = []
            appended = []

            async def fake_api(method, path, **kwargs):
                self.assertEqual(method, "POST")
                self.assertIn("native/staging/stage-1/preview", path)
                return preview_response

            async def fast_sleep(_delay):
                if copy_state["through"] > 0:
                    raise asyncio.CancelledError()

            with (
                patch.object(bridge, "api", side_effect=fake_api) as api_mock,
                patch.object(mac_bridge, "emit", side_effect=lambda *a, **kw: emitted.append((a, kw))),
                patch.object(mac_bridge, "append_rolling", side_effect=lambda *a: appended.append(a)),
                patch.object(mac_bridge.asyncio, "sleep", side_effect=fast_sleep),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await bridge.live_preview(take)

            api_mock.assert_awaited_once()
            self.assertEqual(len(appended), 1)
            self.assertEqual(copy_state["through"], 4 * 48_000)
            self.assertTrue(copy_state["changed"].is_set())
            self.assertEqual(take.state["text"], "hello meeting")
            self.assertEqual(len(take.state["meetingTurns"]), 1)
            self.assertTrue(any(args == ("preview",) for args, _ in emitted))

    async def test_non_meeting_recording_skips_preview_when_live_off(self):
        with TemporaryDirectory() as temp:
            wav = Path(temp) / "capture.wav"
            wav.write_bytes(b"\x00" * (44 + 2 * 4 * 48_000))

            bridge = mac_bridge.MacBridge(Path(temp), SimpleNamespace())
            take = mac_bridge.Capture(
                child=None,
                stage={"id": "stage-2", "path": str(wav)},
                marker=None,
                settings={"languageHints": ["en"]},
                state={"live": False, "text": "", "segments": []},
                global_dictation=False,
                started=True,
                copy_state=None,
            )

            sleeps = 0

            async def fast_sleep(_delay):
                nonlocal sleeps
                sleeps += 1
                if sleeps > 2:
                    raise asyncio.CancelledError()

            with (
                patch.object(bridge, "api", new_callable=AsyncMock) as api_mock,
                patch.object(mac_bridge.asyncio, "sleep", side_effect=fast_sleep),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await bridge.live_preview(take)

            api_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
