"""Release checks and the installed desktop update handoff."""

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import webbrowser

import httpx
import linux_direct
from engine_paths import library_path


RELEASES = "https://github.com/AdamGera/Jaro/releases"
FEED = "https://api.github.com/repos/AdamGera/Jaro/releases/latest"
VERSION = linux_direct.VERSION
MAX_INSTALLER = linux_direct.MAX_ARCHIVE
INSTALL_ERROR = "Jaro could not finish the update. Check for new versions to try again."
REJECTED_ERROR = "This update failed to start. Jaro will check again for a different build."
CHANGED_STAGE_ERROR = "The prepared update changed. Check for new versions to download it again."
STARTUP_DELAY = 10
CHECK_INTERVAL = 60 * 60
# The engine has up to 60 seconds to complete its startup handshake.
WINDOWS_STARTUP_WAIT = 75
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
        elif kind in ("debian", "ubuntu"):
            target_suffix = r"_ubuntu24\.04" if kind == "ubuntu" else ""
            matched = re.fullmatch(rf"jaro_{re.escape(version)}-([1-9][0-9]{{0,5}}){target_suffix}_amd64\.deb", name)
            if not matched:
                continue
            revision = int(matched[1])
        else:
            continue
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


class RestartTransfer(Exception):
    """The server could not resume the requested range."""


class IncompleteTransfer(OSError):
    """The response ended before the advertised download size."""


async def installer_bytes(client, url, *, offset=0, size=None):
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    for _ in range(4):
        async with client.stream("GET", url, headers=headers) as response:
            if response.is_redirect:
                url = str(response.next_request.url)
                destination = urlsplit(url)
                if (destination.scheme != "https" or destination.netloc != "release-assets.githubusercontent.com"
                        or destination.username or destination.password):
                    raise ValueError("Unexpected installer download location.")
                continue
            if offset and response.status_code in (200, 416):
                raise RestartTransfer
            response.raise_for_status()
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("Unexpected installer encoding.")
            if offset:
                if (response.status_code != 206
                        or response.headers.get("Content-Range") != f"bytes {offset}-{size - 1}/{size}"):
                    raise ValueError("The installer download did not resume at the requested offset.")
            elif response.status_code != 200:
                raise ValueError("Unexpected installer response.")
            async for chunk in response.aiter_bytes(256 * 1024):
                yield chunk
            return
    raise ValueError("Too many installer redirects.")


async def signed_linux_release(release, publisher):
    url = f"{RELEASES}/download/v{release.version}/{linux_direct.MANIFEST}"
    content = bytearray()
    async with asyncio.timeout(15), httpx.AsyncClient(timeout=10, trust_env=False) as client:
        async for chunk in installer_bytes(client, url):
            content.extend(chunk)
            if len(content) > linux_direct.MAX_MANIFEST:
                raise ValueError("The update manifest is too large.")
        proof = await asyncio.to_thread(linux_direct.verify_manifest, content, publisher)
    if (proof["version"] != release.version or proof["size"] != release.size
            or (release.digest and proof["sha256"] != release.digest)):
        raise ValueError("The signed manifest does not match the release download.")
    return replace(release, digest=proof["sha256"]), bytes(content)


def download_partial(cache, digest):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("The update does not have a valid checksum.")
    cache.mkdir(mode=0o700, exist_ok=True)
    if os.name == "nt":
        if cache.is_symlink() or cache.is_junction() or not cache.is_dir():
            raise ValueError("Invalid Jaro update cache directory.")
    else:
        linux_direct.require_private_directory(cache)
    partial = cache / (digest + ".partial")
    for entry in cache.iterdir():
        if entry != partial and re.fullmatch(r"[a-f0-9]{64}\.partial", entry.name):
            if not entry.is_dir() or entry.is_symlink():
                entry.unlink(missing_ok=True)
    return partial


