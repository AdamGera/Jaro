#!/usr/bin/env python3
"""Install and restart signed per-user Jaro releases on Linux amd64."""

import argparse
import base64
from contextlib import contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


KIND = "linux-direct"
ARCHIVE = "Jaro-linux-x64.tar.gz"
MANIFEST = "Jaro-linux-x64.update.json"
PUBLISHER = "dxMjVxu9nD8oMW/Cx+DNTJ2sYv8JS8z95lJkz63hBMM="
VERSION = r"(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){2}"
MAX_ARCHIVE = 1024 ** 3
MAX_MANIFEST = 4096
APP_ID = "local.jardaudio.desktop"
PUBLIC_KEY_PREFIX = bytes.fromhex("302a300506032b6570032100")
REJECTED_UPDATE = "rejected-update.sha256"
STARTUP_CAPABILITY = "portable-startup-v1"
WINDOWS_OWNED_STARTUP = "windows-owned-startup-v1"
STARTUP_ENV = "JARO_UPDATE_STARTUP"
STARTUP_RECEIPT = "startup.ready"
STARTUP_TRIAL = "startup-trial.json"
WORKSPACE_CONNECTED = "workspace-connected"
WINDOW_STARTUP_SECONDS = 30
# The workspace first waits up to 60 seconds for the engine, then opens its window.
UPDATE_STARTUP_SECONDS = 110
PROBE_STOP_SECONDS = 8


def system_python(payload=None):
    if payload is not None:
        lib = payload / "venv/lib"
        if lib.is_dir():
            versions = sorted(entry.name for entry in lib.iterdir()
                              if entry.is_dir() and re.fullmatch(r"python3\.[0-9]+", entry.name))
            for version in versions:
                candidate = Path("/usr/bin") / version
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
            if versions:
                raise ValueError(f"This Jaro release requires {' or '.join(versions)} in /usr/bin.")
    current = Path(f"/usr/bin/python{sys.version_info[0]}.{sys.version_info[1]}")
    if current.is_file():
        return str(current)
    for candidate in ("/usr/bin/python3.13", "/usr/bin/python3.12", "/usr/bin/python3"):
        if Path(candidate).is_file():
            return candidate
    return sys.executable


def signed_message(version, size, digest):
    return f"jaro.linux-x64.v1\n{version}\n{size}\n{digest}\n".encode("ascii")


def verify_manifest(raw, publisher):
    if len(raw) > MAX_MANIFEST:
        raise ValueError("The update manifest is too large.")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.keys() != {"version", "size", "sha256", "signature"}:
        raise ValueError("Invalid update manifest.")
    version, size, digest = data["version"], data["size"], data["sha256"]
    if (not isinstance(version, str) or not re.fullmatch(VERSION, version)
            or type(size) is not int or not 0 < size <= MAX_ARCHIVE
            or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)
            or not isinstance(data["signature"], str)):
        raise ValueError("Invalid update manifest.")
    public = base64.b64decode(publisher, validate=True)
    signature = base64.b64decode(data["signature"], validate=True)
    if len(public) != 32 or len(signature) != 64:
        raise ValueError("Invalid update signature or publisher key.")
    payload = signed_message(version, size, digest)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        openssl = "/usr/bin/openssl" if Path("/usr/bin/openssl").is_file() else shutil.which("openssl")
        if not openssl:
            raise ValueError("Install openssl or python3-cryptography to verify Linux updates.")
        with tempfile.TemporaryDirectory(prefix="jaro-signature-") as temporary:
            work = Path(temporary)
            key, message, sig = (work / name for name in ("key", "message", "signature"))
            key.write_bytes(PUBLIC_KEY_PREFIX + public)
            message.write_bytes(payload)
            sig.write_bytes(signature)
            result = subprocess.run([openssl, "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
                                     "-inkey", key, "-rawin", "-in", message, "-sigfile", sig],
                                    capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("The update signature does not match the installed publisher key.")
    else:
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
        except InvalidSignature as error:
            raise ValueError("The update signature does not match the installed publisher key.") from error
    return data


def xdg_home(variable, default):
    directory = Path(os.environ.get(variable, ""))
    return directory if directory.is_absolute() else Path.home() / default


def install_root():
    return xdg_home("XDG_DATA_HOME", ".local/share").resolve() / "jaro-app"


def require_private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"Jaro needs a private, user-owned directory at {path}.")


def active_version(root):
    require_private_directory(root)
    require_private_directory(root / "versions")
    target = os.readlink(root / "current")
    if not re.fullmatch(r"versions/" + VERSION, target):
        raise ValueError("Invalid Jaro installation pointer.")
    version = root / target
    require_private_directory(version)
    return version


