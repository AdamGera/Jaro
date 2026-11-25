import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import opencode_wsl_worker as worker


class WindowsDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_preference_fallback_and_selected_service(self):
        import annotation_routing as routing
        from opencode_wsl import Distribution

        native = routing.Registration("http://127.0.0.1:1234", "127.0.0.1", 1234, 22, "2.0", None, "service.json")
        guest = {"origin": native.origin, "pid": native.pid, "version": native.version, "channel": native.channel, "key": "c" * 64}
        distribution = Distribution("Ubuntu", "4c730f09-bb44-4fab-b209-431a9f729c50")

        async def find_native(_, expected, directory, *, channel):
            if expected not in (None, native.key) or channel not in (None, native.channel):
                raise routing.RouteUnavailable("missing service")
            return native

        directory = Path("fixture")
        with (patch.object(routing, "sys", SimpleNamespace(platform="win32")),
              patch.object(routing, "state_directory", return_value=directory),
              patch.object(routing, "discover_native", side_effect=find_native),
              patch("opencode_wsl.enabled", return_value=False) as enabled,
              patch("opencode_wsl.default_distribution", return_value=distribution),
              patch("opencode_wsl.discover", new_callable=AsyncMock, return_value=guest) as find_guest):
            self.assertIs(await routing.discover(None, None, directory), native)
            find_guest.assert_not_called()
            enabled.return_value = True
            wsl = await routing.discover(None, None, directory)
            self.assertEqual((wsl.key, wsl.wsl, wsl.channel), (guest["key"], distribution, "wsl:service.json"))
            self.assertIs(await routing.discover(None, native.key, directory), native)
            self.assertEqual(await routing.discover(None, wsl.key, directory), wsl)
            find_guest.return_value = None
            self.assertIs(await routing.discover(None, None, directory), native)
            with self.assertRaises(routing.RouteUnavailable):
                await routing.discover(None, wsl.key, directory)

    async def test_settings_require_the_owned_desktop_and_exact_origin(self):
        import httpx
        from fastapi import FastAPI
        from portable_workspace import BOOTSTRAP, configure_workspace
        from server import read_opencode_wsl, write_opencode_wsl

        app = FastAPI()
        origin = "http://127.0.0.1:43923"
        record = {"port": 43923}
        configure_workspace(app, record, origin)
        app.state.draining = False
        endpoint = "/api/portable/annotation-handoff/wsl"
        app.add_api_route(endpoint, read_opencode_wsl, methods=["GET"])
        app.add_api_route(endpoint, write_opencode_wsl, methods=["PUT"])
        with (patch("server.sys", SimpleNamespace(platform="win32")), patch("server.URL", origin),
              patch("opencode_wsl.enabled", return_value=False), patch("opencode_wsl.set_enabled") as save):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                headers = {"Origin": origin}
                await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await client.get(endpoint)).status_code, 403)
                await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                self.assertEqual((await client.get(endpoint)).json(), {"enabled": False})
                payload = {"enabled": True}
                self.assertEqual((await client.put(endpoint, json=payload)).status_code, 403)
                self.assertGreaterEqual((await client.put(endpoint, headers={"Origin": "https://other.test"}, json=payload)).status_code, 400)
                save.assert_not_called()
                self.assertEqual((await client.put(endpoint, headers=headers, json=payload)).json(), payload)
                save.assert_called_once_with(True)


@unittest.skipUnless(sys.platform == "linux", "checks the Linux listener inside the guest")
class LinuxGuestTests(unittest.TestCase):
    def test_guest_process_revalidates_registration_before_sending_or_withdrawing(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop/tests"))
        from opencode_fixture import local_opencode

        with tempfile.TemporaryDirectory() as folder, local_opencode(Path(folder) / "opencode") as fixture:
            registration, requests, posted, outcome = fixture

            def command(payload, scope="ubuntu"):
                script = Path(worker.__file__).read_text()
                if payload["action"] == "match-project":
                    script = Path(worker.__file__).with_name("annotation_projects.py").read_text() + "\n" + script
                return subprocess.run([sys.executable, "-I", "-S", "-c", script],
                                      input=json.dumps({**payload, "scope": scope}), capture_output=True, text=True, timeout=8,
                                      env={**os.environ, "XDG_STATE_HOME": folder})

            valid = registration.read_text()
            registration.chmod(0o644)
            self.assertIsNone(json.loads(command({"action": "find"}).stdout))
            self.assertEqual(requests, [])
            registration.chmod(0o600)
            record = json.loads(command({"action": "find"}).stdout)
            self.assertNotIn("password", record)
            project = dict(action="match-project", expected=record["key"], channel=record["channel"],
                           target="/home/fixture/workspace", directories=["/home/fixture/workspace2", "/home/fixture/workspace/subdir"])
            self.assertEqual(json.loads(command(project).stdout), [1])
            self.assertNotEqual(command(project, scope="different-distribution").returncode, 0)
            destination = dict(action="request", expected=record["key"], channel=record["channel"])
            session = "/api/session/ses_project"
            payload = {**destination, "method": "POST", "path": session + "/prompt",
                       "body": {"text": "Local narration", "files": [{"name": "fixture.png", "uri": "data:image/png;base64,AA=="}], "delivery": "queue"}}
            self.assertNotEqual(command(payload, scope="different-distribution").returncode, 0)
            registration.write_text(json.dumps({**json.loads(valid), "pid": 2**31 - 1}))
            self.assertNotEqual(command(payload).returncode, 0)
            self.assertEqual(posted, [])
            registration.write_text(valid)
            status, body = json.loads(command(payload).stdout)
            self.assertEqual(status, 200)
            self.assertEqual(posted[0], (session + "/prompt", payload["body"]))
            message = json.loads(base64.b64decode(body))["data"]["id"]
            result = command({**destination, "method": "DELETE", "path": session + "/inbox/" + message})
            self.assertEqual(json.loads(result.stdout)[0], 200)
            self.assertEqual(outcome["inbox"], ["msg_other"])
