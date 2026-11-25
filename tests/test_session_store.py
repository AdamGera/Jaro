import base64
import errno
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import annotation_retention
import annotation_storage
from annotation_assets import staged_annotation_paths
from engine_paths import ENGINE_ROOT
import meeting_brief
import session_images
from meeting_rolling import RATE, append_rolling, read_rolling, rolling_path
from meeting_retention import archive_path, audio_trash_path
from native_recordings import recording_marker
from session_store import SHUTDOWN_INTERRUPTION, MissingSession, SessionStore
from saved_annotations import annotation_detail
from saved_meetings import get_meeting, meeting_detail, meeting_search_report, meetings, render_export, search_meetings
from personal_notes import NoteEdit, save
from session_notes import passages


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SessionStore(self.root / "data")
        self.trash = self.root / "Trash"
        self.trash.mkdir()
        self.original = self.root / "meeting.wav"
        self.original.write_bytes(b"original audio")
        copy = self.root / "upload"
        copy.write_bytes(self.original.read_bytes())
        self.key, self.audio = self.store.create(copy, self.original.name, "audio/wav", {
            "recording": False, "diarization": True, "languageHints": ["en", "ar"],
        }, 8)
        for start, end, text in [(0, 4, "أهلاً"), (4, 8, "again")]:
            self.store.append(self.key, {"language": "Arabic", "text": text, "words": [{"text": text}],
                "segments": [{"speaker": 1, "start": start, "end": end, "text": text}],
                "activity": [{"speaker": 1, "start": start, "end": end}]}, end)

    def send_to_fixture_trash(self, path):
        shutil.move(path, self.trash / path.name)

    def retention_annotation(self, age, *, older=False, processing=False, pending_writing=False, location=None, missing_video=None):
        key = str(uuid4())
        suffix, mime = ("wav", "audio/wav") if missing_video else ("mp4", "video/mp4")
        upload = self.root / f"{key}.{suffix}"
        upload.write_bytes(b"fixture movie")
        assets = staged_annotation_paths(upload)
        assets["manifest"].write_text("{}")
        annotation = {"version": 1, "source": "display", "frames": [], "strokeCount": 1}
        if missing_video:
            annotation["missingVideo"] = missing_video
        else:
            assets["contact-sheet"].write_bytes(b"fixture sheet")
            annotation["frames"] = [0.6]
        _, movie = self.store.create(upload, f"Annotation.{suffix}", mime, {
            "recording": False, "diarization": False, "languageHints": ["en"],
        }, 2, key=key, annotation=annotation, annotation_location=location)
        self.store.append(key, {"text": "Look at this chart.", "language": "English", "words": [], "segments": []}, 1)
        if not processing:
            self.store.finish(key)
        with self.store.connection() as db:
            db.execute("UPDATE sessions SET created=? WHERE id=?",
                       ((datetime.now(timezone.utc) - timedelta(days=age)).isoformat(), key))
            if not pending_writing:
                db.execute("UPDATE overviews SET status='error' WHERE id=?", (key,))
            if older:
                db.execute("UPDATE sessions SET options=json_remove(options,'$.annotationRetentionEligible') WHERE id=?", (key,))
        return key, movie

    def test_annotation_folder_is_pinned_and_a_replaced_mount_does_not_expire_data(self):
        original = self.root / "External"
        replacement = self.root / "Another folder"
        original.mkdir()
        replacement.mkdir()
        with self.store.connection() as db:
            annotation_storage.select(db, str(original), self.store.root)
            chosen = annotation_storage.status(db, self.store.audio)
        with patch("annotation_storage.os.link", side_effect=OSError(errno.EXDEV, "Different volume")):
            video, movie = self.retention_annotation(9, location=chosen["storageId"])
            record, full_movie = self.retention_annotation(35, location=chosen["storageId"])
        self.assertEqual(movie.parents[1], original.resolve())
        self.assertEqual(movie.read_bytes(), b"fixture movie")
        self.assertEqual(annotation_detail(self.store, video)["videoPath"], str(movie))
        self.assertIsNone(self.store.get(video)["audioPath"])
        with self.store.connection() as db:
            annotation_storage.select(db, str(replacement), self.store.root)
            self.assertEqual(annotation_storage.status(db, self.store.audio)["folder"], str(replacement.resolve()))
        self.assertEqual(self.store.media(video)[0], movie)

        aside = self.root / "Detached"
        original.rename(aside)
        original.mkdir()
        recreated = original / movie.parent.name
        recreated.mkdir()
        (recreated / movie.name).write_text("Unrelated file")
        reopened = SessionStore(self.store.root)
        saved = reopened.get(video)
        self.assertTrue(saved["annotationStorageUnavailable"])
        self.assertIsNone(saved["audioUrl"])
        self.assertEqual(annotation_detail(reopened, video)["transcript"], "Look at this chart.")
        self.assertIsNone(annotation_detail(reopened, video)["contactSheetPath"])
        self.assertEqual(annotation_retention.sweep(reopened, self.send_to_fixture_trash), (0, sorted([video, record])))
        with self.assertRaises(annotation_storage.Unavailable):
            reopened.delete(record)
        self.assertEqual((recreated / movie.name).read_text(), "Unrelated file")
        self.assertTrue((aside / full_movie.relative_to(original.resolve())).is_file())

        original.rename(self.root / "Replacement data")
        aside.rename(original)
        self.assertEqual(annotation_retention.sweep(reopened, self.send_to_fixture_trash), (2, []))
        self.assertIsNone(reopened.get(video)["audioUrl"])
        self.assertEqual(reopened.annotation_asset(video, "contact-sheet")[0].read_bytes(), b"fixture sheet")
        self.assertFalse(full_movie.parent.exists())
        self.assertTrue((self.trash / f"Jaro Annotation {record}" / "Capture" / full_movie.name).is_file())

    def test_annotation_folder_recovery_only_removes_its_own_unpublished_capture(self):
        folder = self.root / "External"
        folder.mkdir()
        key = str(uuid4())
        other_key = str(uuid4())
        with self.store.connection() as db:
            annotation_storage.select(db, str(folder), self.store.root)
            location = annotation_storage.status(db, self.store.audio)["storageId"]
            db.executemany("INSERT INTO annotation_file_operations VALUES(?,?,'import')",
                           [(key, location), (other_key, location)])
        partial = annotation_storage.allocate(folder, key)
        unowned = annotation_storage.bundle_path(folder, other_key)
        unowned.mkdir()
        (unowned / "unrelated").write_text("Keep me")
        SessionStore(self.store.root)
        self.assertFalse(partial.exists())
        self.assertEqual((unowned / "unrelated").read_text(), "Keep me")

    def test_annotation_retention_expires_video_then_record_and_preserves_older_and_busy_items(self):
        aged, movie = self.retention_annotation(8)
        legacy, legacy_movie = self.retention_annotation(95, older=True)
        active, active_movie = self.retention_annotation(95, processing=True)
        waiting, waiting_movie = self.retention_annotation(95, pending_writing=True)
        recent, recent_movie = self.retention_annotation(1)
        reader = SessionStore(self.store.root, read_only=True)
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0002 0003 01 01 1100 ffd9")
        with self.store.connection() as db:
            image = session_images.add(db, aged, jpeg)

        self.assertEqual(annotation_retention.sweep(self.store, self.send_to_fixture_trash), (1, []))
        self.assertFalse(movie.exists())
        self.assertEqual(list(self.trash.iterdir())[0].read_bytes(), b"fixture movie")
        with self.assertRaises(MissingSession):
            reader.media(aged)
        saved = reader.get(aged)
        self.assertIsNone(saved["audioUrl"])
        self.assertEqual(saved["transcript"]["text"], "Look at this chart.")
        self.assertEqual(reader.annotation_asset(aged, "contact-sheet")[0].read_bytes(), b"fixture sheet")
        self.assertEqual(reader.annotation_asset(aged, "manifest")[0].read_text(), "{}")
        with reader.connection() as db:
            self.assertEqual(session_images.get(db, aged, image["id"]), jpeg)
        self.assertEqual({row["id"] for row in reader.annotations()}, {aged, legacy, active, waiting, recent})
        self.assertTrue(all(path.is_file() for path in (legacy_movie, active_movie, waiting_movie, recent_movie)))

        with self.store.connection() as db:
            db.execute("UPDATE sessions SET created='2000-01-01T00:00:00Z' WHERE id=?", (aged,))
            annotation_retention.set_policy(db, 7, 30, True)
        self.assertEqual(annotation_retention.sweep(self.store, self.send_to_fixture_trash), (2, []))
        self.assertEqual({row["id"] for row in reader.annotations()}, {active, waiting, recent})
        archive = self.trash / f"Jaro Annotation {aged}"
        self.assertIn("Look at this chart.", (archive / "Narration.md").read_text())
        manifest = json.loads((archive / "Annotation.json").read_text())
        self.assertEqual(manifest["id"], aged)
        stored_image, = manifest["tables"]["session_images"]
        self.assertEqual(stored_image["id"], image["id"])
        self.assertEqual((archive / stored_image["file"]).read_bytes(), jpeg)
        self.assertEqual((archive / "Capture" / "contact-sheet.png").read_bytes(), b"fixture sheet")
        self.assertTrue((self.trash / f"Jaro Annotation {legacy}" / "Capture" / legacy_movie.name).is_file())

    def test_narration_without_video_uses_only_annotation_record_retention(self):
        key, narration = self.retention_annotation(12, missing_video="screenInterrupted")
        self.assertEqual(annotation_retention.sweep(self.store, self.send_to_fixture_trash), (0, []))
        self.assertTrue(narration.is_file())
        detail = annotation_detail(self.store, key)
        self.assertIsNone(detail["videoPath"])
        self.assertEqual(detail["audioPath"], str(narration))
        with self.store.connection() as db:
            db.execute("UPDATE sessions SET created='2000-01-01T00:00:00Z' WHERE id=?", (key,))
        self.assertEqual(annotation_retention.sweep(self.store, self.send_to_fixture_trash), (1, []))
        self.assertTrue((self.trash / f"Jaro Annotation {key}" / "Capture" / narration.name).is_file())

    def test_annotation_trash_failure_keeps_data_available_and_recovers_after_restart(self):
        media, movie = self.retention_annotation(9)
        record, full_movie = self.retention_annotation(95)

        def unavailable(_):
            raise OSError("Trash temporarily unavailable")

        self.assertEqual(annotation_retention.sweep(self.store, unavailable), (0, sorted([media, record])))
        self.assertEqual(self.store.media(media)[0], movie)
        self.assertEqual(self.store.media(record)[0], full_movie)
        stranded = self.store.root / ".annotation-trash" / "transfer-interrupted"
        stranded.mkdir()
        (stranded / "staging").write_text("Duplicate")
        reopened = SessionStore(self.store.root)
        self.assertEqual(annotation_retention.sweep(reopened, self.send_to_fixture_trash), (2, []))
        self.assertFalse(stranded.exists())
        self.assertFalse(movie.exists())
        self.assertFalse(full_movie.exists())
        self.assertIsNotNone(reopened.get(media)["audioRemovedAt"])
        with self.assertRaises(MissingSession):
            reopened.get(record)

    def test_annotation_expiry_preserves_concurrent_edits_and_retries_committed_video_cleanup(self):
        media, movie = self.retention_annotation(9)
        record, full_movie = self.retention_annotation(95)

        def save_then_trash(path):
            if path.name == f"Jaro Annotation {record}":
                with self.store.connection() as db:
                    db.execute("UPDATE sessions SET name='Edited title' WHERE id=?", (record,))
            self.send_to_fixture_trash(path)

        with patch("annotation_retention.cleanup_videos", return_value=[]):
            self.assertEqual(annotation_retention.sweep(self.store, save_then_trash), (1, []))
        self.assertTrue(full_movie.exists())
        self.assertEqual(self.store.get(record)["name"], "Edited title")
        self.assertTrue(movie.exists())
        with self.assertRaises(MissingSession):
            self.store.media(media)

        with self.store.connection() as db:
            annotation_retention.set_policy(db, None, None, False)
        reopened = SessionStore(self.store.root)
        self.assertEqual(annotation_retention.sweep(reopened, self.send_to_fixture_trash), (0, []))
        self.assertFalse(movie.exists())
        self.assertTrue(full_movie.exists())

    def test_completed_session_and_managed_copy_survive_restart(self):
        self.store.finish(self.key)
        reopened = SessionStore(self.store.root)
        saved = reopened.get(self.key)
        self.assertEqual(saved["transcript"]["text"], "أهلاً again")
        self.assertEqual(saved["transcript"]["speakerActivity"][0]["intervals"], [{"start": 0, "end": 8}])
        self.assertEqual(saved["transcript"]["segments"][0]["end"], 8)
        self.assertEqual(len(saved["transcript"]["words"]), 2)
        self.assertEqual(len(reopened.list("أهلاً AGAIN")), 1)
        reopened.delete(self.key)
        self.assertFalse(self.audio.exists())
        self.assertTrue(self.original.exists())
        with self.assertRaises(MissingSession):
            reopened.get(self.key)
        with reopened.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)

    def test_processing_session_recovers_partial_results_after_restart(self):
        with self.assertRaises(ValueError):
            self.store.delete(self.key)
        saved = SessionStore(self.store.root).get(self.key)
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["transcript"]["text"], "أهلاً again")
        self.assertTrue(saved["transcript"]["interruption"])
        self.assertTrue(self.audio.exists())

    def test_offline_annotation_cli_resolves_saved_assets_and_interrupted_narration(self):
        ids = ["aaaaaa00-0000-4000-8000-000000000000", "aaaaaa11-0000-4000-8000-000000000000"]
        narration = " ".join(["Review the chart."] * 90)
        sheet = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1sAAAAASUVORK5CYII=")
        for index, key in enumerate(ids):
            upload = self.root / f"sample-{index}.mp4"
            upload.write_bytes(b"fixture movie")
            artifacts = staged_annotation_paths(upload)
            artifacts["manifest"].write_text("{}")
            artifacts["contact-sheet"].write_bytes(sheet)
            self.store.create(upload, f"Annotation {index}.mp4", "video/mp4", {
                "recording": False, "diarization": False, "languageHints": ["en"],
            }, 2, key=key,
                              annotation={"version": 1, "source": "window", "frames": [0.2, 1.1], "strokeCount": 1})
            self.store.append(key, {"text": narration, "language": "English", "words": [], "segments": []}, 1)
            self.store.finish(key, "Narration stopped early." if index else None)

        env = {**os.environ, "RUMBLE_DATA_DIR": str(self.store.root), "RUMBLE_PORT": "1"}
        cli = [sys.executable, str(ENGINE_ROOT / "jardctl.py")]

        def command(*args, check=True):
            result = subprocess.run([*cli, *args, "--json"], env=env, text=True, capture_output=True, timeout=10)
            if check:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            self.assertEqual(result.returncode, 1)
            return json.loads(result.stderr)["error"]

        before = self.store.path.read_bytes(), set(self.store.root.rglob("*"))
        rows = command("annotate", "list")["annotations"]
        self.assertEqual([item["id"] for item in rows], ids[::-1])
        self.assertEqual(command("annotate", "list", "--limit", "1")["annotations"], rows[:1])
        latest = command("capture", "show", "latest")
        self.assertEqual(latest["id"], ids[1])
        self.assertEqual(latest["status"], "interrupted")
        self.assertEqual(latest["transcript"], narration)
        self.assertEqual(latest["interruption"], "Narration stopped early.")
        self.assertEqual(latest["frames"], [0.2, 1.1])
        self.assertEqual(latest["route"], f"/?session={ids[1]}&tab=overview")
        self.assertEqual(Path(latest["videoPath"]).read_bytes(), b"fixture movie")
        self.assertEqual(Path(latest["manifestPath"]).name, "annotation.json")
        self.assertEqual(Path(latest["contactSheetPath"]).name, "contact-sheet.png")
        self.assertEqual(command("annotate", "show", ids[0][:8].upper())["id"], ids[0])
        self.assertIn("more than one", command("annotate", "show", "aaaaaa", check=False))
        self.assertEqual(command("annotate", "show", self.key, check=False), "Annotation not found.")
        context = command("annotate", "context", "latest")
        self.assertIn("0:00.2, 0:01.1", context["markdown"])
        self.assertIn(narration, context["markdown"])
        self.assertEqual(context["contactSheetPath"], latest["contactSheetPath"])
        self.assertNotIn(str(self.store.root.resolve()), context["markdown"])

        def mcp(*requests):
            lines = "".join(json.dumps({"id": index, "jsonrpc": "2.0", **request}) + "\n" for index, request in enumerate(requests))
            output = subprocess.check_output([*cli, "mcp"], input=lines, env=env, text=True, timeout=10)
            return [json.loads(line) for line in output.splitlines()]

        def tool(name, **arguments):
            return {"method": "tools/call", "params": {"name": name, "arguments": arguments}}

        def resource(uri):
            return {"method": "resources/read", "params": {"uri": uri}}

        uri = "jard-annotation://" + ids[1]
        tools, listing, first, second, plain, picture, missing = mcp(
            {"method": "tools/list"}, tool("list_annotations", limit=1),
            tool("get_annotation", annotation_id="latest", max_chars=1000),
            tool("get_annotation", annotation_id=ids[1], cursor=1000),
            resource(uri), resource(uri + "/contact-sheet"), tool("get_annotation", annotation_id=self.key),
        )
        self.assertIn("get_annotation", {entry["name"] for entry in tools["result"]["tools"]})
        self.assertEqual(json.loads(listing["result"]["content"][0]["text"])["annotations"][0]["id"], ids[1])
        text, image = first["result"]["content"]
        page = json.loads(text["text"])
        self.assertEqual(page["next_cursor"], 1000)
        self.assertIn(narration[:1000], page["markdown"])
        self.assertEqual((image["type"], image["mimeType"], base64.b64decode(image["data"])), ("image", "image/png", sheet))
        second_content = second["result"]["content"]
        self.assertEqual(len(second_content), 1)
        self.assertIn(narration[1000:], json.loads(second_content[0]["text"])["markdown"])
        self.assertEqual(plain["result"]["contents"][0]["text"], context["markdown"])
        self.assertEqual(base64.b64decode(picture["result"]["contents"][0]["blob"]), sheet)
        self.assertTrue(missing["result"]["isError"])
        self.assertEqual(before, (self.store.path.read_bytes(), set(self.store.root.rglob("*"))))

        Path(latest["contactSheetPath"]).unlink()
        fallback, missing_image = mcp(tool("get_annotation", annotation_id=ids[1]), resource(uri + "/contact-sheet"))
        self.assertEqual(len(fallback["result"]["content"]), 1)
        self.assertIn(narration, json.loads(fallback["result"]["content"][0]["text"])["markdown"])
        self.assertEqual(missing_image["error"]["code"], -32002)

    def create_meeting(self, *, status="complete"):
        mix, mic, system = (self.root / name for name in ("mix.wav", "mic.wav", "system.wav"))
        for file in (mix, mic, system):
            file.write_bytes(b"fixture audio")
        key, path = self.store.create(mix, "Project sync", "audio/wav", {
            "recording": True, "diarization": True, "sourceSpeakerTurns": True, "meetingProvider": "Zoom",
        }, 12, tracks={"mic": mic, "system": system})
        for start, end, text in [(1, 3, "Review the budget."), (8, 10, "Send the agenda.")]:
            self.store.append(key, {"text": text, "language": "English", "words": [],
                "segments": [{"speaker": 2, "start": start, "end": end, "text": text}]}, end)
        if status == "complete":
            self.store.finish(key)
        with self.store.connection() as db:
            save(db, key, NoteEdit(revision=0, document={"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Follow up Tuesday."}]}]}))
        return key, path

    def test_meeting_query_export_and_read_only_agent_after_audio_removal(self):
        key, path = self.create_meeting()
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0002 0003 01 01 1100 ffd9")
        with self.store.connection() as db:
            meeting_brief.enqueue(db, key)
            generation = db.execute("SELECT generation FROM meeting_briefs WHERE id=?", (key,)).fetchone()[0]
            self.assertTrue(meeting_brief.settle(db, key, generation, {"title": "Review", "summary": "The team set a deadline."}))
            image = session_images.add(db, key, jpeg)
        from session_agent_context import session_payload

        text, _, _, _ = session_payload(self.store.root, key, "t3")
        handoff_copy = Path(json.loads(text.splitlines()[-1].removeprefix("- ")))
        self.assertEqual(handoff_copy.read_bytes(), jpeg)
        self.assertEqual([row["id"] for row in self.store.list("zoom", meetings_only=True)], [key])
        self.assertEqual([row["id"] for row in self.store.list("follow up tuesday", meetings_only=True)], [key])
        self.assertEqual(self.store.list("follow up tuesday"), [])
        removed = self.store.remove_meeting_audio(key, trash=self.send_to_fixture_trash)
        self.assertFalse(path.parent.exists())
        self.assertFalse(handoff_copy.exists())
        with self.store.connection() as db:
            self.assertEqual(session_images.get(db, key, image["id"]), jpeg)
        self.assertEqual(self.store.remove_meeting_audio(key, trash=self.send_to_fixture_trash), removed)
        self.assertEqual({item.name for item in (self.trash / audio_trash_path(self.store.root, key).name).iterdir()},
                         {"source.wav", "mic.wav", "system.wav"})
        with self.assertRaises(MissingSession):
            self.store.media(key)
        reader = SessionStore(self.store.root, read_only=True)
        before = reader.path.read_bytes()
        session = get_meeting(reader, "latest")
        self.assertIsNone(session["audioUrl"])
        self.assertEqual(session["audioRemovedAt"], removed)
        detail = meeting_detail(session, "00:07.5", "00:09.0")
        self.assertEqual(detail["range"], {"from_ms": 7500, "to_ms": 9000})
        self.assertEqual([turn["text"] for turn in detail["turns"]], ["Send the agenda."])
        self.assertIn("00:00:08,000 --> 00:00:10,000", render_export(detail, "srt"))
        self.assertIn("Them: Send", render_export(detail, "vtt"))
        self.assertIn("Follow up Tuesday.", render_export(detail, "notes"))
        self.assertEqual(search_meetings(reader, "AGENDA")[0]["route"], f"/?session={key}&tab=transcript&at=8")
        with self.assertRaises(ValueError):
            meeting_detail(session, "00:09", "00:08")

        cli = [sys.executable, str(ENGINE_ROOT / "jardctl.py")]
        env = {**os.environ, "RUMBLE_DATA_DIR": str(reader.root), "RUMBLE_PORT": "1"}
        show = [*cli, "meetings", "show", "latest"]
        bounds = ["--from", "7.5s", "--to", "00:09"]
        output = json.loads(subprocess.check_output([*show, *bounds, "--json"], text=True, env=env, timeout=10))
        self.assertEqual(output["turns"], detail["turns"])
        self.assertEqual(output["range"], detail["range"])
        document = subprocess.check_output([*show, *bounds, "--md"], text=True, env=env, timeout=10)
        self.assertIn("- **Range:** 00:07.500–00:09", document)
        self.assertIn("- **Status:** Complete · final transcript", document)
        self.assertIn("Follow up Tuesday.", document)
        self.assertIn("Send the agenda.", document)
        self.assertNotIn("Review the budget.", document)
        self.assertEqual(document.count("## Summary"), 1)
        self.assertNotIn("Follow up Tuesday.", render_export(detail, "md"))
        for flag, expected in (("--srt", ""), ("--vtt", "WEBVTT\n\n")):
            empty = subprocess.check_output([*show, "--to", "1s", flag], text=True, env=env, timeout=10)
            self.assertEqual(empty, expected)
        ambiguous = subprocess.run([*cli, "--json", "meetings", "show", "latest", "--srt"],
                                   capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(ambiguous.returncode, 1)
        self.assertEqual(ambiguous.stdout, "")
        self.assertIn("single meeting output format", ambiguous.stderr)
        requests = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "get_transcript",
                                                       "arguments": {"meeting_id": key, "from": 7.5, "to": "9s"}}},
            {"id": 4, "method": "tools/call", "params": {"name": "get_meeting_notes", "arguments": {"meeting_id": key}}},
            {"id": 5, "method": "resources/read", "params": {"uri": f"jard-meeting://{key}"}},
        ]
        result = subprocess.run([*cli, "mcp"], input="".join(json.dumps({"jsonrpc": "2.0", **item}) + "\n" for item in requests),
                                capture_output=True, text=True, check=True, env=env, timeout=10)
        init, tools, transcript, notes, resource = [json.loads(line)["result"] for line in result.stdout.splitlines()]
        self.assertEqual(init["protocolVersion"], "2025-11-25")
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in tools["tools"]))
        transcript = json.loads(transcript["content"][0]["text"])
        self.assertEqual(transcript["turns"], detail["turns"])
        self.assertEqual(transcript["range"], detail["range"])
        self.assertEqual(json.loads(notes["content"][0]["text"])["personalNotes"], "Follow up Tuesday.")
        self.assertIn("Follow up Tuesday.", resource["contents"][0]["text"])
        self.assertIn("Send the agenda.", resource["contents"][0]["text"])
        self.assertEqual(resource["contents"][0]["text"].count("## Summary"), 1)
        self.assertEqual(reader.path.read_bytes(), before)

    def test_meeting_overlap_and_ranked_passages(self):
        self.store.set_meeting_retention_days(0)
        key, _ = self.create_meeting()
        with self.store.connection() as db:
            db.execute("""UPDATE sessions SET options=json_set(options,
                '$.meetingStartedAt', '2026-10-20T23:59:50+00:00', '$.meetingEndedAt', '2026-10-21T00:00:10+00:00') WHERE id=?""", (key,))
        reader = SessionStore(self.store.root, read_only=True)
        self.assertEqual([row["id"] for row in meetings(reader, "zoom", since="2026-10-21T00:00Z",
                                                       until="2026-10-21T01:00Z")], [key])
        self.assertFalse(meetings(reader, since="2026-10-21T00:00:11Z"))
        self.assertFalse(meetings(reader, until="2026-10-20T23:59:50Z"))
        with self.assertRaises(ValueError):
            meetings(reader, since="now", until="1h")
        hits = search_meetings(reader, '"Send the agenda"', identifier=key[:8], since="2026-10-21T00:00Z")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["snippet"], "**Send** **the** **agenda**.")
        self.assertEqual(search_meetings(reader, "agneda")[0]["start"], 8)
        self.assertFalse(search_meetings(reader, "agneda", fuzzy=False))
        note = search_meetings(reader, '"up Tuesday"')[0]
        self.assertEqual(note["source"], "personalNotes")
        self.assertEqual(note["route"], f"/?session={key}&tab=notes")

    def test_meeting_and_mcp_search_use_the_saved_glossary_with_timestamps(self):
        from meeting_mcp import call as mcp_call

        self.store.set_meeting_retention_days(0)
        key, _ = self.create_meeting()
        reader = SessionStore(self.store.root, read_only=True)
        before = reader.path.read_bytes()
        snapshot = {"JardSystemDictation": {
            "vocabulary": "Agendaforge", "vocabularySoundsLike": {"agendaforge": ["the agenda"]}}}
        with patch("saved_dictations.read_private_history_plist", return_value=snapshot) as preferences:
            args = {"query": "Agendaforge", "meeting_id": key}
            hits = search_meetings(reader, args["query"], identifier=key)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["snippet"], "Send **the** **agenda**.")
            self.assertEqual(hits[0]["route"], f"/?session={key}&tab=transcript&at=8")
            created = datetime.fromisoformat(reader.get(key)["created"]).astimezone(timezone.utc)
            self.assertEqual(datetime.fromisoformat(hits[0]["absoluteTime"]).astimezone(timezone.utc),
                             (created + timedelta(seconds=8)).replace(microsecond=created.microsecond // 1000 * 1000))
            self.assertEqual(mcp_call(reader, "search_transcripts", args)["passages"], hits)
            self.assertEqual(mcp_call(reader, "search_transcripts", {**args, "fuzzy": False})["passages"], [])
            self.assertEqual(preferences.call_count, 2)
        self.assertEqual(before, reader.path.read_bytes())

    def test_companion_search_counts_before_limiting_and_returns_neighboring_speech(self):
        from meeting_mcp import call as mcp_call

        self.store.set_meeting_retention_days(0)
        key, _ = self.create_meeting(status="processing")
        for start, end, speaker, text in [
            (12, 14, 1, "Read the supporting material. " * 9),
            (17, 19, 2, "Review that agenda again after the supporting material has been read."),
        ]:
            self.store.append(key, {"text": text, "words": [], "language": "English",
                "segments": [{"speaker": speaker, "start": start, "end": end, "text": text}]}, end)
        self.store.finish(key)
        with self.store.connection() as db:
            db.execute("""UPDATE sessions SET options=json_set(options,
                '$.meetingStartedAt', '2026-10-21T09:00:00+00:00', '$.meetingEndedAt', '2026-10-21T09:00:19+00:00') WHERE id=?""", (key,))
        reader = SessionStore(self.store.root, read_only=True)
        before = reader.path.read_bytes()
        report = mcp_call(reader, "search_transcripts", {"query": "agenda", "meeting_id": key, "limit": 1, "fuzzy": False})
        self.assertEqual((report["totalMatches"], report["searchedMeetings"], len(report["passages"])), (2, 1, 1))
        self.assertEqual(report["matchingMeetings"], [
            {"id": key, "title": "Project sync", "status": "complete", "transcriptSource": "final", "matchCount": 2}])
        hit = report["passages"][0]
        self.assertEqual((hit["start"], hit["transcriptSource"]), (8, "final"))
        self.assertEqual(datetime.fromisoformat(hit["absoluteTime"]).astimezone(timezone.utc),
                         datetime(2026, 10, 21, 9, 0, 8, tzinfo=timezone.utc))
        self.assertEqual(hit["before"]["text"], "Review the budget.")
        self.assertEqual(hit["after"]["speaker"], "You")
        self.assertLessEqual(len(hit["after"]["text"]), 120)
        self.assertTrue(hit["after"]["text"].endswith("…"))
        self.assertEqual(search_meetings(reader, "agenda", 1, fuzzy=False), [hit])
        note = search_meetings(reader, '"up Tuesday"', 1, fuzzy=False)[0]
        self.assertNotIn("before", note)
        self.assertNotIn("after", note)
        self.assertNotIn("absoluteTime", note)
        empty = meeting_search_report(reader, "agenda", since="2026-10-22T00:00Z", fuzzy=False)
        self.assertEqual(empty, {"passages": [], "totalMatches": 0, "searchedMeetings": 0, "matchingMeetings": []})
        cli = [sys.executable, str(ENGINE_ROOT / "jardctl.py"), "meetings", "search", "agenda", "--limit", "1", "--no-fuzzy"]
        environment = {**os.environ, "RUMBLE_DATA_DIR": str(reader.root), "RUMBLE_PORT": "1"}
        self.assertEqual(json.loads(subprocess.check_output(cli + ["--json"], env=environment, timeout=10)), [hit])
        rendered = subprocess.check_output(cli, env=environment, text=True, timeout=10)
        self.assertIn("2 matching passages", rendered)
        self.assertIn("Showing the top 1.", rendered)
        self.assertIn("Before (00:01 Them): Review the budget.", rendered)
        self.assertIn("After (00:12 You):", rendered)
        self.assertEqual(before, reader.path.read_bytes())

        second, _ = self.create_meeting()
        combined = meeting_search_report(reader, "agenda", limit=1, fuzzy=False)
        self.assertEqual(combined["totalMatches"], 3)
        self.assertEqual({row["id"]: row["matchCount"] for row in combined["matchingMeetings"]}, {key: 2, second: 1})
        final_turn = meeting_search_report(reader, "agenda", identifier=second, fuzzy=False)["passages"][0]
        self.assertNotIn("after", final_turn)

    def test_companion_search_keeps_staged_speech_separate(self):
        key = str(uuid4())
        start = "2026-11-21T10:00:00+00:00"
        live = {"id": key, "name": "Live call", "created": start, "meetingStartedAt": start,
                "duration": 6, "status": "recording", "staged": True, "meeting": True, "meetingTurns": [
                    {"speaker": 1, "start": 0, "end": 2, "text": "The earlier roll call."},
                    {"speaker": 2, "start": 3, "end": 5, "text": "The agenda comes next."}]}
        reader = SessionStore(self.store.root, read_only=True)
        report = meeting_search_report(reader, "agenda", live=live)
        hit = report["passages"][0]
        self.assertEqual((hit["transcriptSource"], hit["status"], hit["route"]), ("rolling", "recording", None))
        self.assertEqual((hit["before"]["speaker"], hit["before"]["start"]), ("You", 0))
        self.assertNotIn("after", hit)
        self.assertEqual(meeting_search_report(reader, "agenda", live=live, include_in_progress=False)["passages"], [])

    def test_rolling_survives_staging_transfer_interruption_and_per_source_final_coverage(self):
        key = str(uuid4())
        staging = self.store.root / "staging"
        staging.mkdir()
        audio = staging / key
        audio.write_bytes(bytes(44 + RATE * 2 * 10))
        recording_marker(audio).write_text('{"meetingProvider":"Zoom","meetingCalendar":{"title":"Planning"}}')
        local = {"speaker": 1, "start": 0.4, "end": 1.4, "text": "Early later"}
        remote = {"speaker": 2, "start": 0.2, "end": 0.6, "text": "Remote overlap"}
        old_words = [{**local, "end": .8, "text": "Early"}, {**local, "start": 1.1, "text": "later"}]
        live = {"text": "Remote overlap Early later", "segments": [remote, local], "words": old_words, "activity": []}
        append_rolling(audio, 0, RATE * 2, live)
        with rolling_path(audio).open("ab") as tape:
            tape.write(b'[96000,144000,{"text":"partial')
        reader = SessionStore(self.store.root, read_only=True)
        before = meeting_detail(get_meeting(reader, key))
        self.assertEqual((before["title"], before["status"], before["transcriptSource"]), ("Planning", "interrupted", "rolling"))
        self.assertEqual([turn["text"] for turn in before["turns"]], ["Remote overlap", "Early later"])
        self.assertEqual(next(iter(meetings(reader)))["id"], key)

        options = {"recording": True, "diarization": True, "meeting": True, "sourceSpeakerTurns": True}
        self.store.create(audio, "Planning", "audio/wav", options, 10, key=key, rolling=read_rolling(audio, RATE * 10))
        rolling_path(audio).unlink()
        self.assertEqual(get_meeting(reader, key)["transcript"]["text"], "Remote overlap Early later")

        tuned = {"speaker": 1, "start": .5, "end": .85, "text": "Corrected"}
        self.store.append(key, {"text": "Corrected", "segments": [tuned], "words": [],
                               "meetingCoverage": {"1": [[0, 1]]}}, 1)
        during = get_meeting(reader, key)
        self.assertEqual(during["transcript"]["text"], "Remote overlap Corrected later")
        self.assertEqual([turn["text"] for turn in during["meetingTurns"]], ["Remote overlap", "Corrected", "later"])
        # Silence is still coverage. Only the source actually decoded replaces its draft.
        self.store.append(key, {"text": "", "segments": [], "words": [], "meetingCoverage": {"2": [[0, 2]]}}, 2)
        self.assertEqual(get_meeting(reader, key)["transcript"]["text"], "Corrected later")
        reopened = SessionStore(self.store.root)
        self.assertEqual(reopened.get(key)["transcript"]["text"], "Corrected later")
        self.assertEqual(meeting_detail(get_meeting(reader, key))["transcriptSource"], "rolling")

        self.store.append(key, {"text": "Recovered later", "segments": [{**local, "start": 1.1, "text": "Recovered later"}],
                               "words": [], "meetingCoverage": {"1": [[1, 2]]}}, 3)
        self.store.finish(key)
        final = get_meeting(reader, key)
        self.assertEqual(final["transcript"]["text"], "Corrected Recovered later")
        self.assertEqual(meeting_detail(final)["transcriptSource"], "final")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM meeting_rolling WHERE session_id=?", (key,)).fetchone()[0], 0)

    def test_recovery_from_mix_preserves_attributed_words_and_adds_uncovered_speech(self):
        audio = self.root / "mixed.wav"
        audio.write_bytes(b"audio")
        turn = {"speaker": 2, "start": 0.5, "end": 1.5, "text": "Confirmed speaker."}
        rolling = {"text": turn["text"], "segments": [turn], "words": [], "activity": []}
        key, _ = self.store.create(audio, "Mixed meeting", "audio/wav", {"recording": True, "diarization": False, "meeting": True},
                                   3, rolling=[(0, RATE * 2, rolling)])
        span = {"speaker": None, "start": 0, "end": 3, "text": "Repeated newly recovered."}
        words = [{"text": "Repeated", "start": .7, "end": 1.1}, {"text": "newly", "start": 1.6, "end": 1.9},
                 {"text": "recovered.", "start": 2, "end": 2.8}]
        self.store.append(key, {"text": span["text"], "segments": [span], "words": words}, 3)
        self.store.finish(key)
        detail = meeting_detail(get_meeting(self.store, key))
        self.assertEqual([(item["speaker"], item["text"]) for item in detail["turns"]],
                         [("Them", "Confirmed speaker."), (None, "newly recovered.")])
        with self.store.connection() as db:
            self.assertEqual([row["text"] for row in passages(db, key)], ["Them: Confirmed speaker.", "newly recovered."])

    def test_pending_meeting_checkpoint_and_recovery_attempts_survive_restart(self):
        key, audio = self.create_meeting(status="processing")
        with self.store.connection() as db:
            db.execute("UPDATE meeting_finalization SET next_frame=? WHERE session_id=?", (10 * RATE, key))
        self.store.finish(key, SHUTDOWN_INTERRUPTION, closing=True)
        self.assertTrue(self.store.get(key)["finalizationPending"])

        first = SessionStore(self.store.root)
        resume = first.claim_recording_finalization()
        self.assertEqual((resume["id"], resume["next_frame"]), (key, 10 * RATE))
        first.finish(key, SHUTDOWN_INTERRUPTION, closing=True, recovered=True)
        with first.connection() as db:
            self.assertEqual(db.execute("SELECT attempts FROM meeting_finalization WHERE session_id=?", (key,)).fetchone()[0], 0)

        for _ in range(2):
            # Simulate termination after the attempt was committed, without the shutdown handler.
            restart = SessionStore(self.store.root)
            self.assertEqual(restart.claim_recording_finalization()["id"], key)
        last = SessionStore(self.store.root)
        self.assertIsNone(last.claim_recording_finalization())
        saved = last.get(key)
        self.assertFalse(saved["finalizationPending"])
        self.assertEqual(saved["status"], "interrupted")
        self.assertIn("two attempts", saved["transcript"]["interruption"])
        self.assertIn("Send the agenda.", saved["transcript"]["text"])
        self.assertTrue(audio.is_file())

    def test_offline_readers_can_open_a_library_before_finalization_migration(self):
        key, _ = self.create_meeting()
        with self.store.connection() as db:
            db.execute("DROP VIEW readable_sessions")
            db.execute("DROP VIEW expired_meetings")
            db.execute("DROP TABLE meeting_finalization")
        reader = SessionStore(self.store.root, read_only=True)
        self.assertFalse(reader.get(key)["finalizationPending"])
        self.assertFalse(reader.list("", meetings_only=True)[0]["finalizationPending"])

    def test_retention_only_expires_finished_old_meeting_audio_without_pending_notes(self):
        self.store.set_meeting_retention_days(0)
        old, old_audio = self.create_meeting()
        pending, pending_audio = self.create_meeting()
        chatting, chatting_audio = self.create_meeting()
        active, active_audio = self.create_meeting(status="processing")
        _, recent_audio = self.create_meeting()
        with self.store.connection() as db:
            db.executemany("UPDATE sessions SET options=json_set(options,'$.meetingEndedAt','2000-01-01T00:00:00Z') WHERE id=?",
                           [(key,) for key in (old, pending, active, chatting)])
            db.execute("UPDATE overviews SET status='ready' WHERE id IN (?,?)", (old, chatting))
            db.execute("UPDATE meeting_briefs SET status='ready' WHERE id IN (?,?,?)", (old, chatting, pending))
            db.execute("UPDATE overviews SET status='queued' WHERE id=?", (pending,))
            db.execute("INSERT INTO chat_turns(id,message_id,question) VALUES(?,?,?)", (chatting, "question", "What changed?"))
        self.store.expire_meeting_audio(self.send_to_fixture_trash)
        self.assertTrue(old_audio.is_file())
        self.store.set_meeting_audio_days(-1)
        self.assertEqual(self.store.expire_meeting_audio(self.send_to_fixture_trash), [])
        self.assertTrue(old_audio.is_file())
        self.assertEqual(SessionStore(self.store.root, read_only=True).meeting_audio_days(), -1)
        self.store.set_meeting_audio_days(7)
        self.assertEqual(self.store.expire_meeting_audio(self.send_to_fixture_trash), [])
        self.assertFalse(old_audio.exists())
        self.assertTrue(self.store.get(old)["transcript"]["text"])
        for path in (pending_audio, chatting_audio, active_audio, recent_audio, self.audio):
            self.assertTrue(path.is_file())

    def test_audio_only_trash_failure_retries_after_restart_without_retention_enabled(self):
        key, audio = self.create_meeting()
        def unavailable(_):
            raise OSError("Trash unavailable")

        when = self.store.remove_meeting_audio(key, trash=unavailable)
        staged = audio_trash_path(self.store.root, key)
        self.assertTrue((staged / audio.name).exists())
        self.assertFalse(audio.exists())
        reopened = SessionStore(self.store.root)
        self.assertIsNone(reopened.meeting_audio_days())
        self.assertEqual(reopened.expire_meeting_audio(self.send_to_fixture_trash), [])
        session = reopened.get(key)
        self.assertEqual(session["audioRemovedAt"], when)
        self.assertIn("Send the agenda.", session["transcript"]["text"])
        self.assertIn("Follow up Tuesday.", render_export(meeting_detail(get_meeting(reopened, key)), "notes"))
        self.assertFalse(staged.exists())
        self.assertTrue((self.trash / staged.name / audio.name).is_file())

    def test_audio_stage_recovery_distinguishes_uncommitted_and_legacy_committed_paths(self):
        aborted, audio = self.create_meeting()
        staged = audio_trash_path(self.store.root, aborted)
        staged.parent.mkdir()
        audio.parent.rename(staged)
        reopened = SessionStore(self.store.root)
        self.assertTrue(audio.is_file())
        self.assertFalse(staged.exists())
        self.assertIsNone(reopened.get(aborted).get("audioRemovedAt"))

        legacy, legacy_audio = reopened.create(self.original, "Legacy", "audio/wav", {
            "recording": True, "diarization": False, "meeting": True, "audioRemovedAt": "2000-01-01T00:00:00Z"
        }, 1)
        older_path = reopened.audio / (legacy + ".removing-audio")
        legacy_audio.parent.rename(older_path)
        migrated = SessionStore(self.store.root)
        self.assertTrue(older_path.is_dir())
        self.assertEqual(migrated.expire_meeting_audio(self.send_to_fixture_trash), [])
        self.assertFalse(older_path.exists())
        self.assertTrue((self.trash / audio_trash_path(self.store.root, legacy).name / legacy_audio.name).exists())

    def test_audio_retention_preserves_an_older_finite_setting_and_explicit_forever(self):
        with self.store.connection() as db:
            db.execute("INSERT INTO meeting_audio_retention VALUES(1,30)")
        upgraded = SessionStore(self.store.root)
        self.assertEqual(upgraded.meeting_audio_days(), 30)
        upgraded.set_meeting_audio_days(-1)
        with upgraded.connection() as db:
            self.assertIsNone(db.execute("SELECT days FROM meeting_audio_retention").fetchone())
        self.assertEqual(SessionStore(self.store.root).meeting_audio_days(), -1)

    def test_whole_meeting_retention_uses_end_time_and_preserves_work_in_progress(self):
        old, _ = self.create_meeting()
        legacy, _ = self.create_meeting()
        pending, _ = self.create_meeting()
        active, _ = self.create_meeting(status="processing")
        chatting, _ = self.create_meeting()
        future, _ = self.create_meeting()
        with self.store.connection() as db:
            db.execute("UPDATE sessions SET options=json_remove(options,'$.retentionApplies') WHERE id=?", (legacy,))
            db.executemany("UPDATE sessions SET options=json_set(options,'$.meetingEndedAt','2000-01-01T00:00:00Z') WHERE id=?",
                           [(key,) for key in (old, legacy, pending, active, chatting)])
            db.execute("UPDATE overviews SET status='ready' WHERE id IN (?,?,?)", (old, legacy, chatting))
            db.execute("UPDATE meeting_briefs SET status='ready' WHERE id IN (?,?,?,?)", (old, legacy, chatting, pending))
            db.execute("UPDATE overviews SET status='queued' WHERE id=?", (pending,))
            db.execute("INSERT INTO chat_turns(id,message_id,question) VALUES(?,?,?)", (chatting, "fixture", "What changed?"))
        reader = SessionStore(self.store.root, read_only=True)
        self.assertIsNone(self.store.meeting_retention_days())
        self.assertEqual({row["id"] for row in meetings(reader)}, {legacy, pending, active, chatting, future})
        for read in (reader.get, reader.media):
            with self.assertRaises(MissingSession):
                read(old)
        self.store.set_meeting_retention_days(7)
        self.assertEqual({row["id"] for row in meetings(reader)}, {pending, active, chatting, future})
        self.store.set_meeting_retention_days(0)
        self.assertEqual({row["id"] for row in meetings(reader)}, {old, legacy, pending, active, chatting, future})

    def test_explicit_exemption_protects_previously_misclassified_recordings(self):
        key, audio = self.create_meeting()
        with self.store.connection() as db:
            db.execute("""UPDATE sessions SET created='2000-01-01T00:00:00Z',
                       options=json_set(options,'$.retentionApplies',0) WHERE id=?""", (key,))
            db.execute("UPDATE overviews SET status='error' WHERE id=?", (key,))
            db.execute("UPDATE meeting_briefs SET status='error' WHERE id=?", (key,))
        self.store.set_meeting_retention_days(7)
        self.store.set_meeting_audio_days(7)
        self.assertEqual(self.store.expire_meetings(self.send_to_fixture_trash), [])
        self.assertEqual(self.store.expire_meeting_audio(self.send_to_fixture_trash), [])
        self.assertTrue(audio.is_file())
        self.assertIsNone(self.store.get(key)["meetingRetentionDays"])
        self.assertIsNone(self.store.list("", meetings_only=True)[0]["meetingRetentionDays"])

    def test_trash_snapshot_survives_failure_and_engine_restart(self):
        key, audio = self.create_meeting()
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0002 0003 01 01 1100 ffd9")
        user_export = self.root / "My edited transcript.md"
        user_export.write_text("My edits.", encoding="utf8")
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
            image = session_images.add(db, key, jpeg)
            meeting_brief.settle(db, key, 1, result={"title": "Schedule review", "summary": "Send the agenda."})
            db.execute("UPDATE sessions SET created='2000-01-01T00:00:00+00:00' WHERE id=?", (key,))

        def unavailable(_):
            raise OSError("Trash temporarily unavailable")

        self.assertEqual(self.store.expire_meetings(unavailable), [key])
        staged = archive_path(self.store.root, key)
        self.assertTrue(staged.is_dir())
        self.assertEqual((staged / "Audio" / audio.name).read_bytes(), audio.read_bytes())
        self.assertTrue(self.store.exists(key))  # The accepted stage ID cannot be reused.
        with self.assertRaises(MissingSession):
            get_meeting(self.store, key)
        self.assertIn("Send the agenda.", (staged / "Transcript.md").read_text())
        self.assertIn("Follow up Tuesday.", (staged / "Notes.md").read_text())
        self.assertIn("## Summary", (staged / "Transcript.md").read_text())
        manifest = json.loads((staged / "Meeting.json").read_text())
        self.assertEqual(manifest["id"], key)
        stored_image, = manifest["tables"]["session_images"]
        self.assertEqual(stored_image["id"], image["id"])
        restarted = SessionStore(self.store.root)
        trash = self.trash
        self.assertEqual(restarted.expire_meetings(lambda source: shutil.move(source, trash / source.name)), [])
        self.assertFalse(staged.exists())
        self.assertFalse(audio.exists())
        self.assertTrue((trash / staged.name / "Audio" / audio.name).exists())
        self.assertEqual((trash / staged.name / stored_image["file"]).read_bytes(), jpeg)
        self.assertEqual(user_export.read_text(), "My edits.")

    def test_snapshot_failure_preserves_originals_and_retries_without_duplicate_archives(self):
        key, audio = self.create_meeting()
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='ready' WHERE id=?", (key,))
            db.execute("UPDATE meeting_briefs SET status='ready' WHERE id=?", (key,))
            db.execute("UPDATE sessions SET created='2000-01-01T00:00:00+00:00' WHERE id=?", (key,))
        staged = archive_path(self.store.root, key)

        def incomplete(*args):
            staged.mkdir()
            (staged / "Transcript.md").write_text("Uncommitted transcript.")
            raise OSError("Write interrupted")

        with patch("meeting_retention.write_snapshot", side_effect=incomplete):
            self.assertEqual(self.store.expire_meetings(), [key])
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sessions WHERE id=?", (key,)).fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM meeting_trash WHERE id=?", (key,)).fetchone())
        self.assertTrue(audio.is_file())
        self.assertFalse(self.store.list("", meetings_only=True))
        self.assertEqual(self.store.expire_meetings(lambda path: shutil.move(path, self.root / "Recovered snapshot")), [])
        self.assertIn("Send the agenda.", (self.root / "Recovered snapshot" / "Transcript.md").read_text())