def managed_root(version):
    root = install_root()
    try:
        if version == active_version(root) and (root / "manager.py").is_file():
            read_publisher(root)
            return root
    except (OSError, ValueError):
        pass
    return None


def read_publisher(root):
    require_private_directory(root)
    require_private_directory(root / "versions")
    publisher = json.loads((root / "installed.json").read_bytes())
    if not isinstance(publisher, str) or len(base64.b64decode(publisher, validate=True)) != 32:
        raise ValueError("Invalid Jaro installation receipt.")
    return publisher


@contextmanager
def installation_lock(root, *, exclusive=False, timeout=0):
    import fcntl

    require_private_directory(root)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with (root / "in-use").open("ab") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Close Jaro and wait for its command-line jobs before installing.")
                time.sleep(.1)
        yield lock.fileno()


class UpdateInProgress(ValueError):
    pass


@contextmanager
def update_lock(root):
    import fcntl

    require_private_directory(root)
    with (root / "update-in-use").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpdateInProgress("Jaro is finishing an update. Reopen it in a moment.") from error
        yield


def sync_installation(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def archive_members(archive):
    total = 0
    for index, item in enumerate(archive):
        path = Path(item.name)
        total += item.size
        if (index >= 100_000 or total > 4 * MAX_ARCHIVE or item.size < 0 or path.is_absolute()
                or ".." in path.parts or not (item.isdir() or item.isfile())):
            raise ValueError("The update contains an invalid archive member.")
        yield item


def point_to(root, version):
    with tempfile.TemporaryDirectory(prefix=".current-", dir=root) as directory:
        link = Path(directory) / "current"
        link.symlink_to(version.relative_to(root))
        link.replace(root / "current")
        sync_installation(root)


def install_manager(root, source):
    with tempfile.TemporaryDirectory(prefix=".manager-", dir=root) as directory:
        manager = Path(directory) / "manager.py"
        shutil.copyfile(source, manager)
        manager.chmod(0o700)
        manager.replace(root / manager.name)


def rejected_update(root):
    if root is None:
        return ""
    path = root / REJECTED_UPDATE
    try:
        if path.is_symlink():
            return ""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            if stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                data = file.read(66)
                if re.fullmatch(rb"[0-9a-f]{64}\n", data):
                    return data[:-1].decode("ascii")
    except OSError:
        pass
    return ""


def reject_update(root, digest):
    with tempfile.TemporaryDirectory(prefix=".update-rejected-", dir=root) as directory:
        receipt = Path(directory) / REJECTED_UPDATE
        receipt.write_bytes((digest + "\n").encode("ascii"))
        receipt.replace(root / REJECTED_UPDATE)


def record_startup_trial(root, previous, release, staging):
    trial = {"previous": previous.name, "version": release["version"], "digest": release["sha256"],
             "staging": str(staging)}
    with tempfile.TemporaryDirectory(prefix=".startup-trial-", dir=root) as directory:
        record = Path(directory) / STARTUP_TRIAL
        with record.open("w", encoding="ascii") as file:
            json.dump(trial, file)
            file.flush()
            os.fsync(file.fileno())
        record.replace(root / STARTUP_TRIAL)
        sync_installation(root)


def read_startup_trial(root):
    try:
        with (root / STARTUP_TRIAL).open("rb") as file:
            trial = json.loads(file.read(MAX_MANIFEST + 1))
    except FileNotFoundError:
        return None
    if not isinstance(trial, dict) or trial.keys() != {"previous", "version", "digest", "staging"}:
        raise ValueError("Invalid Jaro update recovery record.")
    if (not all(isinstance(value, str) for value in trial.values())
            or not all(re.fullmatch(VERSION, trial[key]) for key in ("previous", "version"))
            or trial["previous"] == trial["version"] or not re.fullmatch(r"[a-f0-9]{64}", trial["digest"])):
        raise ValueError("Invalid Jaro update recovery record.")
    staging = Path(trial["staging"])
    if (not staging.is_absolute() or ".." in staging.parts or not staging.name.startswith("jaro-update-")):
        raise ValueError("Invalid Jaro update startup folder.")
    return trial


def clear_startup_trial(root):
    (root / STARTUP_TRIAL).unlink(missing_ok=True)
    sync_installation(root)


def finish_startup_trial(root, previous, installed, digest, *, confirmed):
    restored = False
    with installation_lock(root, exclusive=not confirmed, timeout=4):
        if os.readlink(root / "current") == str(installed.relative_to(root)):
            if confirmed:
                require_private_directory(installed)
                try:
                    install_manager(root, installed / "engine/linux_direct.py")
                except OSError as error:
                    print(f"Could not refresh the Jaro launcher: {error}", file=sys.stderr)
            else:
                require_private_directory(previous)
                with suppress(OSError):
                    reject_update(root, digest)
                point_to(root, previous)
                prune_versions(root, previous)
                restored = True
        clear_startup_trial(root)
    return restored


def settle_interrupted_update(root):
    """Resolve an abandoned trial while the caller holds the update lock."""
    trial = read_startup_trial(root)
    if trial is None:
        return False
    staging = Path(trial["staging"])
    confirmed = False
    try:
        require_private_directory(staging)
        require_private_directory(root / "versions" / trial["version"])
        # The existing startup handoff confirms only the authenticated workspace.
        receipt = staging / STARTUP_RECEIPT
        if stat.S_ISREG(receipt.lstat().st_mode):
            with receipt.open("rb") as file:
                confirmed = file.read(7) == b"ready\n"
    except (OSError, ValueError):
        pass
    versions = root / "versions"
    restored = finish_startup_trial(root, versions / trial["previous"], versions / trial["version"],
                                   trial["digest"], confirmed=confirmed)
    # The recorded folder can be on a different TMPDIR from a later launch.
    with suppress(OSError, ValueError):
        require_private_directory(staging)
        with (staging / "restart.json").open("rb") as file:
            intent = json.loads(file.read(MAX_MANIFEST + 1))
        if intent == {"version": trial["version"], "digest": trial["digest"]}:
            shutil.rmtree(staging)
    return restored


def start_updated_workspace(launcher, payload, staging, environment, *, legacy_timeout):
    if not (payload / STARTUP_CAPABILITY).is_file():
        try:
            return subprocess.Popen([launcher], env=environment).wait(timeout=legacy_timeout) == 0
        except subprocess.TimeoutExpired:
            return True

    receipt = staging / STARTUP_RECEIPT
    receipt.unlink(missing_ok=True)
    process = subprocess.Popen([launcher], env={**environment, STARTUP_ENV: str(staging)},
                               start_new_session=sys.platform != "win32")
    until = time.monotonic() + UPDATE_STARTUP_SECONDS
    while True:
        if receipt.is_file():
            return True
        if process.poll() is not None:
            return receipt.is_file()
        if time.monotonic() >= until:
            if sys.platform == "win32" and not (payload / WINDOWS_OWNED_STARTUP).is_file():
                return None  # An older launcher can leave its children running.
            return False if stop_update_probe(process) else None
        time.sleep(.05)


def stop_update_probe(process):
    """Stop the launcher we started before allowing the caller to roll back."""
    try:
        if sys.platform == "win32":
            process.kill()  # The stable launcher owns a kill-on-close job.
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=PROBE_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                return False
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return process.poll() is not None
    return True


def prune_versions(root, *keep):
    try:
        for entry in (root / "versions").iterdir():
            if entry in keep or not re.fullmatch(VERSION, entry.name) or entry.is_symlink():
                continue
            try:
                require_private_directory(entry)
                if (entry / "portable-release.json").is_file():
                    shutil.rmtree(entry)
            except (OSError, ValueError) as error:
                print(f"Could not remove an old Jaro version: {error}", file=sys.stderr)
    except OSError as error:
        print(f"Could not list old Jaro versions: {error}", file=sys.stderr)


def install_version(root, source, manifest, *, startup=None):
    version = manifest["version"]
    target = root / "versions" / version
    current = None
    if os.path.lexists(root / "current"):
        current = active_version(root)
        parts = lambda value: tuple(map(int, value.split(".")))
        if parts(version) <= parts(current.name):
            raise ValueError("This Jaro version is already installed or older.")
    with source.open("rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size != manifest["size"]
                or hashlib.file_digest(file, "sha256").hexdigest() != manifest["sha256"]):
            raise ValueError("The archive does not match its signed manifest.")
        file.seek(0)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=target.parent) as directory:
            payload = Path(directory) / "payload"
            payload.mkdir(mode=0o700)
            with tarfile.open(fileobj=file, mode="r|gz") as archive:
                archive.extractall(payload, members=archive_members(archive), filter="data")
            release = json.loads((payload / "portable-release.json").read_bytes())
            if (release != {"kind": KIND, "version": version, "revision": 0}
                    or not os.access(payload / "bin/jaro", os.X_OK)
                    or not (payload / "desktop/portable/open_workspace.py").is_file()
                    or not (payload / "engine/linux_direct.py").is_file()
                    or not (payload / "venv/pyvenv.cfg").is_file()):
                raise ValueError("The archive does not contain the specified Jaro release.")
            (payload / "venv/bin").mkdir(exist_ok=True)
            (payload / "venv/bin/python").symlink_to(system_python(payload))
            validate_payload(payload)
            if target.exists():
                require_private_directory(target)
                shutil.rmtree(target)
            payload.replace(target)
            sync_installation(target.parent)
    if startup is not None and current is not None and (target / STARTUP_CAPABILITY).is_file():
        record_startup_trial(root, current, manifest, startup)
    point_to(root, target)
    prune_versions(root, target, current)
    return target


