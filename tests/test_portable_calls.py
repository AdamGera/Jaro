import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from portable_call_pages import BrowserPage, Call, displayed_meet_room, select_call
from portable_call_surface import act, current_control
from portable_calls import CallWatcher
from session_store import SessionStore
from windows_capture import parse_browser_calls


class PortableCallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = SimpleNamespace(library=SessionStore(Path(temporary.name)), portable_recording=None)
        self.start = AsyncMock()
        self.finish = AsyncMock()
        self.start.side_effect = self.capture
        self.finish.side_effect = self.saved
        self.time = 1
        self.watch = self.new_watcher()

    async def capture(self, _languages, _provider):
        key = uuid4()
        self.state.portable_recording = SimpleNamespace(key=key, worker=SimpleNamespace(done=lambda: False))
        return key

    async def saved(self, _key):
        self.state.portable_recording = None

    def new_watcher(self):
        return CallWatcher(self.state, AsyncMock(), AsyncMock(), self.start, self.finish,
                           lambda: self.state.portable_recording is None, lambda: self.time)

    def settings(self, enabled=True, auto_stop=False, **policies):
        self.watch.settings({"enabled": enabled, "autoStop": auto_stop, "policies": policies})

    async def observe(self, streams, seconds=1, playback=frozenset(), pages=None):
        for _ in range(seconds):
            self.time += 1
            key = await self.watch.observe(streams, playback, pages)
            if key:
                await self.watch.stop(key, monitoring=True)

    async def test_permissions_ask_and_snooze_without_starting_capture(self):
        await self.observe({"slack"}, 5)
        self.assertIsNone(self.watch.status()["offer"])
        self.settings(slack="never")
        await self.observe({"slack"}, 5)
        self.assertIsNone(self.watch.status()["offer"])

        self.settings()
        await self.observe({"zoom", "firefox"}, 3)
        offer = self.watch.status()["offer"]
        self.assertEqual(offer["name"], "Zoom")
        self.start.assert_not_called()
        await self.watch.answer(offer["token"], "dismiss", ())
        await self.observe({"zoom"}, 6)
        self.assertIsNone(self.watch.status()["offer"])
        await self.observe(set(), 61)
        await self.observe({"Zoom.exe"}, 3)
        token = self.watch.status()["offer"]["token"]
        with self.assertRaises(HTTPException):
            await self.watch.answer(offer["token"], "record", ())
        await self.watch.answer(token, "record", ("ar",))
        self.start.assert_awaited_once_with(("ar",), Call("zoom", "zoom"))
        await self.observe(set(), 40)
        self.finish.assert_not_called()

    async def test_browser_needs_twenty_seconds_and_never_records_automatically(self):
        with self.assertRaises(HTTPException):
            self.settings(chrome="auto")
        self.settings()
        await self.observe({"Chrome.exe"}, 20)
        self.assertIsNone(self.watch.status()["offer"])
        await self.observe({"Chrome.exe"})
        self.assertEqual(self.watch.status()["offer"]["name"], "Chrome")
        self.start.assert_not_called()

    async def test_saved_default_and_explicit_ask_keep_site_precedence(self):
        legacy = {"enabled": True, "autoStop": True, "policies": {"chrome": "never", "google-meet": "ask"}}
        with self.state.library.connection() as db:
            db.execute("INSERT INTO portable_call_preferences VALUES(1,?)", (json.dumps(legacy),))
        self.watch = self.new_watcher()
        self.assertEqual(self.watch.status()["defaultPolicy"], "ask")
        self.watch.settings({**legacy, "defaultPolicy": "auto"})
        meet = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij")], True)
        await self.observe({"chrome"}, 3, pages={"chrome": meet})
        before = self.watch.status()["offer"]
        self.assertEqual(before["name"], "Google Meet")
        self.start.assert_not_called()

        # An older settings client should not overwrite the existing default.
        self.settings(auto_stop=True, chrome="never", **{"google-meet": "ask"})
        self.assertEqual(self.watch.status()["offer"]["token"], before["token"])
        self.assertEqual(self.watch.status()["defaultPolicy"], "auto")
        self.settings(auto_stop=True, chrome="never")
        self.assertIsNone(self.watch.status()["offer"])
        self.assertEqual(self.watch.policy_for(meet), "never")
        self.settings(auto_stop=True)
        self.assertEqual(self.watch.policy_for(meet), "auto")
        self.start.assert_not_called()

        self.watch = self.new_watcher()
        await self.observe({"chrome"}, 21)
        offer = self.watch.status()["offer"]
        self.assertEqual(offer["name"], "Chrome")
        self.assertFalse(offer["canAlways"])
        self.start.assert_not_called()

    async def test_call_settings_api_accepts_legacy_clients_and_validates_defaults(self):
        from portable_workspace import BOOTSTRAP, configure_workspace
        from server import change_portable_call_settings, edit_portable_call_settings, portable_call_status

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        runtime = {"port": 43721}
        app = FastAPI()
        calls = "/api/portable/calls"
        app.add_api_route(calls, portable_call_status, methods=["GET"])
        app.add_api_route(calls + "/settings", change_portable_call_settings, methods=["PUT"])
        app.add_api_route(calls + "/settings", edit_portable_call_settings, methods=["PATCH"])
        app.state.portable_recording_dir = self.state.library.root
        app.state.portable_calls = self.watch
        self.state.draining = False
        configure_workspace(app, runtime, origin)
        legacy = {"enabled": True, "autoStop": True, "policies": {"zoom": "ask"}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            self.assertEqual((await client.get(calls)).status_code, 403)
            await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": runtime["workspaceKey"]})
            with patch("server.URL", origin):
                update = await client.put(calls + "/settings", headers=headers, json={**legacy, "defaultPolicy": "never"})
                self.assertEqual(update.json()["defaultPolicy"], "never")
                self.assertIs(update.json()["stopRequestedCalls"], False)
                opted_in = await client.patch(calls + "/settings", headers=headers,
                                              json={"action": "stopRequestedCalls", "enabled": True})
                self.assertIs(opted_in.json()["stopRequestedCalls"], True)
                migrated = (await client.put(calls + "/settings", headers=headers, json=legacy)).json()
                self.assertEqual(migrated["defaultPolicy"], "never")
                self.assertIs(migrated["stopRequestedCalls"], True)
                self.assertIs(self.new_watcher().status()["stopRequestedCalls"], True)
                invalid = await client.put(calls + "/settings", headers=headers, json={**legacy, "defaultPolicy": "unknown"})
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual((await client.get(calls)).json()["policies"], legacy["policies"])
                rule = await client.patch(calls + "/settings", headers=headers,
                                          json={"action": "setPolicy", "provider": "slack", "policy": "never"})
                self.assertEqual(rule.json()["policies"], {"zoom": "ask", "slack": "never"})
                default = await client.patch(calls + "/settings", headers=headers,
                                             json={"action": "setDefaultPolicy", "policy": "auto"})
                self.assertEqual(default.json()["policies"], rule.json()["policies"])
                reset = await client.patch(calls + "/settings", headers=headers,
                                           json={"action": "setPolicy", "provider": "zoom", "policy": "inherit"})
                self.assertEqual(reset.json()["policies"], {"slack": "never"})
                self.assertEqual(reset.json()["defaultPolicy"], "auto")
        self.start.assert_not_called()

    async def test_recent_sources_are_saved_for_skipped_calls_without_page_details(self):
        settings = {"enabled": True, "autoStop": False, "stopRequestedCalls": False,
                    "defaultPolicy": "never", "policies": {}}
        self.watch.settings(settings)
        meet = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij?secret=yes")], True)
        with patch("portable_calls.time", return_value=2_000_000_000):
            await self.observe({"zoom"}, 3)
        with patch("portable_calls.time", return_value=2_000_000_001):
            await self.observe({"chrome"}, 3, pages={"chrome": meet})
        self.assertIsNone(self.watch.status()["offer"])
        self.start.assert_not_called()
        with self.state.library.connection() as db:
            saved = json.loads(db.execute("SELECT preferences FROM portable_call_preferences WHERE id=1").fetchone()[0])
        self.assertEqual(saved, {**settings, "recentSources": {"zoom": 2_000_000_000, "google-meet": 2_000_000_001}})
        self.watch = self.new_watcher()
        self.assertEqual([row["id"] for row in self.watch.status()["providers"][:2]], ["google-meet", "zoom"])

    async def test_malformed_settings_default_safely_and_keep_the_exact_original(self):
        for raw in ("{ broken", "null", '["old", "schema"]', b"\xff", "{ broken"):
            with self.subTest(raw=raw), self.state.library.connection() as db:
                db.execute("INSERT OR REPLACE INTO portable_call_preferences VALUES(1,?)", (raw,))
            self.watch = self.new_watcher()
            status = self.watch.status()
            self.assertFalse(status["enabled"])
            self.assertEqual(status["defaultPolicy"], "ask")
            self.assertIn("invalid", status["settingsWarning"])
            await self.observe({"zoom"}, 3)
            with self.state.library.connection() as db:
                self.assertEqual(db.execute("SELECT preferences FROM portable_call_preferences").fetchone()[0], raw)
            self.settings(enabled=False)
            self.assertEqual(self.watch.status()["settingsWarning"], "")
        with self.state.library.connection() as db:
            originals = {row[0] for row in db.execute("SELECT preferences FROM portable_call_preferences_recovery")}
        self.assertEqual(originals, {"{ broken", "null", '["old", "schema"]', b"\xff"})
        self.assertEqual(self.new_watcher().status()["settingsWarning"], "")
        self.start.assert_not_called()

    async def test_invalid_rules_ask_under_an_automatic_default_and_preserve_other_choices(self):
        raw = ('{ "enabled": true, "autoStop": "true", "defaultPolicy": "auto", '
               '"policies": {"zoom": {"invalid": 1}, "slack": "never", "future-provider": "future-rule"}, '
               '"future": {"nested": [7]}, "recentSources": {"zoom": false} }')
        with self.state.library.connection() as db:
            db.execute("INSERT INTO portable_call_preferences VALUES(1,?)", (raw,))
        self.watch = self.new_watcher()
        prior = self.watch.status()
        self.assertTrue(prior["enabled"])
        self.assertFalse(prior["autoStop"])
        self.assertEqual(prior["defaultPolicy"], "auto")
        self.assertEqual(prior["policies"], {"zoom": "ask", "slack": "never"})
        await self.observe({"zoom"}, 4)
        self.assertEqual(self.watch.status()["offer"]["name"], "Zoom")
        self.assertTrue(self.watch.recent_sources["zoom"])
        self.start.assert_not_called()
        with self.state.library.connection() as db:
            self.assertEqual(db.execute("SELECT preferences FROM portable_call_preferences").fetchone()[0], raw)
        self.watch.settings({**self.watch.preferences, "autoStop": True})
        with self.state.library.connection() as db:
            restored = json.loads(db.execute("SELECT preferences FROM portable_call_preferences").fetchone()[0])
            self.assertEqual(db.execute("SELECT preferences FROM portable_call_preferences_recovery").fetchone()[0], raw)
        self.assertTrue(restored["autoStop"])
        self.assertEqual(restored["future"], {"nested": [7]})
        self.assertEqual(restored["policies"], {"zoom": "ask", "slack": "never", "future-provider": "future-rule"})
        self.assertEqual(restored["recentSources"], self.watch.recent_sources)
        self.assertEqual(self.new_watcher().status()["settingsWarning"], "")

    async def test_unsafe_preference_types_do_not_opt_in_or_start_automatically(self):
        with self.state.library.connection() as db:
            db.execute("INSERT INTO portable_call_preferences VALUES(1,?)",
                       ('{"enabled":"false","stopRequestedCalls":"true","defaultPolicy":"auto","policies":[]}',))
        self.watch = self.new_watcher()
        self.assertFalse(self.watch.status()["enabled"])
        self.assertFalse(self.watch.status()["stopRequestedCalls"])
        self.assertEqual(self.watch.status()["defaultPolicy"], "ask")
        await self.observe({"zoom"}, 4)
        self.start.assert_not_called()

    def test_failed_repair_and_changed_row_preserve_the_saved_preferences(self):
        with self.state.library.connection() as db:
            db.execute("INSERT INTO portable_call_preferences VALUES(1,'broken')")
            db.executescript("""CREATE TRIGGER cannot_save_backup BEFORE INSERT ON portable_call_preferences_recovery
                                BEGIN SELECT RAISE(ABORT,'backup write failed'); END;""")
        self.watch = self.new_watcher()
        with self.assertRaises(HTTPException) as failed:
            self.settings()
        self.assertEqual(failed.exception.status_code, 503)
        with self.state.library.connection() as db:
            self.assertEqual(db.execute("SELECT preferences FROM portable_call_preferences").fetchone()[0], "broken")
            db.execute("DROP TRIGGER cannot_save_backup")
            db.execute("UPDATE portable_call_preferences SET preferences='{}'")
        with self.assertRaises(HTTPException) as stale:
            self.settings()
        self.assertEqual(stale.exception.status_code, 409)
        self.assertFalse(self.watch.preferences["enabled"])
        with self.state.library.connection() as db:
            self.assertEqual(db.execute("SELECT preferences FROM portable_call_preferences").fetchone()[0], "{}")
            self.assertIsNone(db.execute("SELECT preferences FROM portable_call_preferences_recovery").fetchone())

    def test_unreadable_settings_leave_detection_off_and_cannot_be_overwritten(self):
        with patch.object(self.state.library, "connection", side_effect=sqlite3.OperationalError):
            self.watch = self.new_watcher()
        self.assertFalse(self.watch.status()["enabled"])
        self.assertIn("Could not read", self.watch.status()["settingsWarning"])
        with self.assertRaises(HTTPException) as failed:
            self.settings()
        self.assertEqual(failed.exception.status_code, 409)
        with self.state.library.connection() as db:
            self.assertIsNone(db.execute("SELECT preferences FROM portable_call_preferences").fetchone())

    async def test_recent_source_write_failure_leaves_the_offer_available(self):
        self.settings()
        with patch.object(self.watch, "save_preferences", side_effect=HTTPException(503, "Library not writable.")):
            await self.observe({"zoom"}, 3)
        self.assertEqual(self.watch.status()["offer"]["name"], "Zoom")
        self.assertEqual(self.watch.status()["providers"][0]["id"], "zoom")

    async def test_automatic_stop_uses_confirmed_absence_and_can_be_overridden(self):
        self.settings(auto_stop=True, teams="auto")
        self.watch = self.new_watcher()
        await self.observe({"ms-teams.exe"}, 3)
        key = self.watch.active.id
        self.start.assert_awaited_once_with((), Call("teams", "teams"))
        await self.observe(set(), 6)
        self.assertIsNotNone(self.watch.active.stop_at)
        await self.observe(None, 60)
        self.assertIsNone(self.watch.active.stop_at)
        await self.observe(set(), 6)
        self.watch.keep_recording(key)
        await self.observe(set(), 36)
        self.finish.assert_not_called()
        await self.watch.stop(key)
        await self.observe(set(), 61)
        await self.observe({"ms-teams"}, 3)
        key = self.watch.active.id
        await self.observe(set(), 33)
        self.assertIsNotNone(self.watch.active)
        await self.observe(set())
        self.finish.assert_awaited_with(key)
        self.assertEqual(self.watch.status()["completed"], str(key))

    async def test_long_observation_gap_does_not_start_a_recording(self):
        self.settings(slack="auto")
        await self.observe({"slack"})
        self.time += 120
        await self.observe({"slack"})
        self.start.assert_not_called()

    async def test_accepted_offer_waits_for_microphone_after_recording_starts(self):
        self.watch.settings({**self.watch.preferences, "enabled": True, "stopRequestedCalls": True})
        await self.observe({"Chrome.exe"}, 21)
        await self.watch.answer(self.watch.status()["offer"]["token"], "record", ())
        key = self.watch.active.id
        await self.observe(set(), playback={"Chrome.exe"})
        await self.observe(set(), 40)
        self.finish.assert_not_called()
        await self.observe({"Chrome.exe"})
        await self.observe(set(), 40, playback={"chrome"})
        await self.observe(set(), 6, playback=None)
        self.assertIsNone(self.watch.status()["active"]["stopSeconds"])
        await self.observe(set(), 4)
        self.watch.keep_recording(key)
        await self.observe(set(), 40)
        self.finish.assert_not_called()
        await self.observe({"chrome"})
        await self.observe(set(), 34)
        self.finish.assert_awaited_once_with(key)

    async def test_manual_meeting_watches_one_native_app_without_enabling_detection(self):
        self.watch.settings({**self.watch.preferences, "stopRequestedCalls": True})
        manual = Call("", "")
        self.watch.microphone_users.return_value = {"ms-teams.exe", "teams-for-linux", "chrome"}
        key = await self.watch.begin(manual, ("ar",), show_unmatched=False)
        self.start.assert_awaited_once_with(("ar",), manual)
        self.assertEqual(self.watch.status()["active"]["name"], "Meeting")
        await self.observe(set(), 40)
        self.finish.assert_not_called()
        await self.observe({"teams-for-linux"})
        await self.observe(set(), 4)
        self.assertIsNotNone(self.watch.status()["active"]["stopSeconds"])
        self.watch.settings({**self.watch.preferences, "stopRequestedCalls": False})
        await self.observe(set(), 40)
        self.assertIsNone(self.watch.status()["active"]["stopSeconds"])
        self.finish.assert_not_called()
        self.watch.settings({**self.watch.preferences, "stopRequestedCalls": True})
        await self.observe({"firefox"}, 34)
        self.finish.assert_awaited_once_with(key)
        self.assertIsNone(self.watch.status()["offer"])

    async def test_browser_only_ambiguous_and_unavailable_manual_meetings_stay_manual(self):
        from portable_capture import CaptureError

        self.watch.settings({**self.watch.preferences, "stopRequestedCalls": True})
        manual = Call("", "")
        self.watch.microphone_users.side_effect = [{"firefox"}, {"zoom", "ms-teams.exe"}, CaptureError("unavailable")]
        for _ in range(3):
            key = await self.watch.begin(manual, (), show_unmatched=False)
            self.assertIsNone(self.watch.active)
            await self.observe(set(), 40)
            self.finish.assert_not_called()
            await self.saved(key)

    async def test_requested_stop_switch_does_not_enable_automatic_stop(self):
        self.watch.settings({**self.watch.preferences, "enabled": True, "stopRequestedCalls": True,
                             "policies": {"zoom": "auto"}})
        await self.observe({"zoom"}, 4)
        self.assertTrue(self.watch.active.automatic)
        await self.observe(set(), 40)
        self.finish.assert_not_called()

    async def test_only_explicit_dual_source_meetings_consult_the_call_watcher(self):
        from server import URL, start_portable_recording

        app = FastAPI()
        app.state.status = "ready"
        app.state.portable_recording_dir = self.state.library.root
        app.state.portable_calls = self.watch
        app.post("/record")(start_portable_recording)
        self.watch.settings({**self.watch.preferences, "stopRequestedCalls": True})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL,
                                     headers={"Origin": URL}) as client:
            with patch("server.begin_portable_recording", new_callable=AsyncMock, return_value={"id": "session"}) as start:
                for mode in ("mic", "both", "system"):
                    reply = await client.post("/record", params={"mode": mode, "language": "en"})
                    self.assertEqual(reply.json(), {"id": "session"})
                    start.assert_awaited_with(app.state, mode, False, ("en",), live=False, diarization=False)
            self.watch.microphone_users.assert_not_awaited()
            self.watch.microphone_users.return_value = {"zoom"}
            reply = await client.post("/record?meeting=true&mode=both&language=en")
            self.assertEqual(reply.json()["id"], str(self.watch.active.id))
            self.start.assert_awaited_once_with(("en",), Call("", ""))

    async def test_desktop_actions_use_the_current_presentation(self):
        self.settings(auto_stop=True)
        await self.observe({"zoom"}, 3)
        offer = current_control(self.watch)
        self.assertEqual((offer.mode, offer.name, offer.seconds(self.time)), ("offer", "Zoom", 20))
        await act(self.watch, offer, b"stop")
        self.start.assert_not_called()
        await act(self.watch, offer, b"dismiss")
        await act(self.watch, offer, b"record")
        self.start.assert_not_called()

        await self.observe(set(), 61)
        await self.observe({"zoom"}, 3)
        await act(self.watch, current_control(self.watch), b"record")
        self.start.assert_awaited_once_with((), Call("zoom", "zoom"))
        recording = current_control(self.watch)
        self.assertEqual(recording.mode, "recording")
        await act(self.watch, recording, b"stop")
        self.finish.assert_awaited_once_with(recording.key)

    async def test_desktop_keep_rejects_an_old_countdown_for_the_same_meeting(self):
        self.settings(auto_stop=True, zoom="auto")
        await self.observe({"zoom"}, 3)
        await self.observe(set(), 4)
        old = current_control(self.watch)
        self.assertEqual(old.mode, "ending")
        await self.observe({"zoom"})
        await self.observe(set(), 4)
        current = current_control(self.watch)
        self.assertEqual(old.key, current.key)
        self.assertNotEqual(old.began, current.began)
        await act(self.watch, old, b"keep")
        self.assertEqual(current_control(self.watch), current)
        await act(self.watch, current, b"keep")
        self.assertEqual(current_control(self.watch).mode, "automatic")
        await self.observe(set(), 40)
        self.assertEqual(current_control(self.watch).mode, "automatic")
        await self.observe({"zoom"})
        await self.observe(set(), 4)
        self.assertEqual(current_control(self.watch).mode, "ending")

    async def test_auto_stop_waits_for_call_playback_and_can_start_the_next_call(self):
        self.settings(auto_stop=True, zoom="auto")
        await self.observe(set(), 40, playback={"Zoom.exe"})
        self.start.assert_not_called()
        await self.observe({"zoom"}, 3)
        key = self.watch.active.id
        await self.observe(set(), 62, playback={"ZOOM.exe"})
        self.assertIsNone(self.watch.active.stop_at)
        self.finish.assert_not_called()

        await self.observe(set(), 4, playback={"spotify"})
        self.assertIsNotNone(self.watch.active.stop_at)
        await self.observe(set(), playback=None)
        self.assertIsNone(self.watch.active.stop_at)
        await self.observe(set(), 4, playback={"spotify"})
        self.assertIsNotNone(self.watch.active.stop_at)
        await self.observe(set(), playback={"zoom"})
        self.assertIsNone(self.watch.active.stop_at)

        await self.observe(set(), 34, playback={"spotify"})
        self.finish.assert_awaited_once_with(key)
        await self.observe({"zoom"}, 3)
        self.assertEqual(self.start.await_count, 2)
        self.assertNotEqual(self.watch.active.id, key)

    async def test_windows_site_policy_uses_verified_browser_pages(self):
        self.watch.browser_pages = AsyncMock()
        self.settings(chrome="never", teams="auto", **{"google-meet": "ask"})
        teams = select_call("chrome", [BrowserPage(address="https://teams.cloud.microsoft/meet/123")], True)
        await self.observe({"Chrome.exe"}, 3, pages={"chrome": teams})
        self.start.assert_awaited_once_with((), teams)
        self.assertEqual(self.watch.active.call.source, "chrome")
        self.assertIn("google-meet", [app["id"] for app in self.watch.status()["providers"]])
        await self.watch.stop(self.watch.active.id)

        meet = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij")], True)
        await self.observe({"chrome"}, 3, pages={"chrome": meet})
        self.assertEqual(self.watch.status()["offer"]["name"], "Google Meet")
        self.assertEqual(self.start.await_count, 1)
        self.assertNotIn("abc-defg-hij", str(self.watch.status()))

    async def test_zoom_web_and_native_have_independent_policies(self):
        self.watch.browser_pages = AsyncMock()
        self.settings(zoom="never", chrome="never", **{"zoom-web": "auto"})
        await self.observe({"CptHost.exe"}, 3)
        self.start.assert_not_called()
        self.assertIsNone(self.watch.status()["offer"])
        self.assertIn("zoom", self.watch.recent_sources)
        call = parse_browser_calls(b"U\t0\t0\thttps://app.zoom.us/wc/12345678901/join?pwd=private\ndone\n", ("chrome",))
        await self.observe({"Chrome.exe"}, 3, pages=call)
        self.start.assert_awaited_once_with((), call["chrome"])
        status = self.watch.status()
        self.assertEqual(status["active"]["name"], "Zoom (web)")
        self.assertNotIn("private", str(status))
        self.assertNotIn("12345678901", str(status))

    async def test_discord_site_offer_needs_confirmation_even_with_an_auto_rule(self):
        self.watch.browser_pages = AsyncMock()
        self.settings(**{"discord-web": "auto"})
        page = BrowserPage(address="https://discord.com/channels/123456789012345678/234567890123456789")
        call = select_call("chrome", [page], True)
        await self.observe({"chrome"}, 22, pages={"chrome": call})
        self.start.assert_not_called()
        status = self.watch.status()
        self.assertEqual(status["offer"]["name"], "Discord (web)")
        self.assertFalse(status["offer"]["canAlways"])
        provider = next(item for item in status["providers"] if item["id"] == "discord-web")
        self.assertFalse(provider["canAlwaysRecord"])
        await self.watch.answer(status["offer"]["token"], "record", ())
        self.start.assert_awaited_once_with((), call)

    async def test_open_policy_options_stay_until_answered_or_call_withdrawn(self):
        self.settings()
        await self.observe({"zoom"}, 3)
        compact = current_control(self.watch)
        await act(self.watch, compact, b"more")
        options = current_control(self.watch)
        self.assertEqual(options.mode, "offer-options")
        self.assertNotEqual(compact.key, options.key)
        await act(self.watch, compact, b"record")
        self.start.assert_not_called()
        await self.observe({"zoom"}, 25)
        self.assertEqual(current_control(self.watch), options)
        self.assertEqual(self.watch.status()["policies"], {})
        await act(self.watch, options, b"back")
        restored = current_control(self.watch)
        self.assertEqual((restored.mode, restored.seconds(self.time)), ("offer", 20))
        await act(self.watch, restored, b"more")
        await self.observe(set(), 4)
        self.assertIsNone(current_control(self.watch))
        self.assertEqual(self.watch.status()["policies"], {})

    async def test_offer_always_saves_site_policy_and_records_this_call_on_request(self):
        self.settings(auto_stop=True, chrome="never", **{"google-meet": "ask"})
        meet = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij")], True)
        pages = {"chrome": meet}
        await self.observe({"chrome"}, 3, pages=pages)
        await act(self.watch, current_control(self.watch), b"more")
        options = self.watch.status()["offer"]
        self.assertTrue(options["canAlways"])
        await self.watch.answer(options["token"], "always", ("ar",))
        self.start.assert_awaited_once_with(("ar",), meet)
        self.assertFalse(self.watch.active.automatic)
        await self.observe(set(), 36)
        self.finish.assert_not_called()
        await self.watch.stop(self.watch.active.id)

        self.watch = self.new_watcher()
        self.assertEqual(self.watch.status()["policies"], {"chrome": "never", "google-meet": "auto"})
        await self.observe({"chrome"}, 3, pages=pages)
        self.assertTrue(self.watch.active.automatic)

    async def test_unknown_browser_can_deny_but_cannot_enable_automatic_recording(self):
        self.settings()
        await self.observe({"chrome"}, 21)
        await act(self.watch, current_control(self.watch), b"more")
        options = self.watch.status()["offer"]
        self.assertFalse(options["canAlways"])
        with self.assertRaises(HTTPException) as raised:
            await self.watch.answer(options["token"], "always", ())
        self.assertEqual(raised.exception.status_code, 400)
        await act(self.watch, current_control(self.watch), b"never")
        self.start.assert_not_called()
        self.assertIsNone(self.watch.status()["offer"])

        self.watch = self.new_watcher()
        self.assertEqual(self.watch.status()["policies"], {"chrome": "never"})
        await self.observe({"chrome"}, 30)
        self.assertIsNone(self.watch.status()["offer"])

    async def test_failed_policy_save_reissues_offer_without_starting(self):
        self.settings()
        await self.observe({"zoom"}, 3)
        await act(self.watch, current_control(self.watch), b"more")
        before = current_control(self.watch)
        with patch.object(self.watch, "save_preferences", side_effect=HTTPException(503, "Policy save failed.")):
            await act(self.watch, before, b"always")
        reissued = current_control(self.watch)
        self.assertNotEqual(before.key, reissued.key)
        self.assertEqual(reissued.mode, "offer-options")
        self.assertEqual(self.watch.status()["policies"], {})
        self.start.assert_not_called()

    async def test_title_fallback_and_cached_site_do_not_automatically_record(self):
        self.settings(**{"google-meet": "auto"})
        guessed = select_call("chrome", [BrowserPage(title="Meet - Standup - Google Chrome")], True)
        await self.observe({"chrome"}, 3, pages={"chrome": guessed})
        self.assertEqual(self.watch.status()["offer"]["name"], "Google Meet")
        self.start.assert_not_called()
        self.settings(chrome="never", **{"google-meet": "auto"})
        confirmed = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij")], True)
        await self.observe({"chrome"}, pages={"chrome": confirmed})
        await self.observe({"chrome"}, 3)
        self.assertEqual(self.watch.status()["offer"]["name"], "Google Meet")
        self.start.assert_not_called()

    async def test_existing_offer_requires_confirmation_when_page_verification_drops(self):
        self.settings()
        call = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij")], True)
        present = {"chrome": call}
        await self.observe({"chrome"}, 3, pages=present)
        self.assertTrue(self.watch.status()["offer"]["canAlways"])
        await act(self.watch, current_control(self.watch), b"more")
        token = self.watch.status()["offer"]["token"]
        await self.observe({"chrome"})
        self.assertFalse(self.watch.status()["offer"]["canAlways"])
        with self.assertRaisesRegex(HTTPException, "needs confirmation"):
            await self.watch.answer(token, "always", ())
        self.start.assert_not_called()
        await self.observe({"chrome"}, pages=present)
        self.assertTrue(self.watch.status()["offer"]["canAlways"])

    async def test_different_verified_room_counts_down_and_can_be_kept(self):
        self.settings(auto_stop=True, **{"google-meet": "auto"})
        pages = []
        for code in ("abc-defg-hij", "abc-defg-xyz", "xyz-abcd-xyz"):
            call = select_call("chrome", [BrowserPage(address=f"https://meet.google.com/{code}")], True)
            pages.append({"chrome": call})
        original, second, third = pages
        await self.observe({"chrome"}, 3, pages=original)
        key = self.watch.active.id
        await self.observe({"chrome"}, 3, pages=second)
        self.assertIsNone(self.watch.active.stop_at)
        await self.observe({"chrome"}, 2, pages=second)
        self.assertEqual(current_control(self.watch).mode, "switching")
        await self.observe({"chrome"})
        self.assertIsNone(self.watch.active.stop_at)
        await self.observe({"chrome"}, 5, pages=second)
        await act(self.watch, current_control(self.watch), b"keep")
        await self.observe({"chrome"}, 34, pages=second)
        self.finish.assert_not_called()

        await self.observe({"chrome"}, 35, pages=third)
        self.finish.assert_awaited_once_with(key)
        await self.observe({"chrome"}, pages=third)
        self.assertNotEqual(self.watch.active.id, key)
        self.assertEqual(self.watch.active.call, third["chrome"])

    async def test_chromium_room_hint_can_end_but_cannot_start_automatic_recording(self):
        self.settings(auto_stop=True, **{"google-meet": "auto"})
        hint = displayed_meet_room("meet.google.com/xyz-wxyz-xyz")
        page = BrowserPage(room_hint=hint)
        next_room = {"chromium": select_call("chromium", [page], True)}
        await self.observe({"chromium"}, 3, pages=next_room)
        self.assertFalse(self.watch.status()["offer"]["canAlways"])
        self.start.assert_not_called()

        self.watch = self.new_watcher()
        original = BrowserPage(address="https://meet.google.com/abc-defg-hij")
        await self.observe({"chromium"}, 3, pages={"chromium": select_call("chromium", [original], True)})
        key = self.watch.active.id
        await self.observe({"chromium"}, 5, pages=next_room)
        self.assertEqual(current_control(self.watch).mode, "switching")
        await self.observe({"chromium"}, 32, pages=next_room)
        self.finish.assert_awaited_once_with(key)
        await self.observe({"chromium"}, 5, pages=next_room)
        self.start.assert_awaited_once()