def update_checksum(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


async def receive_installer(client, release, partial, offset):
    options = {"offset": offset, "size": release.size} if offset else {}
    with partial.open("ab" if offset else "xb") as output:
        async for chunk in installer_bytes(client, release.url, **options):
            offset += len(chunk)
            if offset > release.size:
                raise ValueError("The installer transfer exceeded its limits.")
            output.write(chunk)
    return offset


async def stage_installer(release, directory, filename="installer.exe", *, cache=None):
    partial = download_partial(cache, release.digest) if cache else directory / "installer.partial"
    offset = 0
    if cache:
        with suppress(FileNotFoundError):
            info = partial.lstat()
            if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= release.size:
                offset = info.st_size
    if not offset:
        partial.unlink(missing_ok=True)
    try:
        async with asyncio.timeout(600), httpx.AsyncClient(timeout=15, trust_env=False) as client:
            if offset < release.size:
                try:
                    offset = await receive_installer(client, release, partial, offset)
                except RestartTransfer:
                    partial.unlink()
                    offset = await receive_installer(client, release, partial, 0)
        if offset != release.size:
            raise IncompleteTransfer("The installer download ended before all bytes arrived.")
        if await asyncio.to_thread(update_checksum, partial) != release.digest:
            raise ValueError("The installer does not match the published download.")
        promote = asyncio.create_task(asyncio.to_thread(shutil.move, partial, directory / filename))
        try:
            await asyncio.shield(promote)
        except asyncio.CancelledError:
            # A cross-volume copy may still be running. Keep its staging directory until it finishes.
            with suppress(OSError):
                await promote
            raise
    except ValueError:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if not cache:
            partial.unlink(missing_ok=True)


def windows_local_app_data():
    import ctypes
    from ctypes import wintypes

    # Use the same per-user folder as Setup and the native launcher.
    folder = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    get_folder = ctypes.WinDLL("shell32").SHGetFolderPathW
    get_folder.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                           wintypes.DWORD, wintypes.LPWSTR)
    get_folder.restype = ctypes.c_long
    if get_folder(None, 0x001C, None, 0, folder) < 0 or not folder.value:  # CSIDL_LOCAL_APPDATA
        raise OSError("Could not locate the Windows Local AppData folder.")
    return Path(folder.value)


def windows_launcher():
    return windows_local_app_data() / "Programs/Jaro/bin/Jaro.exe"


