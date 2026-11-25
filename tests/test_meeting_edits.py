import json
import tempfile
import unittest
from pathlib import Path

import meeting_edits
import meeting_exports
import meeting_brief
import personal_notes
from session_store import SessionStore
from saved_meetings import meeting_detail, render_export


class MeetingEditsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = SessionStore(self.root / "library")
        self.export = self.root / "Exports"
        self.export.mkdir()
        with self.store.connection() as db:
            meeting_exports.set_folder(db, self.store.root, str(self.export))
        source = self.root / "take.wav"
        source.write_bytes(b"original mic and system recording")
        self.key, self.audio = self.store.create(source, "Meeting.wav", "audio/wav", {
            "meeting": True, "meetingTitle": "My title", "provisionalTitle": "Secret",
            "recording": True, "diarization": True, "languageHints": ["en"],
        }, 5)
        turns = [{"speaker": 1, "start": 0, "end": 3, "text": "keep"},
                 {"speaker": 2, "start": 1, "end": 2, "text": "secret"},
                 {"speaker": 1, "start": 3, "end": None, "text": "still here"}]
        words = [{**turn, "separator": " "} for turn in turns[:2]] + [
            {"speaker": 1, "start": None, "end": None, "separator": sep, "text": text}
            for text, sep in [("still", " "), ("here", "")]]
        self.store.append(self.key, {"text": "keep secret still here", "language": "English", "segments": turns,
                                    "words": words, "activity": []}, 5)
        self.store.finish(self.key)
        meeting_exports.flush(self.store)
        self.revision = meeting_edits.revision(turns)

    def permit(self):
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO meeting_edit_policy VALUES(1,1)")
            db.execute("UPDATE overviews SET status='error' WHERE id=?", (self.key,))
            db.execute("UPDATE meeting_briefs SET status='ready',title='Secret',summary='Secret' WHERE id=?", (self.key,))

    def test_saved_transcript_search_notes_media_and_exports(self):
        with self.assertRaisesRegex(ValueError, "Turn on"):
            meeting_edits.delete_lines(self.store, self.key, self.revision, [1])
        self.permit()
        with self.store.connection() as db:
            note = personal_notes.NoteEdit(revision=0, document={"type":"doc","content":[
                {"type":"paragraph","content":[{"type":"text","text":"Handwritten notes"}]}]})
            personal_notes.save(db, self.key, note)
        meeting_edits.delete_lines(self.store, self.key, self.revision, [1])
        meeting_exports.flush(self.store)
        saved = self.store.get(self.key, include_turns=True)
        self.assertEqual(saved["transcript"]["text"], "keep still here")
        self.assertEqual([w["text"] for w in saved["transcript"]["words"]], ["keep", "still", "here"])
        self.assertEqual([t["text"] for t in saved["meetingTurns"]], ["keep", "still here"])
        self.assertNotEqual(saved["transcriptEditRevision"], self.revision)
        self.assertEqual(saved["meetingTitle"], "My title")
        self.assertNotIn("provisionalTitle", saved)
        self.assertIsNone(saved["meetingBrief"]["summary"])
        self.assertEqual(saved["transcript"]["durationSeconds"], 5)
        self.assertEqual(Path(self.audio).read_bytes(), b"original mic and system recording")
        self.assertNotIn("secret", render_export(meeting_detail(saved), "md").lower())
        self.assertNotIn("secret", next(self.export.glob("*.md")).read_text().lower())
        self.assertEqual(self.store.list("secret"), [])
        self.assertIn("Handwritten notes", json.dumps(saved["personalNotes"]))

    def test_managed_companion_loses_deleted_summary_and_malformed_words_roll_back(self):
        self.permit()
        with self.store.connection() as db:
            meeting_exports.set_separate_notes(db, True)
            meeting_brief.enqueue(db, self.key, refinalize=True)
            generation = meeting_brief.next_job(db)["generation"]
            meeting_brief.settle(db, self.key, generation, {"title": "Secret", "summary": "Secret detail"})
        meeting_exports.flush(self.store)
        notes = next(self.export.glob("*.notes.md"))
        self.assertIn("Secret detail", notes.read_text())
        with self.store.connection() as db:
            db.execute("""UPDATE chunks SET payload=json_set(payload,'$.words[0].text','unmatched')
                          WHERE session_id=?""", (self.key,))
        with self.assertRaisesRegex(ValueError, "matched"):
            meeting_edits.delete_lines(self.store, self.key, self.revision, [1])
        self.assertIn("secret", self.store.get(self.key)["transcript"]["text"])
        meeting_edits.delete_lines(self.store, self.key, self.revision, [0, 1, 2])
        meeting_exports.flush(self.store)
        self.assertNotIn("Secret detail", notes.read_text())
        self.assertIn("Open transcript", notes.read_text())

    def test_refuses_stale_selection_busy_work_and_manually_edited_overviews(self):
        self.permit()
        with self.assertRaisesRegex(ValueError, "transcript changed"):
            meeting_edits.delete_lines(self.store, self.key, "0" * 64, [1])
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='queued' WHERE id=?", (self.key,))
        with self.assertRaisesRegex(ValueError, "Wait"):
            meeting_edits.delete_lines(self.store, self.key, self.revision, [1])
        self.permit()
        with self.store.connection() as db:
            db.execute("""UPDATE overviews SET body='{"manuallyEdited":true}',status='ready' WHERE id=?""", (self.key,))
        with self.assertRaisesRegex(ValueError, "manually edited"):
            meeting_edits.delete_lines(self.store, self.key, self.revision, [0, 1, 2])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT search_text FROM sessions WHERE id=?", (self.key,)).fetchone()[0],
                             "keep secret still here")

    def test_all_lines_can_be_removed_while_manually_changed_export_survives(self):
        self.permit()
        exported = next(self.export.glob("*.md"))
        exported.write_text("Edited externally")
        meeting_edits.delete_lines(self.store, self.key, self.revision, [0, 1, 2])
        meeting_exports.flush(self.store)
        self.assertEqual(exported.read_text(), "Edited externally")
        self.assertEqual(self.store.get(self.key)["transcript"]["text"], "")
