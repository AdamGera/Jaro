"""Start the portable Jaro engine and open it in the Native SDK host."""
import argparse
from contextlib import suppress
from http.client import HTTPConnection
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from sdk_embedded import owned_service, verify_service
from engine_paths import library_path
from linux_direct import STARTUP_ENV, STARTUP_RECEIPT, WINDOW_STARTUP_SECONDS, WORKSPACE_CONNECTED
from desktop_instance import STARTUP_VARIABLES, claim_desktop, replace_private, startup_environment


def desktop_argument(value):
    value = str(value)
    if any(ord(character) < 32 for character in value):
        raise ValueError("The Jaro desktop launcher path contains an unsupported control character.")
    # Exec quoting is applied after Desktop Entry string escapes have been read.
    escaped = "".join("\\" + character if character in '\\$"\x60' else character for character in value)
    return '"' + escaped.replace("\\", "\\\\").replace("%", "%%") + '"'


def ensure_wayland_desktop_entry(host):
    if sys.platform != "linux" or not (
        os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    ):
        return
    default = Path.home() / ".local/share"
    data = Path(os.environ.get("XDG_DATA_HOME", default))
    applications = (data if data.is_absolute() else default) / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "local.jardaudio.desktop.desktop"
    command = " ".join(map(desktop_argument, (sys.executable, Path(__file__).resolve(), host)))
    try:
        with entry.open("x", encoding="utf-8") as file:
            file.write("[Desktop Entry]\nType=Application\nName=Jaro\nComment=Local recordings and dictation\n"
                       f"Exec={command}\nTerminal=false\nStartupNotify=true\nStartupWMClass=local.jardaudio.desktop\n")
    except FileExistsError:
        pass


def open_workspace(host, *, installed=False, background=False):
    with claim_desktop(library_path()) as instance:
        if not instance.primary:
            if not background:
                instance.activate()
            return 0
        return run_owned_workspace(instance, host, installed=installed, background=background)


