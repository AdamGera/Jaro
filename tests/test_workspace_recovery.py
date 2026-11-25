import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from desktop import mac_bridge
from engine_lifecycle import browser_workspace_url, install_token, open_browser_workspace, workspace_key


class WorkspaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_in_browser_hands_the_verified_library_to_the_browser_launcher(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        async with httpx.AsyncClient() as client:
            with patch.object(mac_bridge, "library_path", return_value=root):
                bridge = mac_bridge.MacBridge(root, client)
            bridge.connected = True
            with (patch.object(mac_bridge, "running_engine", return_value={"status": "ready"}),
                  patch.object(mac_bridge, "open_browser_workspace") as launch):
                await bridge.dispatch("jard.open", {})
                launch.assert_called_once_with(root)

    def test_macos_browser_launch_keeps_the_credential_out_of_arguments(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        install_token(root)
        with (patch("engine_lifecycle.sys.platform", "darwin"),
              patch("engine_lifecycle.subprocess.run") as launch):
            launch.return_value.returncode = 0
            open_browser_workspace(root)
        url = browser_workspace_url(root)
        launch.assert_called_once_with(["/usr/bin/osascript"], input=f'open location "{url}"\n',
                                       text=True, capture_output=True, timeout=10)

    async def exercise_stale_workspace(self, *, guarded):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token = install_token(root)
        requests = []
        workspace_available = False

        def respond(request):
            requests.append((request.method, request.url.path))
            self.assertEqual(request.headers["X-Jard-Token"], token)
            if request.url.path == "/":
                code = 200 if workspace_available else 500
            elif request.url.path.startswith("/api/native/update-quiesce/"):
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
                    send.assert_called_once_with({"navigate": "app", "workspaceKey": workspace_key(token)})
                    expected += [("GET", "/"), ("POST", f"/api/clients/{bridge.client_id}")]
                self.assertEqual(requests, expected)

    async def test_restarts_verified_engine_when_its_workspace_returns_legacy_500(self):
        await self.exercise_stale_workspace(guarded=False)

    async def test_does_not_restart_when_another_client_or_job_holds_the_engine(self):
        await self.exercise_stale_workspace(guarded=True)

    async def test_update_can_quiesce_before_the_workspace_connects(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        token = install_token(root)
        requests = []
        response_status = 202

        def respond(request):
            requests.append(request)
            return httpx.Response(response_status)

        async with httpx.AsyncClient(base_url=mac_bridge.URL, transport=httpx.MockTransport(respond)) as client:
            for state in ("offline", "idle", "busy", "foreign"):
                with self.subTest(state=state), patch.object(mac_bridge, "library_path", return_value=root):
                    bridge = mac_bridge.MacBridge(root, client)
                    bridge.wait_for_engine_exit = AsyncMock()
                    response_status = 409 if state == "busy" else 202
                    requests.clear()
                    status = {"status": "ready"} if state in ("idle", "busy") else None
                    with patch.object(mac_bridge, "running_engine", return_value=status,
                                      side_effect=RuntimeError("Unknown engine") if state == "foreign" else None):
                        if state in ("offline", "idle"):
                            self.assertEqual(await bridge.dispatch("jard.updateQuiesce", {}), {"ready": True})
                            self.assertTrue(bridge.updating)
                            bridge.wait_for_engine_exit.assert_awaited_once()
                            for command in ("jard.prepare", "jard.setup"):
                                with self.assertRaisesRegex(ValueError, "preparing to restart"):
                                    await bridge.dispatch(command, {})
                        else:
                            with self.assertRaises(RuntimeError if state == "foreign" else httpx.HTTPStatusError):
                                await bridge.dispatch("jard.updateQuiesce", {})
                            self.assertFalse(bridge.updating)
                            bridge.wait_for_engine_exit.assert_not_awaited()
                    if state in ("idle", "busy"):
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(requests[0].url.path, f"/api/native/update-quiesce/{bridge.client_id}")
                        self.assertEqual(requests[0].headers["X-Jard-Token"], token)
                    else:
                        self.assertEqual(requests, [])
                    self.assertFalse(bridge.connected)

    async def test_update_quiesce_protects_pending_engine_preparation(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        async with httpx.AsyncClient() as client:
            with patch.object(mac_bridge, "library_path", return_value=root):
                bridge = mac_bridge.MacBridge(root, client)
            bridge.wait_for_engine_exit = AsyncMock()
            with patch.object(mac_bridge, "running_engine", return_value=None) as engine:
                async with bridge.setup_lock:
                    with self.assertRaisesRegex(ValueError, "still preparing"):
                        await bridge.dispatch("jard.updateQuiesce", {})
                    self.assertFalse(bridge.updating)
                    engine.assert_not_called()
                self.assertEqual(await bridge.dispatch("jard.updateQuiesce", {}), {"ready": True})
                engine.assert_called_once()