def validate_payload(payload):
    python = payload / "venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("The update Python runtime is unavailable.")
    result = subprocess.run(
        [str(python), "-EsB", "-c", "import sys; assert sys.version_info >= (3, 12)"],
        capture_output=True, timeout=10,
    )
    if result.returncode:
        raise ValueError("The update runtime is incompatible with this system's Python.")


def desktop_value(value):
    value = str(value)
    if any(ord(character) < 32 for character in value):
        raise ValueError("The Jaro installation path contains a control character.")
    return value.replace("\\", "\\\\")


def desktop_argument(value):
    escaped = "".join("\\" + character if character in '\\$"`' else character for character in str(value))
    return '"' + desktop_value(escaped).replace("%", "%%") + '"'


def read_manifest(path, publisher):
    with path.open("rb") as file:
        return verify_manifest(file.read(MAX_MANIFEST + 1), publisher)


def bootstrap(root, archive, manifest, publisher):
    release = read_manifest(manifest, publisher)
    command = desktop_argument(root / "jaro")
    icon = desktop_value(root / "current/share/jaro.png")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require_private_directory(root)
    (root / "versions").mkdir(exist_ok=True, mode=0o700)
    require_private_directory(root / "versions")
    with update_lock(root):
        settle_interrupted_update(root)
        with installation_lock(root, exclusive=True):
            receipt = root / "installed.json"
            if receipt.exists():
                if read_publisher(root) != publisher:
                    raise ValueError("The installer uses a different publisher key.")
            else:
                install_manager(root, Path(__file__).resolve())
                with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=root) as directory:
                    temporary = Path(directory)
                    for name in ("jaro", "jardctl"):
                        link = temporary / name
                        link.symlink_to("manager.py")
                        link.replace(root / name)
                    key = temporary / receipt.name
                    key.write_text(json.dumps(publisher), encoding="ascii")
                    key.replace(receipt)
            if not os.path.lexists(root / "current") or active_version(root).name != release["version"]:
                installed = install_version(root, archive, release)
                install_manager(root, installed / "engine/linux_direct.py")
    entries = root.parent / "applications"
    entries.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jaro-desktop-", dir=entries) as temporary:
        entry = Path(temporary) / f"{APP_ID}.desktop"
        entry.write_text("[Desktop Entry]\nType=Application\nName=Jaro\nTerminal=false\nStartupNotify=true\n"
                         f"Exec={command}\nIcon={icon}\nStartupWMClass={APP_ID}\nCategories=AudioVideo;Audio;\n",
                         encoding="utf-8")
        entry.replace(entries / entry.name)
    source_icon = root / "current/share/jaro.png"
    if source_icon.is_file():
        icon_dir = root.parent / "icons/hicolor/512x512/apps"
        icon_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_icon, icon_dir / f"{APP_ID}.png")
    print(f"Jaro is installed. Open it from the desktop menu or run {command}.")


