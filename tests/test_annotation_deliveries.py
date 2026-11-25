from contextlib import closing
from dataclasses import replace
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from annotation_assets import annotation_origin
from annotation_deliveries import AnnotationDeliveries, ReceiptError, UNDO_SECONDS
from annotation_routing import Registration, RoutingError, annotation_rule_scope, owns_listener, route_scopes, suggested_session


class AnnotationDeliveryTests(unittest.TestCase):
    def test_website_rules_match_the_longest_path_with_scheme_and_port(self):
        from annotation_routing import captured_rule_scope

        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            addresses = ("docs.example.test", "docs.example.test/team", "docs.example.test:8443/team",
                         "https://docs.example.test:8443/team", "docs.example.test/team/release")
            for index, value in enumerate(addresses):
                scope = annotation_rule_scope("site", value, "darwin")
                store.remember_rule(scope, value, f"ses_{index}", "service.json")

            def capture(address):
                return {"origin": annotation_origin({"app": "com.fixture.browser", "siteUrl": address})}

            private = capture("https://user:password@docs.example.test:8443/team/release/new?secret=query#private")
            self.assertEqual(private["origin"]["siteUrl"], "https://docs.example.test:8443/team/release/new")
            self.assertEqual(store.preferred_rule(route_scopes(private))["target"], "ses_4")
            stored = store.preferred_rule(route_scopes(capture("https://docs.example.test:8443/team/other")))
            self.assertEqual(stored["target"], "ses_3")
            self.assertEqual(store.preferred_rule(route_scopes(capture("http://docs.example.test:8443/team/other")))["target"], "ses_2")
            self.assertEqual(store.preferred_rule(route_scopes(capture("https://docs.example.test/team-tools")))["target"], "ses_0")
            self.assertIsNone(captured_rule_scope(private, "site", "docs.example.test/other"))
            self.assertEqual(captured_rule_scope(private, "site", "docs.example.test:8443")["key"], "docs.example.test:8443")
            self.assertTrue(store.forget_rule(route_scopes(private), store.preferred_rule(route_scopes(private))["id"]))
            self.assertEqual(store.preferred_rule(route_scopes(private))["target"], "ses_3")

    def test_shared_website_requires_a_project_and_rejects_ambiguous_paths(self):
        from annotation_routing import captured_rule_scope
        from portable_annotation_origin import source_for_context
        from portable_dictation import OutputContext

        observed = source_for_context("x11", OutputContext("firefox", site_url="https://www.github.com/team/repo/issues/42"))
        self.assertEqual(captured_rule_scope({"origin": observed}, "site")["key"], "github.com/team/repo")
        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            for value in ("github.com", "github.com/team", "github.com/team/repo%2Ftools", "github.com/team/%2e%2e/repo",
                          "github.com/team/repo?token=private", "http://127.0.0.1:9000/app"):
                with self.subTest(value=value), self.assertRaises(RoutingError):
                    annotation_rule_scope("site", value, "linux")
            scope = annotation_rule_scope("site", "github.com/team/repo", "linux")
            store.remember_rule(scope, "Repo", "ses_repo", "service.json")
            self.assertEqual(store.preferred_rule(route_scopes({"origin": observed}))["target"], "ses_repo")
            sibling = annotation_origin({"app": "linux.firefox", "siteUrl": "https://github.com/team/repo-tools"})
            self.assertIsNone(store.preferred_rule(route_scopes({"origin": sibling})))
            self.assertFalse(route_scopes({"origin": {"app": "com.fixture.browser", "site": "github.com"}}))

    def test_project_matching_joins_git_worktrees_and_excludes_unrelated_repositories(self):
        from annotation_projects import match_project_paths, project_path

        self.assertIsNone(project_path(r"\\server\share\project", windows=True))
        self.assertIsNone(project_path(r"C:\project\NUL.txt", windows=True))
        self.assertEqual(str(project_path("C:/repo/subdir", windows=True)), r"C:\repo\subdir")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            main = root / "main"
            worktree = root / "worktree"
            other = root / "other"
            linked = main / ".git/worktrees/branch"
            linked.mkdir(parents=True)
            worktree.mkdir()
            (other / ".git").mkdir(parents=True)
            (worktree / ".git").write_text(f"gitdir: {linked}\n")
            (linked / "commondir").write_text("../..\n")
            directories = list(map(str, [other, worktree, main / "package", root / "unrelated"]))
            self.assertEqual(match_project_paths(str(main), directories), [1, 2])
            self.assertEqual(match_project_paths(str(worktree), directories), [1, 2])

    def test_settings_source_matches_existing_capture_scopes(self):
        from portable_annotation_origin import source_for_context
        from portable_dictation import OutputContext

        browser = source_for_context("wayland", OutputContext("org.mozilla.firefox", "www.docs.example.test"))
        self.assertEqual(annotation_rule_scope("site", "WWW.Docs.Example.Test.", "linux"),
                         route_scopes({"origin": browser})[0])
        with tempfile.TemporaryDirectory() as folder:
            project = str(Path(folder).resolve() / "project")
            terminal = annotation_origin({"app": "com.apple.Terminal", "terminal": {"project": project, "folder": project + "/child"}})
            self.assertEqual(annotation_rule_scope("terminal", project, "darwin"), route_scopes({"origin": terminal})[0])
        for kind, source, platform in (("site", "github.com", "win32"), ("app", "WindowsTerminal.exe", "win32"),
                                       ("site", "127.0.0.1", "darwin"), ("terminal", str(Path.home()), "darwin")):
            with self.subTest(source=source), self.assertRaises(RoutingError):
                annotation_rule_scope(kind, source, platform)

    def test_portable_scope_excludes_shared_hosts_and_general_apps(self):
        from portable_annotation_origin import source_for_context
        from portable_dictation import OutputContext

        desktop = source_for_context("windows", OutputContext("ReviewPad.exe"))
        self.assertEqual(route_scopes({"origin": desktop}),
                         [{"kind": "app", "key": "windows.reviewpad", "label": "ReviewPad.exe"}])
        browser = source_for_context("wayland", OutputContext("org.mozilla.firefox", "www.docs.example.test"))
        self.assertEqual(route_scopes({"origin": browser}),
                         [{"kind": "site", "key": "docs.example.test", "label": "docs.example.test"}])
        for context in (OutputContext("chrome"), OutputContext("chrome", "github.com"),
                        OutputContext("chrome", "127.0.0.1"), OutputContext("WindowsTerminal.exe"),
                        OutputContext("jaro-call-surface"), OutputContext("com.github.wez.wezterm"),
                        OutputContext("not/an/app")):
            with self.subTest(context=context):
                self.assertIsNone(source_for_context("windows", context))

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux listener table")
    def test_linux_registration_identifies_the_listening_process_and_address(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            record = Registration(f"http://127.0.0.1:{port}", "127.0.0.1", port, os.getpid(),
                                  "2.0.0", None, "service.json")
            self.assertFalse(owns_listener(record))
            listener.listen()
            self.assertTrue(owns_listener(record))
            self.assertFalse(owns_listener(replace(record, pid=os.getppid())))
            self.assertFalse(owns_listener(replace(record, address="127.0.0.2")))
        self.assertFalse(owns_listener(record))

    @unittest.skipUnless(sys.platform == "win32", "requires the Windows listener table")
    def test_windows_registration_identifies_ipv4_and_ipv6_listeners(self):
        for family, address, host in ((socket.AF_INET, "127.0.0.1", "127.0.0.1"),
                                      (socket.AF_INET6, "::1", "[::1]")):
            with self.subTest(host=host), socket.socket(family) as listener:
                listener.bind((address, 0))
                port = listener.getsockname()[1]
                record = Registration(f"http://{host}:{port}", host, port, os.getpid(), "2.0.0", None, "service.json")
                self.assertFalse(owns_listener(record))
                listener.listen()
                self.assertTrue(owns_listener(record))
                self.assertFalse(owns_listener(replace(record, pid=0x7fffffff)))
                listener.close()
                self.assertFalse(owns_listener(record))

    @unittest.skipUnless(sys.platform == "win32", "requires Windows Owner Rights inheritance")
    def test_windows_receipts_use_the_default_private_library(self):
        with tempfile.TemporaryDirectory() as folder:
            library = Path(folder) / "Jaro"
            library.mkdir(mode=0o700)
            store = AnnotationDeliveries(library)
            key = store.begin("capture", "service", {"id": "ses_one", "title": "First"}, True)
            store.change(key, "queued", "msg_one")
            self.assertEqual(AnnotationDeliveries(library).recent("capture")[0]["status"], "queued")

    def test_settings_edit_is_local_and_invalidates_stale_delivery_and_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            self.assertEqual(store.list_rules(), [])
            self.assertFalse(store.path.exists())
            source = {"kind": "site", "key": "example.test", "label": "example.test"}
            first = store.remember_rule(source, "Same name", "ses_alpha", "service-alpha.json", True)
            self.assertTrue(store.enqueue_route("capture", {}, first, 10))
            store.manage_rule(first["id"], automatic=False)
            updated, = store.list_rules()
            self.assertEqual((updated["target"], updated["channel"], updated["automatic"]),
                             ("ses_alpha", "service-alpha.json", False))
            with self.assertRaises(ReceiptError):
                store.claim_route("capture", [source], first["id"], "service", {"id": "ses_alpha", "title": "Same name"}, False)
            with self.assertRaises(ReceiptError):
                store.manage_rule(first["id"])
            with self.assertRaises(ReceiptError):
                store.manage_rule(updated["id"], automatic="false")
            store.manage_rule(updated["id"], automatic=True)
            final, = store.list_rules()
            self.assertTrue(final["automatic"])
            store.manage_rule(final["id"])
            self.assertEqual(store.list_rules(), [])
            self.assertFalse(store.recent("capture"))

    def test_settings_reads_suggestion_rules_saved_before_automatic_delivery_existed(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            store.path.touch(mode=0o600)
            with closing(sqlite3.connect(store.path)) as db, db:
                db.execute("""CREATE TABLE annotation_rules(kind TEXT,source TEXT,id TEXT UNIQUE,title TEXT,
                              target TEXT,channel TEXT,PRIMARY KEY(kind,source))""")
                db.execute("INSERT INTO annotation_rules VALUES('app','org.example.app','old','Old chat','ses_old','service.json')")
            rule, = store.list_rules()
            self.assertFalse(rule["automatic"])
            store.manage_rule(rule["id"], automatic=True)
            self.assertTrue(store.list_rules()[0]["automatic"])

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

    def test_portable_terminal_rules_match_the_working_folder_inside_a_repository(self):
        from portable_annotation_origin import source_for_context
        from portable_dictation import OutputContext

        cases = (
            ("x11", "linux", "xterm", "/work/Project", "/work/Project/client", "/work/Project/client/sub",
             "/work/Project/client-tools", "/work/ProjectTools"),
            ("windows", "win32", "WindowsTerminal.exe", r"C:\Work\Project", r"C:\Work\Project\Client",
             r"c:\work\project\client\sub", r"c:\work\PROJECT\client-tools", r"c:\work\ProjectTools"),
        )
        for adapter, platform, app, project, subproject, folder, sibling, other in cases:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as root:
                store = AnnotationDeliveries(root)
                for rule_path, target in ((project, "ses_project"), (subproject, "ses_client")):
                    store.remember_rule(annotation_rule_scope("terminal", rule_path, platform), target, target, "service.json")

                def preferred(folder, repo):
                    origin = source_for_context(adapter, OutputContext(app, terminal={"folder": folder, "project": repo}))
                    return store.preferred_rule(route_scopes({"origin": origin}))

                self.assertEqual(preferred(folder, project)["target"], "ses_client")
                self.assertEqual(preferred(sibling, project)["target"], "ses_project")
                self.assertIsNone(preferred(other, other))
                self.assertIsNone(source_for_context(adapter, OutputContext(app)))

        for platform, value in (("win32", "/work/Project"), ("win32", r"\\server\share\Project"), ("linux", r"C:\Work\Project")):
            with self.subTest(platform=platform, value=value), self.assertRaises(RoutingError):
                annotation_rule_scope("terminal", value, platform)

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
