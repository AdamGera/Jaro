import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import personal_notes
import writing_cache
from fastapi import FastAPI

from session_notes import RegenerationConflict, edit_overview, publish, selection
from session_store import SessionStore
from writing_api import router


def version(language, title):
    return {
        "language": language,
        "title": title,
        "summary": "Short lead",
        "markdown": "## Decisions\n- Friday",
        "topics": [{"id": 1, "start": 0, "label": "Decision", "detail": "Friday."}],
    }


class NoteStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = SessionStore(Path(temp.name) / "library")
        source = Path(temp.name) / "original.wav"
        source.write_bytes(b"audio")
        self.key, _ = self.store.create(
            source, source.name, "audio/wav", {"recording": False, "diarization": False}, 25
        )
        for end, lang in [(2, "English"), (25, "Arabic")]:
            self.store.append(self.key, dict(text="Speech", language=lang, words=[], segments=[]), end)
        self.store.finish(self.key)

    def choose(self, lang, retry=False):
        with self.store.connection() as db:
            return selection(db, self.key, lang, retry)

    def complete(self, lang, title):
        with self.store.connection() as db:
            publish(db, self.key, lang, version(lang, title))

    def test_duration_weighted_language_switches_survive_late_results_and_restart(self):
        self.assertEqual(self.store.get(self.key)["overview"]["autoLanguage"], "ar")
        self.complete("ar", "اجتماع")
        self.assertEqual(self.choose("en")["version"]["title"], "اجتماع")
        self.choose("he")
        self.complete("en", "Friday decision")
        self.assertEqual(self.store.get(self.key)["name"], "اجتماع")
        self.complete("he", "פגישה")
        self.assertEqual(self.choose("en")["version"]["title"], "Friday decision")
        self.store = SessionStore(self.store.root)
        self.assertEqual(self.store.get(self.key)["audioName"], "original.wav")
        self.assertEqual(self.store.list("original.wav")[0]["name"], "Friday decision")
        self.assertEqual(self.store.list("Friday")[0]["id"], self.key)
        self.assertEqual(self.choose("auto")["version"]["title"], "اجتماع")

    def test_old_completed_session_queues_only_when_opened(self):
        with self.store.connection() as db:
            db.execute("DELETE FROM notes")
        self.assertEqual(self.store.list("")[0]["pendingOverview"], 0)
        self.assertEqual(self.store.get(self.key)["overview"]["status"], "queued")
        self.assertEqual(self.store.list("")[0]["pendingOverview"], 1)

    def test_translation_after_failed_base_can_retry_and_be_deleted_while_running(self):
        with self.store.connection() as db:
            db.execute("UPDATE overviews SET status='error',error='test'")
        self.assertEqual(self.choose("he")["status"], "blocked")
        self.assertEqual(self.choose("he", retry=True)["status"], "queued")
        self.store.delete(self.key)
        self.complete("he", "Late result")
        with self.store.connection() as db:
            for table in ("notes", "overviews", "window_notes", "chat_turns"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def duplicate(self, name, delta=0):
        source = self.store.root / name
        source.write_bytes(b"identical audio")
        key, _ = self.store.create(source, name, "audio/wav", {"recording": False, "diarization": False}, 25)
        for end, lang in [(2 + delta, "English"), (25, "Arabic")]:
            self.store.append(key, dict(text="Speech", language=lang, words=[], segments=[]), end)
        self.store.finish(key)
        return key

    def test_personal_notes_snapshot_and_language_update_are_independent(self):
        self.complete("ar", "Arabic overview")
        self.choose("en")
        self.complete("en", "English overview")

        def edit(text, revision):
            return personal_notes.NoteEdit(revision=revision, document={"type": "doc", "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": text}]}]})

        with self.store.connection() as db:
            self.assertEqual(personal_notes.save(db, self.key, edit("Follow up on pricing", 0)), 1)
            self.assertIsNone(personal_notes.save(db, self.key, edit("A competing edit", 0)))
            self.assertTrue(selection(db, self.key)["notesChanged"])
            queued = selection(db, self.key, "en", update_personal=True)
            self.assertEqual(queued["version"]["title"], "English overview")
            self.assertEqual(queued["scope"], "overview")
            self.assertTrue(queued["updatingPersonal"])
            personal_notes.save(db, self.key, edit("Follow up on delivery", 1))
            snapshot = db.execute("SELECT queued_personal FROM overviews WHERE id=? AND lang='en'",
                                  (self.key,)).fetchone()[0]
            self.assertEqual(snapshot, "## Follow up on pricing")
            from chat_context import snapshot as chat_snapshot
            self.assertEqual(chat_snapshot(db, self.key)["personal"], "## Follow up on delivery")
            publish(db, self.key, "en", version("en", "English update"))
            self.assertTrue(selection(db, self.key)["notesChanged"])
            selection(db, self.key, "en", update_personal=True)
            publish(db, self.key, "en", version("en", "English update"))
            self.assertFalse(selection(db, self.key)["notesChanged"])
            self.assertTrue(selection(db, self.key, "auto")["notesChanged"])
            personal_notes.save(db, self.key, personal_notes.NoteEdit(revision=2, document=personal_notes.EMPTY))
            self.assertTrue(selection(db, self.key, "en")["notesChanged"])

    def test_manual_overview_is_local_and_survives_timeline_publication(self):
        with self.store.connection() as db:
            original = version("ar", "Session title")
            original["markdown"] = "Original text ⟦2⟧"
            publish(db, self.key, "ar", original)
            revision = selection(db, self.key)["editRevision"]
            with self.assertRaisesRegex(ValueError, "timestamps"):
                edit_overview(db, self.key, "ar", revision, "Manual lead", "New ⟦99⟧")
            edit_overview(db, self.key, "ar", revision, "Manual lead", "Edited text ⟦2⟧")
            # A retry of the same successful write must be safe.
            edit_overview(db, self.key, "ar", revision, "Manual lead", "Edited text ⟦2⟧")
            with self.assertRaisesRegex(RegenerationConflict, "changed"):
                edit_overview(db, self.key, "ar", revision, "Stale edit", "")
            from chat_context import snapshot
            self.assertEqual(snapshot(db, self.key)["note"]["summary"], "Manual lead")

        peer = self.duplicate("same-audio.wav")
        self.assertIsNone(self.store.get(peer)["overview"]["version"])
        with self.store.connection() as db:
            selection(db, self.key, "ar", regenerate="timeline")
            publish(db, self.key, "ar", version("ar", "Timeline output"))
            saved = selection(db, self.key)["version"]
            self.assertEqual(saved["title"], "Session title")
            self.assertEqual(saved["markdown"], "Edited text ⟦2⟧")
            selection(db, self.key, "ar", regenerate="overview")
            publish(db, self.key, "ar", original)
            self.assertEqual(selection(db, self.key)["version"], original)

    def test_personal_checklist_round_trips_and_keeps_completion_in_context(self):
        def item(text, checked):
            return {"type": "taskItem", "attrs": {"checked": checked}, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

        document = {"type": "doc", "content": [
            {"type": "taskList", "content": [item("Read proposal", True), item("Send feedback", False)]}]}
        edit = personal_notes.NoteEdit(document=document, revision=0)
        with self.store.connection() as db:
            self.assertEqual(personal_notes.save(db, self.key, edit), 1)
            self.assertEqual(personal_notes.get(db, self.key)["document"], document)
            self.assertEqual(personal_notes.as_context(db, self.key), "- [x] Read proposal\n- [ ] Send feedback")

    def test_initial_overview_cannot_reuse_identical_audio_with_different_personal_notes(self):
        self.complete("ar", "Original recording")
        with self.store.connection() as db:
            draft = personal_notes.stage(db, {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "My private context"}]}]})
        source = self.store.root / "with-notes.wav"
        source.write_bytes(b"audio")
        key, _ = self.store.create(source, source.name, "audio/wav", {"recording": False, "diarization": False},
                                   25, notes_draft=draft)
        for end, lang in [(2, "English"), (25, "Arabic")]:
            self.store.append(key, dict(text="Speech", language=lang, words=[], segments=[]), end)
        self.store.finish(key)
        saved = self.store.get(key)
        self.assertEqual(saved["overview"]["status"], "queued")
        self.assertEqual(saved["personalNotes"]["revision"], 1)
        with self.store.connection() as db:
            row = db.execute("SELECT queued_personal FROM overviews WHERE id=?", (key,)).fetchone()
            self.assertEqual(row[0], "My private context")

    def test_different_speaker_activity_does_not_reuse_a_peer_overview(self):
        self.complete("ar", "First recording")
        source = self.store.root / "different-speakers.wav"
        source.write_bytes(b"audio")
        key, _ = self.store.create(source, source.name, "audio/wav", {"recording": False, "diarization": True}, 25)
        for begin, end, lang in [(0, 2, "English"), (2, 25, "Arabic")]:
            self.store.append(key, dict(text="Speech", language=lang, words=[], segments=[],
                                        activity=[{"speaker": 1, "start": begin, "end": end}]), end)
        self.store.finish(key)
        self.assertEqual(self.store.get(key)["overview"]["status"], "queued")

    def test_exact_peer_reuses_artifacts_but_changed_anchors_miss(self):
        with writing_cache.scope(self.store, self.key):
            writing_cache.remember("summary-stage", {"result": "decision"})
        with writing_cache.scope(self.store, self.key, "chat"):
            writing_cache.remember("private-chat-stage", {"result": "private"})
        with self.store.connection() as db:
            db.execute("INSERT INTO window_notes VALUES(?,?,?)", (self.key, 0, "{}"))
        self.complete("ar", "اجتماع")
        self.choose("en")
        self.complete("en", "Friday decision")
        twin = self.duplicate("duplicate.wav")
        changed = self.duplicate("retimed.wav", delta=1)
        self.assertEqual(self.store.get(twin)["overview"]["status"], "ready")
        self.assertEqual(self.store.get(changed)["overview"]["status"], "queued")
        with self.store.connection() as db:
            self.assertEqual(selection(db, twin, "en")["version"]["title"], "Friday decision")
            self.assertEqual(
                db.execute("SELECT count(*) FROM window_notes WHERE id=?", (twin,)).fetchone()[0], 1
            )
        self.store.delete(self.key)
        self.store = SessionStore(self.store.root)
        with writing_cache.scope(self.store, twin):
            self.assertEqual(writing_cache.recall("summary-stage"), {"result": "decision"})
            self.assertIsNone(writing_cache.recall("private-chat-stage"))
        self.store.delete(twin)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writing_results").fetchone()[0], 0)

    def test_regeneration_is_private_atomic_and_invalidates_only_derived_versions(self):
        self.complete("ar", "Original")
        self.choose("en")
        self.complete("en", "English")
        twin = self.duplicate("peer.wav")
        with self.store.connection() as db:
            selection(db, twin, "en")
            db.executemany("INSERT INTO window_notes VALUES(?,?,?)", [(self.key, i, "{}") for i in (0, -1, -2, -3, -4)])
            value = selection(db, self.key, "en", regenerate=True)
            self.assertEqual((value["status"], value["version"]["title"]), ("queued", "English"))
            selection(db, self.key, "en", regenerate=True)
            self.assertEqual(db.execute("SELECT generation FROM overviews WHERE id=? AND lang='en'", (self.key,)).fetchone()[0], 1)
            self.assertEqual(selection(db, self.key, "auto")["status"], "ready")
            self.assertEqual(db.execute("SELECT count(*) FROM window_notes WHERE id=?", (self.key,)).fetchone()[0], 5)
        self.complete("en", "New translation")
        with self.store.connection() as db:
            value = selection(db, self.key, "auto", regenerate=True)
            self.assertEqual((value["status"], value["version"]["title"]), ("queued", "Original"))
            self.assertEqual([r[0] for r in db.execute("SELECT part FROM window_notes WHERE id=?", (self.key,))], [0])
            self.assertEqual(selection(db, self.key, "en")["version"]["title"], "New translation")
        self.complete("ar", "New native")
        with self.store.connection() as db:
            updated = selection(db, self.key, "en")
            self.assertEqual((updated["status"], updated["version"]["title"]), ("queued", "New translation"))
            self.assertEqual(selection(db, twin, "auto")["version"]["title"], "Original")
            self.assertEqual(selection(db, twin, "en")["version"]["title"], "English")
        with writing_cache.scope(self.store, self.key):
            writing_cache.remember("existing", {"title": "automatic"})
            with writing_cache.scope(self.store, self.key, fresh=True):
                self.assertIsNone(writing_cache.recall("existing"))
                writing_cache.remember("existing", {"title": "regenerated"})
            self.assertEqual(writing_cache.recall("existing"), {"title": "automatic"})

    def test_scoped_publication_preserves_sibling_and_other_language_after_restart(self):
        saved = version("ar", "Original")
        saved["extra"] = "legacy metadata"
        with self.store.connection() as db:
            publish(db, self.key, "ar", saved)
        self.choose("en")
        self.complete("en", "English")
        with self.store.connection() as db:
            selection(db, self.key, "auto", regenerate="timeline")
            selection(db, self.key, "auto", regenerate="timeline")
            with self.assertRaises(RegenerationConflict):
                selection(db, self.key, "auto", regenerate="overview")
        self.store = SessionStore(self.store.root)
        with self.store.connection() as db:
            value = selection(db, self.key)
            self.assertEqual((value["scope"], value["version"]), ("timeline", saved))
            self.assertEqual(db.execute("SELECT generation FROM overviews WHERE id=? AND lang='ar'",
                                        (self.key,)).fetchone()[0], 1)
            new_topics = [{"id": 2, "start": 6, "label": "New chapter"}]
            publish(db, self.key, "ar", {**version("ar", "Unused"), "summary": "Unused", "topics": new_topics})
            expected = {**saved, "topics": new_topics}
            self.assertEqual(selection(db, self.key)["version"], expected)
            selection(db, self.key, "auto", regenerate="overview")
            db.execute("UPDATE overviews SET status='error' WHERE id=? AND lang='ar'", (self.key,))
            retry = selection(db, self.key, "auto", retry=True)
            self.assertEqual((retry["scope"], retry["version"]), ("overview", expected))
            publish(db, self.key, "ar", {**version("ar", "Unused"), "summary": "New lead", "markdown": "New text"})
            self.assertEqual(selection(db, self.key)["version"], {**expected, "summary": "New lead", "markdown": "New text"})
            other = selection(db, self.key, "en")
            self.assertEqual((other["status"], other["scope"], other["version"]), ("ready", None, version("en", "English")))

    def test_scope_migration_preserves_old_published_session(self):
        self.complete("ar", "Saved")
        with self.store.connection() as db:
            db.execute("ALTER TABLE overviews DROP COLUMN scope")
        migrated = SessionStore(self.store.root).get(self.key)["overview"]
        self.assertIsNone(migrated["scope"])
        self.assertEqual(migrated["version"], version("ar", "Saved"))

    def test_refinalize_updates_transcript_and_bounds(self):
        self.complete("ar", "Old title")
        self.store.append(self.key, dict(text="Late decision", language="English", words=[], segments=[]), 50)
        self.store.finish(self.key)
        saved = self.store.get(self.key)
        self.assertIn("Late decision", saved["transcript"]["text"])
        self.assertIsNone(saved["overview"]["version"])
        self.assertEqual(saved["overview"]["autoLanguage"], "en")

    def test_upgrade_preserves_published_notes_until_explicit_regeneration(self):
        self.complete("ar", "عربي")
        self.choose("en")
        self.complete("en", "Decision")
        with self.store.connection() as db:
            db.execute("UPDATE notes SET revision='initial-test-build' WHERE id=?", (self.key,))
        self.assertEqual(self.choose("en")["status"], "ready")
        self.assertEqual(self.store.get(self.key)["overview"]["version"]["title"], "Decision")
        twin = self.duplicate("after-upgrade.wav")
        self.assertEqual(self.store.get(twin)["overview"]["status"], "queued")
        with self.store.connection() as db:
            result = selection(db, self.key, "auto", regenerate=True)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["version"]["title"], "عربي")
            from insights import PIPELINE_REVISION
            self.assertEqual(db.execute("SELECT revision FROM notes WHERE id=?", (self.key,)).fetchone()[0], PIPELINE_REVISION)


