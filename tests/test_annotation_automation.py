import asyncio
from contextlib import asynccontextmanager
import tempfile
from types import SimpleNamespace
import unittest
from time import time
from unittest.mock import AsyncMock, patch

from annotation_deliveries import ReceiptError
from annotation_routing import RouteUnavailable
from annotation_automation import AnnotationAutomation
from annotation_routing import route_scopes
from managed_preferences import OPENCODE_MANAGED, opencode_managed
from portable_annotation_panel import PortableAnnotationPanel


class AnnotationAutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.origin = {"app": "org.example.browser", "name": "Example", "site": "example.test"}
        self.events = []
        self.sent = []

        async def deliver(capture, target, service, authorize):
            delivery = await authorize(service, {"id": target, "title": "Test"}, False)
            self.sent.append((capture, target, service))
            self.routes.receipts.change(delivery, "queued", "msg_test")

        self.deliver = deliver
        self.load = AsyncMock(return_value={"status": "complete", "annotation": {"origin": self.origin}})
        self.routes = AnnotationAutomation(self.folder.name, self.load, deliver, self.events.append)
        self.addAsyncCleanup(self.routes.close)
        self.scope = route_scopes({"origin": self.origin})[0]
        self.routes.receipts.remember_rule(self.scope, "Test", "ses_test", "service.json", True)
        restore = patch("annotation_automation.resolve_automatic_rule",
                        new_callable=AsyncMock, return_value=("bound", {"id": "ses_test", "title": "Test"}))
        self.resolve = restore.start()
        self.addCleanup(restore.stop)

    async def wait_phase(self, capture, phase):
        async with asyncio.timeout(3):
            while self.routes.status(capture)["phase"] != phase:
                await asyncio.sleep(.01)
        return self.routes.status(capture)

    async def wait_confirmation(self, capture, after=None):
        async with asyncio.timeout(3):
            while True:
                for event in reversed(self.events):
                    nonce = event.get("nonce")
                    if event["capture"] == capture and nonce and nonce != after:
                        return nonce
                await asyncio.sleep(.01)

    async def test_visible_offer_held_then_sent_once(self):
        await self.routes.enqueue("demo", self.origin, time())
        await self.wait_phase("demo", "offer")
        await self.routes.panel_action("demo", "shown", held=True)
        await self.wait_phase("demo", "held")
        await asyncio.sleep(.05)
        self.assertFalse(self.sent)
        self.routes.routes["demo"].remaining = .01
        await self.routes.panel_action("demo", "resume")
        first = await self.wait_confirmation("demo")
        self.assertTrue(self.routes.confirm_from_panel("demo", first, "held"))
        await self.wait_phase("demo", "held")
        self.assertFalse(self.sent)
        await self.routes.panel_action("demo", "resume")
        second = await self.wait_confirmation("demo", first)
        self.assertFalse(self.routes.confirm_from_panel("demo", first, "ready"))
        self.assertFalse(self.routes.confirm_from_panel("different", second, "ready"))
        self.assertTrue(self.routes.confirm_from_panel("demo", second, "ready"))
        self.assertFalse(self.routes.confirm_from_panel("demo", second, "ready"))
        await self.wait_phase("demo", "sent")
        await self.routes.panel_action("demo", "resume")
        self.assertEqual(self.sent, [("demo", "ses_test", "bound")])
        self.assertEqual(self.events[-1]["receiptStatus"], "queued")
        self.assertGreater(self.events[-1]["undoUntil"], time() * 1000)
        automatic = self.routes.automatic_receipt("demo")
        self.assertEqual(self.events[-1]["deliveryId"], automatic["deliveryId"])
        manual = self.routes.receipts.begin("demo", "different", {"id": "ses_other", "title": "Other"}, False)
        self.assertNotEqual(automatic["deliveryId"], manual)
        self.assertEqual(self.routes.automatic_receipt("demo"), automatic)
        self.routes.receipts.change(automatic["deliveryId"], "withdrawal-unconfirmed")
        status = await self.routes.query("demo")
        self.assertEqual(status["receiptStatus"], "withdrawal-unconfirmed")
        self.assertNotIn("undoUntil", status)

    async def test_headless_or_cancelled_never_sends(self):
        with patch("annotation_automation.OFFER_TIMEOUT", .05):
            await self.routes.enqueue("headless", self.origin, time())
            await self.wait_phase("headless", "cancelled")
        await self.routes.enqueue("visible", self.origin, time())
        await self.wait_phase("visible", "offer")
        await self.routes.panel_action("visible", "shown")
        self.assertTrue(await self.routes.cancel("visible"))
        await self.wait_phase("visible", "cancelled")
        self.assertFalse(self.sent)

    async def test_missing_or_unavailable_confirmation_never_claims_a_send(self):
        with patch("annotation_automation.CONFIRMATION_TIMEOUT", .12):
            for capture in ("gone", "locked", "closed"):
                await self.routes.enqueue(capture, self.origin, time())
                await self.wait_phase(capture, "offer")
                self.routes.routes[capture].remaining = .01
                await self.routes.panel_action(capture, "shown")
                nonce = await self.wait_confirmation(capture)
                if capture == "locked":
                    self.assertTrue(self.routes.confirm_from_panel(capture, nonce, "unavailable"))
                elif capture == "closed":
                    self.assertTrue(await self.routes.cancel(capture))
                    self.assertFalse(self.routes.confirm_from_panel(capture, nonce, "ready"))
                await self.wait_phase(capture, "cancelled")
                self.assertEqual(self.routes.receipts.recent(capture), [])
        self.assertFalse(self.sent)

    async def test_provenance_and_rule_replacement_prevent_send(self):
        await self.routes.enqueue("old", self.origin, time() - 10)
        self.assertEqual(self.routes.status("old")["phase"], "idle")
        self.load.return_value = {"status": "complete", "annotation": {"origin": {**self.origin, "site": "other.test"}}}
        await self.routes.enqueue("mismatch", self.origin, time())
        await self.wait_phase("mismatch", "failed")
        self.load.return_value = {"status": "complete", "annotation": {"origin": self.origin}}
        await self.routes.enqueue("replaced", self.origin, time())
        await self.wait_phase("replaced", "offer")
        self.routes.receipts.remember_rule(self.scope, "Other", "ses_other", "service.json", True)
        self.routes.routes["replaced"].remaining = .02
        await self.routes.panel_action("replaced", "shown")
        self.assertTrue(self.routes.confirm_from_panel("replaced", await self.wait_confirmation("replaced"), "ready"))
        self.assertIn("changed", (await self.wait_phase("replaced", "failed"))["message"])
        self.assertFalse(self.sent)

    async def test_project_countdown_stays_bound_to_the_shown_chat_and_its_project(self):
        project = f"{self.folder.name}/project"
        self.routes.receipts.remember_rule(self.scope, "Project", f"project:{project}", "service.json", True)
        self.resolve.return_value = ("bound", {"id": "ses_shown", "title": "Chosen", "directory": project})
        current = {"id": "ses_shown", "title": "Chosen", "directory": project + "/subfolder"}

        async def deliver(capture, target, service, authorize):
            self.assertEqual(target, "ses_shown")
            receipt = await authorize(service, current, False)
            self.sent.append(target)
            self.routes.receipts.change(receipt, "queued", "msg_test")

        self.routes.deliver = deliver
        await self.routes.enqueue("first", self.origin, time())
        await self.wait_phase("first", "offer")
        self.routes.routes["first"].remaining = .01
        await self.routes.panel_action("first", "shown")
        self.assertTrue(self.routes.confirm_from_panel("first", await self.wait_confirmation("first"), "ready"))
        await self.wait_phase("first", "sent")
        await self.routes.enqueue("moved", self.origin, time())
        await self.wait_phase("moved", "offer")
        current["directory"] = f"{self.folder.name}/other"
        self.routes.routes["moved"].remaining = .01
        await self.routes.panel_action("moved", "shown")
        self.assertIn("left the saved project", (await self.wait_phase("moved", "failed"))["message"])
        self.assertEqual(self.sent, ["ses_shown"])
        self.assertEqual(self.routes.receipts.recent("moved"), [])

    async def test_unavailable_route_reopens_with_fresh_countdown_and_can_cancel_offline(self):
        self.resolve.side_effect = RouteUnavailable("OpenCode is temporarily unavailable.")
        await self.routes.enqueue("restart", self.origin, time())
        await self.wait_phase("restart", "waiting")
        await self.routes.enqueue("offscreen", self.origin, time())
        await self.wait_phase("offscreen", "waiting")
        await self.routes.close()
        self.assertEqual((await self.routes.query("restart"))["phase"], "waiting")
        self.assertTrue(self.routes.receipts.set_pending_route("offscreen", "cancelled", "Automatic send cancelled."))

        self.resolve.side_effect = None
        self.routes = AnnotationAutomation(self.folder.name, self.load, self.deliver, self.events.append)
        self.addAsyncCleanup(self.routes.close)
        await self.routes.resume()
        await self.wait_phase("restart", "offer")
        self.assertEqual((await self.routes.query("offscreen"))["phase"], "cancelled")
        self.assertEqual(self.sent, [])
        self.routes.routes["restart"].remaining = .01
        await self.routes.panel_action("restart", "shown")
        self.assertTrue(self.routes.confirm_from_panel("restart", await self.wait_confirmation("restart"), "ready"))
        await self.wait_phase("restart", "sent")
        self.assertEqual(self.sent, [("restart", "ses_test", "bound")])

    async def test_runner_claim_recovers_confirmation_or_uncertainty_without_repost(self):
        store = self.routes.receipts
        scopes = route_scopes({"origin": self.origin})
        rule = store.preferred_rule(scopes)
        for capture in ("accepted", "unknown"):
            self.assertTrue(store.enqueue_route(capture, self.origin, rule, 16))
            delivery = store.claim_route(capture, scopes, rule["id"], "bound", {"id": "ses_test"}, False)
            if capture == "accepted":
                store.change(delivery, "queued", "msg_test")
            with self.assertRaises(ReceiptError):
                store.claim_route(capture, scopes, rule["id"], "bound", {"id": "ses_test"}, False)
        await self.routes.resume()
        self.assertEqual((await self.routes.query("accepted"))["phase"], "sent")
        self.assertEqual((await self.routes.query("unknown"))["phase"], "unconfirmed")
        self.assertFalse(self.sent)

    async def test_managed_launch_preserves_pending_routes_and_allows_local_cancellation(self):
        store = self.routes.receipts
        rule = store.preferred_rule([self.scope])
        for capture in ("resume", "cancel"):
            self.assertTrue(store.enqueue_route(capture, self.origin, rule, 16))

        opencode_managed.cache_clear()
        self.addCleanup(opencode_managed.cache_clear)
        with patch("managed_preferences.forced_boolean", return_value=True):
            await self.routes.resume()
            await self.routes.enqueue("new", self.origin, time())
            self.assertIsNone(self.routes.runner)
            self.assertEqual((await self.routes.query("new"))["phase"], "idle")
            pending = await self.routes.query("resume")
            self.assertEqual((pending["phase"], pending["message"]), ("waiting", OPENCODE_MANAGED))
            self.assertEqual(store.preferred_rule([self.scope]), rule)
            self.assertTrue(await self.routes.cancel("cancel"))
            self.resolve.assert_not_called()
            self.load.assert_not_called()

        opencode_managed.cache_clear()
        await self.routes.resume()
        await self.wait_phase("resume", "offer")
        self.assertEqual((await self.routes.query("cancel"))["phase"], "cancelled")
        self.assertEqual(self.sent, [])
        self.routes.routes["resume"].remaining = .01
        await self.routes.panel_action("resume", "shown")
        self.assertTrue(self.routes.confirm_from_panel("resume", await self.wait_confirmation("resume"), "ready"))
        await self.wait_phase("resume", "sent")
        self.assertEqual(self.sent, [("resume", "ses_test", "bound")])

    async def test_another_runner_cannot_reconcile_live_claims(self):
        await self.routes.resume()
        store = self.routes.receipts
        scopes = route_scopes({"origin": self.origin})
        rule = store.preferred_rule(scopes)
        store.enqueue_route("in-flight", self.origin, rule, 16)
        with self.assertRaisesRegex(ReceiptError, "confirmation expired"):
            store.claim_route("in-flight", scopes, rule["id"], "bound", {"id": "ses_test"}, False, confirmed_before=0)
        self.assertFalse(store.recent("in-flight"))
        store.claim_route("in-flight", scopes, rule["id"], "bound", {"id": "ses_test"}, False)
        other = AnnotationAutomation(self.folder.name, self.load, self.deliver, self.events.append)
        self.addAsyncCleanup(other.close)
        await other.resume()
        self.assertIsNone(other.runner)
        self.assertEqual((await other.query("in-flight"))["phase"], "sending")
        await self.routes.close()
        await other.resume()
        self.assertEqual((await other.query("in-flight"))["phase"], "unconfirmed")


class PortableAnnotationPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_undo_uses_the_receipt_shown_and_reports_the_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            undo = AsyncMock(return_value={"status": "withdrawn"})
            panel = PortableAnnotationPanel("fixture", directory, AsyncMock(), AsyncMock(), undo)
            presentations = []

            @asynccontextmanager
            async def launch(mode, title, seconds):
                presentations.append((mode, title, seconds))
                yield SimpleNamespace(stdout=SimpleNamespace(readline=AsyncMock(return_value=b"undo\n")), wait=AsyncMock())

            panel.launch = launch
            detail = {"capture": "shown-capture", "deliveryId": "shown-receipt", "phase": "sent", "title": "Release review",
                      "receiptStatus": "queued", "undoUntil": int((time() + 30) * 1000)}
            panel.notify(detail)
            await asyncio.wait_for(panel.presentation, 1)
            undo.assert_awaited_once_with("shown-receipt")
            self.assertEqual(presentations, [("annotation-undo", "Release review", 5),
                                             ("annotation-result", "Annotation withdrawn", 3)])
            panel.notify({**detail, "receiptStatus": "unconfirmed"})
            panel.notify({**detail, "undoUntil": 0})
            self.assertIsNone(panel.presentation)
