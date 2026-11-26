"""Discover and authenticate desktop commands for the current Windows user and library."""
from contextlib import closing
import hmac
from http.client import HTTPConnection, HTTPException
import json
import os
import re
from uuid import UUID

from engine_lifecycle import HOST, library_id
from sdk_discovery import SDK_VERSION
from sdk_embedded import verify_service


ROUTE = "/api/portable/dictation/command"
MEETING_TRASH_ROUTE = "/api/portable/meetings/trash"
MEETING_COMMAND_ROUTE = "/api/portable/meetings/command"
ANNOTATION_COMMAND_ROUTE = "/api/portable/annotations/command"
ONBOARDING_ROUTE = "/api/portable/onboarding/command"
REGISTRY = r"Software\Jaro\Dictation"


def authorized(record, header):
    return record is not None and hmac.compare_digest(header.encode(), ("Bearer " + record["token"]).encode())


def identity(root):
    return library_id(os.path.normcase(root))


def publish(root, sdk):
    import winreg

    record = {key: sdk[key] for key in ("apiVersion", "pid", "port")}
    record["libraryId"] = identity(root)
    record["token"] = hmac.digest(bytes.fromhex(sdk["token"]), b"jaro:windows:dictation:v1", "sha256").hex()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY, access=winreg.KEY_SET_VALUE) as registry:
        winreg.SetValueEx(registry, record["libraryId"], 0, winreg.REG_SZ, json.dumps(record))
    return record


def remove(record):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as registry:
        key = record["libraryId"]
        if winreg.QueryValueEx(registry, key) == (json.dumps(record), winreg.REG_SZ):
            winreg.DeleteValue(registry, key)


def discover(root):
    import winreg

    key = identity(root)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0, winreg.KEY_QUERY_VALUE) as registry:
            payload, kind = winreg.QueryValueEx(registry, key)
        if kind != winreg.REG_SZ or len(payload) > 2048:
            raise ValueError("invalid discovery record")
        record = json.loads(payload)
        port = record["port"]
        if (type(record["apiVersion"]) is not int or record["apiVersion"] != SDK_VERSION
                or type(record["pid"]) is not int or record["pid"] <= 1
                or type(port) is not int or not 1 <= port <= 65535 or record["libraryId"] != key
                or not re.fullmatch(r"[0-9a-f]{64}", record["token"])):
            raise ValueError("invalid discovery record")
        return record
    except (OSError, ValueError, KeyError, TypeError) as failure:
        raise RuntimeError("Open Jaro for this library to use desktop commands.") from failure


def dispatch(root, action, *, raw=False, printed=False):
    return request_command(root, ROUTE, {"action": action, "raw": raw, "printed": printed},
                           timeout=70 if printed else 10)


def dispatch_meeting_trash(root, identifier):
    return request_command(root, MEETING_TRASH_ROUTE, {"id": str(UUID(str(identifier)))}, timeout=125)


def dispatch_meeting(root, action, *, title=""):
    return request_command(root, MEETING_COMMAND_ROUTE, {"action": action, "title": title}, timeout=2 if action == "live" else 60)


def dispatch_annotation(root, action, **options):
    return request_command(root, ANNOTATION_COMMAND_ROUTE, {"action": action, **options},
                           timeout=180 if action in ("stop", "toggle") else 20,
                           limit=2 * 1024 * 1024 if action == "destinations" else 1024 * 1024)


def dispatch_onboarding(root):
    return request_command(root, ONBOARDING_ROUTE, {}, timeout=5)


def request_command(root, route, payload, *, timeout, limit=1024 * 1024):
    record = discover(root)
    port = record["port"]
    origin = f"http://{HOST}:{port}"
    try:
        with closing(HTTPConnection(HOST, port, timeout=3)) as connection:
            verify_service(connection, record, identify_path=ROUTE)
            connection.sock.settimeout(timeout)
            connection.request("POST", route, json.dumps(payload), {
                "Origin": origin, "Content-Type": "application/json", "Authorization": "Bearer " + record["token"],
            })
            with connection.getresponse() as response:
                status = response.status
                result = json.loads(response.read(limit))
        if not isinstance(result, dict):
            raise ValueError("invalid command response")
    except (OSError, HTTPException, RuntimeError, ValueError, AttributeError) as failure:
        raise RuntimeError("Could not connect to the running Jaro workspace.") from failure
    if status != 200:
        detail = result.get("detail")
        raise RuntimeError(detail if isinstance(detail, str) else "Jaro could not perform the desktop command.")
    return result