class FakeWriter:
    def __init__(self):
        self.calls = 0
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def chat_priority(self):
        async with self.lock:
            yield

    async def chat_tokens(self, key, question, history, context=None):
        self.calls += 1
        await asyncio.sleep(0.01)
        yield "Friday "
        yield "⟦0.0⟧"


class ChatPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_retry_and_cross_session_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "library")
            ids = []
            for i in range(2):
                source = Path(directory) / f"source-{i}.wav"
                source.write_bytes(b"audio")
                key, _ = store.create(
                    source, source.name, "audio/wav", {"recording": False, "diarization": False}, 2
                )
                store.append(key, dict(text="Friday", language="English", words=[], segments=[]), 2)
                store.finish(key)
                ids.append(key)
            app = FastAPI()
            app.include_router(router)
            app.state.library = store
            app.state.insights = FakeWriter()
            app.state.insights.wake = asyncio.Event()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                path = f"/api/sessions/{ids[0]}/chat"
                request = {"id": "question-1", "question": "When?"}
                self.assertEqual((await client.post(path, json=request)).text, "Friday ⟦0.0⟧")
                self.assertEqual((await client.post(path, json=request)).text, "Friday ⟦0.0⟧")
                messages = (await client.get(path)).json()
                self.assertEqual(len(messages), 2)
                self.assertEqual(messages[1]["metadata"]["status"], "complete")
                self.assertEqual((await client.get(f"/api/sessions/{ids[1]}/chat")).json(), [])
                self.assertEqual(
                    (await client.post(path, json={**request, "question": "Different?"})).status_code, 409
                )
                self.assertEqual(
                    (
                        await client.post(path, json=request, headers={"origin": "https://example.com"})
                    ).status_code,
                    403,
                )
                twin_path = f"/api/sessions/{ids[1]}/chat"
                await client.post(twin_path, json={**request, "id": "different-client-id"})
                self.assertEqual(app.state.insights.calls, 1)
                await client.post(twin_path, json={**request, "id": "next-turn"})
                self.assertEqual(app.state.insights.calls, 2)
                await client.post(path, json={**request, "id": "original-followup"})
                self.assertEqual(app.state.insights.calls, 2)

                # Simultaneous requests in two independent sessions share a single final answer.
                concurrent = {"id": "shared-request", "question": "Who confirmed?"}
                responses = await asyncio.gather(
                    client.post(path, json=concurrent), client.post(twin_path, json=concurrent)
                )
                self.assertEqual([r.text for r in responses], ["Friday ⟦0.0⟧"] * 2)
                self.assertEqual(app.state.insights.calls, 3)
                # Historical answers survive transcript changes without stale timestamp links.
                store.append(ids[0], dict(text="Updated", language="English", words=[], segments=[]), 5)
                store.finish(ids[0])
                self.assertEqual((await client.get(path)).json()[1]["parts"][0]["text"], "Friday")
                self.assertIn("⟦", (await client.get(twin_path)).json()[1]["parts"][0]["text"])

    async def test_exact_chat_replay_changes_with_displayed_note_and_regeneration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "library")
            p = Path(directory) / "fixture"
            p.write_bytes(b"fixture")
            key, _ = store.create(p, "test.wav", "audio/wav", {"recording": False, "diarization": False}, 2)
            store.append(key, {"text": "Friday", "language": "English", "words": [], "segments": []}, 2)
            store.finish(key)
            app = FastAPI()
            app.include_router(router)
            app.state.library = store
            app.state.insights = FakeWriter()
            path = f"/api/sessions/{key}/chat"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
                async def ask(expected):
                    r = await c.post(path, json={"id": "repeat", "question": "What is the exact title?"})
                    self.assertTrue(r.is_success)
                    self.assertEqual(app.state.insights.calls, expected)
                await ask(1)
                with store.connection() as db:
                    publish(db, key, "en", version("en", "Friday"))
                await ask(2)
                await ask(2)
                with store.connection() as db:
                    publish(db, key, "es", version("es", "Viernes"))
                    selection(db, key, "es")
                await ask(3)
                with store.connection() as db:
                    selection(db, key, "es", regenerate=True)
                    publish(db, key, "es", version("es", "Viernes"))
                await ask(4)
                await ask(4)

    async def test_chat_keeps_partial_answers_and_a_retryable_error(self):
        class Interrupted(FakeWriter):
            async def chat_tokens(self, *args):
                yield "Part of the response"
                raise RuntimeError("Local writing model could not start.")

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "library")
            p = Path(directory) / "fixture"
            p.write_bytes(b"fixture")
            key, _ = store.create(p, "test.wav", "audio/wav", {"recording": False, "diarization": False}, 2)
            store.append(key, {"text": "Friday", "language": "English", "words": [], "segments": []}, 2)
            store.finish(key)
            app = FastAPI()
            app.include_router(router)
            app.state.library = store
            app.state.insights = Interrupted()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
                path = f"/api/sessions/{key}/chat"
                turn = {"id": "test-error", "question": "When?"}
                self.assertEqual((await c.post(path, json=turn)).text, "Part of the response")
                last = (await c.get(path)).json()[-1]
                self.assertEqual(last["parts"][0]["text"], "Part of the response")
                self.assertEqual(last["metadata"], {"status": "error", "error": "Local writing model could not start."})
                app.state.insights = FakeWriter()
                await c.post(path, json=turn)
                self.assertEqual((await c.get(path)).json()[-1]["metadata"], {"status": "complete"})

    async def test_live_note_draft_upsert_promotion_and_overview_handoff(self):
        from uuid import uuid4
        from insights import GUIDE

        def doc(text):
            return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "library")
            app = FastAPI()
            app.include_router(router)
            app.state.library = store
            app.state.insights = FakeWriter()
            app.state.insights.wake = asyncio.Event()
            draft_id = str(uuid4())
            draft_url = f"/api/personal-note-drafts/{draft_id}"
            session_url = f"/api/sessions/{draft_id}/personal-notes"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                initial = (await client.get(draft_url)).raise_for_status().json()
                self.assertEqual(initial, {"document": personal_notes.EMPTY, "revision": 0, "promoted": False})

                self.assertEqual(
                    (await client.put(draft_url, json={"document": doc("Bad"), "revision": 0},
                                      headers={"origin": "https://example.com"})).status_code,
                    403,
                )
                first = (await client.put(draft_url, json={"document": doc("Key point 1"), "revision": 0})).raise_for_status().json()
                self.assertEqual(first, {"revision": 1, "promoted": False})
                # Idempotent duplicate write keeps the same revision.
                dup = (await client.put(draft_url, json={"document": doc("Key point 1"), "revision": 1})).raise_for_status().json()
                self.assertEqual(dup, {"revision": 1, "promoted": False})
                # Stale revision triggers 409 Conflict.
                self.assertEqual(
                    (await client.put(draft_url, json={"document": doc("Stale"), "revision": 0})).status_code,
                    409,
                )
                second = (await client.put(draft_url, json={"document": doc("Key point 1 + 2"), "revision": 1})).raise_for_status().json()
                self.assertEqual(second, {"revision": 2, "promoted": False})
                hydrated = (await client.get(draft_url)).raise_for_status().json()
                self.assertEqual(hydrated, {"document": doc("Key point 1 + 2"), "revision": 2, "promoted": False})

                # Promote draft into a processing session preserving revision continuity.
                audio = Path(directory) / "recording.wav"
                audio.write_bytes(b"audio")
                key, _ = store.create(
                    audio, "Recording.wav", "audio/wav", {"recording": True, "diarization": False},
                    12, key=draft_id,
                )
                self.assertEqual(key, draft_id)
                self.assertEqual(store.get(draft_id)["personalNotes"]["revision"], 2)

                # A racing PUT to the draft endpoint after stop transparently writes through to personal_notes.
                promoted = (await client.put(draft_url, json={"document": doc("Typed right after stop"), "revision": 2})).raise_for_status().json()
                self.assertEqual(promoted, {"revision": 3, "promoted": True})
                promoted_get = (await client.get(draft_url)).raise_for_status().json()
                self.assertEqual(promoted_get, {"document": doc("Typed right after stop"), "revision": 3, "promoted": True})

                # Finishing transcription queues the initial overview with the latest personal notes.
                store.append(draft_id, {"text": "Spoken meeting content", "language": "English", "words": [], "segments": []}, 12)
                store.finish(draft_id)
                with store.connection() as db:
                    queued = db.execute("SELECT queued_personal FROM overviews WHERE id=?", (draft_id,)).fetchone()[0]
                self.assertEqual(queued, "Typed right after stop")

                # Trailing edit before any writer checkpoint updates queued_personal in-place.
                trailing = (await client.put(session_url, json={"document": doc("Trailing note before start"), "revision": 3})).raise_for_status().json()
                self.assertEqual(trailing, {"revision": 4})
                with store.connection() as db:
                    queued = db.execute("SELECT queued_personal FROM overviews WHERE id=?", (draft_id,)).fetchone()[0]
                    self.assertEqual(queued, "Trailing note before start")
                    # Once GUIDE checkpoint exists, further edits leave queued_personal frozen.
                    db.execute("INSERT INTO window_notes(id,part,data) VALUES(?,?,?)", (draft_id, GUIDE, "{}"))

                later = (await client.put(session_url, json={"document": doc("Edit after writer started"), "revision": 4})).raise_for_status().json()
                self.assertEqual(later, {"revision": 5})
                with store.connection() as db:
                    queued = db.execute("SELECT queued_personal FROM overviews WHERE id=?", (draft_id,)).fetchone()[0]
                    self.assertEqual(queued, "Trailing note before start")
                    self.assertTrue(selection(db, draft_id)["notesChanged"])
