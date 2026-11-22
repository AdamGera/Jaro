import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from desktop import mac_bridge


class WorkspaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_stale_workspace(self, *, guarded):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".native-token").write_text("a" * 64)
        requests = []
        workspace_available = False

        def respond(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/":
                code = 200 if workspace_available else 500
            elif request.url.path.startswith("/api/native/update-quiesce/"):
                self.assertEqual(request.headers["X-Jard-Token"], "a" * 64)
                code = 409 if guarded else 202
            else:
                self.assertTrue(request.url.path.startswith("/api/clients/"))
                self.assertTrue(workspace_available)
                code = 204
            return httpx.Response(code)

        async def launch(_, action):
            nonlocal workspace_available
            if action == "model-status":
                return '{"ready": true}'
            self.assertEqual(action, "start")
            workspace_available = True

        async with httpx.AsyncClient(base_url=mac_bridge.URL, transport=httpx.MockTransport(respond)) as client:
            with patch.object(mac_bridge, "library_path", return_value=root):
                bridge = mac_bridge.MacBridge(root, client)
            bridge.start_control = AsyncMock()
            bridge.wait_for_engine_exit = AsyncMock()
            bridge.automatic_annotations.resume = AsyncMock()
            bridge.lease_task = object()
            verified_engine = {"status": "ready"}
            with patch.object(mac_bridge, "running_engine", return_value=verified_engine), \
                    patch.object(mac_bridge, "run_helper", side_effect=launch) as helper, \
                    patch.object(mac_bridge, "send") as send:
                expected = [("GET", "/"), ("POST", f"/api/native/update-quiesce/{bridge.client_id}")]
                if guarded:
                    with self.assertRaises(httpx.HTTPStatusError) as error:
                        await bridge.prepare()
                    self.assertEqual(error.exception.response.status_code, 409)
                    self.assertFalse(bridge.connected)
                    helper.assert_not_awaited()
                    bridge.wait_for_engine_exit.assert_not_awaited()
                    send.assert_not_called()
                else:
                    self.assertEqual(await bridge.prepare(), {"ready": True})
                    self.assertTrue(bridge.connected)
                    bridge.wait_for_engine_exit.assert_awaited_once()
                    send.assert_called_once_with({"navigate": "app"})
                    expected += [("GET", "/"), ("POST", f"/api/clients/{bridge.client_id}")]
                self.assertEqual(requests, expected)

    async def test_restarts_verified_engine_when_its_workspace_returns_legacy_500(self):
        await self.exercise_stale_workspace(guarded=False)

    async def test_does_not_restart_when_another_client_or_job_holds_the_engine(self):
        await self.exercise_stale_workspace(guarded=True)
