import asyncio
import fcntl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from desktop.mac_bridge import Capture, MacBridge
from meeting_brief import LiveTitlePlan, clean_title, set_automatic
from meeting_rolling import MIXED_SPEAKERS, RATE, append_rolling
from native_recordings import read_meeting_title, recording_marker, write_meeting_title, write_recording_metadata
from session_store import SessionStore


def turn(words, start, end):
    return {"text": "word " * words, "start": start, "end": end}


class LiveTitlePlanTests(unittest.TestCase):
    def test_title_preserves_joiners_used_by_the_spoken_language(self):
        self.assertEqual(clean_title("برنامه‌ریزی محصول"), "برنامه‌ریزی محصول")
        with self.assertRaises(ValueError):
            clean_title("Review\x00 meeting")

    def test_speech_threshold_word_threshold_refresh_and_attempt_cap(self):
        plan = LiveTitlePlan()
        plan.observe(200, [turn(59, 0, 92)])
        self.assertFalse(plan.due())
        plan.observe(300, [turn(1, 99, 100)])
        self.assertTrue(plan.due())
        plan.record(published=False, timeline=100)
        plan.observe(400, [turn(55, 135, 159)])
        self.assertFalse(plan.due())
        plan.observe(500, [turn(1, 159, 160)])
        self.assertTrue(plan.due())
        plan.record(published=True, timeline=160)
        plan.observe(600, [turn(70, 595, 599)])
        self.assertFalse(plan.due())
        plan.observe(700, [turn(1, 599, 600)])
        self.assertTrue(plan.due())
        plan.record(published=False, timeline=600)
        plan.observe(800, [turn(100, 660, 700)])
        self.assertTrue(plan.finished)
        self.assertFalse(plan.due())

        words = LiveTitlePlan()
        words.observe(100, [turn(599, 100, 150)])
        self.assertFalse(words.due(), "silence before the first turn does not count")
        words.observe(200, [turn(1, 150, 151)])
        self.assertTrue(words.due())
        words.record(published=True, timeline=151)
        words.observe(300, [turn(20, 598, 600)])
        words.record(published=True, timeline=600)
        self.assertTrue(words.finished)


class BridgeLiveTitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "stage"
        self.bridge = object.__new__(MacBridge)
        self.bridge.capture_lock = asyncio.Lock()
        self.bridge.dictation_jobs = set()
        plan = LiveTitlePlan()
        plan.observe(1234, [turn(600, 0, 92)])
        self.take = Capture(SimpleNamespace(returncode=0), {"id": "stage", "path": str(self.path)}, None,
                            {}, {"meeting": "active", "meetingStartedAt": "2026-01-01T00:00:00Z",
                                 "meetingLabel": "Zoom", "seconds": 92}, False, started=True, title_plan=plan)
        self.bridge.captures = {"recording": self.take}

    async def test_dictation_delays_title_then_accepted_title_reaches_disk_live_reads_and_event(self):
        ready = asyncio.Event()
        self.bridge.dictation_jobs.add("dictating")

        async def api(method, route, **options):
            self.assertEqual(method, "POST")
            self.assertEqual(route, "native/staging/stage/meeting-title")
            self.assertEqual(options["params"], {"through": 1234})
            return {"title": "  Launch   review  "}

        def published(*args, **kwargs):
            self.assertEqual((args, kwargs), (("meetingTitle",), {"meeting": "active", "title": "Launch review"}))
            self.assertEqual(read_meeting_title(self.path), "Launch review")
            ready.set()

        self.bridge.api = api
        with patch("desktop.mac_bridge.LIVE_TITLE_INTERVAL", .005), patch("desktop.mac_bridge.emit", side_effect=published):
            self.take.title_job = asyncio.create_task(self.bridge.live_titles(self.take))
            try:
                await asyncio.sleep(.03)
                self.assertFalse(ready.is_set())
                self.assertEqual(self.take.title_plan.attempts, 0)
                self.bridge.dictation_jobs.clear()
                await asyncio.wait_for(ready.wait(), 1)
                self.assertEqual(self.bridge.live_meeting(turns=False)["name"], "Launch review")
            finally:
                await self.bridge.stop(self.take)
                await asyncio.gather(self.take.title_job, return_exceptions=True)

    async def test_stop_fences_a_late_response_and_keeps_the_earlier_title(self):
        requested = asyncio.Event()
        earlier = write_meeting_title(self.path, "Original title")
        self.take.state["provisionalTitle"] = earlier

        async def api(*args, **kwargs):
            requested.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return {"title": "Late title"}

        self.bridge.api = api
        with patch("desktop.mac_bridge.LIVE_TITLE_INTERVAL", .005), patch("desktop.mac_bridge.emit") as emitted:
            self.take.title_job = asyncio.create_task(self.bridge.live_titles(self.take))
            await asyncio.wait_for(requested.wait(), 1)
            await self.bridge.stop(self.take)
            await asyncio.wait_for(self.take.title_job, 1)
            emitted.assert_not_called()
        self.assertEqual(read_meeting_title(self.path), earlier)
        self.assertEqual(self.bridge.live_meeting(turns=False)["name"], earlier)

    async def test_engine_releases_the_shared_writer_on_disconnect(self):
        import server

        entered = asyncio.Event()
        available = asyncio.Event()
        available.set()

        async def ready():
            entered.set()
            await asyncio.Future()

        async def disconnected():
            return entered.is_set()

        model = SimpleNamespace(ready=ready, available=available, lock=asyncio.Lock())
        key = uuid4()
        state = SimpleNamespace(draining=False, submitting=set(), insights=SimpleNamespace(model=model),
                                staging=self.path.parent, native_token="fixture", jobs=set(),
                                library=SessionStore(self.path.parent / "library"))
        request = SimpleNamespace(app=SimpleNamespace(state=state), headers={"x-jard-token": "fixture"},
                                  is_disconnected=disconnected)
        (state.staging / str(key)).touch()
        with patch("server.staged_meeting_title_rows", return_value=[{"start": 0, "text": "Review the launch."}]):
            result = await asyncio.wait_for(server.write_staged_meeting_title(request, key, through=1234), 2)
        self.assertEqual(result.status_code, 499)
        self.assertFalse(model.lock.locked())
        self.assertFalse(state.jobs)

    async def test_engine_uses_the_language_of_spoken_turns_within_the_requested_window(self):
        import server

        available = asyncio.Event()
        available.set()
        model = SimpleNamespace(ready=AsyncMock(), available=available, lock=asyncio.Lock(),
                                structured=AsyncMock(return_value={"title": "مراجعة الإطلاق"}))
        key = uuid4()
        state = SimpleNamespace(draining=False, submitting=set(), insights=SimpleNamespace(model=model),
                                staging=self.path.parent, native_token="fixture", jobs=set(),
                                library=SessionStore(self.path.parent / "library"))
        request = SimpleNamespace(app=SimpleNamespace(state=state), headers={"x-jard-token": "fixture"},
                                  is_disconnected=AsyncMock(return_value=False))
        path = state.staging / str(key)
        path.touch()
        for index, (language, text) in enumerate((
            ("English", ""), ("Arabic", "قرر الفريق إطلاق المنتج يوم الثلاثاء."),
            ("Hebrew", "הצוות דן במועד ההשקה."),
        )):
            segments = [{"speaker": 1, "start": index, "end": index + 1, "text": text}] if text else []
            append_rolling(path, index * RATE, (index + 1) * RATE,
                           {"language": language, "text": text, "segments": segments, "words": [], "activity": [],
                            "meetingSpeakerMode": MIXED_SPEAKERS})
        with recording_marker(path).open("w+b") as marker:
            fcntl.flock(marker, fcntl.LOCK_EX)
            write_recording_metadata(marker, {"meetingProvider": "Fixture"})
            result = await server.write_staged_meeting_title(request, key, through=2 * RATE)
            self.assertEqual(result, {"title": "مراجعة الإطلاق"})
            instructions, speech = model.structured.call_args.args[:2]
            self.assertIn("response text in Arabic.", instructions)
            self.assertIn("Speaker 1: قرر الفريق", speech)
            self.assertNotIn("הצוות", speech)
            await server.write_staged_meeting_title(request, key, through=3 * RATE)
            instructions, speech = model.structured.call_args.args[:2]
            self.assertIn("Keep the original language.", instructions)
            self.assertNotIn("Write the response text in", instructions)
            self.assertIn("Speaker 1: הצוות", speech)

    async def test_engine_discards_a_title_if_automatic_notes_change_during_generation(self):
        import server

        entered, release, available = asyncio.Event(), asyncio.Event(), asyncio.Event()
        available.set()

        async def ready():
            pass

        async def disconnected():
            return False

        async def generate(model, rows):
            entered.set()
            await release.wait()
            return "Launch plan"

        model = SimpleNamespace(ready=ready, available=available, lock=asyncio.Lock())
        key = uuid4()
        state = SimpleNamespace(draining=False, submitting=set(), insights=SimpleNamespace(model=model),
                                staging=self.path.parent, native_token="fixture", jobs=set(),
                                library=SessionStore(self.path.parent / "library"))
        request = SimpleNamespace(app=SimpleNamespace(state=state), headers={"x-jard-token": "fixture"},
                                  is_disconnected=disconnected)
        (state.staging / str(key)).touch()
        with patch("server.staged_meeting_title_rows", return_value=[{"start": 0, "text": "Review the launch."}]) as read, \
                patch("meeting_brief.generate_live_title", side_effect=generate):
            job = asyncio.create_task(server.write_staged_meeting_title(request, key, through=1234))
            await asyncio.wait_for(entered.wait(), 2)
            with state.library.connection() as db:
                set_automatic(db, False)
            self.assertEqual(await server.write_staged_meeting_title(request, key, through=1234), {"automatic": False})
            self.assertEqual(read.call_count, 1)
            with state.library.connection() as db:
                set_automatic(db, True)
            release.set()
            self.assertEqual(await asyncio.wait_for(job, 2), {"automatic": False})
            self.assertEqual(await server.write_staged_meeting_title(request, key, through=1234), {"title": "Launch plan"})
