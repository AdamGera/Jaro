"""Coordination shared by the browser launcher and the packaged app."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from engine_paths import library_path


HOST = "127.0.0.1"
PORT = int(os.environ.get("RUMBLE_PORT", 8765))
URL = f"http://{HOST}:{PORT}"
API_VERSION = 6
LEASE_SECONDS = 90
WORKSPACE_CONNECT = "/_desktop/connect"


def desktop_control_path(root):
    pointer = root / ".native-control"
    with os.fdopen(os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("Invalid desktop pointer")
        path = Path(file.read(256).decode().strip())
    if path.name != "socket" or not path.parent.name.startswith("jard-control-") or path.parent.parent != Path("/tmp"):
        raise OSError("Invalid desktop socket")
    folder = path.parent.lstat()
    if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.geteuid() or folder.st_mode & 0o077:
        raise OSError("Invalid desktop socket owner")
    if not stat.S_ISSOCK(path.lstat().st_mode):
        raise OSError("The desktop control service is unavailable")
    return path


def library_id(root):
    return hashlib.sha256(os.fsencode(root)).hexdigest()


@contextmanager
def startup_lock():
    import fcntl

    root = library_path()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / ".launch.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_native_token(root):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(root / ".native-token", flags), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("Invalid Jaro engine credential.")
        token = file.read(66)
        if not re.fullmatch(rb"[0-9a-f]{64}\n?", token):
            raise OSError("Invalid Jaro engine credential.")
    return token.removesuffix(b"\n").decode("ascii")


def install_token(root):
    # Only the process holding .server.lock can create this file.
    try:
        return read_native_token(root)
    except OSError:
        pass
    token = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix=".engine-key-", dir=root) as directory:
        partial = Path(directory) / "token"
        with os.fdopen(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(token + "\n")
        partial.replace(root / ".native-token")
    return token


def engine_proof(token, challenge):
    return hmac.digest(token.encode(), challenge.encode("ascii"), "sha256").hex()


def workspace_key(token):
    # A binary prefix also separates this credential from older ASCII identity challenges.
    return hmac.digest(token.encode(), b"\xffjaro:workspace:v1", "sha256").hex()


def browser_workspace_url(root):
    return f"{URL}{WORKSPACE_CONNECT}#{workspace_key(read_native_token(root))}"


def open_browser_workspace(root):
    url = browser_workspace_url(root)
    if sys.platform == "darwin":
        # Send the credential over stdin so it stays out of process arguments.
        result = subprocess.run(["/usr/bin/osascript"], input=f'open location "{url}"\n',
                                text=True, capture_output=True, timeout=10)
        if result.returncode == 0:
            return
    else:
        import webbrowser

        if webbrowser.open(url):
            return
    raise RuntimeError("Could not open Jaro in the default browser.")


def running_engine():
    """None if the port is free. A busy incompatible port is a recoverable launch error."""
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            pass
    except ConnectionRefusedError:
        return None
    except OSError as error:
        raise RuntimeError(f"Could not reach {URL}: {error}") from error
    challenge = secrets.token_hex(16)
    try:
        with closing(HTTPConnection(HOST, PORT, timeout=2)) as connection:
            connection.request("GET", f"/api/status?challenge={challenge}")
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ValueError("unexpected response")
                info = json.loads(response.read(32_768))
        if not isinstance(info, dict) or info.get("model") != "Reson8/Rumble-2-Flash":
            raise ValueError("unexpected server")
    except (OSError, ValueError, HTTPException) as error:
        raise RuntimeError(f"Another application is using {URL}. Close it and reopen Jaro.") from error
    if info.get("apiVersion") != API_VERSION:
        raise RuntimeError("Another Jaro version is running. Quit it, then reopen Jaro.")
    root = library_path()
    if info.get("libraryId") != library_id(root):
        raise RuntimeError("The running Jaro engine is using a different session library.")
    try:
        token = read_native_token(root)
    except OSError as error:
        raise RuntimeError("Could not verify the running Jaro engine.") from error
    proof = info.get("proof")
    if not isinstance(proof, str) or not proof.isascii() or not hmac.compare_digest(proof, engine_proof(token, challenge)):
        raise RuntimeError("Could not verify the running Jaro engine.")
    return info


class ClientLeases:
    def __init__(self):
        self.deadlines = {}
        self.streams = set()
        self.desktop_streams = set()
        self.closing = asyncio.Event()
        self.last_activity = time.monotonic()

    def renew(self, client):
        now = time.monotonic()
        self.deadlines[client] = now + LEASE_SECONDS
        self.last_activity = now

    def detach(self, client):
        self.deadlines.pop(client, None)

    @contextmanager
    def browser_connection(self, client=None, *, desktop=False):
        connection = (client, object())
        self.streams.add(connection)
        if desktop:
            self.desktop_streams.add(connection)
        self.last_activity = time.monotonic()
        try:
            yield
        finally:
            self.streams.discard(connection)
            self.desktop_streams.discard(connection)
            self.last_activity = time.monotonic()

    def idle(self, busy):
        now = time.monotonic()
        self.deadlines = {k: v for k, v in self.deadlines.items() if v > now}
        if busy or self.streams or self.deadlines:
            self.last_activity = now
            return False
        return now - self.last_activity >= 30


def begin_draining(state):
    state.draining = True
    state.leases.closing.set()
    if updater := getattr(state, "portable_updater", None):
        updater.changed.set()


def quit_engine():
    if not running_engine():
        print("Jaro is already stopped.")
        return
    headers = {
        "Origin": URL, "X-Jard-Action": "quit", "X-Jard-Token": read_native_token(library_path()),
    }
    with closing(HTTPConnection(HOST, PORT, timeout=3)) as connection:
        connection.request("POST", "/api/quit", headers=headers)
        with connection.getresponse() as response:
            if response.status != 202:
                raise RuntimeError("Jaro could not quit.")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                pass
        except OSError:
            print("Jaro has quit.")
            return
        time.sleep(0.15)
    raise RuntimeError("Jaro is still finishing its shutdown.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--quit", required=True, action="store_true")
    parser.parse_args()
    try:
        quit_engine()
    except (OSError, RuntimeError, HTTPException) as error:
        parser.exit(1, str(error) + "\n")
