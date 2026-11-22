"""Coordination shared by the browser launcher and the packaged app."""
import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from engine_paths import library_path


HOST = "127.0.0.1"
PORT = int(os.environ.get("RUMBLE_PORT", 8765))
URL = f"http://{HOST}:{PORT}"
API_VERSION = 5
LEASE_SECONDS = 90


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


def install_token(root):
    # Only the process holding .server.lock can create this file.
    path = root / ".native-token"
    if path.is_file():
        token = path.read_text().strip()
        if len(token) == 64:
            return token
    token = secrets.token_hex(32)
    partial = root / ".native-token.new"
    with open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
        output.write(token + "\n")
    os.replace(partial, path)
    return token


def engine_proof(token, challenge):
    return hmac.digest(token.encode(), challenge.encode("ascii"), "sha256").hex()


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
        with urlopen(f"{URL}/api/status?challenge={challenge}", timeout=2) as response:
            info = json.loads(response.read(32_768))
        if info.get("model") != "Reson8/Rumble-2-Flash":
            raise ValueError("unexpected server")
    except (OSError, URLError, ValueError, AttributeError) as error:
        raise RuntimeError(f"Another application is using {URL}. Close it and reopen Jaro.") from error
    if info.get("apiVersion") != API_VERSION:
        raise RuntimeError("Another Jaro version is running. Quit it, then reopen Jaro.")
    root = library_path()
    if info.get("libraryId") != library_id(root):
        raise RuntimeError("The running Jaro engine is using a different session library.")
    try:
        token = (root / ".native-token").read_text().strip()
    except OSError as error:
        raise RuntimeError("Could not verify the running Jaro engine.") from error
    if not hmac.compare_digest(info.get("proof", ""), engine_proof(token, challenge)):
        raise RuntimeError("Could not verify the running Jaro engine.")
    return info


class ClientLeases:
    def __init__(self):
        self.deadlines = {}
        self.streams = set()
        self.last_activity = time.monotonic()

    def renew(self, client):
        now = time.monotonic()
        self.deadlines[client] = now + LEASE_SECONDS
        self.last_activity = now

    def detach(self, client):
        self.deadlines.pop(client, None)

    @contextmanager
    def browser_connection(self):
        connection = object()
        self.streams.add(connection)
        self.last_activity = time.monotonic()
        try:
            yield
        finally:
            self.streams.discard(connection)
            self.last_activity = time.monotonic()

    def idle(self, busy):
        now = time.monotonic()
        self.deadlines = {k: v for k, v in self.deadlines.items() if v > now}
        if busy or self.streams or self.deadlines:
            self.last_activity = now
            return False
        return now - self.last_activity >= 30

def quit_engine():
    if not running_engine():
        print("Jaro is already stopped.")
        return
    request = Request(URL + "/api/quit", method="POST", headers={"Origin": URL, "X-Jard-Action": "quit"})
    with urlopen(request, timeout=3) as response:
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
    except (OSError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
