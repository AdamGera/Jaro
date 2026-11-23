from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from portable_call_pages import BrowserPage, Call, select_call
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


class CallPageTests(unittest.TestCase):
    def test_real_hosts_and_meet_room_normalization(self):
        first = select_call("chrome", [BrowserPage(address="https://meet.google.com/abc-defg-hij?authuser=1")], True)
        same = select_call("chrome", [BrowserPage(address="https://meet.google.com/meet/ABC-DEFG-HIJ#room")], True)
        self.assertEqual(first, same)
        for url in ("http://meet.google.com/abc-defg-hij", "https://meet.google.com/landing",
                    "https://meet.google.com.evil.test/abc-defg-hij",
                    "https://evil.test/?https://meet.google.com/abc-defg-hij", "https://meet.google.com:8080/abc-defg-hij",
                    "https://evil@meet.google.com/abc-defg-hij"):
            self.assertIsNone(select_call("chrome", [BrowserPage("Meet - Spoof", url)], True), url)

    def test_native_rows_keep_titles_on_timeout_and_reject_ambiguous_rooms(self):
        prefix = b"T\t0\t0\tInbox - Microsoft Edge\nT\t0\t1\tMeet - Standup - Microsoft Edge\n"
        result = parse_browser_calls(prefix, ("edge",))
        self.assertEqual(result["edge"].provider, "google-meet")
        self.assertFalse(result["edge"].verified)
        meet = b"U\t0\t1\thttps://meet.google.com/abc-defg-hij\n"
        self.assertFalse(parse_browser_calls(prefix + meet, ("edge",))["edge"].verified)
        self.assertTrue(parse_browser_calls(prefix + meet + b"done\n", ("edge",))["edge"].verified)
        other = b"U\t0\t0\thttps://meet.google.com/xyz-wxyz-xyz\n"
        self.assertEqual(parse_browser_calls(prefix + meet + other + b"done\n", ("edge",)), {})