def signature_command(path):
    # Pin the verified Authenticode publisher subject (rather than raw leaf cert bytes)
    # so certificate renewal with the same publisher keeps in-app updates working.
    system = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = dict(os.environ, JARO_SIGNATURE_TARGET=str(path))
    environment["PSModulePath"] = str(system.parent / "Modules")
    return [system, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
        "$s = Microsoft.PowerShell.Security\\Get-AuthenticodeSignature -LiteralPath $env:JARO_SIGNATURE_TARGET; "
        "if ($s.Status -ne 'Valid' -or -not $s.SignerCertificate -or -not $s.SignerCertificate.Subject) { exit 1 }; "
        "[Console]::Out.Write($s.SignerCertificate.Subject)"], environment


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
    def __init__(self, version, kind, revision=0, staging=None, previous_error="", publisher=None, install_root=None,
                 *, download_cache=None):
        self.version = version
        self.kind = kind
        self.revision = revision
        self.staging = staging
        self.publisher = publisher
        self.install_root = install_root
        self.download_cache = download_cache
        self.offer = None
        self.ready = False
        self.operation = asyncio.Lock()
        self.changed = asyncio.Event()
        self.state = {"phase": "idle", "currentVersion": f"{version}-{revision}" if revision else version}
        if previous_error:
            message = REJECTED_ERROR if previous_error == "rejected" else INSTALL_ERROR
            self.state.update(phase="error", message=message, manualCheck=True)

    def status(self):
        if updates_disabled(self.kind):
            return {**self.state, "phase": "unavailable", "updatesDisabled": True}
        return self.state.copy()

    def publish(self, **changes):
        self.state.update(changes)
        self.changed.set()
        self.changed = asyncio.Event()

    def acknowledge_settings(self, token):
        if self.state.get("openUpdateSettings") == token:
            del self.state["openUpdateSettings"]
            self.publish()

    async def watch(self):
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            if not updates_disabled(self.kind):
                await self.check(manual=False)
            await asyncio.sleep(CHECK_INTERVAL)

    def clear_installer(self):
        self.ready = False
        if self.staging:
            for name in (*self.staged_files, "installer.partial"):
                with suppress(OSError):
                    (self.staging / name).unlink(missing_ok=True)

    @property
    def staged_files(self):
        if self.kind == linux_direct.KIND:
            return (linux_direct.ARCHIVE, linux_direct.MANIFEST)
        return ("installer.exe",)

    def has_staged_files(self):
        return bool(self.staging and all((self.staging / name).is_file() for name in self.staged_files))

    def has_verified_stage(self):
        return self.ready and self.has_staged_files() and not self.was_rejected(self.offer)

    async def checked_release(self):
        version, offer = await latest_release(self.kind)
        manifest = None
        if (self.kind == linux_direct.KIND and self.staging and self.publisher and offer
                and version_key(version) > version_key(self.version)):
            offer, manifest = await signed_linux_release(offer, self.publisher)
        return version, offer, manifest

    async def check(self, *, manual=True):
        if self.state.get("restartInProgress") or updates_disabled(self.kind):
            return self.status()
        if self.operation.locked():
            if manual:
                self.publish(manualCheck=True, restartError="")
            return self.status()
        # Leave a previous install failure visible until the user checks again.
        previous_message = self.state.get("message")
        if not manual and previous_message == INSTALL_ERROR:
            return self.status()
        async with self.operation:
            staged = self.has_verified_stage()
            self.publish(phase="checking", manualCheck=manual, message="", staged=staged,
                         restartError="" if manual else self.state.get("restartError", ""))
            try:
                version, offer, manifest = await self.checked_release()
                if self.state.get("restartInProgress"):
                    self.publish(phase="ready", staged=True, message="")
                else:
                    await self.use_release(version, offer, manifest)
            except asyncio.CancelledError:
                staged = self.has_verified_stage()
                self.publish(phase="ready" if staged else "idle", staged=staged)
                raise
            except RELEASE_FAILURES:
                if self.has_verified_stage():
                    self.publish(phase="ready", staged=True, message="")
                else:
                    if not manual and previous_message == REJECTED_ERROR:
                        self.publish(phase="error", staged=False, message=REJECTED_ERROR, manualCheck=True)
                    else:
                        self.publish(phase="error", staged=False, message="Could not check for updates.")
                return self.status()
        return self.status()

    async def use_release(self, version, offer, manifest):
        current = version_key(self.version), self.revision
        eligible = (version_key(version), offer.revision if offer else 0) > current
        rejected = eligible and offer is not None and self.was_rejected(offer)
        staged = self.has_verified_stage()
        automatic = (eligible and offer is not None and self.staging is not None
                     and (manifest is not None if self.kind == linux_direct.KIND else
                          self.kind == "windows" and bool(offer.digest)))
        if staged and (not automatic or rejected or offer == self.offer):
            self.publish(phase="ready", staged=True, version=self.offer.version, message="")
            return
        if not staged:
            self.clear_installer()
            self.offer = None
            self.publish(staged=False)
        if rejected:
            self.discard_rejected_offer()
        elif not eligible:
            self.publish(phase="current", version=None, staged=False, message="Jaro is up to date.")
        elif offer is None:
            message = "The latest Jaro release has no download for this platform."
            self.publish(phase="idle", version=None, staged=False, message=message if self.state["manualCheck"] else "")
        else:
            await self.prepare_offer(offer, manifest, staged=staged, automatic=automatic)

    async def prepare_offer(self, release, manifest, *, staged, automatic):
        messages = {
            "debian": "Install the downloaded Debian package, then reopen Jaro.",
            "ubuntu": "Install the downloaded Ubuntu package, then reopen Jaro.",
            linux_direct.KIND: "Download the signed Linux install files from the release.",
            "windows": "Download the Windows installer to update Jaro.",
        }
        message = messages[self.kind]
        if automatic:
            try:
                if await self.stage_offer(release, manifest, staged):
                    self.offer = release
                    self.ready = True
                    self.publish(phase="ready", version=release.version, staged=True, message="")
                    return
            except (httpx.TransportError, TimeoutError, IncompleteTransfer):
                if self.download_cache and not staged:
                    self.offer = release
                    self.publish(phase="error", version=release.version, staged=False,
                                 message="Download interrupted. Check for new versions to resume.")
                    return
                message = "Jaro could not download the update."
            except RELEASE_FAILURES:
                message = "Jaro could not verify the download for automatic update."
        if staged:
            self.publish(phase="ready", version=self.offer.version, staged=True, message="")
        else:
            self.offer = release
            self.publish(phase="download", version=release.version, staged=False, message=message)

    async def stage_offer(self, release, manifest, staged):
        if self.kind == "windows":
            signer = await verified_signer(windows_launcher())
            if not signer:
                return False
        self.publish(phase="downloading", version=self.offer.version if staged else release.version,
                     staged=staged, message="")
        with tempfile.TemporaryDirectory(dir=self.staging) as directory:
            candidate = Path(directory)
            download = {"cache": self.download_cache} if self.download_cache else {}
            if self.kind == linux_direct.KIND:
                await stage_installer(release, candidate, linux_direct.ARCHIVE, **download)
                (candidate / linux_direct.MANIFEST).write_bytes(manifest)
                self.promote_linux_stage(candidate, staged)
            else:
                await stage_installer(release, candidate, **download)
                self.publish(phase="staging")
                if signer != await verified_signer(candidate / "installer.exe"):
                    raise ValueError("The installer is signed by a different certificate.")
                (candidate / "installer.exe").replace(self.staging / "installer.exe")
        return True

    def promote_linux_stage(self, candidate, staged):
        # The stable launcher expects the archive and manifest at fixed paths.
        # Keep the original archive until both replacements have succeeded.
        archive = self.staging / linux_direct.ARCHIVE
        previous = candidate / "previous"
        if staged:
            os.link(archive, previous)
        (candidate / linux_direct.ARCHIVE).replace(archive)
        try:
            (candidate / linux_direct.MANIFEST).replace(self.staging / linux_direct.MANIFEST)
        except OSError:
            if staged:
                previous.replace(archive)
            else:
                archive.unlink(missing_ok=True)
            raise

    async def download(self):
        if self.state["phase"] != "download" or self.offer is None:
            raise ValueError("Check for a newer Jaro release first.")
        url = f"{RELEASES}/tag/v{self.offer.version}" if self.kind == linux_direct.KIND else self.offer.url
        if not await asyncio.to_thread(webbrowser.open, url):
            raise OSError("No browser could open the download.")
        return self.status()

    def prepare_restart(self):
        self.require_verified_stage()
        intent = self.staging / "restart.json"
        partial = intent.with_suffix(".partial")
        try:
            with partial.open("x", encoding="ascii") as file:
                json.dump({"version": self.offer.version, "digest": self.offer.digest}, file)
            partial.replace(intent)
        finally:
            partial.unlink(missing_ok=True)
        self.publish(phase="applying", restartInProgress=True)

    def require_verified_stage(self):
        if not self.ready or not self.staging or self.offer is None:
            raise ValueError("No verified update is ready to install.")
        if self.was_rejected(self.offer):
            self.discard_rejected_offer()
            raise ValueError(REJECTED_ERROR)
        if not self.has_staged_files():
            self.clear_installer()
            self.offer = None
            self.publish(phase="idle", version=None, staged=False, message="The prepared installer is missing. Check for new versions again.")
            raise ValueError(self.state["message"])

    def was_rejected(self, offer):
        return bool(offer.digest and offer.digest == linux_direct.rejected_update(self.install_root))

    def discard_rejected_offer(self):
        self.clear_installer()
        self.offer = None
        self.publish(phase="error", version=None, staged=False, message=REJECTED_ERROR, manualCheck=True)

    def verify_staged_payload(self):
        offer = self.offer
        payload = self.staging / self.staged_files[0]
        info = payload.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != offer.size:
            raise ValueError(CHANGED_STAGE_ERROR)
        if self.kind == linux_direct.KIND:
            manifest = self.staging / linux_direct.MANIFEST
            if not stat.S_ISREG(manifest.lstat().st_mode):
                raise ValueError(CHANGED_STAGE_ERROR)
            release = linux_direct.read_manifest(manifest, self.publisher)
            if (release["version"], release["size"], release["sha256"]) != (offer.version, offer.size, offer.digest):
                raise ValueError(CHANGED_STAGE_ERROR)
        if update_checksum(payload) != offer.digest:
            raise ValueError(CHANGED_STAGE_ERROR)

    async def confirm_restart(self):
        if self.state.get("restartInProgress"):
            raise ValueError("Jaro is already preparing to restart.")
        if self.operation.locked() and not self.state.get("staged"):
            raise ValueError("Jaro is still checking for updates.")
        self.require_verified_stage()
        self.publish(restartInProgress=True, restartError="")
        try:
            async with self.operation:
                try:
                    async with asyncio.timeout(15):
                        version, offer, manifest = await self.checked_release()
                except RELEASE_FAILURES:
                    pass
                else:
                    self.require_verified_stage()
                    await self.use_release(version, offer, manifest)
                self.require_verified_stage()
                try:
                    await asyncio.to_thread(self.verify_staged_payload)
                except RELEASE_FAILURES as error:
                    self.clear_installer()
                    self.offer = None
                    self.publish(phase="error", version=None, staged=False, message=CHANGED_STAGE_ERROR, manualCheck=True)
                    raise ValueError(CHANGED_STAGE_ERROR) from error
        finally:
            if self.state["phase"] in ("checking", "downloading", "staging") and self.ready:
                self.publish(phase="ready", version=self.offer.version, staged=True)
            self.publish(restartInProgress=False)


