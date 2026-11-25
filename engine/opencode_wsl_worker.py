"""The standard-library OpenCode transport run inside the selected WSL distribution."""
import base64
from hashlib import sha256
from http.client import HTTPConnection, HTTPException
from ipaddress import ip_address
from itertools import islice
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
from urllib.parse import urlencode, urlsplit

MAX_REGISTRATION = 64 * 1024
MAX_REQUEST = 16 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
CHANNEL = re.compile(r"service(?:-[\w.-]{1,128})?\.json")


def registration_data(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("url"), str):
            return None
        url = urlsplit(value["url"])
        if (url.scheme != "http" or url.username is not None or url.password is not None or
                url.path not in ("", "/") or url.query or url.fragment or url.hostname is None):
            return None
        address = ip_address("127.0.0.1" if url.hostname == "localhost" else url.hostname)
        if address.is_unspecified:
            address = ip_address("127.0.0.1" if address.version == 4 else "::1")
        pid = value["pid"]
        version = value.get("version")
        password = value.get("password")
        port = 80 if url.port is None else url.port
        if (not address.is_loopback or "%" in str(address) or type(pid) is not int or not 0 < pid <= 2**31 - 1 or
                not port or (version is not None and (not isinstance(version, str) or len(version) > 160)) or
                (password is not None and not isinstance(password, str))):
            return None
        host = f"[{address}]" if address.version == 6 else str(address)
        return dict(origin=f"http://{host}:{port}", address=host, port=port, pid=pid, version=version, password=password)
    except (TypeError, ValueError, KeyError, UnicodeError):
        return None


def owns_linux_listener(pid, host, port):
    uid = os.geteuid()
    process = Path("/proc") / str(pid)
    address = ip_address(host.strip("[]"))
    suffix = "6" if address.version == 6 else ""
    # Linux represents IPv4 and IPv6 addresses as native-order 32-bit words.
    packed = address.packed
    hex_address = "".join(f"{int.from_bytes(packed[i:i + 4], sys.byteorder):08X}" for i in range(0, len(packed), 4))
    bound = {f"{hex_address}:{port:04X}", f"{'0' * len(hex_address)}:{port:04X}"}
    try:
        if process.stat().st_uid != uid:
            return False
        # Check the network namespace in which the request will connect.
        with Path(f"/proc/net/tcp{suffix}").open(encoding="ascii") as table:
            next(table, None)
            sockets = set()
            for line in table:
                parts = line.split()
                if len(parts) > 9 and parts[1] in bound and parts[3] == "0A":
                    if parts[7] != str(uid):
                        return False
                    sockets.add(f"socket:[{parts[9]}]")
        if not sockets:
            return False
        for descriptor in (process / "fd").iterdir():
            try:
                sockets.discard(os.readlink(descriptor))
                if not sockets:
                    return True
            except FileNotFoundError:
                continue
    except OSError:
        pass
    return False


def registration(directory, channel, scope):
    try:
        descriptor = os.open(directory / channel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            raw = stream.read(MAX_REGISTRATION + 1)
        data = registration_data(raw) if len(raw) <= MAX_REGISTRATION else None
        if not data or not owns_linux_listener(data["pid"], data["address"], data["port"]):
            return None
        process = Path("/proc") / str(data["pid"])
        start = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        identity = (scope, os.geteuid(), boot, start, data["origin"], data["pid"], data["version"])
        data["key"] = sha256(json.dumps(identity).encode()).hexdigest()
        data["channel"] = channel
        data["password"] = data["password"] or os.environ.get("OPENCODE_PASSWORD")
        return data
    except (OSError, IndexError, UnicodeError):
        return None


def exchange(data, method, path, *, params=None, body=None):
    if method not in ("GET", "POST", "DELETE") or not re.fullmatch(r"/api/(?:info|session(?:/[a-zA-Z0-9_-]+)*)", path):
        raise ValueError("invalid OpenCode request")
    headers = {"Accept-Encoding": "identity"}
    password = data["password"]
    if password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
    if body is not None:
        body = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    target = path + ("?" + urlencode(params) if params else "")
    conn = HTTPConnection(data["address"].strip("[]"), data["port"], timeout=1.5)
    try:
        # Send only to the process that owns the selected guest loopback listener.
        if not owns_linux_listener(data["pid"], data["address"], data["port"]):
            raise OSError("OpenCode stopped listening")
        conn.request(method, target, body=body, headers=headers)
        result = conn.getresponse()
        raw = result.read(MAX_RESPONSE + 1) if 200 <= result.status < 300 else b""
        if len(raw) > MAX_RESPONSE:
            raise ValueError("OpenCode response too large")
        return result.status, raw
    finally:
        conn.close()


def find(directory, scope, expected, channel):
    try:
        with os.scandir(directory) as entries:
            others = sorted(entry.name for entry in islice(entries, 512)
                            if entry.name != "service.json" and CHANNEL.fullmatch(entry.name))
    except OSError:
        return None
    for name in (channel,) if channel else ("service.json", *others[:15]):
        data = registration(directory, name, scope)
        if data is None or (expected and expected != data["key"]):
            continue
        try:
            status, raw = exchange(data, "GET", "/api/info")
            info = json.loads(raw) if status == 200 else None
            if isinstance(info, dict):
                info = info.get("data", info)
            if (isinstance(info, dict) and type(info.get("pid")) is int and info["pid"] == data["pid"] and
                    isinstance(info.get("version"), str) and (data["version"] is None or info["version"] == data["version"])):
                return {key: data[key] for key in ("key", "channel", "origin", "pid", "version")}
        except (OSError, ValueError, HTTPException):
            continue
    return None


def main():
    # Bound the guest even if Windows stops waiting or WSL relays cancellation late.
    signal.alarm(6)
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError("OpenCode request too large")
    command = json.loads(raw)
    scope, expected, channel = (command.get(key) for key in ("scope", "expected", "channel"))
    if channel is not None and not CHANNEL.fullmatch(channel):
        raise ValueError("invalid OpenCode registration channel")
    directory = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "opencode"
    if command["action"] == "find":
        result = find(directory, scope, expected, channel)
    elif command["action"] in ("request", "match-project"):
        data = registration(directory, channel, scope) if channel else None
        if data is None or data["key"] != expected:
            raise OSError("The selected OpenCode service is unavailable")
        if command["action"] == "match-project":
            # Included by the Windows adapter so lookups use the guest filesystem.
            result = match_project_paths(command["target"], command["directories"])
        else:
            status, body = exchange(data, command["method"], command["path"], params=command.get("params"), body=command.get("body"))
            result = [status, base64.b64encode(body).decode("ascii")]
    else:
        raise ValueError("invalid OpenCode operation")
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