class CallPageTests(unittest.TestCase):
    def test_scheme_less_room_hints_must_be_unambiguous(self):
        first = BrowserPage(room_hint=displayed_meet_room("meet.google.com/abc-defg-hij?authuser=1"))
        second = BrowserPage(room_hint=displayed_meet_room("meet.google.com/xyz-wxyz-xyz"))
        confirmed = BrowserPage(address="https://meet.google.com/abc-defg-hij")
        self.assertTrue(first.room_hint)
        self.assertIsNone(select_call("chromium", [first, second], True))
        self.assertIsNone(select_call("chromium", [confirmed, second], True))
        self.assertFalse(select_call("chromium", [confirmed, first], True).verified)
        self.assertIsNone(select_call("chromium", [first], False))
        for value in ("meet.google.com.evil.test/abc-defg-hij", "meet.google.com@evil.test/abc-defg-hij",
                      "meet.google.com:80/abc-defg-hij", "meet.google.com/abc-defg-hij/extra"):
            self.assertEqual(displayed_meet_room(value), "", value)

    def test_real_hosts_and_meet_room_normalization(self):
        first = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij?authuser=1")], True)
        same = select_call("chrome", [BrowserPage(address="https://meet.google.com/meet/ABC-DEFG-HIJ#room")], True)
        self.assertEqual(first, same)
        for url in ("http://meet.google.com/abc-defg-hij", "https://meet.google.com/landing",
                    "https://meet.google.com/lookup/my-meeting",
                    "https://meet.google.com/about/abc-defg-hij",
                    "https://meet.google.com.evil.test/abc-defg-hij",
                    "https://evil.test/?https://meet.google.com/abc-defg-hij", "https://meet.google.com:8080/abc-defg-hij",
                    "https://evil@meet.google.com/abc-defg-hij"):
            self.assertIsNone(select_call("chrome", [BrowserPage("Meet - Spoof", url)], True), url)

    def test_explicit_call_routes_are_verified_and_compared_by_room(self):
        suites = {
            "zoom-web": (
                "https://app.zoom.us/wc/12345678901/join?pwd=private",
                "https://tenant.zoomgov.com/wc/12345678901/start#fragment",
                "https://app.zoom.us/wc/12345678902/join"),
            "slack-web": (
                "https://app.slack.com/huddle/T012AB3C4/C012AB3C4",
                "https://app.slack.com/huddle/t012ab3c4/c012ab3c4/",
                "https://app.slack.com/huddle/T012AB3C4/C012AB3C5"),
            "jitsi": (
                "https://meet.jit.si/Jaro%20Fixture?profile=private",
                "https://meet.jit.si/jaro%20fixture/",
                "https://meet.jit.si/AnotherRoom"),
        }
        for provider, addresses in suites.items():
            with self.subTest(provider=provider):
                one, same, other = (BrowserPage(address=address) for address in addresses)
                call = select_call("chrome", [one, same], True)
                self.assertEqual(call.provider, provider)
                self.assertTrue(call.verified)
                self.assertTrue(call.room)
                self.assertFalse(select_call("chrome", [one], False).verified)
                self.assertIsNone(select_call("chrome", [one, other], True))
        for url in ("https://app.zoom.us/wc", "https://zoom.us/j/12345678901",
                    "https://app.zoom.us.evil.test/wc/12345678901/join",
                    "https://app.slack.com/client/T012AB3C4/C012AB3C4",
                    "https://example.com/huddle/T012AB3C4/C012AB3C4",
                    "https://discord.com/login", "https://meet.jit.si/", "https://meet.jit.si/config.js"):
            self.assertIsNone(select_call("chrome", [BrowserPage(address=url)], True), url)

    def test_native_rows_keep_titles_on_timeout_and_reject_ambiguous_rooms(self):
        prefix = b"T\t0\t0\tInbox - Microsoft Edge\nT\t0\t1\tMeet - Standup - Microsoft Edge\n"
        result = parse_browser_calls(prefix, ("edge",))
        self.assertEqual(result["edge"].provider, "google-meet")
        self.assertFalse(result["edge"].verified)
        meet = b"U\t0\t1\thttps://meet.google.com/abc-defg-hij\n"
        self.assertFalse(parse_browser_calls(prefix + meet, ("edge",))["edge"].verified)
        self.assertFalse(parse_browser_calls(prefix + meet + b"done\n", ("edge",))["edge"].verified)
        inbox = b"U\t0\t0\thttps://example.test/inbox\n"
        self.assertFalse(parse_browser_calls(prefix + inbox + meet + b"done\n", ("edge",))["edge"].verified)
        self.assertTrue(parse_browser_calls(meet + b"done\n", ("edge",))["edge"].verified)
        other = b"U\t0\t0\thttps://meet.google.com/xyz-wxyz-xyz\n"
        self.assertEqual(parse_browser_calls(prefix + meet + other + b"done\n", ("edge",)), {})