def run_owned_workspace(instance, host, *, installed, background):
    if not installed:
        try:
            ensure_wayland_desktop_entry(host)
        except (OSError, ValueError) as error:
            print(f"Could not register the Jaro desktop launcher: {error}", file=sys.stderr)
    command = [sys.executable, str(ROOT / "engine/server.py"), "--portable-workspace"]
    environment = dict(os.environ)
    environment.pop("JARO_PORTABLE_RELEASE", None)
    environment.pop("JARO_UPDATE_STAGING", None)
    staging = None
    direct = None
    metadata = ROOT / "portable-release.json"
    if installed and metadata.is_file():
        environment["JARO_PORTABLE_RELEASE"] = metadata.read_text(encoding="ascii")
        if os.name == "nt":
            from portable_login import installed_launcher

            if installed_launcher(ROOT):
                staging = Path(tempfile.mkdtemp(prefix="jaro-update-"))
        elif sys.platform == "linux":
            from linux_direct import managed_root

            direct = managed_root(ROOT)
            if direct:
                staging = Path(tempfile.mkdtemp(prefix="jaro-update-")).resolve()
        if staging:
            environment["JARO_UPDATE_STAGING"] = str(staging)
    if os.name == "nt":
        environment["JARO_WINDOWS_CAPTURE"] = str(host.with_name("jaro-capture.exe"))
        environment["JARO_WINDOWS_DICTATION"] = str(host.with_name("jaro-dictation.exe"))
        environment["JARO_WINDOWS_SCREEN"] = str(host.with_name("jaro-screen.exe"))
        environment["JARO_DICTATION_INDICATOR"] = str(host.with_name("jaro-indicator.exe"))
        environment["JARO_DICTATION_AUDIO"] = str(host.with_name("jaro-audio-control.exe"))
        environment["JARO_CALL_SURFACE"] = str(host.with_name("jaro-call-surface.exe"))
        environment["JARO_TRAY_HELPER"] = str(host.with_name("jaro-tray.exe"))
        environment["JARO_SAVE_VIDEO_DIALOG"] = str(host.with_name("jaro-save-video.exe"))
    elif sys.platform == "linux":
        environment["JARO_SAVE_VIDEO_DIALOG"] = str(host.with_name("jaro-save-video"))
        environment["JARO_LINUX_PEN"] = str(host.with_name("jaro-pen"))
        environment["JARO_WAYLAND_FOCUS"] = str(host.with_name("jaro-focused-window"))
        environment["JARO_WAYLAND_CLIPBOARD"] = str(host.with_name("jaro-wayland-clipboard"))
        environment["JARO_DICTATION_INDICATOR"] = str(host.with_name("jaro-indicator"))
        environment["JARO_CALL_SURFACE"] = str(host.with_name("jaro-call-surface"))
    scheduled = False
    try:
        result = run_workspace(instance, host, command, environment, background=background)
        if staging and (staging / "restart.json").is_file():
            if direct:
                subprocess.Popen([direct / "manager.py", "apply", staging], start_new_session=True,
                                 stdin=subprocess.DEVNULL)
            else:
                subprocess.Popen([sys.executable, ROOT / "engine/portable_update.py", staging, str(os.getppid())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            scheduled = True
            return 0
        return result
    finally:
        if staging and not scheduled:
            shutil.rmtree(staging, ignore_errors=True)


def run_workspace(instance, host, command, environment, *, background):
    with tempfile.TemporaryDirectory(prefix="jaro-tray-") as directory:
        tray = Path(directory)
        environment["JARO_DESKTOP_TRAY"] = str(tray)
        with owned_service(command, stderr=None, env=environment) as (record, engine):
            return run_desktop(instance, host, record, engine, tray, background=background)


def take_open(instance, tray):
    activation = instance.take_activation()
    if activation is not None:
        return activation
    try:
        (tray / "open").unlink()
    except FileNotFoundError:
        return None
    return b""


def update_startup_folder():
    if not (value := os.environ.get(STARTUP_ENV)):
        return None
    folder = Path(value)
    if (not folder.is_absolute() or folder.parent.resolve() != Path(tempfile.gettempdir()).resolve()
            or not folder.name.startswith("jaro-update-") or not folder.is_dir() or folder.is_symlink()):
        raise ValueError("Invalid Jaro update startup folder.")
    return folder


def run_desktop(instance, host, record, engine, tray, *, background):
    connection = HTTPConnection("127.0.0.1", record["port"], timeout=3)
    try:
        verify_service(connection, record)
    finally:
        connection.close()
    key = record.get("workspaceDesktopKey")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise RuntimeError("Jaro’s workspace did not return a valid connection key.")
    base = {**os.environ, "JARO_WORKSPACE_PORT": str(record["port"]), "JARO_WORKSPACE_KEY": key}
    base.pop(STARTUP_ENV, None)
    update_startup = None if background else update_startup_folder()
    result = 0
    activation = None
    if background:
        while (activation := take_open(instance, tray)) is None:
            try:
                return engine.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    while engine.poll() is None:
        closed = tray / "workspace-closed"
        closed.unlink(missing_ok=True)
        environment = dict(base)
        if activation is not None:
            for name in STARTUP_VARIABLES:
                environment.pop(name, None)
            environment.update(startup_environment(activation))
        result = run_window(instance, host, engine, tray, environment, update_startup=update_startup)
        update_startup = None
        if result != 0 or engine.poll() is not None:
            return result
        closed.touch()
        while (activation := take_open(instance, tray)) is None:
            try:
                return engine.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    return engine.returncode


def confirm_update_startup(folder, tray):
    if not (tray / WORKSPACE_CONNECTED).is_file():
        return False
    replace_private(folder / STARTUP_RECEIPT, b"ready\n")
    return True


def run_window(instance, host, engine, tray, environment, *, update_startup=None):
    if sys.platform == "win32":
        # The default WebView2 profile follows the versioned executable path.
        # Keep workspace drafts and preferences with this library across updates.
        environment = environment.copy()
        environment.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(library_path() / ".webview2"))
    startup_deadline = time.monotonic() + WINDOW_STARTUP_SECONDS if update_startup is not None else None
    with subprocess.Popen([host], stdin=subprocess.PIPE, bufsize=0, env=environment) as window:
        try:
            with suppress(OSError):
                instance.set_window(window.pid)
                replace_private(tray / "window-pid", str(window.pid).encode("ascii"))
            try:
                os.set_blocking(window.stdin.fileno(), False)
                forwarding = True
            except OSError:
                forwarding = False
            pending = b""
            while engine.poll() is None:
                if update_startup is not None:
                    if confirm_update_startup(update_startup, tray):
                        update_startup = None
                    elif time.monotonic() >= startup_deadline:
                        return 1
                if forwarding and not pending and (activation := take_open(instance, tray)) is not None:
                    pending = activation[1:] + b"\n"
                if pending:
                    with suppress(OSError):
                        pending = pending[window.stdin.write(pending) or 0:]
                try:
                    result = window.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    continue
                if update_startup is not None and not confirm_update_startup(update_startup, tray):
                    return 1
                return result
            return 1 if update_startup is not None else engine.returncode
        finally:
            with suppress(OSError):
                (tray / "window-pid").unlink(missing_ok=True)
                instance.pid.unlink(missing_ok=True)
            if window.poll() is None:
                window.terminate()
                try:
                    window.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    window.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--background", action="store_true", help="Start the engine and wait for a request to open the workspace.")
    parser.add_argument("host", type=Path, help="Path to the built Jaro Native SDK executable.")
    args = parser.parse_args()
    if sys.platform == "linux" and STARTUP_ENV in os.environ:
        signal.signal(signal.SIGTERM, exit_update_probe)
    try:
        return open_workspace(args.host.resolve(strict=True), installed=args.installed, background=args.background)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")


def exit_update_probe(_signal, _frame):
    # Unwind the workspace and engine owners when the updater cancels a probe.
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
