from importlib.util import find_spec
import sys
from unittest import IsolatedAsyncioTestCase, skipUnless
from unittest.mock import AsyncMock, patch

from portable_call_pages import BrowserPage


@skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux accessibility client")
class LinuxCallPageTests(IsolatedAsyncioTestCase):
    async def test_focused_browser_host_is_scoped_and_discards_a_changed_window(self):
        from linux_call_pages import focused_browser_host
        from wayland_field import FocusWindow

        window = FocusWindow(31, 100, "Fixture tab")
        target = (":1.4", "/window")

        async def connect(read, *, timeout):
            self.assertLessEqual(timeout, .4)
            return await read(None)

        page = BrowserPage(address="https://scope.example.test/private?token=fixture")
        probe = AsyncMock(return_value=window)
        with (patch("linux_call_pages.browser_owner", return_value=True) as process,
              patch("linux_call_pages.on_accessibility_bus", side_effect=connect) as accessibility,
              patch("linux_call_pages.accessible_active_window", return_value=target) as active,
              patch("linux_call_pages.browser_window", return_value=(page, True)) as read):
            self.assertEqual(await focused_browser_host(window, probe), "scope.example.test")
            active.assert_awaited_once_with(None, window, require_active=False, accepts_pid=None)
            read.assert_awaited_once_with(None, target)
            probe.return_value = FocusWindow(window.id, window.pid, "Another tab")
            self.assertEqual(await focused_browser_host(window, probe), "")
            process.return_value = False
            accessibility.reset_mock()
            self.assertEqual(await focused_browser_host(window, probe), "")
            accessibility.assert_not_awaited()

    async def test_wayland_browser_requires_matching_owner_and_unique_window_title(self):
        from dbus_fast import Variant
        from linux_call_pages import focused_browser_host
        from wayland_field import DBUS, FocusWindow

        window = FocusWindow(None, None, "Fixture tab", "org.mozilla.firefox")
        known = (":1.1", "/root")
        foreign = (":1.2", "/root")
        target = (known[0], "/window")
        owners = {known[0]: 41, foreign[0]: 42}
        titles = [target]
        page = BrowserPage(address="https://scope.example.test/private")

        async def connect(reader, *, timeout):
            return await reader(None)

        async def query(bus, name, path, interface, member, signature="", body=None):
            if interface == DBUS:
                return [owners[body[0]]]
            return [Variant("s", window.title)]

        async def children(bus, parent):
            if parent == foreign:
                self.fail("Queried windows from a different browser")
            return titles if parent == known else [known, foreign]

        def browser_process(pid, binaries):
            self.assertIn("firefox-esr", binaries)
            return pid == 41

        with (patch("linux_call_pages.browser_owner", side_effect=browser_process),
              patch("linux_call_pages.on_accessibility_bus", side_effect=connect),
              patch("wayland_field.call", side_effect=query),
              patch("wayland_field.accessible_children", side_effect=children),
              patch("wayland_field.has_accessible_state", side_effect=lambda bus, ref, flag: ref == target),
              patch("linux_call_pages.browser_window", return_value=(page, True)) as read):
            probe = AsyncMock(return_value=window)
            self.assertEqual(await focused_browser_host(window, probe), "scope.example.test")
            titles.append((known[0], "/other"))
            self.assertEqual(await focused_browser_host(window, probe), "")
            self.assertEqual(await focused_browser_host(FocusWindow(None, None, window.title, "editor"), probe), "")
            read.assert_awaited_once_with(None, target)

    async def test_chromium_address_checks_focus_before_and_after_reading(self):
        from dbus_fast import Variant
        from linux_call_pages import FOCUSED, OMNIBOX_CLASS, SHOWING, chromium_address

        window = (":1.4", "/window")
        entry = (":1.4", "/location")
        address = "https://meet.google.com/abc-defg-hij"
        setup = [[[entry]], [Variant("(so)", list(window))], [{"class": OMNIBOX_CLASS}]]
        count = [Variant("i", len(address))]
        for first, last, expected, reads in (
            (SHOWING | FOCUSED, SHOWING, "", False),
            (SHOWING, SHOWING | FOCUSED, "", True),
            (SHOWING, SHOWING, address, True),
        ):
            with (self.subTest(first=first, last=last),
                  patch("linux_call_pages.query", side_effect=[
                      *setup, [[first, 0]], count, [address], [[last, 0]],
                  ]) as query):
                self.assertEqual(await chromium_address(None, window), expected)
            self.assertEqual(any(call.args[4] == "GetText" for call in query.await_args_list), reads)

    async def test_chromium_fallback_preserves_ambiguous_or_in_page_entries(self):
        from dbus_fast import Variant
        from linux_call_pages import chromium_address

        window = (":1.4", "/window")
        entry = (":1.4", "/location")
        for replies in (
            [[[entry, (":1.4", "/second")]]],
            [[[entry]], [Variant("(so)", [":1.4", "/document"])], [95]],
        ):
            with patch("linux_call_pages.query", side_effect=replies) as query:
                self.assertEqual(await chromium_address(None, window), "")
            self.assertNotIn("GetText", [call.args[4] for call in query.await_args_list])

    async def test_only_browser_processes_linked_to_the_microphone_supply_a_page(self):
        from linux_call_pages import read_browsers

        browser = (":1.4", "/root")
        other_profile = (":1.5", "/root")
        roots = [browser, other_profile]
        page = BrowserPage(address="https://meet.google.com/abc-defg-hij?private=fixture")
        windows = AsyncMock(return_value=([page], True))
        with (patch("linux_call_pages.accessible_children", return_value=roots),
              patch("linux_call_pages.process_id", side_effect=[101, 102]),
              patch("linux_call_pages.browser_windows", windows)):
            # The audio service is permitted to match its owning browser. A
            # second active stream without a page makes automatic start unsafe.
            result = await read_browsers(None, {"chromium": [{125, 101}, {126, 103}]})
        windows.assert_awaited_once_with(None, browser)
        self.assertEqual(result["chromium"].provider, "google-meet")
        self.assertFalse(result["chromium"].verified)
        self.assertNotIn("private", repr(result))

    async def test_firefox_native_sharing_alert_does_not_compete_with_the_call_page(self):
        from dbus_fast import Variant
        from linux_call_pages import ALERT, browser_window

        window = (":1.4", "/indicator")
        with (patch("linux_call_pages.has_accessible_state", return_value=True),
              patch("linux_call_pages.query", side_effect=[[[]], [ALERT]])):
            self.assertEqual(await browser_window(None, window), (None, True))

        # An ordinary window with an unknown page still makes matching ambiguous.
        with (patch("linux_call_pages.has_accessible_state", return_value=True),
              patch("linux_call_pages.query", side_effect=[[[]], [23], [Variant("s", "Other page")], [[]]])):
            self.assertEqual(await browser_window(None, window), (BrowserPage(title="Other page"), False))

    async def test_an_embedded_document_cannot_supply_the_window_address(self):
        from dbus_fast import Variant
        from linux_call_pages import browser_window

        window = (":1.4", "/window")
        child = (":1.4", "/frame")
        with (patch("linux_call_pages.has_accessible_state", return_value=True),
              patch("linux_call_pages.query", side_effect=[
                  [[child]], [Variant("(so)", [":1.4", "/outer"])], [95],
                  [Variant("s", "Ordinary page")],
              ]) as query):
            page, complete = await browser_window(None, window)
        self.assertEqual(page, BrowserPage(title="Ordinary page"))
        self.assertFalse(complete)
        self.assertNotIn("GetAttributeValue", [call.args[4] for call in query.await_args_list])
