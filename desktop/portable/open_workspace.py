"""Start the portable Jaro engine and open it in the Native SDK host."""
import argparse
from http.client import HTTPConnection
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from sdk_embedded import owned_service, verify_service


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
                       f"Exec={command}\nTerminal=false\nStartupWMClass=local.jardaudio.desktop\n")
    except FileExistsError:
        pass


def open_workspace(host):
    try:
        ensure_wayland_desktop_entry(host)
    except (OSError, ValueError) as error:
        print(f"Could not register the Jaro desktop launcher: {error}", file=sys.stderr)
    command = [sys.executable, str(ROOT / "engine/server.py"), "--portable-workspace"]
    environment = dict(os.environ)
    if os.name == "nt":
        environment["JARO_WINDOWS_CAPTURE"] = str(host.with_name("jaro-capture.exe"))
    with owned_service(command, stderr=None, env=environment) as (record, engine):
        connection = HTTPConnection("127.0.0.1", record["port"], timeout=3)
        try:
            verify_service(connection, record)
        finally:
            connection.close()
        key = record.get("workspaceKey")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise RuntimeError("Jaro’s workspace did not return a valid connection key.")
        with subprocess.Popen([host], env={
            **os.environ, "JARO_WORKSPACE_PORT": str(record["port"]), "JARO_WORKSPACE_KEY": key,
        }) as window:
            try:
                while engine.poll() is None:
                    try:
                        return window.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        continue
                return engine.returncode
            finally:
                if window.poll() is None:
                    window.terminate()
                    try:
                        window.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        window.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", type=Path, help="Path to the built Jaro Native SDK executable.")
    args = parser.parse_args()
    try:
        return open_workspace(args.host.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
