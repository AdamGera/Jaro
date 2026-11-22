import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import meeting_brief
import meeting_exports
from saved_meetings import get_meeting, latest_transcript, meeting_detail, parse_offset, render_export
from meeting_retention import archive_path
from session_store import MissingSession, SessionStore


class MeetingExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = SessionStore(self.root / "library")
        self.folder = self.root / "Meeting exports"
        self.folder.mkdir()

    def choose(self, folder):
        with self.store.connection() as db:
            return meeting_exports.set_folder(db, self.store.root, str(folder) if folder is not None else None)

    def meeting(self, *, complete=True, meeting=True):
        source = self.root / "source.wav"
        source.write_bytes(b"fixture")
        key, _ = self.store.create(source, "Review.wav", "audio/wav", {
            "meeting": meeting, "meetingStartedAt": "2026-09-17T08:15:00Z", "meetingCalendar": {"title": "Agenda / review"},
            "recording": True, "diarization": False, "languageHints": ["en"],
        }, 8)
        self.store.append(key, {"text": "Review the project timeline.", "words": [], "language": "English",
                               "segments": [{"speaker": 1, "start": 1, "end": 7, "text": "Review the project timeline."}]}, 8)
        if complete:
            self.store.finish(key)
        return key

    def summarize(self, key, title, summary, *, regenerate=False):
        with self.store.connection() as db:
            if regenerate:
                meeting_brief.enqueue(db, key)
            generation = db.execute("SELECT generation FROM meeting_briefs WHERE id=?", (key,)).fetchone()[0]
            self.assertTrue(meeting_brief.settle(db, key, generation, {"title": title, "summary": summary}))

    def test_markdown_clock_uses_recording_start_without_changing_speech_timing(self):
        self.choose(self.folder)
        with self.store.connection() as db:
            self.assertFalse(meeting_exports.clock_24(db))
            meeting_exports.set_clock_24(db, True)
            meeting_exports.set_separate_notes(db, True)
        key = self.meeting()
        detail = meeting_detail(self.store.get(key, include_turns=True))
        started = datetime.fromisoformat(detail["startedAt"]).astimezone()
        day = started.date().isoformat()
        clock_12 = f"{started.hour % 12 or 12}:{started.minute:02d} {'AM' if started.hour < 12 else 'PM'}"
        self.assertIn(f"- **Date:** {day}, {clock_12}", render_export(detail, "md"))
        self.summarize(key, "Project timeline", "The group reviewed the timeline.")
        meeting_exports.flush(self.store)
        expected = f"- **Date:** {day}, {started:%H:%M}"
        for suffix in ("transcript.md", "notes.md"):
            path, = self.folder.glob(f"*.{suffix}")
            self.assertIn(expected, path.read_text())
        self.assertEqual(render_export(detail, "srt"), render_export(detail, "srt", use_24_hour=True))
        self.assertEqual(render_export(detail, "vtt"), render_export(detail, "vtt", use_24_hour=True))

    def test_transcript_offsets_and_live_range_boundaries(self):
        self.assertEqual(parse_offset("113:03.125"), 6_783_125)
        self.assertEqual(parse_offset("1:53:03.125"), 6_783_125)
        self.assertEqual(parse_offset("1.25h"), 4_500_000)
        self.assertEqual(parse_offset("1.5 m"), 90_000)
        self.assertEqual(parse_offset(1.0005), 1001)
        self.assertIsNone(parse_offset(" "))
        for invalid in ("-0", "1:60", "1.5:03", "1:60:05", "inf", "1e30s"):
            with self.subTest(offset=invalid), self.assertRaises(ValueError):
                parse_offset(invalid)
        with self.assertRaisesRegex(ValueError, "after the start"):
            meeting_detail(self.store.get(self.meeting()), end="0.0001")

        started = (datetime.now(timezone.utc) - timedelta(seconds=8)).isoformat()
        live = {"id": str(uuid4()), "name": "Live review", "created": started, "meetingStartedAt": started,
                "status": "recording", "duration": 1, "meeting": True,
                "meetingTurns": [{"speaker": 1, "start": .25, "end": .75, "text": "Welcome."}]}
        session = get_meeting(self.store, live["id"], live=live)
        detail = meeting_detail(session, start="2s", end="4s")
        self.assertEqual(detail["range"], {"from_ms": 2000, "to_ms": 4000})
        self.assertEqual(detail["turns"], [])
        self.assertEqual(meeting_detail(session, end=".75")["turns"][0]["text"], "Welcome.")
        self.assertEqual(meeting_detail(session, end=".25")["turns"], [])
        self.assertEqual(meeting_detail(session, start=".75")["turns"], [])
        with self.assertRaisesRegex(ValueError, "so far"):
            meeting_detail(session, start="1h")
        with self.assertRaisesRegex(ValueError, "meeting's end"):
            meeting_detail(self.store.get(self.meeting()), start="8s")

    def test_subtitles_omit_empty_turns_and_escape_text_and_speaker_names(self):
        detail = {"title": "Review", "turns": [
            {"start": 0., "end": .5, "speaker": "Speaker A", "text": " \n "},
            {"start": 1., "end": 2.25, "speaker": "<Review> & I", "text": "Hello\n\n A --> B"},
            {"start": 3., "end": None, "speaker": None, "text": "Untimed"},
        ]}
        self.assertEqual(render_export(detail, "srt"),
                         "1\n00:00:01,000 --> 00:00:02,250\n<Review> & I: Hello A -> B\n")
        self.assertEqual(render_export(detail, "vtt"),
                         "WEBVTT\n\n00:00:01.000 --> 00:00:02.250\n&lt;Review&gt; &amp; I: Hello A -&gt; B\n")

    def test_copy_latest_meeting_uses_current_turns_or_most_recent_readable_speech(self):
        with self.assertRaisesRegex(ValueError, "No meeting transcript"):
            latest_transcript(self.store)
        saved = self.meeting()
        self.meeting(meeting=False)
        source = self.root / "source.wav"
        source.write_bytes(b"fixture")
        silent, _ = self.store.create(source, "Silent.wav", "audio/wav", {
            "meeting": True, "meetingStartedAt": "2026-09-17T09:00:00Z", "diarization": False, "recording": True,
        }, 8)
        self.store.finish(silent)
        opened = datetime.now(timezone.utc).isoformat()
        live = {"id": str(uuid4()), "name": "Live meeting", "meeting": True, "created": opened,
                "meetingStartedAt": opened, "duration": 10, "status": "recording", "meetingTurns": []}
        reader = SessionStore(self.store.root, read_only=True)
        self.assertEqual(latest_transcript(reader, live), {
            "session": saved, "text": "**00:01 · Speaker 1**  \nReview the project timeline.",
        })
        live["meetingTurns"] = [{"speaker": 1, "start": 4, "end": 7, "text": "The build is ready."}]
        self.assertEqual(latest_transcript(reader, live), {
            "session": live["id"], "text": "**00:04 · You**  \nThe build is ready.",
        })
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (saved,))
        self.summarize(saved, "Agenda", "The team reviewed the timeline.")
        self.assertFalse(self.store.delete(saved, trash=lambda path: shutil.move(path, self.root / "Archived recording")))
        with self.assertRaisesRegex(ValueError, "No meeting transcript"):
            latest_transcript(reader)

    def pair(self):
        self.choose(self.folder)
        with self.store.connection() as db:
            meeting_exports.set_separate_notes(db, True)
        key = self.meeting()
        meeting_exports.flush(self.store)
        self.summarize(key, "Timeline", "The team approved the timeline.")
        meeting_exports.flush(self.store)
        exported = self.store.get(key)["meetingExport"]
        return key, self.folder / exported["filename"], self.folder / exported["notesFilename"]

    def age(self, key):
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
            db.execute("UPDATE sessions SET created='2000-01-01T00:00:00Z' WHERE id=?", (key,))

    def test_opt_in_collision_numbering_safe_mode_and_folder_snapshot(self):
        legacy = self.meeting()
        self.assertIsNone(self.store.get(legacy)["meetingExport"])
        with self.assertRaisesRegex(ValueError, "outside the session library"):
            self.choose(self.store.audio)
        self.choose(self.folder)
        first = self.meeting()
        self.assertEqual(self.store.get(first)["meetingExport"]["status"], "pending")
        stem = meeting_exports.file_stem(meeting_detail(self.store.get(first, include_turns=True)))
        self.assertTrue(stem.endswith("Agenda - review"), stem)
        collision = self.folder / (stem + ".md")
        collision.write_text("User's file")
        second_folder = self.root / "Second"
        second_folder.mkdir()
        self.choose(second_folder)
        second = self.meeting()
        other = self.meeting(meeting=False)
        self.choose(None)
        unexported = self.meeting()
        meeting_exports.flush(self.store)
        first_name = self.store.get(first)["meetingExport"]["filename"]
        self.assertEqual(first_name, f"{stem} (2).md")
        first_file = self.folder / first_name
        self.assertEqual(collision.read_text(), "User's file")
        self.assertIn("Review the project timeline.", first_file.read_text())
        self.assertEqual(stat.S_IMODE(first_file.stat().st_mode), 0o600)
        self.assertTrue((second_folder / self.store.get(second)["meetingExport"]["filename"]).is_file())
        self.assertIsNone(self.store.get(other)["meetingExport"])
        self.assertIsNone(self.store.get(unexported)["meetingExport"])
        self.assertEqual(list(self.folder.glob(".jard-export-*")), [])

    def test_restart_adopts_a_published_file_without_replacing_user_edits(self):
        self.choose(self.folder)
        key = self.meeting()
        with patch("meeting_exports.mark_ready", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                meeting_exports.flush(self.store)
        original, = self.folder.glob("*.md")
        expected_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        original.write_text("Edited by the user.")
        reopened = SessionStore(self.store.root)
        meeting_exports.flush(reopened)
        self.assertEqual(list(self.folder.iterdir()), [original])
        self.assertEqual(original.read_text(), "Edited by the user.")
        self.assertEqual(reopened.get(key)["meetingExport"]["status"], "ready")
        with reopened.connection() as db:
            self.assertEqual(db.execute("SELECT sha256 FROM meeting_exports WHERE id=?", (key,)).fetchone()[0], expected_hash)

    def test_folder_replacement_does_not_write_and_meeting_still_completes(self):
        self.choose(self.folder)
        key = self.meeting()
        self.folder.rename(self.root / "Original")
        self.folder.mkdir()
        meeting_exports.flush(self.store)
        session = self.store.get(key)
        self.assertEqual(session["status"], "complete")
        self.assertEqual(session["meetingExport"]["status"], "error")
        self.assertIn("Choose the export folder again", session["meetingExport"]["error"])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_summary_updates_transcript_and_companion_without_taking_user_files(self):
        self.choose(self.folder)
        with self.store.connection() as db:
            meeting_exports.set_separate_notes(db, True)
        key = self.meeting()
        stem = meeting_exports.file_stem(meeting_detail(self.store.get(key, include_turns=True)))
        collision = self.folder / (stem + ".notes.md")
        collision.write_text("Already here")
        meeting_exports.flush(self.store)
        exported = self.store.get(key)["meetingExport"]
        transcript = self.folder / exported["filename"]
        self.assertEqual(transcript.name, stem + " (2).transcript.md")
        self.assertIsNone(exported["notesFilename"])
        self.summarize(key, "Timeline", "The team approved the timeline.")
        meeting_exports.flush(self.store)
        notes = self.folder / self.store.get(key)["meetingExport"]["notesFilename"]
        self.assertEqual(notes.name, stem + " (2).notes.md")
        self.assertIn(f"[Open transcript]({quote(transcript.name)})", notes.read_text())
        self.assertIn("The team approved the timeline.", notes.read_text())
        self.assertIn("The team approved the timeline.", transcript.read_text())
        self.assertEqual(collision.read_text(), "Already here")

        # Even after the setting is off, an existing managed companion follows
        # the summary until the user edits it or removes it.
        with self.store.connection() as db:
            meeting_exports.set_separate_notes(db, False)
        self.summarize(key, "Timeline", "The decision was confirmed.", regenerate=True)
        meeting_exports.flush(self.store)
        self.assertIn("The decision was confirmed.", notes.read_text())
        notes.write_text("My edited notes")
        transcript.write_text("My edited transcript")
        self.summarize(key, "Timeline", "A new rewrite.", regenerate=True)
        meeting_exports.flush(self.store)
        self.assertEqual(notes.read_text(), "My edited notes")
        self.assertEqual(transcript.read_text(), "My edited transcript")
        notes.unlink()
        with self.store.connection() as db:
            meeting_exports.set_separate_notes(db, True)
        self.summarize(key, "Timeline", "Another rewrite.", regenerate=True)
        meeting_exports.flush(self.store)
        self.assertIsNone(self.store.get(key)["meetingExport"]["notesFilename"])
        self.assertFalse(notes.exists())
        self.summarize(key, "Timeline", "Final rewrite.", regenerate=True)
        meeting_exports.flush(self.store)
        self.assertEqual(set(self.folder.iterdir()), {collision, transcript})

    def test_restart_commits_published_summary_edit_before_next_summary(self):
        self.choose(self.folder)
        key = self.meeting()
        meeting_exports.flush(self.store)
        transcript = self.folder / self.store.get(key)["meetingExport"]["filename"]
        self.summarize(key, "Timeline", "An interrupted refresh.")
        with patch("meeting_exports.recover_edit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                meeting_exports.flush(self.store)
        self.assertIn("An interrupted refresh.", transcript.read_text())
        self.store = SessionStore(self.store.root)
        meeting_exports.flush(self.store)
        self.summarize(key, "Timeline", "Recovered and refreshed.", regenerate=True)
        meeting_exports.flush(self.store)
        self.assertIn("Recovered and refreshed.", transcript.read_text())
        self.assertEqual(list(self.folder.iterdir()), [transcript])

    def test_expiry_keeps_manual_copy_even_when_it_replaces_the_original_bytes(self):
        key, transcript, notes = self.pair()
        saved = transcript.read_bytes()
        copy = self.folder / "My copy.md"
        copy.write_bytes(saved)
        copy.replace(transcript)
        manual_notes = self.folder / "Copied Notes.md"
        manual_notes.write_bytes(notes.read_bytes())
        self.summarize(key, "Timeline", "The decision was confirmed.", regenerate=True)
        meeting_exports.flush(self.store)
        self.assertEqual(transcript.read_bytes(), saved)
        self.age(key)
        trash = self.root / "Trash"
        trash.mkdir()
        self.assertEqual(self.store.expire_meetings(lambda item: shutil.move(item, trash / item.name)), [])
        self.assertEqual(transcript.read_bytes(), saved)
        self.assertTrue(manual_notes.exists())
        self.assertFalse(notes.exists())
        self.assertIn("The decision was confirmed.", (trash / notes.name).read_text())

    def test_expiry_retries_one_unmoved_file_after_restart_and_preserves_edited_companion(self):
        key, transcript, notes = self.pair()
        archived = transcript.read_bytes()
        notes.write_text("My private meeting edits.")
        self.age(key)
        trash = self.root / "Trash"
        trash.mkdir()

        def unavailable(item):
            if item.name == transcript.name:
                raise OSError("Finder Trash temporarily unavailable")
            shutil.move(item, trash / item.name)

        self.assertEqual(self.store.expire_meetings(unavailable), [key])
        captured, = self.folder.glob(f".jard-trash-*/{transcript.name}")
        self.assertEqual(captured.read_bytes(), archived)
        self.assertEqual(notes.read_text(), "My private meeting edits.")
        transcript.write_bytes(archived)
        restarted = SessionStore(self.store.root)
        self.assertEqual(restarted.expire_meetings(lambda item: shutil.move(item, trash / item.name)), [])
        self.assertEqual(transcript.read_bytes(), archived)
        self.assertEqual((trash / transcript.name).read_bytes(), archived)
        self.assertEqual(notes.read_text(), "My private meeting edits.")
        self.assertFalse(captured.parent.exists())

    def test_manual_trash_retries_exports_and_preserves_edited_or_manual_files(self):
        key, transcript, notes = self.pair()
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
        archive = archive_path(self.store.root, key)
        manual = self.folder / "Manually saved copy.md"
        manual.write_bytes(transcript.read_bytes())
        notes.write_text("My private edits.")
        trashed = []

        def unavailable(path):
            trashed.append(path.name)
            raise OSError("Finder unavailable")

        self.assertTrue(self.store.delete(key, trash=unavailable))
        self.assertEqual(trashed, [transcript.name])
        self.assertEqual(self.store.list("", meetings_only=True), [])
        self.assertIn("The team approved the timeline.", (archive / "Transcript.md").read_text())
        self.assertEqual((archive / "Audio" / "source.wav").read_bytes(), b"fixture")
        self.assertEqual(notes.read_text(), "My private edits.")

        restarted = SessionStore(self.store.root)
        trash = self.root / "Trash"
        trash.mkdir()

        def move(path):
            trashed.append(path.name)
            shutil.move(path, trash / path.name)

        self.assertFalse(restarted.delete(key, trash=move))
        self.assertEqual(trashed, [transcript.name, transcript.name, archive.name])
        self.assertEqual(manual.read_bytes(), (trash / transcript.name).read_bytes())
        self.assertEqual(notes.read_text(), "My private edits.")
        self.assertTrue((trash / archive.name / "Meeting.json").is_file())
        with self.assertRaises(MissingSession):
            restarted.delete(key, trash=move)

    def test_manual_trash_refuses_processing_notes_and_incomplete_snapshots(self):
        key = self.meeting(complete=False)
        move = lambda path: self.fail(f"Unexpected move: {path}")
        with self.assertRaisesRegex(ValueError, "meeting and its notes"):
            self.store.delete(key, trash=move)
        self.store.finish(key)
        with self.assertRaisesRegex(ValueError, "meeting and its notes"):
            self.store.delete(key, trash=move)
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
            db.execute("UPDATE meeting_briefs SET status='ready' WHERE id=?", (key,))
        with patch("meeting_retention.write_snapshot", side_effect=OSError("Could not write archive")):
            with self.assertRaisesRegex(OSError, "Could not write archive"):
                self.store.delete(key, trash=move)
        self.assertEqual(self.store.get(key)["status"], "complete")
        self.assertTrue(self.store.media(key)[0].is_file())

    def test_user_title_survives_regeneration_and_renames_pair_past_a_collision(self):
        key, old_transcript, old_notes = self.pair()
        stamp = old_transcript.name[:15]
        collision = self.folder / f"{stamp} Project handoff.notes.md"
        collision.write_text("Unrelated file")
        with self.store.connection() as db:
            meeting_exports.set_descriptive_names(db, True)
        self.assertEqual(self.store.rename_meeting(key, "  Project   handoff "), "Project handoff")
        self.summarize(key, "Generated title", "Keep the next milestone.", regenerate=True)
        meeting_exports.flush(self.store)
        stored = self.store.get(key)
        self.assertEqual(stored["name"], "Project handoff")
        self.assertEqual(self.store.list("handoff", meetings_only=True)[0]["id"], key)
        self.assertEqual(stored["meetingBrief"]["title"], "Generated title")
        export = stored["meetingExport"]
        transcript = self.folder / f"{stamp} Project handoff (2).transcript.md"
        companion = self.folder / f"{stamp} Project handoff (2).notes.md"
        self.assertEqual((export["filename"], export["notesFilename"]), (transcript.name, companion.name))
        self.assertEqual(collision.read_text(), "Unrelated file")
        self.assertFalse(old_transcript.exists())
        self.assertFalse(old_notes.exists())
        self.assertTrue(transcript.read_text().startswith("# Project handoff\n"))
        self.assertIn(quote(transcript.name), companion.read_text())

    def test_restart_completes_a_rename_interrupted_between_the_two_files(self):
        key, transcript, notes = self.pair()
        with self.store.connection() as db:
            meeting_exports.set_descriptive_names(db, True)
        self.store.rename_meeting(key, "Morning handoff")
        real_link = os.link

        def interrupt_companion(source, target, **options):
            if source == notes.name:
                raise KeyboardInterrupt
            return real_link(source, target, **options)

        with patch("meeting_exports.os.link", side_effect=interrupt_companion):
            with self.assertRaises(KeyboardInterrupt):
                meeting_exports.flush(self.store)
        self.assertTrue(transcript.is_file())
        self.assertTrue(notes.is_file())
        restarted = SessionStore(self.store.root)
        meeting_exports.flush(restarted)
        export = restarted.get(key)["meetingExport"]
        targets = {self.folder / export["filename"], self.folder / export["notesFilename"]}
        self.assertEqual(set(self.folder.iterdir()), targets)
        self.assertIn(quote(export["filename"]), (self.folder / export["notesFilename"]).read_text())
        self.assertEqual(restarted.get(key)["name"], "Morning handoff")

    def test_title_updates_markdown_with_stable_filenames_by_default(self):
        key, transcript, notes = self.pair()
        self.store.rename_meeting(key, "Named meeting")
        meeting_exports.flush(self.store)
        self.assertEqual(set(self.folder.iterdir()), {transcript, notes})
        self.assertTrue(transcript.read_text().startswith("# Named meeting\n"))
        self.assertTrue(notes.read_text().startswith("# Named meeting\n"))