def remove_desktop_entry(path, command, *, autostart=False):
    with suppress(FileNotFoundError):
        if not stat.S_ISREG(path.lstat().st_mode):
            return
        with path.open(encoding="utf-8", errors="replace") as file:
            content = file.read(8193)
        if len(content) > 8192:
            return
        lines = content.splitlines()
        if f"Exec={command}" in lines and (not autostart or "X-Jaro-Managed=true" in lines):
            path.unlink()


def uninstall(root):
    if not os.path.lexists(root):
        return
    library = os.environ.get("RUMBLE_DATA_DIR")
    if library and Path(library).expanduser().resolve().is_relative_to(root):
        raise ValueError("Move the recording library outside the Jaro installation before uninstalling.")
    command = desktop_argument(root / "jaro")
    name = f"{APP_ID}.desktop"
    autostart = xdg_home("XDG_CONFIG_HOME", ".config") / "autostart" / name
    with update_lock(root), installation_lock(root, exclusive=True):
        read_publisher(root)
        remove_desktop_entry(root.parent / "applications" / name, command)
        remove_desktop_entry(autostart, f"{command} --background", autostart=True)
        with suppress(OSError):
            (root.parent / f"icons/hicolor/512x512/apps/{APP_ID}.png").unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix=".jaro-uninstall-", dir=root.parent) as holding:
            root.replace(Path(holding) / root.name)


