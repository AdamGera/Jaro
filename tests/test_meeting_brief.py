import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import meeting_brief
from insights import Insights
from local_qwen import LocalQwen
from saved_meetings import get_meeting, meeting_detail, meetings, render_export, search_meetings
from session_store import SessionStore
from writing_api import router


class MeetingBriefTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SessionStore(Path(temp.name))

    def create(self, text="The committee approved the Tuesday launch.", complete=True):
        source = self.store.root / "fixture.wav"
        source.write_bytes(b"meeting fixture")
        key, _ = self.store.create(source, "Fixture.wav", "audio/wav",
            {"meeting": True, "recording": True, "diarization": False, "languageHints": ["en"]}, 10)
        self.store.append(key, {"text": text, "words": [], "language": "English",
                               "segments": [{"speaker": 1, "start": 0, "end": 10, "text": text}]}, 10)
        if complete:
            self.store.finish(key)
        return key

    def test_generated_source_includes_opening_middle_and_end_within_budget(self):
        rows = [{"start": index * 59, "text": f"Turn {index:03} " + "word " * 110} for index in range(90)]
        result = meeting_brief.excerpt(rows, 12_000)
        self.assertLessEqual(len(result), 12_000)
        self.assertIn("Turn 000", result)
        self.assertIn("Turn 089", result)
        self.assertRegex(result, r"Turn 0[345][0-9]")
        self.assertIn("[…]", result)
        self.assertIn("[87:31]", result)

    def test_finish_queues_and_regeneration_fences_old_results(self):
        key = self.create()
        reader = SessionStore(self.store.root, read_only=True)
        self.assertEqual(reader.get(key)["meetingBrief"]["status"], "queued")
        with self.store.connection() as db:
            meeting_brief.settle(db, key, 1, result={"title": "Tuesday launch", "summary": "The committee approved the launch."})
            meeting_brief.enqueue(db, key)
            meeting_brief.enqueue(db, key)
            self.assertEqual(meeting_brief.next_job(db)["generation"], 2)
            meeting_brief.settle(db, key, 1, result={"title": "Stale", "summary": "Discard me."})
            meeting_brief.settle(db, key, 2, error="The local writer was unavailable.")
        record = get_meeting(reader, key)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["name"], "Tuesday launch")
        self.assertEqual(record["meetingBrief"]["error"], "The local writer was unavailable.")
        self.assertEqual(reader.list("Tuesday launch", meetings_only=True)[0]["id"], key)
        detail = meeting_detail(record)
        self.assertEqual(detail["notes"]["summary"], "The committee approved the launch.")
        self.assertIn("## Summary", render_export(detail, "md"))
        self.assertLess(render_export(detail, "md").index("## Summary"), render_export(detail, "md").index("## Transcript"))
        self.assertIn("Jard’s local Qwen", render_export(detail, "notes"))
        self.assertTrue(any(hit["source"] == "summary" for hit in search_meetings(reader, "approved")))
        with self.store.connection() as db:
            db.execute("""UPDATE sessions
                SET options=json_set(options,'$.meetingCalendar',json_object('title','Planning call'))
                WHERE id=?""", (key,))
        self.assertEqual(meetings(reader, "Tuesday launch")[0]["name"], "Planning call")
        with_calendar = meeting_detail(get_meeting(reader, key))
        self.assertEqual(with_calendar["summaryTitle"], "Tuesday launch")
        self.assertIn("**Tuesday launch**", render_export(with_calendar, "md"))

    def test_manual_route_queues_older_meeting_and_protects_pending_work_from_expiry(self):
        key = self.create()
        empty = self.create(text="")
        recording = self.create(complete=False)
        with self.store.connection() as db:
            db.execute("DELETE FROM meeting_briefs WHERE id=?", (key,))
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
        app = FastAPI()
        app.include_router(router)
        wake = asyncio.Event()
        app.state.library = self.store
        app.state.insights = SimpleNamespace(wake=wake)
        app.state.draining = False
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            route = f"/api/sessions/{key}/meeting-brief"
            self.assertIsNone(client.get(route).json())
            self.assertEqual(client.post(route, headers={"Origin": "http://another-app.test"}).status_code, 403)
            for invalid in (empty, recording):
                self.assertEqual(client.post(f"/api/sessions/{invalid}/meeting-brief").status_code, 409)
            self.assertEqual(client.post(route).json()["status"], "queued")
            self.assertTrue(wake.is_set())
            with self.store.connection() as db:
                db.execute("UPDATE sessions SET created='2000-01-01T00:00:00Z' WHERE id=?", (key,))
                self.assertIsNone(db.execute("SELECT id FROM expired_meetings WHERE id=?", (key,)).fetchone())
                meeting_brief.settle(db, key, 1, error="Fixture")
                self.assertIsNotNone(db.execute("SELECT id FROM expired_meetings WHERE id=?", (key,)).fetchone())

    def test_turning_off_automatic_notes_fences_jobs_and_keeps_manual_writing(self):
        pending = self.create()
        published = self.create()
        manual = self.create()
        current_recording = self.create(complete=False)
        with self.store.connection() as db:
            self.assertEqual(meeting_brief.automatic_policy(db), (True, 0))
            meeting_brief.settle(db, published, 1, {"title": "Tuesday launch", "summary": "The launch was approved."})
            meeting_brief.enqueue(db, published, refinalize=True, automatic=True)
            meeting_brief.enqueue(db, manual)
            meeting_brief.enqueue(db, manual, refinalize=True, automatic=True)
            meeting_brief.set_automatic(db, False)
            meeting_brief.set_automatic(db, False)

        reopened = SessionStore(self.store.root)
        reopened.finish(current_recording)
        self.assertIsNone(reopened.get(current_recording)["meetingBrief"])
        self.assertIsNone(reopened.get(pending)["meetingBrief"])
        self.assertEqual(reopened.get(published)["meetingBrief"]["status"], "ready")
        self.assertEqual(reopened.get(published)["meetingBrief"]["title"], "Tuesday launch")
        self.assertEqual(reopened.get(manual)["meetingBrief"]["status"], "queued")
        with reopened.connection() as db:
            self.assertEqual(meeting_brief.automatic_policy(db), (False, 1))
            meeting_brief.enqueue(db, pending)
            meeting_brief.settle(db, pending, 1, {"title": "Stale result"})
            self.assertIsNone(meeting_brief.read(db, pending)["title"])
            meeting_brief.settle(db, pending, 3, {"title": "Manual title", "summary": "The committee approved it."})
            meeting_brief.set_automatic(db, True)
        self.assertEqual(reopened.get(pending)["meetingBrief"]["title"], "Manual title")
        self.assertEqual(reopened.get(self.create())["meetingBrief"]["status"], "queued")

    @unittest.skipUnless(os.environ.get("JARD_TEST_REAL_MEETING_BRIEF") == "1", "requires the installed local writing model")
    def test_local_qwen_can_summarize_a_single_fact(self):
        async def run():
            model = LocalQwen()
            try:
                await model.ready()
                with patch("meeting_brief.editorial_context", return_value={}):
                    brief = await meeting_brief.generate(model, [
                        {"start": 0, "text": "Them: The books for the library arrive on Monday."},
                    ], None, language="English")
                    hebrew = await meeting_brief.generate(model, [
                        {"start": 0, "text": "Speaker B: הספרים החדשים לספרייה יגיעו ביום שני."},
                    ], None, language="Hebrew")
                    arabic = await meeting_brief.generate(model, [
                        {"start": 0, "text": "Speaker B: ستصل كتب المكتبة يوم الإثنين."},
                    ], None, language="English")
                    live_arabic = await meeting_brief.generate_live_title(model, [
                        {"start": 0, "text": "Speaker 1: قرر الفريق إطلاق المنتج يوم الثلاثاء.", "language": "Arabic"},
                    ])
                    live_hebrew = await meeting_brief.generate_live_title(model, [
                        {"start": 0, "text": "Speaker 1: ועדת השיווק אישרה להתחיל את הקמפיין ביום שלישי.", "language": "Hebrew"},
                    ])
                self.assertIn("Monday", brief["summary"])
                self.assertNotRegex(brief["summary"], r"(?i)following week|new collection|\[\d")
                meeting_brief.validate(brief)
                self.assertRegex(hebrew["title"], r"[א-ת]")
                self.assertIn("שני", hebrew["summary"])
                self.assertRegex(arabic["title"], r"[\u0600-\u06ff]")
                self.assertRegex(arabic["summary"], r"[إا]ثنين")
                self.assertRegex(live_arabic, r"[\u0600-\u06ff]")
                self.assertRegex(live_hebrew, r"[א-ת]")
            finally:
                await model.close()
        asyncio.run(run())

    @unittest.skipUnless(os.environ.get("JARD_TEST_REAL_MEETING_BRIEF") == "1", "requires the installed local writing model")
    def test_local_qwen_writes_and_publishes_a_hebrew_meeting_summary(self):
        async def run():
            key = self.create("ועדת השיווק נפגשה כדי לקבוע את מועד ההשקה. הוועדה החליטה להשיק ביום שלישי. "
                              "נועה תשלח את לוח הזמנים לכל הצוות ביום ראשון.")
            with self.store.connection() as db:
                db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
            with patch("meeting_brief.editorial_context", return_value={}):
                worker = Insights(self.store)
                try:
                    async with asyncio.timeout(95):
                        while (result := self.store.get(key)["meetingBrief"])["status"] == "queued":
                            await asyncio.sleep(.05)
                    self.assertEqual(result["status"], "ready", result)
                    self.assertRegex(result["title"], r"[א-ת]")
                    self.assertRegex(result["summary"], r"[א-ת]")
                    self.assertIn("שלישי", result["summary"])
                    self.assertEqual(self.store.get(key)["name"], result["title"])
                finally:
                    await worker.close()
        asyncio.run(run())
