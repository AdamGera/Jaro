import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from runpy import run_path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI

from engine_lifecycle import ClientLeases
import linux_direct
from portable_update import INSTALL_ERROR, PortableUpdater, Release, apply_windows, installer_bytes, release_for, stage_installer
from portable_workspace import BOOTSTRAP, configure_workspace
from server import connect_browser, portable_update_action, portable_update_status


async def write_installer_fixture(offer, staging):
    (staging / "installer.exe").write_bytes(offer.digest.encode("ascii"))


class PortableUpdateTests(unittest.IsolatedAsyncioTestCase):
    def test_desktop_relaunch_queues_activation_and_unlocks_on_exit(self):
        module = Path(__file__).resolve().parents[1] / "desktop/portable/desktop_instance.py"
        claim_desktop = run_path(str(module))["claim_desktop"]
        with TemporaryDirectory() as directory:
            library = Path(directory) / "library"
            with claim_desktop(library) as first:
                self.assertTrue(first.primary)
                self.assertIsNone(first.take_activation())
                with claim_desktop(library) as second:
                    self.assertFalse(second.primary)
                    second.activate()
                    second.activate()
                self.assertEqual(first.take_activation(), b"")
                self.assertIsNone(first.take_activation())
                with claim_desktop(Path(directory) / "different-library") as other:
                    self.assertTrue(other.primary)
            with claim_desktop(library) as reopened:
                self.assertTrue(reopened.primary)
                self.assertIsNone(reopened.take_activation())

    def test_only_published_matching_artifacts_are_selected(self):
        assets = [
            {"name": name, "size": 200, "state": "uploaded", "digest": "sha256:" + "a" * 64}
            for name in ("Jaro-arm64.zip", "Jaro-Setup-0.0.10-windows-x64-test.exe", "jaro_0.0.10-2_amd64.deb",
                         "jaro_0.0.10-12_amd64.deb", "jaro_0.0.10-21_arm64.deb", "Jaro-Setup-0.0.10-windows-x64.exe")
        ]
        data = {"tag_name": "v0.0.10", "assets": assets}
        _, debian = release_for(data, "debian")
        _, windows = release_for(data, "windows")
        self.assertEqual(debian.name, "jaro_0.0.10-12_amd64.deb")
        self.assertEqual(windows.url, "https://github.com/AdamGera/Jaro/releases/download/v0.0.10/Jaro-Setup-0.0.10-windows-x64.exe")
        self.assertEqual(release_for({**data, "assets": assets[:2]}, "windows"), ("0.0.10", None))
        with self.assertRaises(ValueError):
            release_for({**data, "prerelease": True}, "windows")

        archive = {**assets[0], "name": linux_direct.ARCHIVE}
        manifest = {**assets[0], "name": linux_direct.MANIFEST}
        self.assertEqual(release_for({**data, "assets": [*assets, archive]}, linux_direct.KIND), ("0.0.10", None))
        self.assertEqual(release_for({**data, "assets": [*assets, manifest, archive]}, linux_direct.KIND)[1].name,
                         linux_direct.ARCHIVE)

    @unittest.skipUnless(sys.platform == "linux" and Path("/usr/bin/openssl").is_file(), "Linux update runtime")
    async def test_signed_linux_update_stages_applies_and_recovers(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            key = work / "key.pem"
            subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "Ed25519", "-out", key], check=True)
            public = subprocess.check_output(["/usr/bin/openssl", "pkey", "-in", key, "-pubout", "-outform", "DER"])
            publisher = base64.b64encode(public[-32:]).decode("ascii")

            def bundle(version):
                package = work / version
                payload = package / "payload"
                launcher = payload / "bin/jaro"
                launcher.parent.mkdir(parents=True)
                launcher.write_text("#!/bin/sh\nexit 0\n")
                launcher.chmod(0o755)
                workspace = payload / "desktop/portable"
                workspace.mkdir(parents=True)
                (workspace / "open_workspace.py").touch()
                runtime = payload / "venv"
                runtime.mkdir()
                (runtime / "pyvenv.cfg").touch()
                (payload / "portable-release.json").write_text(json.dumps({
                    "version": version, "kind": linux_direct.KIND, "revision": 0,
                }))
                archive = package / linux_direct.ARCHIVE
                with tarfile.open(archive, "w:gz") as output:
                    for item in payload.iterdir():
                        output.add(item, arcname=item.name)
                contents = archive.read_bytes()
                digest, size = hashlib.sha256(contents).hexdigest(), len(contents)
                message = package / "message"
                message.write_bytes(linux_direct.signed_message(version, size, digest))
                signature = subprocess.check_output(["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin",
                                                     "-inkey", key, "-in", message])
                manifest = package / linux_direct.MANIFEST
                manifest.write_text(json.dumps({"version": version, "size": size, "sha256": digest,
                                                "signature": base64.b64encode(signature).decode("ascii")}))
                return archive, manifest, Release(version, archive.name, 0, size, digest)

            archive, manifest, _ = bundle("0.0.9")
            root = work / "jaro-app"
            linux_direct.bootstrap(root, archive, manifest, publisher)
            self.assertEqual(linux_direct.active_version(root).name, "0.0.9")
            with self.assertRaisesRegex(ValueError, "signature"):
                linux_direct.read_manifest(manifest, linux_direct.PUBLISHER)
            self.assertEqual((root / "jaro").readlink(), Path("manager.py"))
            library = work / "jaro"
            library.mkdir()
            (library / "session.txt").touch()
            retired = root / "versions/0.0.8"
            retired.mkdir(mode=0o700)
            (retired / "portable-release.json").touch()
            unrelated = root / "versions/0.0.7"
            unrelated.symlink_to(library, target_is_directory=True)

            for version, outcome in [("0.0.10", "success"), ("0.0.11", "tampered"), ("0.0.12", "failed launch")]:
                archive, manifest, offer = bundle(version)

                async def transfer(_, url):
                    path = manifest if url.endswith(manifest.name) else archive
                    yield path.read_bytes()

                with TemporaryDirectory(prefix="jaro-update-") as directory:
                    staging = Path(directory).resolve()
                    updater = PortableUpdater(linux_direct.active_version(root).name, linux_direct.KIND,
                                              staging=staging, publisher=publisher)
                    with (patch("portable_update.latest_release", return_value=(version, offer)),
                          patch("portable_update.installer_bytes", new=transfer)):
                        self.assertEqual((await updater.check())["phase"], "ready")
                        await updater.confirm_restart()
                        updater.prepare_restart()
                    if outcome == "tampered":
                        (staging / archive.name).write_bytes(b"changed after verification")
                    process = Mock()
                    process.wait.return_value = int(outcome == "failed launch")
                    real_popen = subprocess.Popen

                    def start(args, *positional, **kwargs):
                        if args[0] == root / "jaro":
                            return process
                        return real_popen(args, *positional, **kwargs)

                    with patch("linux_direct.subprocess.Popen", side_effect=start) as launch:
                        linux_direct.apply_update(root, staging)
                    self.assertFalse(staging.exists())
                    self.assertEqual(linux_direct.active_version(root).name, "0.0.10")
                    self.assertFalse(retired.exists())
                    self.assertTrue(unrelated.is_symlink())
                    if outcome == "success":
                        self.assertTrue((root / "versions/0.0.9").is_dir())
                    else:
                        self.assertFalse((root / "versions" / version).exists())
                    self.assertTrue((library / "session.txt").is_file())
                    launches = [call for call in launch.call_args_list if call.args[0][0] == root / "jaro"]
                    self.assertEqual(len(launches), 2 if outcome == "failed launch" else 1)
                    self.assertEqual(launches[-1].kwargs["env"].get("JARO_UPDATE_ERROR"),
                                     None if outcome == "success" else "1")

    async def test_installer_size_and_digest_are_both_required(self):
        installer = b"fixture installer"
        digest = hashlib.sha256(installer).hexdigest()
        offer = Release("0.0.10", "test.exe", 0, len(installer), digest)
        with TemporaryDirectory() as directory:
            stage = Path(directory)
            async def chunks(*_):
                yield content
            with patch("portable_update.installer_bytes", new=chunks):
                for content in (b"short", b"012345678901234567890", b"fixture installeQ"):
                    with self.assertRaises(ValueError):
                        await stage_installer(offer, stage)
                    self.assertFalse(list(stage.iterdir()))
                content = installer
                await stage_installer(offer, stage)
            self.assertEqual((stage / "installer.exe").read_bytes(), installer)

    @unittest.skipUnless(sys.platform == "linux", "Linux installation lock")
    def test_direct_uninstall_waits_for_jaro_and_preserves_the_library(self):
        with TemporaryDirectory() as directory:
            data = Path(directory)
            root, library = data / "jaro-app", data / "jaro"
            library.mkdir()
            session = library / "session"
            session.touch()
            root.mkdir(mode=0o700)
            (root / "versions").mkdir(mode=0o700)
            (root / "installed.json").write_text(json.dumps(linux_direct.PUBLISHER))
            name = f"{linux_direct.APP_ID}.desktop"
            launcher = data / "applications" / name
            login = data / "config/autostart" / name
            command = linux_direct.desktop_argument(root / "jaro")
            launcher.parent.mkdir()
            launcher.write_text(f"[Desktop Entry]\nExec={command}\n")
            login.parent.mkdir(parents=True)
            login.write_text("[Desktop Entry]\nExec=/home/test/custom-launcher\n")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(data / "config"), "RUMBLE_DATA_DIR": str(library)}):
                with linux_direct.installation_lock(root):
                    with self.assertRaisesRegex(ValueError, "Close Jaro"):
                        linux_direct.uninstall(root)
                self.assertTrue(launcher.exists())
                self.assertTrue(root.exists())
                linux_direct.uninstall(root)
            self.assertFalse(root.exists())
            self.assertFalse(launcher.exists())
            self.assertTrue(login.exists())
            self.assertTrue(session.exists())

    async def test_installer_download_never_follows_an_unexpected_redirect(self):
        release = Release("0.0.10", "Jaro-Setup-0.0.10-windows-x64.exe", 0, 3, "")
        for target in ("https://release-assets.githubusercontent.com/fixture", "https://example.org/fixture"):
            seen = []

            def response(request):
                seen.append(str(request.url))
                if len(seen) == 1:
                    return httpx.Response(302, headers={"Location": target})
                return httpx.Response(200, content=b"exe")

            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                if target.endswith("githubusercontent.com/fixture"):
                    self.assertEqual(b"".join([chunk async for chunk in installer_bytes(client, release.url)]), b"exe")
                    self.assertEqual(seen, [release.url, target])
                else:
                    with self.assertRaises(ValueError):
                        [chunk async for chunk in installer_bytes(client, release.url)]
                    self.assertEqual(seen, [release.url])

    async def test_cancelled_transfer_removes_the_partial_installer(self):
        offer = Release("0.0.10", "test.exe", 0, 3, "a" * 64)
        started = asyncio.Event()

        async def interrupted(*_):
            yield b"x"
            started.set()
            await asyncio.Event().wait()

        with TemporaryDirectory() as directory, patch("portable_update.installer_bytes", new=interrupted):
            folder = Path(directory)
            transfer = asyncio.create_task(stage_installer(offer, folder))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue((folder / "installer.partial").is_file())
            transfer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await transfer
            self.assertFalse(list(folder.iterdir()))

    async def test_check_reports_platform_specific_current_and_manual_installs(self):
        offer = Release("0.0.9", "jaro_0.0.9-22_amd64.deb", 22, 25, "")
        updater = PortableUpdater("0.0.9", "debian", 21)
        with patch("portable_update.latest_release", return_value=("0.0.9", offer)):
            self.assertEqual((await updater.check())["phase"], "download")
            with patch("portable_update.webbrowser.open", return_value=True) as open_link:
                await updater.download()
                open_link.assert_called_once_with(offer.url)
        with patch("portable_update.latest_release", return_value=("0.0.10", None)):
            status = await updater.check()
            self.assertIn("no download for this platform", status["message"])
        with patch("portable_update.latest_release", return_value=("0.0.9", None)):
            self.assertEqual((await updater.check())["phase"], "current")

    async def test_background_checks_push_status_on_the_existing_client_stream(self):
        origin = "http://127.0.0.1:43721"
        app = FastAPI()
        state = app.state
        state.leases = ClientLeases()
        state.draining = False
        state.portable_updater = updater = PortableUpdater("0.0.9", "debian", 21)
        offer = Release("0.0.10", "jaro_0.0.10-1_amd64.deb", 1, 25, "")
        request = SimpleNamespace(headers={"origin": origin}, app=app)
        held = asyncio.Event()

        async def release(_):
            await held.wait()
            return offer.version, offer

        with (patch("server.URL", origin), patch("portable_update.STARTUP_DELAY", 0),
              patch("portable_update.latest_release", new=release)):
            response = await connect_browser(uuid4(), request)
            stream = response.body_iterator
            self.assertIn('"phase": "idle"', await anext(stream))
            worker = asyncio.create_task(updater.watch())
            try:
                checking = await asyncio.wait_for(anext(stream), .5)
                self.assertIn('"phase": "checking"', checking)
                self.assertIn('"manualCheck": false', checking)
                held.set()
                ready = await asyncio.wait_for(anext(stream), .5)
                self.assertIn("event: update", ready)
                self.assertIn('"phase": "download"', ready)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await stream.aclose()

    async def test_restart_requires_a_verified_installer_and_exclusive_workspace(self):
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/updates"
        app.add_api_route(endpoint, portable_update_status, methods=["GET"])
        app.add_api_route(endpoint + "/{action}", portable_update_action, methods=["POST"])
        configure_workspace(app, record, origin)
        state = app.state
        state.portable_recording = state.portable_screencast = state.portable_dictation = None
        state.model_setup = state.submitting = state.draining = state.jobs = None
        state.status = "ready"
        state.insights = SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0))
        state.leases = ClientLeases()
        state.request_shutdown = Mock()
        client = str(uuid4())
        headers = {"Origin": origin}
        offer = Release("0.0.10", "Jaro-Setup-0.0.10-windows-x64.exe", 0, 25, "a" * 64)
        with (TemporaryDirectory() as directory, patch("server.URL", origin),
              patch("portable_update.updates_disabled", return_value=False),
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.stage_installer", side_effect=write_installer_fixture),
              patch("portable_update.verified_signer", new_callable=AsyncMock) as signer,
              patch("portable_update.latest_release", new_callable=AsyncMock) as feed):
            feed.return_value = offer.version, offer
            state.portable_updater = PortableUpdater("0.0.9", "windows", staging=Path(directory))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.get(endpoint)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                check, restart = (endpoint + f"/{action}?client={client}" for action in ("check", "restart"))
                signer.side_effect = [b"publisher", b"other"]
                self.assertEqual((await browser.post(check, headers=headers)).json()["phase"], "download")
                self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                signer.side_effect = None
                signer.return_value = b"publisher"
                self.assertEqual((await browser.post(check, headers=headers)).json()["phase"], "ready")
                with state.leases.browser_connection(client), state.leases.browser_connection(str(uuid4())):
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                state.portable_recording = object()
                with state.leases.browser_connection(client):
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                    state.portable_recording = None

                    async def capture_begins(_):
                        state.portable_recording = object()
                        return offer.version, offer

                    feed.side_effect = capture_begins
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                    self.assertFalse((Path(directory) / "restart.json").exists())
                    state.portable_recording = None
                    feed.side_effect = None
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 202)
            self.assertTrue(state.draining)
            state.request_shutdown.assert_called_once()
            self.assertEqual(json.loads((Path(directory) / "restart.json").read_text())["digest"], offer.digest)

    async def test_staged_installer_is_reused_and_removed_if_its_release_changes(self):
        offer = Release("0.0.10", "Jaro-Setup-0.0.10-windows-x64.exe", 0, 25, "a" * 64)
        with (TemporaryDirectory() as directory, patch("portable_update.stage_installer", side_effect=write_installer_fixture) as transfer,
              patch("portable_update.verified_signer", return_value=b"publisher"),
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.latest_release", return_value=(offer.version, offer)) as feed):
            folder = Path(directory)
            updater = PortableUpdater("0.0.9", "windows", staging=folder)
            self.assertEqual((await updater.check(manual=False))["phase"], "ready")
            self.assertEqual((await updater.check(manual=False))["phase"], "ready")
            transfer.assert_awaited_once()
            feed.side_effect = httpx.ConnectError("offline")
            with self.assertRaisesRegex(ValueError, "Could not confirm"):
                await updater.confirm_restart()
            self.assertEqual(updater.status()["phase"], "ready")
            feed.side_effect = None
            feed.return_value = offer.version, replace(offer, digest="b" * 64)
            (folder / "installer.exe").write_bytes(b"a staged installer")
            with self.assertRaisesRegex(ValueError, "available update changed"):
                await updater.confirm_restart()
            self.assertEqual(updater.status()["phase"], "idle")
            self.assertFalse(list(folder.iterdir()))

    def test_worker_relaunches_after_success_or_failure_under_the_installer_lock(self):
        with TemporaryDirectory() as root:
            launcher = Path(root) / "Programs/Jaro/bin/Jaro.exe"
            launcher.parent.mkdir(parents=True)
            pointer = launcher.parent.parent / "current-version"
            contents = b"verified installer"
            for exit_status in (0, 1):
                pointer.write_text("0.0.9", encoding="ascii")
                with TemporaryDirectory(prefix="jaro-update-") as staging:
                    directory = Path(staging)
                    (directory / "installer.exe").write_bytes(contents)
                    (directory / "restart.json").write_text(json.dumps({
                        "version": "0.0.10", "digest": hashlib.sha256(contents).hexdigest(),
                    }))

                    def install(args, **_):
                        self.assertIn("/JAROUPDATE=0.0.9", args)
                        if not exit_status:
                            pointer.write_text("0.0.10", encoding="ascii")
                        return SimpleNamespace(returncode=exit_status)

                    with (patch("portable_update.windows_launcher", return_value=launcher),
                          patch("portable_update.signed_by", return_value=b"publisher"),
                          patch("portable_update.updates_disabled", return_value=False),
                          patch("portable_update.wait_for_launcher", return_value=True) as wait,
                          patch("portable_update.subprocess.run", side_effect=install),
                          patch("portable_update.subprocess.Popen") as launch):
                        launch.return_value.wait.return_value = 0
                        apply_windows(directory, 100)
                    wait.assert_called_once_with(100)
                    self.assertFalse(directory.exists())
                    args, fields = launch.call_args
                    self.assertEqual(args, ([launcher],))
                    self.assertEqual("JARO_UPDATE_ERROR" in fields["env"], bool(exit_status))
                    if exit_status:
                        self.assertEqual(PortableUpdater("0.0.9", "windows", previous_error="1").status()["message"], INSTALL_ERROR)

    def test_windows_update_rolls_back_on_failed_relaunch_and_prunes_old_versions(self):
        with TemporaryDirectory() as root:
            launcher = Path(root) / "Programs/Jaro/bin/Jaro.exe"
            launcher.parent.mkdir(parents=True)
            install_root = launcher.parent.parent
            pointer = install_root / "current-version"
            for name in ("0.0.8", "0.0.9", "0.0.10"):
                folder = install_root / "versions" / name
                folder.mkdir(parents=True)
                (folder / "portable-release.json").write_text("{}", encoding="ascii")
            contents = b"verified installer"
            pointer.write_text("0.0.9", encoding="ascii")
            with TemporaryDirectory(prefix="jaro-update-") as staging:
                directory = Path(staging)
                (directory / "installer.exe").write_bytes(contents)
                (directory / "restart.json").write_text(json.dumps({
                    "version": "0.0.10", "digest": hashlib.sha256(contents).hexdigest(),
                }))

                def install(_args, **_):
                    pointer.write_text("0.0.10", encoding="ascii")
                    return SimpleNamespace(returncode=0)

                first, second = Mock(), Mock()
                first.wait.return_value = 1
                with (patch("portable_update.windows_launcher", return_value=launcher),
                      patch("portable_update.signed_by", return_value=b"CN=Jaro"),
                      patch("portable_update.updates_disabled", return_value=False),
                      patch("portable_update.wait_for_launcher", return_value=True),
                      patch("portable_update.subprocess.run", side_effect=install),
                      patch("portable_update.subprocess.Popen", side_effect=[first, second]) as launch):
                    apply_windows(directory, 100)
                self.assertEqual(pointer.read_text(encoding="ascii").strip(), "0.0.9")
                self.assertFalse((install_root / "versions/0.0.10").exists())
                self.assertTrue((install_root / "versions/0.0.9").is_dir())
                self.assertEqual(launch.call_count, 2)
                self.assertEqual(launch.call_args_list[-1].kwargs["env"].get("JARO_UPDATE_ERROR"), "1")

    async def test_orphan_partial_installer_is_cleared_and_cleanup_download_blocks_restart(self):
        from fastapi import HTTPException
        from server import require_portable_update_idle

        installer = b"fixture installer"
        offer = Release("0.0.10", "test.exe", 0, len(installer), hashlib.sha256(installer).hexdigest())
        with TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "installer.partial").write_bytes(b"leftover")
            updater = PortableUpdater("0.0.9", "windows", staging=stage)
            updater.clear_installer()
            self.assertFalse((stage / "installer.partial").exists())
            (stage / "installer.partial").write_bytes(b"leftover")

            async def chunks(*_):
                yield installer

            with patch("portable_update.installer_bytes", new=chunks):
                await stage_installer(offer, stage)
            self.assertEqual((stage / "installer.exe").read_bytes(), installer)

        state = SimpleNamespace(
            status="ready", draining=False, submitting=set(), jobs=set(), sdk_pending=0, screenshot_pending=False,
            insights=SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0)),
            leases=ClientLeases(), portable_recording=None, portable_screencast=None,
            portable_dictation=SimpleNamespace(busy=False, authorizing=False, paste_authorizing=True),
            portable_recording_lock=None, model_setup=None, stupase_cleanup=SimpleNamespace(downloading=None),
        )
        with self.assertRaises(HTTPException):
            require_portable_update_idle(state, "c1")
        state.portable_dictation.paste_authorizing = False
        state.stupase_cleanup.downloading = object()
        with self.assertRaises(HTTPException):
            require_portable_update_idle(state, "c1")