def load_updater():
    metadata = os.environ.get("JARO_PORTABLE_RELEASE")
    if not metadata:
        return None
    data = json.loads(metadata)
    path = os.environ.get("JARO_UPDATE_STAGING")
    publisher = None
    root = None
    if path and data["kind"] == linux_direct.KIND:
        root = linux_direct.managed_root(Path(__file__).resolve().parents[1])
        if root is not None:
            publisher = linux_direct.read_publisher(root)
    elif path and data["kind"] == "windows" and sys.platform == "win32":
        with suppress(OSError):
            root = windows_launcher().parent.parent
    return PortableUpdater(data["version"], data["kind"], data.get("revision", 0),
                           Path(path) if path else None, os.environ.get("JARO_UPDATE_ERROR", ""), publisher, root,
                           download_cache=library_path() / ".jaro-update-cache" if path else None)


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


def write_version_pointer(pointer, version):
    partial = pointer.with_name(pointer.name + ".partial")
    partial.write_text(version + "\n", encoding="ascii")
    partial.replace(pointer)


@contextmanager
def windows_installation_lock(*, exclusive=True):
    import ctypes
    from ctypes import wintypes

    path = windows_local_app_data() / "Jaro/.installer-lock"
    windows = ctypes.WinDLL("kernel32", use_last_error=True)
    windows.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    windows.CreateFileW.restype = wintypes.HANDLE
    windows.CloseHandle.argtypes = (wintypes.HANDLE,)
    # Setup opens the same file exclusively before replacing the version pointer.
    path.parent.mkdir(parents=True, exist_ok=True)
    sharing = 0 if exclusive else 3  # FILE_SHARE_READ | FILE_SHARE_WRITE
    handle = windows.CreateFileW(str(path), 0xC0000000, sharing, None, 4, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        windows.CloseHandle(handle)


def prune_windows_versions(root, *keep):
    versions = root / "versions"
    if not versions.is_dir():
        return
    keep_names = {name for name in keep if name}
    for entry in versions.iterdir():
        if entry.name in keep_names or not re.fullmatch(VERSION, entry.name) or entry.is_symlink():
            continue
        if (entry / "portable-release.json").is_file():
            shutil.rmtree(entry, ignore_errors=True)


def apply_windows(directory, pid):
    temporary = Path(tempfile.gettempdir()).resolve()
    if directory.parent.resolve() != temporary or not directory.name.startswith("jaro-update-") or directory.is_symlink():
        return
    pending = False
    try:
        pending = apply_windows_update(directory, pid)
    finally:
        if not pending:
            shutil.rmtree(directory, ignore_errors=True)


def apply_windows_update(directory, pid):
    installer = directory / "installer.exe"
    environment = dict(os.environ)
    environment.pop("JARO_UPDATE_ERROR", None)
    environment.pop("JARO_UPDATE_PROBE", None)
    environment.pop(linux_direct.STARTUP_ENV, None)
    installed = None
    version = None
    launcher = None
    attempted_install = False
    updated = False
    try:
        launcher = windows_launcher()
        install_root = launcher.parent.parent
        pointer = install_root / "current-version"
        intent = json.loads((directory / "restart.json").read_text(encoding="ascii"))
        version, digest = intent["version"], intent["digest"]
        if not re.fullmatch(VERSION, version) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid update handoff.")
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
        if digest == linux_direct.rejected_update(install_root):
            environment["JARO_UPDATE_ERROR"] = "rejected"
            raise ValueError("This update already failed to start.")
        attempted_install = True
        result = subprocess.run([installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-", "/NORESTART",
                                 f"/DIR={install_root}", f"/JAROUPDATE={installed}"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        if result.returncode or pointer.read_text(encoding="ascii").strip() != version:
            raise ValueError("The installer did not activate the expected version.")
        updated = True
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        environment.setdefault("JARO_UPDATE_ERROR", "1")
    if launcher is None:
        return
    if updated:
        try:
            started = linux_direct.start_updated_workspace(
                launcher, install_root / "versions" / version, directory,
                {**environment, "JARO_UPDATE_PROBE": "1"}, legacy_timeout=WINDOWS_STARTUP_WAIT,
            )
        except OSError:
            started = False
        if started is None:
            return True  # Keep both versions and leave the startup receipt folder available.
        if started:
            prune_windows_versions(install_root, version, installed)
            return
        environment["JARO_UPDATE_ERROR"] = "1"
    if attempted_install:
        with suppress(OSError), windows_installation_lock():
            if pointer.read_text(encoding="ascii").strip() == version:
                write_version_pointer(pointer, installed)
                if updated:
                    with suppress(OSError):
                        linux_direct.reject_update(install_root, digest)
                        environment["JARO_UPDATE_ERROR"] = "rejected"
                prune_windows_versions(install_root, installed)
    subprocess.Popen([launcher], env=environment)


if __name__ == "__main__" and sys.platform == "win32" and len(sys.argv) == 3:
    apply_windows(Path(sys.argv[1]), int(sys.argv[2]))
