import asyncio
import base64
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from runpy import run_path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request

from engine_lifecycle import ClientLeases
import linux_direct
from portable_tray import MeetingTray
from portable_update import (CHANGED_STAGE_ERROR, INSTALL_ERROR, REJECTED_ERROR, PortableUpdater, Release,
                             apply_windows, installer_bytes, release_for, stage_installer)
from portable_workspace import BOOTSTRAP, configure_workspace
from server import (
    connect_browser, portable_claim_tray_restart, portable_update_action, portable_update_settings_opened,
    portable_update_status, portable_update_view, prepare_portable_restart, restart_from_portable_tray, workspace_page,
)


INSTALLER_FIXTURES = {hashlib.sha256(data).hexdigest(): data for data in (b"a" * 64, b"b" * 64)}


def windows_fixture_offer(version="0.0.10", data=b"a" * 64):
    digest = hashlib.sha256(data).hexdigest()
    return Release(version, f"Jaro-Setup-{version}-windows-x64.exe", 0, len(data), digest)


async def write_installer_fixture(offer, staging):
    (staging / "installer.exe").write_bytes(INSTALLER_FIXTURES[offer.digest])


class PortableUpdateTests(unittest.IsolatedAsyncioTestCase):
    def test_linux_direct_does_not_fall_back_to_a_different_python_abi(self):
        with TemporaryDirectory() as directory:
            payload = Path(directory)
            (payload / "venv/lib/python3.99/site-packages").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "requires python3.99"):
                linux_direct.system_python(payload)

    async def test_closing_the_last_portable_window_waits_for_engine_work_without_a_tray(self):
        from server import engine_work_in_progress, portable_work_in_progress, watch_closed_portable_window

        state = SimpleNamespace(draining=False, status="ready", jobs={object()}, submitting=set(), model_setup=None,
                                portable_recording=None, portable_screencast=None, leases=ClientLeases(),
                                insights=SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0)),
                                request_shutdown=Mock())
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            closed = folder / "workspace-closed"
            closed.touch()
            tray = MeetingTray(state, folder, lambda: False, lambda: None, lambda _: "",
                               shutdown_busy=lambda: engine_work_in_progress(state) or portable_work_in_progress(state))
            tray.submit("quit")
            self.assertTrue((folder / "open").is_file())
            state.request_shutdown.assert_not_called()

            watcher = asyncio.create_task(watch_closed_portable_window(state, folder))
            try:
                await asyncio.sleep(.3)
                state.request_shutdown.assert_not_called()
                state.jobs.clear()
                state.model_setup = {"phase": "downloading"}
                await asyncio.sleep(.3)
                state.request_shutdown.assert_not_called()
                state.model_setup = None
                state.portable_recording = object()
                await asyncio.sleep(.3)
                state.request_shutdown.assert_not_called()
                state.portable_recording = None
                closed.unlink()
                await asyncio.sleep(.3)
                state.request_shutdown.assert_not_called()
                closed.touch()
                await asyncio.wait_for(watcher, 1)
                state.request_shutdown.assert_called_once()
                self.assertTrue(state.draining)
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

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

    def test_running_window_receives_launcher_and_tray_activations_over_its_pipe(self):
        portable = Path(__file__).resolve().parents[1] / "desktop/portable"
        real_popen = subprocess.Popen
        with patch.object(sys, "path", [str(portable), *sys.path]):
            run_window = run_path(str(portable / "open_workspace.py"))["run_window"]
        with TemporaryDirectory() as directory:
            tray = Path(directory)
            (tray / "open").touch()
            activations = iter([b"+startup-token"])
            instance = SimpleNamespace(pid=tray / "instance-pid", set_window=Mock(),
                                       take_activation=lambda: next(activations, None))
            engine = SimpleNamespace(poll=lambda: None)
            fixture = ("import os, sys\nfrom threading import Timer\n"
                       "timer = Timer(5, lambda: os._exit(2))\n"
                       "timer.daemon = True\ntimer.start()\n"
                       "assert sys.stdin.buffer.readline() == b'startup-token\\n'\n"
                       "assert sys.stdin.buffer.readline() == b'\\n'\n")

            def start(_args, **options):
                return real_popen([sys.executable, "-c", fixture], **options)

            with patch("subprocess.Popen", side_effect=start):
                self.assertEqual(run_window(instance, "host", engine, tray, dict(os.environ)), 0)
            instance.set_window.assert_called_once()
            self.assertFalse((tray / "open").exists())
            self.assertFalse((tray / "window-pid").exists())

    def test_windows_webview_profile_stays_with_the_library_across_versions(self):
        portable = Path(__file__).resolve().parents[1] / "desktop/portable"
        with patch.object(sys, "path", [str(portable), *sys.path]):
            run_window = run_path(str(portable / "open_workspace.py"))["run_window"]
        with TemporaryDirectory() as directory, patch.object(sys, "platform", "win32"):
            root = Path(directory).resolve()
            first, second = root / "First Library", root / "Second Library"
            host = root / "versions" / "0.0.9" / "jaro.exe"
            env = {}

            def profile(executable, library, launch_env=env):
                with patch.dict(os.environ, {"RUMBLE_DATA_DIR": str(library)}):
                    with patch("subprocess.Popen", side_effect=OSError("Observe the environment")) as start:
                        with self.assertRaisesRegex(OSError, "Observe the environment"):
                            run_window(None, executable, None, root, launch_env)
                        return start.call_args.kwargs["env"]["WEBVIEW2_USER_DATA_FOLDER"]

            self.assertEqual(profile(host, first), str(first / ".webview2"))
            self.assertEqual(profile(root / "versions/0.0.10/jaro.exe", first), str(first / ".webview2"))
            self.assertEqual(profile(host, second), str(second / ".webview2"))
            self.assertEqual(profile(host, first, {"WEBVIEW2_USER_DATA_FOLDER": str(root / "custom")}), str(root / "custom"))
            self.assertEqual(env, {})

    def test_only_published_matching_artifacts_are_selected(self):
        assets = [
            {"name": name, "size": 200, "state": "uploaded", "digest": "sha256:" + "a" * 64}
            for name in ("Jaro-arm64.zip", "Jaro-Setup-0.0.10-windows-x64-test.exe", "jaro_0.0.10-2_amd64.deb",
                         "jaro_0.0.10-12_amd64.deb", "jaro_0.0.10-21_arm64.deb",
                         "jaro_0.0.10-2_ubuntu24.04_amd64.deb", "jaro_0.0.10-3_ubuntu24.04_amd64.deb",
                         "jaro_0.0.10-21_ubuntu24.10_amd64.deb", "Jaro-Setup-0.0.10-windows-x64.exe")
        ]
        data = {"tag_name": "v0.0.10", "assets": assets}
        _, debian = release_for(data, "debian")
        _, ubuntu = release_for(data, "ubuntu")
        _, windows = release_for(data, "windows")
        self.assertEqual(debian.name, "jaro_0.0.10-12_amd64.deb")
        self.assertEqual(ubuntu.name, "jaro_0.0.10-3_ubuntu24.04_amd64.deb")
        self.assertEqual(release_for({**data, "assets": assets[2:5]}, "ubuntu"), ("0.0.10", None))
        self.assertEqual(release_for({**data, "assets": assets[5:8]}, "debian"), ("0.0.10", None))
        self.assertEqual(windows.url, "https://github.com/AdamGera/Jaro/releases/download/v0.0.10/Jaro-Setup-0.0.10-windows-x64.exe")
        self.assertEqual(release_for({**data, "assets": assets[:2]}, "windows"), ("0.0.10", None))
        with self.assertRaises(ValueError):
            release_for({**data, "prerelease": True}, "windows")

        archive = {"name": linux_direct.ARCHIVE, "size": 200, "state": "uploaded"}
        manifest = {**assets[0], "name": linux_direct.MANIFEST}
        self.assertEqual(release_for({**data, "assets": [*assets, archive]}, linux_direct.KIND), ("0.0.10", None))
        linux_offer = release_for({**data, "assets": [*assets, manifest, archive]}, linux_direct.KIND)[1]
        self.assertEqual(linux_offer.name, linux_direct.ARCHIVE)
        self.assertEqual(linux_offer.digest, "")

    @unittest.skipUnless(sys.platform == "linux" and Path("/usr/bin/openssl").is_file(), "Linux update runtime")
    async def test_signed_linux_update_stages_applies_and_recovers(self):
        with TemporaryDirectory() as temporary:
            work = Path(temporary)
            key = work / "key.pem"
            subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "Ed25519", "-out", key], check=True)
            public = subprocess.check_output(["/usr/bin/openssl", "pkey", "-in", key, "-pubout", "-outform", "DER"])
            publisher = base64.b64encode(public[-32:]).decode("ascii")

            def signed_proof(version, size, digest):
                message = work / "message"
                message.write_bytes(linux_direct.signed_message(version, size, digest))
                signature = subprocess.check_output(["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin",
                                                     "-inkey", key, "-in", message])
                return json.dumps({"version": version, "size": size, "sha256": digest,
                                   "signature": base64.b64encode(signature).decode("ascii")}).encode()

            def bundle(version, variant=""):
                package = work / (version + variant)
                payload = package / "payload"
                launcher = payload / "bin/jaro"
                launcher.parent.mkdir(parents=True)
                launcher.write_text("#!/bin/sh\nexit 0\n")
                launcher.chmod(0o755)
                workspace = payload / "desktop/portable"
                workspace.mkdir(parents=True)
                (workspace / "open_workspace.py").touch()
                manager = payload / "engine/linux_direct.py"
                manager.parent.mkdir()
                manager.write_text(f"# manager for {version}{variant}\n")
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
                manifest = package / linux_direct.MANIFEST
                manifest.write_bytes(signed_proof(version, size, digest))
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

            cases = [("0.0.10", "success"), ("0.0.11", "tampered"), ("0.0.12", "failed launch"),
                     ("0.0.13", "could not start")]
            for version, outcome in cases:
                archive, manifest, offer = bundle(version)
                received = []
                proof = manifest.read_bytes()

                async def transfer(_, url):
                    name = url.rsplit("/", 1)[-1]
                    received.append(name)
                    yield proof if name == manifest.name else archive.read_bytes()

                with TemporaryDirectory(prefix="jaro-update-") as directory:
                    staging = Path(directory).resolve()
                    updater = PortableUpdater(linux_direct.active_version(root).name, linux_direct.KIND,
                                              staging=staging, publisher=publisher, install_root=root)
                    without_digest = replace(offer, digest="")
                    with (patch("portable_update.latest_release", return_value=(version, without_digest)) as feed,
                          patch("portable_update.installer_bytes", new=transfer)):
                        if outcome == "success":
                            feed.return_value = version, replace(offer, digest="a" * 64)
                            self.assertEqual((await updater.check())["phase"], "error")
                            self.assertEqual(received, [manifest.name])
                            feed.return_value = version, without_digest
                            invalid = json.loads(proof)
                            invalid["sha256"] = "b" * 64
                            proof = json.dumps(invalid).encode()
                            self.assertEqual((await updater.check())["phase"], "error")
                            self.assertEqual(received, [manifest.name, manifest.name])
                            proof = manifest.read_bytes()
                        self.assertEqual((await updater.check())["phase"], "ready")
                        self.assertEqual(updater.offer.digest, offer.digest)
                        if outcome == "success":
                            # The optional discovery checksum can appear or disappear.
                            # A signed replacement for the same version still changes the offer.
                            feed.return_value = version, offer
                            await updater.confirm_restart()
                            feed.return_value = version, without_digest
                            self.assertEqual((await updater.check())["phase"], "ready")
                            self.assertEqual(received.count(archive.name), 1)
                            original_proof = proof
                            proof = signed_proof(version, offer.size, "b" * 64)
                            await updater.confirm_restart()
                            self.assertEqual(updater.offer, offer)
                            self.assertEqual((staging / archive.name).read_bytes(), archive.read_bytes())
                            self.assertEqual((staging / manifest.name).read_bytes(), original_proof)
                            self.assertFalse((staging / "restart.json").exists())
                            self.assertEqual(received.count(archive.name), 2)
                            proof = original_proof
                            self.assertEqual((await updater.check())["phase"], "ready")
                            self.assertEqual(received.count(archive.name), 2)
                            replacement_archive, replacement_manifest, replacement = bundle(version, "-republished")
                            original_archive = archive
                            archive, proof = replacement_archive, replacement_manifest.read_bytes()
                            feed.return_value = version, replace(replacement, digest="")
                            original_replace = Path.replace

                            def refuse_manifest(path, target):
                                if path.name == linux_direct.MANIFEST:
                                    raise OSError("fixture could not replace manifest")
                                return original_replace(path, target)

                            with patch.object(Path, "replace", refuse_manifest):
                                await updater.confirm_restart()
                            self.assertEqual(updater.offer, offer)
                            self.assertEqual((staging / archive.name).read_bytes(), original_archive.read_bytes())
                            self.assertEqual((staging / manifest.name).read_bytes(), original_proof)
                            self.assertEqual(set(entry.name for entry in staging.iterdir()), {archive.name, manifest.name})
                            await updater.confirm_restart()
                            self.assertEqual(updater.offer, replacement)
                            self.assertEqual((staging / archive.name).read_bytes(), replacement_archive.read_bytes())
                            self.assertEqual((staging / manifest.name).read_bytes(), proof)
                            for file in (staging / manifest.name, staging / archive.name):
                                contents = file.read_bytes()
                                file.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
                                with self.assertRaisesRegex(ValueError, "The prepared update changed"):
                                    await updater.confirm_restart()
                                self.assertEqual(updater.status()["message"], CHANGED_STAGE_ERROR)
                                self.assertFalse(updater.status()["staged"])
                                self.assertFalse(updater.status()["restartInProgress"])
                                self.assertFalse((staging / "restart.json").exists())
                                self.assertFalse(any(staging.iterdir()))
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
                            if outcome == "could not start" and "JARO_UPDATE_ERROR" not in kwargs["env"]:
                                raise OSError("fixture launch failed")
                            return process
                        return real_popen(args, *positional, **kwargs)

                    with patch("linux_direct.subprocess.Popen", side_effect=start) as launch:
                        linux_direct.apply_update(root, staging)
                    self.assertFalse(staging.exists())
                    self.assertEqual(linux_direct.active_version(root).name, "0.0.10")
                    self.assertEqual((root / "manager.py").read_text(), "# manager for 0.0.10-republished\n")
                    self.assertFalse(retired.exists())
                    self.assertTrue(unrelated.is_symlink())
                    if outcome == "success":
                        self.assertTrue((root / "versions/0.0.9").is_dir())
                    else:
                        self.assertFalse((root / "versions" / version).exists())
                    self.assertTrue((library / "session.txt").is_file())
                    launches = [call for call in launch.call_args_list if call.args[0][0] == root / "jaro"]
                    rejected = outcome in ("failed launch", "could not start")
                    self.assertEqual(len(launches), 2 if rejected else 1)
                    self.assertEqual(launches[-1].kwargs["env"].get("JARO_UPDATE_ERROR"),
                                     "rejected" if rejected else "1" if outcome == "tampered" else None)
                    self.assertEqual(linux_direct.rejected_update(root), offer.digest if rejected else "")
                    if rejected:
                        with TemporaryDirectory(prefix="jaro-update-") as retry:
                            recovered = PortableUpdater("0.0.10", linux_direct.KIND, staging=Path(retry),
                                                        publisher=publisher, install_root=root, previous_error="rejected")
                            received.clear()
                            with (patch("portable_update.latest_release", return_value=(version, without_digest)) as feed,
                                  patch("portable_update.installer_bytes", new=transfer)):
                                status = await recovered.check(manual=False)
                                self.assertEqual((status["phase"], status["message"]), ("error", REJECTED_ERROR))
                                self.assertTrue(status["manualCheck"])
                                self.assertEqual(received, [manifest.name])
                                self.assertFalse(recovered.has_staged_files())
                                archive, manifest, replacement = bundle(version, "-replacement")
                                proof = manifest.read_bytes()
                                feed.return_value = version, replace(replacement, digest="")
                                self.assertEqual((await recovered.check(manual=False))["phase"], "ready")
                                self.assertEqual(received, [manifest.name, manifest.name, archive.name])
                                await recovered.confirm_restart()

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
                    with self.assertRaises((OSError, ValueError)):
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

    async def test_update_resumes_an_interrupted_download_in_a_new_updater(self):
        prefix = b"a" * (256 * 1024)
        installer = prefix + b"installer tail"
        digest = hashlib.sha256(installer).hexdigest()
        offer = Release("0.0.10", "test.exe", 0, len(installer), digest)
        requests = []

        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield prefix
                raise httpx.ReadError("connection interrupted")

        def response(request):
            requests.append((request.url.host, request.headers.get("Range")))
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            if request.url.host == "github.com":
                return httpx.Response(302, headers={"Location": "https://release-assets.githubusercontent.com/test"})
            if request.headers.get("Range") is None:
                return httpx.Response(200, stream=Interrupted())
            self.assertEqual(request.headers["Range"], f"bytes={len(prefix)}-")
            return httpx.Response(206, content=installer[len(prefix):],
                                  headers={"Content-Range": f"bytes {len(prefix)}-{len(installer) - 1}/{len(installer)}"})

        client = httpx.AsyncClient
        with (TemporaryDirectory() as directory,
              patch("portable_update.latest_release", return_value=(offer.version, offer)),
              patch("portable_update.windows_launcher", return_value=Path("launcher.exe")),
              patch("portable_update.verified_signer", return_value="publisher"),
              patch("portable_update.httpx.AsyncClient", side_effect=lambda **_: client(transport=httpx.MockTransport(response)))):
            root = Path(directory)
            cache = root / "cache"
            partial = cache / (digest + ".partial")
            for index, phase in enumerate(("error", "ready")):
                with TemporaryDirectory(dir=root) as staging:
                    stage = Path(staging)
                    updater = PortableUpdater("0.0.9", "windows", staging=stage, download_cache=cache)
                    status = await updater.check()
                    self.assertEqual(status["phase"], phase)
                    if index == 0:
                        self.assertIn("resume", status["message"])
                        self.assertEqual(partial.read_bytes(), prefix)
                        self.assertFalse((stage / "installer.exe").exists())
                    else:
                        self.assertEqual((stage / "installer.exe").read_bytes(), installer)
                        self.assertFalse(partial.exists())
        self.assertEqual(requests, [(host, offset) for offset in (None, f"bytes={len(prefix)}-")
                                    for host in ("github.com", "release-assets.githubusercontent.com")])

    async def test_update_refuses_wrong_range_or_checksum_and_retries_ignored_range(self):
        installer = b"fixture installer"
        digest = hashlib.sha256(installer).hexdigest()
        offer = Release("0.0.10", "test.exe", 0, len(installer), digest)
        client = httpx.AsyncClient
        for scenario in ("wrong range", "wrong checksum", "ignored range"):
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "cache"
                cache.mkdir(mode=0o700)
                partial = cache / (digest + ".partial")
                partial.write_bytes(b"x" if scenario == "wrong checksum" else installer[:1])
                obsolete = cache / ("0" * 64 + ".partial")
                obsolete.touch()
                ranges = []

                def response(request):
                    offset = request.headers.get("Range")
                    ranges.append(offset)
                    if offset is None or scenario == "ignored range":
                        return httpx.Response(200, content=installer)
                    start = 0 if scenario == "wrong range" else 1
                    return httpx.Response(206, content=installer[start:],
                                          headers={"Content-Range": f"bytes {start}-{len(installer) - 1}/{len(installer)}"})

                with patch("portable_update.httpx.AsyncClient", side_effect=lambda **_: client(transport=httpx.MockTransport(response))):
                    if scenario == "ignored range":
                        await stage_installer(offer, root, cache=cache)
                        self.assertEqual((root / "installer.exe").read_bytes(), installer)
                        self.assertEqual(ranges, ["bytes=1-", None])
                    else:
                        with self.assertRaises(ValueError):
                            await stage_installer(offer, root, cache=cache)
                        self.assertFalse((root / "installer.exe").exists())
                        self.assertEqual(ranges, ["bytes=1-"])
                self.assertEqual(list(cache.iterdir()), [])

    async def test_check_reports_platform_specific_current_and_manual_installs(self):
        for kind, suffix in (("debian", ""), ("ubuntu", "_ubuntu24.04")):
            with self.subTest(kind=kind):
                offer = Release("0.0.9", f"jaro_0.0.9-22{suffix}_amd64.deb", 22, 25, "")
                updater = PortableUpdater("0.0.9", kind, 21)
                with patch("portable_update.latest_release", return_value=("0.0.9", offer)) as feed:
                    status = await updater.check()
                    self.assertEqual(status["phase"], "download")
                    self.assertIn(kind.title(), status["message"])
                    feed.assert_awaited_once_with(kind)
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
        offer = windows_fixture_offer()
        with (TemporaryDirectory() as directory, patch("server.URL", origin),
              patch("portable_update.updates_disabled", return_value=False),
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.stage_installer", side_effect=write_installer_fixture) as transfer,
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
                    status = (await browser.get(endpoint, params={"client": client})).json()
                    self.assertTrue(status["restartBlocked"])
                    self.assertIn("Close other", status["restartBlockReason"])
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                state.portable_recording = object()
                with state.leases.browser_connection(client):
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                    state.portable_recording = None

                    newer = windows_fixture_offer("0.0.11", b"b" * 64)

                    async def capture_begins(release, candidate):
                        during = (await browser.get(endpoint, params={"client": client})).json()
                        self.assertEqual((during["phase"], during["version"]), ("downloading", offer.version))
                        self.assertTrue(during["staged"])
                        self.assertTrue(during["restartInProgress"])
                        state.portable_recording = object()
                        await write_installer_fixture(release, candidate)

                    feed.return_value = newer.version, newer
                    transfer.side_effect = capture_begins
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 409)
                    self.assertFalse((Path(directory) / "restart.json").exists())
                    self.assertEqual(state.portable_updater.status()["version"], newer.version)
                    state.portable_recording = None
                    feed.side_effect = httpx.ConnectError("offline")
                    # A same-size change cannot be detected by a presence or size check.
                    (Path(directory) / "installer.exe").write_bytes(b"x" * newer.size)
                    refused = await browser.post(restart, headers=headers)
                    self.assertEqual((refused.status_code, refused.json()["detail"]), (409, CHANGED_STAGE_ERROR))
                    state.request_shutdown.assert_not_called()
                    self.assertFalse(state.draining)
                    self.assertFalse((Path(directory) / "restart.json").exists())
                    self.assertFalse(state.portable_updater.status()["staged"])
                    feed.side_effect = None
                    transfer.side_effect = write_installer_fixture
                    self.assertEqual((await browser.post(check, headers=headers)).json()["phase"], "ready")
                    feed.side_effect = httpx.ConnectError("offline")
                    self.assertEqual((await browser.post(restart, headers=headers)).status_code, 202)
            self.assertTrue(state.draining)
            state.request_shutdown.assert_called_once()
            self.assertEqual(json.loads((Path(directory) / "restart.json").read_text())["digest"], newer.digest)

    async def test_ready_update_reports_when_the_restart_guard_changes(self):
        updater = PortableUpdater("0.0.9", "windows")
        updater.publish(phase="ready", staged=True)
        state = SimpleNamespace(
            portable_updater=updater, portable_recording=None, portable_screencast=None,
            jobs={object()}, submitting=False, model_setup=None, draining=False, status="ready",
            leases=ClientLeases(),
            insights=SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0)),
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state), headers={})
        with patch("portable_update.updates_disabled", return_value=False):
            stream = (await connect_browser(uuid4(), request)).body_iterator
            try:
                self.assertIn("Wait for the local engine", await anext(stream))
                state.jobs.clear()
                state.model_setup = {"phase": "downloading"}
                self.assertIn("Finish the current recording", await asyncio.wait_for(anext(stream), 2))
                state.model_setup = None
                ready = await asyncio.wait_for(anext(stream), 2)
                self.assertIn('"staged": true', ready)
                self.assertNotIn("restartBlocked", ready)
            finally:
                await stream.aclose()

    async def test_only_the_desktop_can_consume_the_current_update_settings_request(self):
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/updates"
        app.add_api_route(endpoint, portable_update_status, methods=["GET"])
        app.add_api_route(endpoint + "/settings-opened", portable_update_settings_opened, methods=["POST"])
        configure_workspace(app, record, origin)
        updater = app.state.portable_updater = PortableUpdater("0.0.9", "windows")
        older, latest = "a" * 24, "b" * 24
        updater.publish(openUpdateSettings=latest)

        with patch("server.URL", origin), patch("portable_update.updates_disabled", return_value=False):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                opened = endpoint + "/settings-opened"
                headers = {"Origin": origin}
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertNotIn("openUpdateSettings", (await browser.get(endpoint)).json())
                response = await browser.post(opened, params={"token": latest}, headers=headers)
                self.assertEqual(response.status_code, 403)

                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                response = await browser.post(opened, params={"token": older}, headers=headers)
                self.assertEqual(response.status_code, 204)
                self.assertEqual((await browser.get(endpoint)).json()["openUpdateSettings"], latest)
                response = await browser.post(opened, params={"token": latest})
                self.assertEqual(response.status_code, 403)
                changed = updater.changed
                response = await browser.post(opened, params={"token": latest}, headers=headers)
                self.assertEqual(response.status_code, 204)
                self.assertTrue(changed.is_set())
                self.assertNotIn("openUpdateSettings", (await browser.get(endpoint)).json())

    async def test_only_the_owned_desktop_webview_sets_a_tray_eligible_claim(self):
        from server import checkpoint_workspace_drafts
        from session_store import SessionStore

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        app.state.leases = ClientLeases()
        app.state.draining = False
        app.state.portable_workspace = True
        app.state.portable_recorder = None
        app.state.portable_dictation = None
        app.add_api_route("/", workspace_page)
        app.add_api_route("/api/portable/workspace-drafts", checkpoint_workspace_drafts, methods=["PUT"])

        @app.get("/fixture/claim/{key}")
        async def claim(key, request: Request):
            stream = (await connect_browser(key, request)).body_iterator
            try:
                await anext(stream)
                return {"owned": len(app.state.leases.desktop_streams), "total": len(app.state.leases.streams)}
            finally:
                await stream.aclose()

        with TemporaryDirectory() as tray, patch("server.URL", origin), patch("server.DIST", Path(tray)):
            root = Path(tray)
            app.state.library = SessionStore(root)
            source = root / "fixture.wav"
            source.write_bytes(b"fixture")
            session, _ = app.state.library.create(source, "Fixture", "audio/wav",
                                                  {"recording": False, "diarization": False}, 1)
            app.state.library.finish(session)
            (root / "index.html").write_text("<html lang='en'><head></head><body>Fixture workspace</body></html>")
            with patch.dict(os.environ, {linux_direct.STARTUP_ENV: tray, "JARO_DESKTOP_TRAY": tray}):
                configure_workspace(app, record, origin)
            receipt = Path(tray) / linux_direct.WORKSPACE_CONNECTED
            for credential, owned in (("workspaceDesktopKey", 1), ("workspaceKey", 0)):
                receipt.unlink(missing_ok=True)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                    headers = {"Origin": origin, "X-Jaro-Workspace-Key": record[credential], "X-Jaro-Desktop": "true"}
                    response = await browser.post(BOOTSTRAP, headers=headers)
                    self.assertEqual(response.status_code, 204)
                    self.assertIn("HttpOnly", response.headers["set-cookie"])
                    key = "jard.personalNotes." + session
                    text = "</script><div>Fixture text</div>"
                    draft = {"revision": 0, "document": {"type": "doc", "content": [{"type": "text", "text": text}]}}
                    values = {key: draft, "jard.recordingMode": "system", "jard.sidebar": "collapsed"}
                    if owned:
                        invalid = await browser.put("/api/portable/workspace-drafts", headers={"Origin": origin},
                                                    json={"values": {"jard.recordingMode": "invalid"}})
                        self.assertEqual(invalid.status_code, 422)
                    checkpoint = await browser.put("/api/portable/workspace-drafts", headers={"Origin": origin},
                                                   json={"values": values})
                    self.assertEqual(checkpoint.status_code, 204 if owned else 403)
                    page = (await browser.get("/")).content
                    self.assertEqual(b"data-jaro-desktop-client" in page, bool(owned))
                    self.assertEqual(b"jaro-workspace-drafts" in page, bool(owned))
                    self.assertEqual(b"Fixture text" in page, bool(owned))
                    self.assertEqual(b'"jard.recordingMode":"system"' in page, bool(owned))
                    self.assertEqual(b'"jard.sidebar":"collapsed"' in page, bool(owned))
                    self.assertNotIn(text.encode(), page)
                    self.assertEqual((await browser.get("/fixture/claim/" + str(uuid4()))).json(),
                                     {"owned": owned, "total": 1})
                    self.assertEqual(receipt.is_file(), bool(owned))
                    self.assertFalse(app.state.leases.desktop_streams)

            app.state.library.delete(session)
            with app.state.library.connection() as db:
                self.assertIsNone(db.execute("SELECT key FROM workspace_drafts").fetchone())
                self.assertEqual(dict(db.execute("SELECT key,value FROM workspace_preferences")), {
                    "jard.recordingMode": "system", "jard.sidebar": "collapsed",
                })

    async def test_tray_quit_releases_the_idle_workspace_stream_without_a_timeout(self):
        for updater in (None, PortableUpdater("0.0.9", "debian")):
            with self.subTest(updater=bool(updater)):
                state = SimpleNamespace(draining=False, leases=ClientLeases(), request_shutdown=Mock(),
                                        portable_updater=updater)
                request = SimpleNamespace(app=SimpleNamespace(state=state), headers={})
                stream = (await connect_browser(uuid4(), request)).body_iterator
                await anext(stream)
                waiting = asyncio.create_task(anext(stream))
                await asyncio.sleep(0)
                tray = MeetingTray(state, None, lambda: False, None, None)
                tray.submit("quit")
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(waiting, 1)
                self.assertFalse(state.leases.streams)
                state.request_shutdown.assert_called_once()

    async def test_tray_defers_to_its_desktop_workspace_and_rechecks_other_clients(self):
        state = SimpleNamespace(
            portable_recording=None, portable_screencast=None, portable_dictation=None,
            submitting=False, jobs=set(), model_setup=None, draining=False, status="ready",
            leases=ClientLeases(), request_shutdown=Mock(),
            insights=SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0)),
        )
        key = str(uuid4())
        origin = "http://127.0.0.1:43722"
        record = {"port": 43722}
        app = FastAPI()
        app.state = state
        configure_workspace(app, record, origin)
        endpoint = "/api/portable/updates"
        app.add_api_route(endpoint, portable_update_status, methods=["GET"])
        app.add_api_route(endpoint + "/claim-tray-restart", portable_claim_tray_restart, methods=["POST"])
        app.add_api_route(endpoint + "/{action}", portable_update_action, methods=["POST"])
        transport = httpx.ASGITransport(app=app)
        offer = windows_fixture_offer()
        with (TemporaryDirectory() as directory, ExitStack() as late_claim, patch("portable_update.updates_disabled", return_value=False),
              patch("server.URL", origin),
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.stage_installer", side_effect=write_installer_fixture),
              patch("portable_update.verified_signer", return_value=b"publisher"),
              patch("portable_update.latest_release", return_value=(offer.version, offer)) as feed):
            folder = Path(directory)
            updater = state.portable_updater = PortableUpdater("0.0.9", "windows", staging=folder)

            async def restart(current):
                await restart_from_portable_tray(state, current, (folder / "window-pid").is_file)

            tray = MeetingTray(state, folder, lambda: bool(state.portable_recording), lambda: None,
                               lambda _: "", restart=restart)
            with patch("portable_update.updates_disabled", return_value=True):
                self.assertEqual(tray.update_view(), "managed")
                tray.submit("check")
                self.assertIsNone(tray.update_job)
            self.assertEqual(tray.update_view(), "check")
            tray.submit("check")
            await tray.update_job
            self.assertEqual(tray.update_view(), "restart")
            self.assertTrue(updater.status()["openUpdateSettings"])
            (folder / "open").unlink()
            state.portable_recording = object()
            self.assertEqual(tray.update_view(), "wait")
            previous_job = tray.update_job
            tray.submit("restart")
            self.assertIs(tray.update_job, previous_job)
            state.portable_recording = None

            with state.leases.browser_connection(key):
                tray.submit("restart")
                await tray.update_job
                self.assertIn("Close other", updater.status()["restartError"])
                self.assertTrue((folder / "open").is_file())
                state.request_shutdown.assert_not_called()
                self.assertFalse((folder / "restart.json").exists())

            async def extra_connects(_):
                late_claim.enter_context(state.leases.browser_connection(key))
                return offer.version, offer

            async with (httpx.AsyncClient(transport=transport, base_url=origin, headers={"Origin": origin}) as owner,
                        httpx.AsyncClient(transport=transport, base_url=origin, headers={"Origin": origin}) as browser):
                for client, credential in ((owner, "workspaceDesktopKey"), (browser, "workspaceKey")):
                    self.assertEqual((await client.post(BOOTSTRAP, headers={"X-Jaro-Workspace-Key": record[credential]})).status_code, 204)

                async def choose_tray_restart():
                    tray.submit("restart")
                    await asyncio.sleep(0)
                    token = (await owner.get(endpoint, params={"client": key})).json()["trayRestart"]
                    self.assertEqual(tray.update_view(), "restarting")
                    self.assertNotIn("trayRestart", (await browser.get(endpoint)).json())
                    claim = endpoint + "/claim-tray-restart"
                    self.assertEqual((await browser.post(claim, params={"token": token})).status_code, 403)
                    self.assertEqual((await owner.post(claim, params={"token": token})).status_code, 204)
                    self.assertEqual((await owner.post(claim, params={"token": token})).status_code, 409)
                    await tray.update_job
                    state.request_shutdown.assert_not_called()
                    self.assertFalse((folder / "restart.json").exists())

                (folder / "window-pid").touch()
                await choose_tray_restart()
                (folder / "window-pid").unlink()

                async def open_during_check(_):
                    (folder / "window-pid").touch()
                    return offer.version, offer

                feed.side_effect = open_during_check
                tray.submit("restart")
                await tray.update_job
                self.assertIn("save any open drafts", updater.status()["restartError"])
                self.assertFalse((folder / "restart.json").exists())
                self.assertFalse(state.draining)
                (folder / "window-pid").unlink()
                feed.side_effect = None
                with state.leases.browser_connection(key, desktop=True):
                    with state.leases.browser_connection(key, desktop=True):
                        tray.submit("restart")
                        await tray.update_job
                        self.assertIn("Close other", updater.status()["restartError"])
                    await choose_tray_restart()
                    feed.side_effect = extra_connects
                    refused = await owner.post(endpoint + "/restart", params={"client": key})
                    self.assertEqual(refused.status_code, 409)
                    self.assertIn("Close other", refused.json()["detail"])
                    state.request_shutdown.assert_not_called()
                    self.assertFalse((folder / "restart.json").exists())
                    late_claim.close()
                    feed.side_effect = None
                    await choose_tray_restart()
                    self.assertEqual((await owner.post(endpoint + "/restart", params={"client": key})).status_code, 202)
            self.assertTrue(state.draining)
            state.request_shutdown.assert_called_once()
            self.assertEqual(json.loads((folder / "restart.json").read_text())["digest"], offer.digest)

    async def test_verified_windows_stage_survives_feed_failure_and_is_replaced_at_restart(self):
        offer = windows_fixture_offer()
        newer = windows_fixture_offer("0.0.11", b"b" * 64)
        with (TemporaryDirectory() as directory, patch("portable_update.stage_installer", side_effect=write_installer_fixture) as transfer,
              patch("portable_update.verified_signer", return_value=b"publisher") as signer,
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.latest_release", return_value=(offer.version, offer)) as feed):
            folder = Path(directory)
            installer = folder / "installer.exe"
            updater = PortableUpdater("0.0.9", "windows", staging=folder)
            self.assertEqual((await updater.check(manual=False))["phase"], "ready")
            self.assertEqual((await updater.check(manual=False))["phase"], "ready")
            transfer.assert_awaited_once()

            for error in (httpx.ConnectError("offline"), TimeoutError()):
                feed.side_effect = error
                await updater.confirm_restart()
            self.assertEqual(updater.status()["phase"], "ready")
            feed.side_effect = None
            for version in ("0.0.9", newer.version):
                feed.return_value = version, None
                self.assertEqual((await updater.check())["version"], offer.version)
                await updater.confirm_restart()
                self.assertEqual(installer.read_bytes(), b"a" * 64)
            transfer.assert_awaited_once()

            feed.return_value = newer.version, newer
            signer.side_effect = [b"publisher", b"different publisher"]
            status = await updater.check()
            self.assertEqual((status["phase"], status["version"], status["staged"]), ("ready", offer.version, True))
            self.assertEqual(installer.read_bytes(), b"a" * 64)
            self.assertEqual(list(folder.iterdir()), [installer])

            signer.side_effect = None
            await updater.confirm_restart()
            self.assertEqual(updater.status()["version"], newer.version)
            self.assertEqual(installer.read_bytes(), b"b" * 64)
            self.assertEqual(list(folder.iterdir()), [installer])
            updater.prepare_restart()
            self.assertEqual(json.loads((folder / "restart.json").read_text()),
                             {"version": newer.version, "digest": newer.digest})

    async def test_restart_takes_priority_over_a_staged_background_refresh(self):
        offer = windows_fixture_offer()
        newer = windows_fixture_offer("0.0.11", b"b" * 64)
        refresh_started, release_refresh = asyncio.Event(), asyncio.Event()
        confirm_started, release_confirm = asyncio.Event(), asyncio.Event()
        calls = 0

        async def releases(_):
            nonlocal calls
            calls += 1
            if calls == 2:
                refresh_started.set()
                await release_refresh.wait()
                return newer.version, newer
            if calls == 3:
                confirm_started.set()
                await release_confirm.wait()
            return offer.version, offer

        with (TemporaryDirectory() as folder,
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.verified_signer", return_value=b"publisher"),
              patch("portable_update.stage_installer", side_effect=write_installer_fixture) as transfer,
              patch("portable_update.latest_release", new=releases)):
            staging = Path(folder)
            updater = PortableUpdater("0.0.9", "windows", staging=staging)
            self.assertEqual((await updater.check())["phase"], "ready")
            refresh = asyncio.create_task(updater.check(manual=False))
            restart = None
            try:
                await asyncio.wait_for(refresh_started.wait(), 1)
                self.assertEqual(updater.status()["phase"], "checking")
                self.assertTrue(updater.status()["staged"])
                change = updater.changed
                restart = asyncio.create_task(updater.confirm_restart())
                await asyncio.wait_for(change.wait(), 1)
                self.assertTrue(updater.status()["restartInProgress"])
                with self.assertRaisesRegex(ValueError, "already preparing"):
                    await updater.confirm_restart()
                release_refresh.set()
                await asyncio.wait_for(confirm_started.wait(), 1)
                self.assertEqual(updater.status()["phase"], "ready")
                self.assertEqual(updater.status()["version"], offer.version)
                self.assertTrue(updater.status()["staged"])
                self.assertEqual((await updater.check())["phase"], "ready")
                transfer.assert_awaited_once()
                self.assertEqual(transfer.await_args.args[0], offer)
                release_confirm.set()
                await asyncio.wait_for(restart, 1)
                self.assertFalse(updater.status()["restartInProgress"])
                updater.prepare_restart()
                self.assertEqual(json.loads((staging / "restart.json").read_text())["digest"], offer.digest)
            finally:
                for task in (refresh, restart):
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_restart_detects_an_installer_removed_during_confirmation(self):
        offer = windows_fixture_offer()
        with (TemporaryDirectory() as folder,
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.verified_signer", return_value=b"publisher"),
              patch("portable_update.stage_installer", side_effect=write_installer_fixture),
              patch("portable_update.latest_release", return_value=(offer.version, offer)) as feed):
            staging = Path(folder)
            updater = PortableUpdater("0.0.9", "windows", staging=staging)
            await updater.check()

            async def remove_installer(_):
                (staging / "installer.exe").unlink()
                return offer.version, offer

            feed.side_effect = remove_installer
            with self.assertRaisesRegex(ValueError, "installer is missing"):
                await updater.confirm_restart()
            status = updater.status()
            self.assertEqual(status["phase"], "idle")
            self.assertFalse(status["restartInProgress"])
            self.assertFalse(status["staged"])
            with self.assertRaises(ValueError):
                updater.prepare_restart()
            self.assertFalse((staging / "restart.json").exists())

    async def test_windows_skips_rolled_back_installer_until_checksum_changes(self):
        offer = windows_fixture_offer()
        replacement = windows_fixture_offer(offer.version, b"b" * 64)
        with (TemporaryDirectory() as folder,
              patch("portable_update.windows_launcher", return_value=Path("launcher")),
              patch("portable_update.verified_signer", return_value=b"publisher"),
              patch("portable_update.stage_installer", side_effect=write_installer_fixture) as transfer,
              patch("portable_update.latest_release", return_value=(offer.version, offer)) as feed):
            root = Path(folder)
            staging = root / "stage"
            staging.mkdir()
            linux_direct.reject_update(root, offer.digest)
            updater = PortableUpdater("0.0.9", "windows", staging=staging, install_root=root, previous_error="rejected")
            feed.side_effect = OSError("offline")
            self.assertEqual((await updater.check(manual=False))["message"], REJECTED_ERROR)
            feed.side_effect = None
            for manual in (False, True):
                status = await updater.check(manual=manual)
                self.assertEqual((status["phase"], status["message"]), ("error", REJECTED_ERROR))
                self.assertTrue(status["manualCheck"])
                self.assertFalse(status["staged"])
            transfer.assert_not_awaited()

            feed.return_value = replacement.version, replacement
            self.assertEqual((await updater.check(manual=False))["phase"], "ready")
            transfer.assert_awaited_once()
            self.assertEqual(transfer.await_args.args[0], replacement)
            await updater.confirm_restart()
            linux_direct.reject_update(root, replacement.digest)
            with self.assertRaisesRegex(ValueError, "failed to start"):
                updater.prepare_restart()
            self.assertFalse(updater.has_staged_files())
            self.assertFalse((staging / "restart.json").exists())

    def test_windows_worker_discards_staging_if_the_installation_folder_is_unavailable(self):
        with TemporaryDirectory(prefix="jaro-update-") as staging:
            directory = Path(staging)
            (directory / "installer.exe").touch()
            with (patch("portable_update.windows_launcher", side_effect=OSError),
                  patch("portable_update.subprocess.Popen") as launch):
                apply_windows(directory, 100)
            self.assertFalse(directory.exists())
            launch.assert_not_called()

    def test_supported_update_waits_for_the_workspace_startup_receipt(self):
        with TemporaryDirectory() as directory:
            payload = Path(directory)
            (payload / linux_direct.STARTUP_CAPABILITY).touch()
            receipt = payload / linux_direct.STARTUP_RECEIPT
            environment = dict(os.environ)
            real_popen = subprocess.Popen

            def start(_args, *, env, start_new_session):
                self.assertEqual(env[linux_direct.STARTUP_ENV], directory)
                process = real_popen([sys.executable, "-c", command], env=env, start_new_session=start_new_session)
                self.addCleanup(process.wait, 3)
                return process

            child = Mock(poll=Mock(return_value=None))
            with (patch("linux_direct.subprocess.Popen", return_value=child),
                  patch("linux_direct.sys.platform", "win32"),
                  patch("linux_direct.UPDATE_STARTUP_SECONDS", 0)):
                receipt.touch()
                self.assertIsNone(linux_direct.start_updated_workspace("launcher", payload, payload, environment, legacy_timeout=1))
                self.assertFalse(receipt.exists())
                child.kill.assert_not_called()
                (payload / linux_direct.WINDOWS_OWNED_STARTUP).touch()
                self.assertFalse(linux_direct.start_updated_workspace("launcher", payload, payload, environment, legacy_timeout=1))
                child.kill.assert_called_once_with()
                child.wait.assert_called_once_with(timeout=linux_direct.PROBE_STOP_SECONDS)

            with patch("linux_direct.subprocess.Popen", side_effect=start):
                command = "raise SystemExit(0)"
                self.assertFalse(linux_direct.start_updated_workspace("launcher", payload, payload, environment, legacy_timeout=1))
                command = f"from pathlib import Path; import time; time.sleep(.1); Path({str(receipt)!r}).touch()"
                self.assertTrue(linux_direct.start_updated_workspace("launcher", payload, payload, environment, legacy_timeout=1))

    @unittest.skipIf(sys.platform == "win32", "POSIX startup process group")
    def test_startup_timeout_stops_only_the_owned_probe_group(self):
        with TemporaryDirectory() as directory:
            payload = Path(directory)
            (payload / linux_direct.STARTUP_CAPABILITY).touch()
            launcher = payload / "launcher"
            launcher.write_text("#!/bin/sh\ntrap '' TERM\nprintf ready\nwhile :; do sleep 1; done\n")
            launcher.chmod(0o700)
            real_popen = subprocess.Popen

            def start(*args, **kwargs):
                process = real_popen(*args, **kwargs, stdout=subprocess.PIPE)
                self.addCleanup(process.stdout.close)
                self.addCleanup(process.wait, timeout=3)
                self.addCleanup(process.kill)
                self.assertEqual(process.stdout.read(5), b"ready")
                return process

            with (patch("linux_direct.subprocess.Popen", side_effect=start) as launch,
                  patch("linux_direct.UPDATE_STARTUP_SECONDS", .05),
                  patch("linux_direct.PROBE_STOP_SECONDS", .05),
                  patch("linux_direct.os.killpg", wraps=os.killpg) as stop):
                self.assertFalse(linux_direct.start_updated_workspace(launcher, payload, payload, os.environ, legacy_timeout=1))
                self.assertTrue(launch.call_args.kwargs["start_new_session"])
            self.assertEqual([call.args[1] for call in stop.call_args_list], [linux_direct.signal.SIGTERM, linux_direct.signal.SIGKILL])

    @unittest.skipIf(sys.platform == "win32", "Linux installation locks and version pointer")
    def test_normal_launcher_recovers_an_abandoned_linux_startup_trial(self):
        for outcome in ("never switched", "unconfirmed", "missing payload", "confirmed"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                profile = Path(directory).resolve()
                root = profile / "install"
                root.mkdir(mode=0o700)
                versions = root / "versions"
                versions.mkdir(mode=0o700)
                previous, candidate = versions / "0.0.9", versions / "0.0.10"
                for payload in (previous, candidate):
                    payload.mkdir(mode=0o700)
                    (payload / "engine").mkdir()
                    (payload / "engine/linux_direct.py").write_text(payload.name)
                    (payload / "portable-release.json").touch()
                library = profile / "library.sqlite3"
                library.write_bytes(b"existing library")
                staging = profile / "jaro-update-test"
                staging.mkdir(mode=0o700)
                digest = "a" * 64
                (staging / "restart.json").write_text(json.dumps({"version": candidate.name, "digest": digest}))
                (root / "manager.py").write_text(previous.name)
                linux_direct.point_to(root, previous)
                linux_direct.record_startup_trial(root, previous, {"version": candidate.name, "sha256": digest}, staging)
                if outcome != "never switched":
                    linux_direct.point_to(root, candidate)
                if outcome == "missing payload":
                    shutil.rmtree(candidate)

                with patch.dict(os.environ, clear=True), patch("linux_direct.os.execv") as execute:
                    with linux_direct.update_lock(root):
                        with self.assertRaisesRegex(linux_direct.UpdateInProgress, "finishing an update"):
                            linux_direct.launch(root, "jaro", [])
                        execute.assert_not_called()
                        if outcome == "unconfirmed":
                            with patch.dict(os.environ, {linux_direct.STARTUP_ENV: str(staging)}):
                                linux_direct.launch(root, "jaro", [])
                            self.assertEqual(execute.call_args.args[0], str(candidate / "venv/bin/python"))
                            execute.reset_mock()
                    confirmed = outcome == "confirmed"
                    if confirmed:
                        (staging / linux_direct.STARTUP_RECEIPT).write_bytes(b"ready\n")
                    with linux_direct.installation_lock(root) if confirmed else nullcontext():
                        linux_direct.launch(root, "jaro", [])
                    selected = candidate if confirmed else previous
                    self.assertEqual(execute.call_args.args[0], str(selected / "venv/bin/python"))
                    rolled_back = outcome in ("unconfirmed", "missing payload")
                    self.assertEqual(os.environ.get("JARO_UPDATE_ERROR"), "rejected" if rolled_back else None)
                self.assertEqual(linux_direct.active_version(root), selected)
                self.assertFalse((root / linux_direct.STARTUP_TRIAL).exists())
                self.assertFalse(staging.exists())
                self.assertEqual((root / "manager.py").read_text(), selected.name)
                self.assertEqual(linux_direct.rejected_update(root), digest if rolled_back else "")
                self.assertEqual(library.read_bytes(), b"existing library")

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
                          patch("portable_update.windows_installation_lock", side_effect=nullcontext),
                          patch("portable_update.subprocess.run", side_effect=install),
                          patch("portable_update.subprocess.Popen") as launch):
                        launch.return_value.wait.return_value = 0
                        apply_windows(directory, 100)
                    wait.assert_called_once_with(100)
                    self.assertFalse(directory.exists())
                    args, fields = launch.call_args
                    self.assertEqual(args, ([launcher],))
                    self.assertEqual("JARO_UPDATE_ERROR" in fields["env"], bool(exit_status))
                    self.assertEqual("JARO_UPDATE_PROBE" in fields["env"], not exit_status)
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
            digest = hashlib.sha256(contents).hexdigest()

            def stage(directory):
                (directory / "installer.exe").write_bytes(contents)
                (directory / "restart.json").write_text(json.dumps({"version": "0.0.10", "digest": digest}))

            with TemporaryDirectory(prefix="jaro-update-") as staging:
                directory = Path(staging)
                stage(directory)

                def install(_args, **_):
                    pointer.write_text("0.0.10", encoding="ascii")
                    return SimpleNamespace(returncode=0)

                first, second, third = Mock(), Mock(), Mock()
                first.wait.return_value = 1
                with (patch("portable_update.windows_launcher", return_value=launcher),
                      patch("portable_update.signed_by", return_value=b"CN=Jaro"),
                      patch("portable_update.updates_disabled", return_value=False),
                      patch("portable_update.wait_for_launcher", return_value=True),
                      patch("portable_update.windows_installation_lock", side_effect=nullcontext) as lock,
                      patch("portable_update.subprocess.run", side_effect=install) as setup,
                      patch("portable_update.subprocess.Popen", side_effect=[first, second, third]) as launch):
                    apply_windows(directory, 100)
                    directory.mkdir()
                    stage(directory)
                    apply_windows(directory, 100)
                self.assertEqual(pointer.read_text(encoding="ascii").strip(), "0.0.9")
                self.assertFalse((install_root / "versions/0.0.10").exists())
                self.assertTrue((install_root / "versions/0.0.9").is_dir())
                self.assertEqual(launch.call_count, 3)
                setup.assert_called_once()
                lock.assert_called_once()
                self.assertEqual(launch.call_args_list[0].kwargs["env"].get("JARO_UPDATE_PROBE"), "1")
                self.assertEqual(launch.call_args_list[1].kwargs["env"].get("JARO_UPDATE_ERROR"), "rejected")
                self.assertNotIn("JARO_UPDATE_PROBE", launch.call_args_list[-1].kwargs["env"])
                self.assertEqual(launch.call_args_list[-1].kwargs["env"].get("JARO_UPDATE_ERROR"), "rejected")
                self.assertEqual(linux_direct.rejected_update(install_root), digest)

    async def test_dismissed_screen_picker_releases_the_restart_guard_after_cleanup(self):
        task = asyncio.get_running_loop().create_future()
        screencast = SimpleNamespace(phase="choosing", saving=False, task=task)
        updater = SimpleNamespace(status=lambda: {"staged": True}, confirm_restart=AsyncMock(),
                                  prepare_restart=Mock(), changed=asyncio.Event())
        state = SimpleNamespace(
            status="ready", draining=False, submitting=set(), jobs=set(), leases=ClientLeases(),
            portable_recording=None, portable_screencast=screencast, model_setup=None,
            insights=SimpleNamespace(active_job=False, chat_count=0, model=SimpleNamespace(audio_users=0)),
            request_shutdown=Mock(), portable_updater=updater,
        )

        def restart_blocked():
            return portable_update_view(state, updater, None).get("restartBlocked", False)

        self.assertTrue(restart_blocked())
        screencast.phase = "cancelled"
        self.assertTrue(restart_blocked())
        task.set_result(None)
        for phase in ("recording", "recorded", "error"):
            with self.subTest(phase=phase):
                screencast.phase = phase
                self.assertTrue(restart_blocked())
        screencast.phase = "cancelled"
        screencast.saving = True
        self.assertTrue(restart_blocked())
        screencast.saving = False
        self.assertFalse(restart_blocked())

        await restart_from_portable_tray(state, updater, window_open=lambda: False)
        updater.confirm_restart.assert_awaited_once()
        updater.prepare_restart.assert_called_once()
        state.request_shutdown.assert_called_once()
        self.assertIs(state.portable_screencast, screencast)

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
