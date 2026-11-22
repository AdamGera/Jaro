from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from annotation_assets import annotation_origin
from annotation_deliveries import AnnotationDeliveries, ReceiptError, UNDO_SECONDS
from annotation_routing import route_scopes, suggested_session


class AnnotationDeliveryTests(unittest.TestCase):
    def test_read_only_listing_and_expiry_after_receipt_store_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            self.assertEqual(store.recent("capture"), [])
            self.assertFalse(store.path.exists())
            with patch("annotation_deliveries.time", return_value=100):
                key = store.begin("capture", "service", {"id": "ses_one", "title": "First"}, True)
                self.assertEqual(store.get(key)["status"], "unconfirmed")
                self.assertFalse(store.get(key)["canUndo"])
                store.change(key, "queued", "msg_one")
            reopened = AnnotationDeliveries(folder)
            with patch("annotation_deliveries.time", return_value=100 + UNDO_SECONDS - 1):
                self.assertTrue(reopened.recent("capture")[0]["canUndo"])
            with patch("annotation_deliveries.time", return_value=100 + UNDO_SECONDS):
                self.assertFalse(reopened.get(key)["canUndo"])

    def test_old_receipt_migrates_when_correction_is_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            store.path.touch(mode=0o600)
            with closing(sqlite3.connect(store.path)) as db, db:
                db.execute("""CREATE TABLE deliveries(id TEXT PRIMARY KEY, annotation TEXT NOT NULL, service TEXT NOT NULL,
                    destination TEXT NOT NULL, message TEXT, state TEXT NOT NULL, created REAL NOT NULL, included_sheet INTEGER NOT NULL)""")
                db.execute("INSERT INTO deliveries VALUES('receipt','capture','service','{}','msg_one','received',100,0)")
            with patch("annotation_deliveries.time", return_value=101):
                before = store.get("receipt")
                self.assertTrue(before["canCorrect"])
                self.assertIsNone(before["correctionMessage"])
                result = store.change("receipt", "corrected", correction_message="msg_correction")
                self.assertEqual(result["message"], "msg_one")
                self.assertEqual(result["correctionMessage"], "msg_correction")
                self.assertFalse(result["canCorrect"])
                self.assertEqual(AnnotationDeliveries(folder).get("receipt"), result)

    def test_symlink_cannot_be_opened_as_receipt_store(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            unrelated = Path(folder) / "private"
            unrelated.write_text("leave this alone")
            store.path.symlink_to(unrelated)
            with self.assertRaises(ReceiptError):
                store.recent("capture")
            with self.assertRaises(ReceiptError):
                store.begin("capture", "service", {}, False)
            self.assertEqual(unrelated.read_text(), "leave this alone")

    def test_capture_scope_and_exact_mention_preserve_unambiguous_priority(self):
        detail = {"transcript": "The Release Review notes", "origin":
                  annotation_origin({"app": "com.example.browser", "site": "example.com.", "name": "Browser"})}
        self.assertEqual([item["kind"] for item in route_scopes(detail)], ["site", "app"])
        candidates = [{"id": "ses_release", "title": "Release Review", "directory": "/app/Other"},
                      {"id": "ses_project", "title": "Tomorrow", "directory": "/app/Browser"}]
        self.assertEqual(suggested_session(detail, candidates)["sessionId"], "ses_release")
        detail["transcript"] = "Review notes"
        self.assertEqual(suggested_session(detail, candidates)["sessionId"], "ses_project")
        origin = annotation_origin({"app": "invalid/no", "name": "invalid"})
        self.assertIsNone(origin)
        stripped = annotation_origin({"app": "com.example.browser", "site": "example.com/private"})
        self.assertNotIn("site", stripped)

    def test_saved_rule_follows_origin_across_captures_and_requires_exact_id_to_forget(self):
        with tempfile.TemporaryDirectory() as root:
            first = {"id": "older", "origin": {"app": "org.example.web", "name": "Web", "site": "first.example"}}
            second = {"id": "newer", "origin": {"app": "org.example.web", "name": "Web", "site": "first.example"}}
            different_site = {"id": "third", "origin": {**first["origin"], "site": "second.example"}}
            scopes = route_scopes(first)
            store = AnnotationDeliveries(root)
            browser = store.remember_rule(scopes[1], "Browser", "ses_browser", "service.json")
            site = store.remember_rule(scopes[0], "Site", "ses_site", "service-dev.json")
            reopened = AnnotationDeliveries(root)
            self.assertEqual(reopened.preferred_rule(route_scopes(second)), site)
            self.assertEqual(reopened.preferred_rule(route_scopes(different_site)), browser)
            self.assertFalse(reopened.forget_rule(route_scopes(different_site), site["id"]))
            self.assertTrue(reopened.forget_rule(route_scopes(second), site["id"]))
            self.assertEqual(reopened.preferred_rule(route_scopes(second)), browser)

    def test_terminal_rules_follow_path_components_and_prefer_the_nearest_project(self):
        def capture(folder, project):
            return {"origin": annotation_origin({"app": "com.mitchellh.ghostty", "name": "Ghostty",
                                                 "terminal": {"folder": folder, "project": project}})}
        outer = capture("/fixture/team", "/fixture/team")
        inner = capture("/fixture/team/Jard/sub", "/fixture/team/Jard")
        sibling = capture("/fixture/teamwork", "/fixture/teamwork")
        with tempfile.TemporaryDirectory() as root:
            store = AnnotationDeliveries(root)
            broad = store.remember_rule(route_scopes(outer)[0], "Team", "ses_team", "service.json", True)
            self.assertEqual(store.preferred_rule(route_scopes(inner)), broad)
            self.assertIsNone(store.preferred_rule(route_scopes(sibling)))
            exact = store.remember_rule(route_scopes(inner)[0], "Jard", "ses_jard", "service.json")
            self.assertEqual(store.preferred_rule(route_scopes(inner)), exact)
            self.assertTrue(store.forget_rule(route_scopes(inner), broad["id"]))
            self.assertEqual(store.preferred_rule(route_scopes(inner)), exact)
        self.assertEqual(route_scopes(capture("/", "/")), [])
        self.assertEqual(route_scopes(capture(str(Path.home()), str(Path.home()))), [])
        self.assertEqual(route_scopes(capture("/fixture/sibling", "/fixture/team")), [])

    def test_send_and_remember_activates_with_receipt_and_undo_restores_the_exact_scope(self):
        with tempfile.TemporaryDirectory() as root:
            store = AnnotationDeliveries(root)
            scopes = route_scopes({"origin": {"app": "org.example.Browser", "name": "Browser", "site": "first.example"}})
            site, app = scopes
            broad = store.remember_rule(app, "Browser", "ses_browser", "service.json")
            original = store.remember_rule(site, "Original", "ses_original", "service.json", True)
            delivery = store.begin("capture", "service", {"title": "Chosen", "id": "ses_chosen"}, False, remember_scope=site)
            self.assertEqual(store.preferred_rule(scopes), original)
            self.assertEqual(store.get(delivery)["rememberedRule"]["status"], "pending")
            confirmed = store.confirm_remembered_delivery(delivery, "msg_one", "service.json")
            self.assertEqual(confirmed["status"], "queued")
            self.assertEqual(confirmed["rememberedRule"], {"status": "saved", "label": "first.example"})
            self.assertEqual(store.preferred_rule(scopes)["id"], delivery)
            self.assertEqual(store.preferred_rule([app]), broad)
            self.assertEqual(AnnotationDeliveries(root).undo_remembered_rule(delivery)["rememberedRule"]["status"], "restored")
            restored = store.preferred_rule(scopes)
            self.assertEqual((restored["target"], restored["automatic"]), (original["target"], original["automatic"]))
            self.assertNotEqual(restored["id"], original["id"])
            self.assertEqual(store.undo_remembered_rule(delivery)["rememberedRule"]["status"], "restored")
            self.assertEqual(store.preferred_rule(scopes), restored)

    def test_send_and_remember_respects_newer_edits_and_deadline(self):
        with tempfile.TemporaryDirectory() as root:
            store = AnnotationDeliveries(root)
            scope = {"kind": "site", "key": "first.example", "label": "first.example"}
            destination = {"title": "Chosen", "id": "ses_chosen"}
            delivery = store.begin("capture", "service", destination, False, remember_scope=scope)
            newer = store.remember_rule(scope, "Newer", "ses_newer", "service.json")
            confirmed = store.confirm_remembered_delivery(delivery, "msg_one", "service.json")
            self.assertEqual(confirmed["rememberedRule"]["status"], "changed")
            self.assertEqual(store.preferred_rule([scope]), newer)

            delivery = store.begin("another", "service", destination, False, remember_scope=scope)
            store.confirm_remembered_delivery(delivery, "msg_two", "service.json")
            newest = store.remember_rule(scope, "Newest", "ses_newest", "service.json")
            self.assertEqual(store.undo_remembered_rule(delivery)["rememberedRule"]["status"], "changed")
            self.assertEqual(store.preferred_rule([scope]), newest)

            self.assertTrue(store.forget_rule([scope], newest["id"]))
            with patch("annotation_deliveries.time", return_value=100):
                delivery = store.begin("first", "service", destination, False, remember_scope=scope)
                receipt = store.confirm_remembered_delivery(delivery, None, "service.json")
                self.assertTrue(receipt["canUndo"])
            with patch("annotation_deliveries.time", return_value=100 + UNDO_SECONDS):
                self.assertEqual(store.undo_remembered_rule(delivery)["rememberedRule"]["status"], "saved")
            with patch("annotation_deliveries.time", return_value=100 + UNDO_SECONDS - 1):
                self.assertEqual(store.undo_remembered_rule(delivery)["rememberedRule"]["status"], "removed")
                self.assertIsNone(store.preferred_rule([scope]))

    def test_terminal_suggestion_prefers_the_visible_title_over_stale_command_arguments(self):
        origin = annotation_origin({"app": "com.apple.Terminal", "name": "Terminal", "terminal":
            {"folder": "/fixture/Jard", "project": "/fixture/Jard", "title": "Release review", "session": "ses_stale"}})
        candidates = [
            {"id": "ses_stale", "title": "Earlier work", "directory": "/fixture/Jard"},
            {"id": "ses_visible", "title": "Release review", "directory": "/fixture/Jard"},
            {"id": "ses_other", "title": "Release review", "directory": "/fixture/Other"},
        ]
        detail = {"origin": origin, "transcript": "Earlier work"}
        self.assertEqual(suggested_session(detail, candidates), {
            "sessionId": "ses_visible", "reason": "Open in the captured terminal"})
        detail["origin"]["terminal"]["title"] = "Release rev…"
        self.assertEqual(suggested_session(detail, candidates)["sessionId"], "ses_visible")
        detail["origin"]["terminal"].pop("title")
        self.assertEqual(suggested_session(detail, candidates)["sessionId"], "ses_stale")
