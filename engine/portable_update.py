"""Release checks and the installed desktop update handoff."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import webbrowser

import httpx
import linux_direct


RELEASES = "https://github.com/AdamGera/Jaro/releases"
FEED = "https://api.github.com/repos/AdamGera/Jaro/releases/latest"
VERSION = linux_direct.VERSION
MAX_INSTALLER = linux_direct.MAX_ARCHIVE
INSTALL_ERROR = "Jaro could not finish the update. Check for new versions to try again."
STARTUP_DELAY = 10
CHECK_INTERVAL = 60 * 60
RELEASE_FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, httpx.HTTPError, subprocess.SubprocessError)


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    revision: int
    size: int
    digest: str

    @property
    def url(self):
        return f"{RELEASES}/download/v{self.version}/{self.name}"


def version_key(version):
    return tuple(map(int, version.split(".")))


def release_for(data, kind):
    tag = data.get("tag_name")
    if data.get("draft") or data.get("prerelease") or not isinstance(tag, str) or not re.fullmatch("v" + VERSION, tag):
        raise ValueError("Invalid stable release.")
    version = tag[1:]
    assets = data["assets"]
    if kind == linux_direct.KIND and not any(
        item.get("name") == linux_direct.MANIFEST and item.get("state") == "uploaded" for item in assets
    ):
        return version, None
    result = []
    for item in assets:
        name = item.get("name")
        if not isinstance(name, str) or item.get("state") != "uploaded":
            continue
        if kind == "windows":
            if name != f"Jaro-Setup-{version}-windows-x64.exe":
                continue
            revision = 0
        elif kind == linux_direct.KIND:
            if name != linux_direct.ARCHIVE:
                continue
            revision = 0
        else:
            matched = re.fullmatch(rf"jaro_{re.escape(version)}-([1-9][0-9]{{0,5}})_amd64\.deb", name)
            if not matched:
                continue
            revision = int(matched[1])
        size, digest = item.get("size"), item.get("digest")
        if type(size) is not int or not 0 < size <= MAX_INSTALLER:
            continue
        digest = digest[7:] if isinstance(digest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", digest) else ""
        result.append(Release(version, name, revision, size, digest))
    return version, max(result, key=lambda release: release.revision, default=None)


async def latest_release(kind):
    content = bytearray()
    async with asyncio.timeout(15), httpx.AsyncClient(timeout=10, trust_env=False) as client:
        async with client.stream("GET", FEED, headers={"Accept": "application/vnd.github+json",
                                                     "User-Agent": "Jaro", "Cache-Control": "no-cache"}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(65_536):
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("Release information is too large.")
    return release_for(json.loads(content), kind)


async def installer_bytes(client, url):
    for _ in range(4):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = str(response.next_request.url)
                destination = urlsplit(url)
                if (destination.scheme != "https" or destination.netloc != "release-assets.githubusercontent.com"
                        or destination.username or destination.password):
                    raise ValueError("Unexpected installer download location.")
                continue
            response.raise_for_status()
            async for chunk in response.aiter_bytes(256 * 1024):
                yield chunk
            return
    raise ValueError("Too many installer redirects.")


async def stage_installer(release, directory, filename="installer.exe"):
    partial = directory / "installer.partial"
    digest = hashlib.sha256()
    size = 0
    try:
        async with asyncio.timeout(600), httpx.AsyncClient(timeout=15, trust_env=False) as client:
            with partial.open("xb") as output:
                async for chunk in installer_bytes(client, release.url):
                    size += len(chunk)
                    if size > release.size:
                        raise ValueError("The installer transfer exceeded its limits.")
                    digest.update(chunk)
                    output.write(chunk)
        if size != release.size or digest.hexdigest() != release.digest:
            raise ValueError("The installer does not match the published download.")
        partial.replace(directory / filename)
    finally:
        partial.unlink(missing_ok=True)


def windows_launcher():
    return Path(os.environ["LOCALAPPDATA"]) / "Programs/Jaro/bin/Jaro.exe"


def signature_command(path):
    # Read the certificate only after Windows has verified the Authenticode signature.
    system = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = dict(os.environ, JARO_SIGNATURE_TARGET=str(path))
    environment["PSModulePath"] = str(system.parent / "Modules")
    return [system, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
        "$s = Microsoft.PowerShell.Security\\Get-AuthenticodeSignature -LiteralPath $env:JARO_SIGNATURE_TARGET; "
        "if ($s.Status -ne 'Valid' -or -not $s.SignerCertificate) { exit 1 }; "
        "[Convert]::ToBase64String($s.SignerCertificate.RawData)"], environment


def signed_by(path):
    command, environment = signature_command(path)
    output = subprocess.run(command,
        env=environment, capture_output=True, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=30, check=False)
    return output.stdout.strip() if output.returncode == 0 else b""


async def verified_signer(path):
    command, environment = signature_command(path)
    process = await asyncio.create_subprocess_exec(
        *command, env=environment, stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        async with asyncio.timeout(30):
            output, _ = await process.communicate()
        return output.strip() if process.returncode == 0 else b""
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


def updates_disabled(kind):
    if kind != "windows" or sys.platform != "win32":
        return False
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        with suppress(OSError):
            with winreg.OpenKey(hive, r"SOFTWARE\Policies\Jard\Jaro") as key:
                value, value_type = winreg.QueryValueEx(key, "DisableUpdates")
                if value_type == winreg.REG_DWORD and value:
                    return True
    return False


class PortableUpdater:
    def __init__(self, version, kind, revision=0, staging=None, previous_error="", publisher=None):
        self.version = version
        self.kind = kind
        self.revision = revision
        self.staging = staging
        self.publisher = publisher
        self.offer = None
        self.ready = False
        self.operation = asyncio.Lock()
        self.changed = asyncio.Event()
        self.state = {"phase": "idle", "currentVersion": f"{version}-{revision}" if revision else version}
        if previous_error:
            self.state.update(phase="error", message=INSTALL_ERROR, manualCheck=True)

    def status(self):
        if updates_disabled(self.kind):
            return {**self.state, "phase": "unavailable", "updatesDisabled": True}
        return self.state.copy()

    def publish(self, **changes):
        self.state.update(changes)
        self.changed.set()
        self.changed = asyncio.Event()

    async def watch(self):
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            if not updates_disabled(self.kind):
                await self.check(manual=False)
            await asyncio.sleep(CHECK_INTERVAL)

    def clear_installer(self):
        self.ready = False
        if self.staging:
            for name in self.staged_files:
                with suppress(OSError):
                    (self.staging / name).unlink(missing_ok=True)

    @property
    def staged_files(self):
        if self.kind == linux_direct.KIND:
            return (linux_direct.ARCHIVE, linux_direct.MANIFEST)
        return ("installer.exe",)

    def has_staged_files(self):
        return bool(self.staging and all((self.staging / name).is_file() for name in self.staged_files))

    async def check(self, *, manual=True):
        if self.state["phase"] == "applying" or updates_disabled(self.kind):
            return self.status()
        if self.operation.locked():
            if manual:
                self.publish(manualCheck=True)
            return self.status()
        # Leave a previous install failure visible until the user checks again.
        if not manual and self.state.get("message") == INSTALL_ERROR:
            return self.status()
        async with self.operation:
            self.publish(phase="checking", manualCheck=manual, message="", staged=False)
            try:
                version, offer = await latest_release(self.kind)
            except RELEASE_FAILURES:
                if self.ready:
                    self.publish(phase="ready", staged=True, message="")
                else:
                    self.publish(phase="error", message="Could not check for updates.")
                return self.status()
            await self.use_release(version, offer)
        return self.status()

    async def use_release(self, version, offer):
        if self.ready and self.offer == offer and self.has_staged_files():
            self.publish(phase="ready", staged=True, message="")
            return
        self.clear_installer()
        self.offer = None
        current = version_key(self.version), self.revision
        if (version_key(version), offer.revision if offer else 0) <= current:
            self.publish(phase="current", version=None, staged=False, message="Jaro is up to date.")
        elif offer is None:
            message = "The latest Jaro release has no download for this platform."
            self.publish(phase="idle", version=None, staged=False, message=message if self.state["manualCheck"] else "")
        else:
            self.offer = offer
            message = ("Install the downloaded Debian package, then reopen Jaro." if self.kind == "debian" else
                       "Download the Windows installer to update Jaro.")
            if self.kind == "windows" and self.staging and offer.digest:
                await self.stage_windows(offer, message)
            elif self.kind == linux_direct.KIND:
                if self.staging and self.publisher and offer.digest:
                    await self.stage_linux(offer)
                else:
                    self.publish(phase="download", version=version, staged=False,
                                 message="Download the signed Linux install files from the release.")
            else:
                self.publish(phase="download", version=version, staged=False, message=message)

    async def stage_windows(self, release, fallback):
        try:
            signer = await verified_signer(windows_launcher())
            if not signer:
                self.publish(phase="download", version=release.version, staged=False, message=fallback)
                return
            self.publish(phase="downloading", version=release.version, staged=False, message="")
            await stage_installer(release, self.staging)
            self.publish(phase="staging")
            verified = await verified_signer(self.staging / "installer.exe")
            if signer != verified:
                raise ValueError("The installer is signed by a different certificate.")
        except RELEASE_FAILURES:
            self.clear_installer()
            self.publish(phase="download", version=release.version, staged=False,
                         message="Jaro could not verify the installer for automatic update.")
            return
        self.ready = True
        self.publish(phase="ready", staged=True)

    async def stage_linux(self, release):
        self.publish(phase="downloading", version=release.version, staged=False, message="")
        content = bytearray()
        url = f"{RELEASES}/download/v{release.version}/{linux_direct.MANIFEST}"
        try:
            async with asyncio.timeout(15), httpx.AsyncClient(timeout=10, trust_env=False) as client:
                async for chunk in installer_bytes(client, url):
                    content.extend(chunk)
                    if len(content) > linux_direct.MAX_MANIFEST:
                        raise ValueError("The update manifest is too large.")
            proof = await asyncio.to_thread(linux_direct.verify_manifest, content, self.publisher)
            if (proof["version"], proof["size"], proof["sha256"]) != (release.version, release.size, release.digest):
                raise ValueError("The signed manifest does not match the release download.")
            await stage_installer(release, self.staging, linux_direct.ARCHIVE)
            (self.staging / linux_direct.MANIFEST).write_bytes(content)
        except RELEASE_FAILURES:
            self.clear_installer()
            self.publish(phase="download", version=release.version, staged=False,
                         message="Jaro could not verify and prepare the Linux update.")
            return
        self.ready = True
        self.publish(phase="ready", staged=True)

    async def download(self):
        if self.state["phase"] != "download" or self.offer is None:
            raise ValueError("Check for a newer Jaro release first.")
        url = f"{RELEASES}/tag/v{self.offer.version}" if self.kind == linux_direct.KIND else self.offer.url
        if not await asyncio.to_thread(webbrowser.open, url):
            raise OSError("No browser could open the download.")
        return self.status()

    def prepare_restart(self):
        if not self.ready or not self.staging or self.offer is None:
            raise ValueError("No verified update is ready to install.")
        intent = self.staging / "restart.json"
        partial = intent.with_suffix(".partial")
        try:
            with partial.open("x", encoding="ascii") as file:
                json.dump({"version": self.offer.version, "digest": self.offer.digest}, file)
            partial.replace(intent)
        finally:
            partial.unlink(missing_ok=True)
        self.publish(phase="applying", restartInProgress=True)

    async def confirm_restart(self):
        if self.operation.locked():
            raise ValueError("Jaro is still checking for updates.")
        if not self.ready or self.offer is None or self.state["phase"] != "ready":
            raise ValueError("No verified update is ready to install.")
        if not self.has_staged_files():
            self.clear_installer()
            self.offer = None
            self.publish(phase="idle", version=None, staged=False, message="The prepared installer is missing. Check for new versions again.")
            raise ValueError(self.state["message"])
        async with self.operation:
            try:
                _, offer = await latest_release(self.kind)
            except RELEASE_FAILURES as error:
                raise ValueError("Could not confirm the latest release. Try restarting again.") from error
            if offer != self.offer:
                self.clear_installer()
                self.offer = None
                self.publish(phase="idle", version=None, staged=False,
                             message="The available update changed. Check for new versions again.")
                raise ValueError(self.state["message"])


def load_updater():
    metadata = os.environ.get("JARO_PORTABLE_RELEASE")
    if not metadata:
        return None
    data = json.loads(metadata)
    path = os.environ.get("JARO_UPDATE_STAGING")
    publisher = None
    if path and data["kind"] == linux_direct.KIND:
        root = linux_direct.managed_root(Path(__file__).resolve().parents[1])
        if root is not None:
            publisher = linux_direct.read_publisher(root)
    return PortableUpdater(data["version"], data["kind"], data.get("revision", 0),
                           Path(path) if path else None, os.environ.get("JARO_UPDATE_ERROR", ""), publisher)


def wait_for_launcher(pid):
    import ctypes
    from ctypes import wintypes

    windows = ctypes.WinDLL("kernel32", use_last_error=True)
    windows.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    windows.OpenProcess.restype = wintypes.HANDLE
    windows.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    windows.WaitForSingleObject.restype = wintypes.DWORD
    windows.CloseHandle.argtypes = (wintypes.HANDLE,)
    process = windows.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
    if not process:
        return ctypes.get_last_error() == 87  # The launcher already exited.
    try:
        return windows.WaitForSingleObject(process, 30_000) == 0
    finally:
        windows.CloseHandle(process)


def apply_windows(directory, pid):
    temporary = Path(tempfile.gettempdir()).resolve()
    if directory.parent.resolve() != temporary or not directory.name.startswith("jaro-update-") or directory.is_symlink():
        return
    launcher = windows_launcher()
    installer = directory / "installer.exe"
    environment = dict(os.environ)
    environment.pop("JARO_UPDATE_ERROR", None)
    try:
        intent = json.loads((directory / "restart.json").read_text(encoding="ascii"))
        version, digest = intent["version"], intent["digest"]
        if not re.fullmatch(VERSION, version) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid update handoff.")
        pointer = launcher.parent.parent / "current-version"
        installed = pointer.read_text(encoding="ascii").strip()
        if (not re.fullmatch(VERSION, installed) or version_key(version) <= version_key(installed)
                or updates_disabled("windows")):
            raise ValueError("This update is no longer applicable.")
        with installer.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                raise ValueError("The prepared installer changed.")
        signed = signed_by(launcher)
        if not signed or signed != signed_by(installer) or not wait_for_launcher(pid):
            raise ValueError("Could not verify the installer or close Jaro.")
        result = subprocess.run([installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
                                 f"/DIR={launcher.parent.parent}", f"/JAROUPDATE={installed}"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if result.returncode or pointer.read_text(encoding="ascii").strip() != version:
            raise ValueError("The installer did not activate the expected version.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        environment["JARO_UPDATE_ERROR"] = "1"
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    subprocess.Popen([launcher], env=environment)


if __name__ == "__main__" and sys.platform == "win32" and len(sys.argv) == 3:
    apply_windows(Path(sys.argv[1]), int(sys.argv[2]))
