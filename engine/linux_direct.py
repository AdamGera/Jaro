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


def system_python(payload=None):
    if payload is not None:
        lib = payload / "venv/lib"
        if lib.is_dir():
            for entry in sorted(lib.iterdir()):
                if re.fullmatch(r"python3\.[0-9]+", entry.name):
                    candidate = Path("/usr/bin") / entry.name
                    if candidate.is_file():
                        return str(candidate)
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


def install_version(root, source, manifest):
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
                    or not (payload / "venv/pyvenv.cfg").is_file()):
                raise ValueError("The archive does not contain the specified Jaro release.")
            (payload / "venv/bin").mkdir(exist_ok=True)
            (payload / "venv/bin/python").symlink_to(system_python(payload))
            validate_payload(payload)
            if target.exists():
                require_private_directory(target)
                shutil.rmtree(target)
            payload.replace(target)
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
    with installation_lock(root, exclusive=True):
        receipt = root / "installed.json"
        if receipt.exists():
            if read_publisher(root) != publisher:
                raise ValueError("The installer uses a different publisher key.")
        else:
            with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=root) as directory:
                temporary = Path(directory)
                manager = temporary / "manager.py"
                shutil.copyfile(Path(__file__).resolve(), manager)
                manager.chmod(0o700)
                manager.replace(root / manager.name)
                for name in ("jaro", "jardctl"):
                    link = temporary / name
                    link.symlink_to("manager.py")
                    link.replace(root / name)
                key = temporary / receipt.name
                key.write_text(json.dumps(publisher), encoding="ascii")
                key.replace(receipt)
        if not os.path.lexists(root / "current") or active_version(root).name != release["version"]:
            install_version(root, archive, release)
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
    with installation_lock(root, exclusive=True):
        read_publisher(root)
        remove_desktop_entry(root.parent / "applications" / name, command)
        remove_desktop_entry(autostart, f"{command} --background", autostart=True)
        with suppress(OSError):
            (root.parent / f"icons/hicolor/512x512/apps/{APP_ID}.png").unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix=".jaro-uninstall-", dir=root.parent) as holding:
            root.replace(Path(holding) / root.name)


def launch(root, name, args):
    with installation_lock(root) as fd:
        version = active_version(root)
        python = str(version / "venv/bin/python")
        if name == "jaro":
            args = [version / "desktop/portable/open_workspace.py", "--installed", version / "bin/jaro", *args]
        elif os.environ.get("JARO_GNOME_SHORTCUT") == "1" and len(args) == 2 and args[0] == "dictation":
            args = [version / "engine/wayland_dictation_commands.py", args[1]]
        else:
            args = [version / "engine/jardctl.py", *args]
        os.set_inheritable(fd, True)
        os.execv(python, [python, "-EsB", *args])


def apply_update(root, staging):
    if (staging.parent != Path(tempfile.gettempdir()).resolve()
            or not staging.name.startswith("jaro-update-")):
        raise ValueError("Invalid Jaro update staging location.")
    require_private_directory(staging)
    environment = dict(os.environ)
    environment.pop("JARO_UPDATE_ERROR", None)
    try:
        with installation_lock(root, exclusive=True, timeout=10):
            previous = active_version(root)
            release = read_manifest(staging / MANIFEST, read_publisher(root))
            with (staging / "restart.json").open("rb") as file:
                intent = json.loads(file.read(MAX_MANIFEST + 1))
            if intent != {"version": release["version"], "digest": release["sha256"]}:
                raise ValueError("The prepared update changed.")
            installed = install_version(root, staging / ARCHIVE, release)
        process = subprocess.Popen([root / "jaro"], env=environment)
        try:
            result = process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            return
        if not result:
            return
        with installation_lock(root, exclusive=True, timeout=4):
            if active_version(root) == installed:
                point_to(root, previous)
                prune_versions(root, previous)
        raise ValueError("The new Jaro version exited during startup.")
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"Jaro update failed: {error}", file=sys.stderr)
        subprocess.Popen([root / "jaro"], env={**environment, "JARO_UPDATE_ERROR": "1"})
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