def launch(root, name, args):
    while True:
        with installation_lock(root) as fd:
            trial = read_startup_trial(root)
            probe = (trial and name == "jaro" and os.environ.get(STARTUP_ENV) == trial["staging"]
                     and os.readlink(root / "current") == "versions/" + trial["version"])
            if not trial or probe:
                version = active_version(root)
                python = str(version / "venv/bin/python")
                if name == "jaro":
                    command = [version / "desktop/portable/open_workspace.py", "--installed", version / "bin/jaro", *args]
                elif os.environ.get("JARO_GNOME_SHORTCUT") == "1" and len(args) == 2 and args[0] == "dictation":
                    command = [version / "engine/wayland_dictation_commands.py", args[1]]
                else:
                    command = [version / "engine/jardctl.py", *args]
                os.set_inheritable(fd, True)
                return os.execv(python, [python, "-EsB", *command])
        with update_lock(root):
            if settle_interrupted_update(root):
                os.environ["JARO_UPDATE_ERROR"] = "rejected"


def apply_update(root, staging):
    if (staging.parent != Path(tempfile.gettempdir()).resolve()
            or not staging.name.startswith("jaro-update-")):
        raise ValueError("Invalid Jaro update staging location.")
    require_private_directory(staging)
    environment = dict(os.environ)
    environment.pop("JARO_UPDATE_ERROR", None)
    environment.pop(STARTUP_ENV, None)
    pending = False
    try:
        with update_lock(root):
            pending = apply_update_locked(root, staging, environment)
    except UpdateInProgress as error:
        print(error, file=sys.stderr)
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"Jaro update failed: {error}", file=sys.stderr)
        environment.setdefault("JARO_UPDATE_ERROR", "1")
        subprocess.Popen([root / "jaro"], env=environment)
    finally:
        # An active probe may still need to publish its receipt after this worker exits.
        if not pending:
            with suppress(OSError, ValueError):
                trial = read_startup_trial(root)
                pending = bool(trial and trial["staging"] == str(staging))
        if not pending:
            shutil.rmtree(staging, ignore_errors=True)


def apply_update_locked(root, staging, environment):
    settle_interrupted_update(root)
    with installation_lock(root, exclusive=True, timeout=10):
        previous = active_version(root)
        release = read_manifest(staging / MANIFEST, read_publisher(root))
        with (staging / "restart.json").open("rb") as file:
            intent = json.loads(file.read(MAX_MANIFEST + 1))
        if intent != {"version": release["version"], "digest": release["sha256"]}:
            raise ValueError("The prepared update changed.")
        if release["sha256"] == rejected_update(root):
            environment["JARO_UPDATE_ERROR"] = "rejected"
            raise ValueError("This update already failed to start.")
        installed = install_version(root, staging / ARCHIVE, release, startup=staging)
    try:
        started = start_updated_workspace(root / "jaro", installed, staging, environment, legacy_timeout=8)
    except OSError as error:
        print(f"Could not start the new Jaro version: {error}", file=sys.stderr)
        started = False
    if started is None:
        print("The updated Jaro workspace has not confirmed startup. The previous version was kept.", file=sys.stderr)
        return True
    if finish_startup_trial(root, previous, installed, release["sha256"], confirmed=started):
        environment["JARO_UPDATE_ERROR"] = "rejected"
    if not started:
        raise ValueError("The new Jaro version exited during startup.")
    return False


def main():
    if os.getuid() == 0 or sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("Use this installer as a regular user on Linux x86_64.")
    name = Path(sys.argv[0]).name
    if name in ("jaro", "jardctl"):
        launch(Path(__file__).resolve().parent, name, sys.argv[1:])
        return
    if name == "manager.py" and len(sys.argv) == 3 and sys.argv[1] == "apply":
        apply_update(Path(__file__).resolve().parent, Path(sys.argv[2]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--publisher-key", default=PUBLISHER, help="Trusted Ed25519 key for a custom release")
    if sys.argv[1:] == ["uninstall"]:
        uninstall(install_root())
        print("Jaro was removed. Your recordings and models are still available.")
        return
    args = parser.parse_args()
    bootstrap(install_root(), args.archive, args.manifest, args.publisher_key)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
