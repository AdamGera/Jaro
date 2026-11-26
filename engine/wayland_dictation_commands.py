"""Connect local CLI commands and desktop key bindings to the Linux workspace."""
import asyncio
from contextlib import suppress
import json
import os
import shlex
import socket
import struct
import sys
from uuid import UUID

from engine_lifecycle import library_id
from engine_paths import ENGINE_ROOT, library_path


ACTIONS = frozenset({"start", "stop", "toggle", "cancel", "paste-last"})
PRINT_ACTION = "stop-print"
RAW_ACTIONS = {"start-raw": "start", "toggle-raw": "toggle"}
GNOME_ACTIONS = {"gnome-toggle": "toggle", "gnome-paste-last": "paste-last", "gnome-cancel": "cancel",
                 "gnome-annotate": "annotate"}
PEER_CREDENTIALS = struct.Struct("3i")
TRASH_MEETING = "trash-meeting "
MEETING_COMMAND = "meeting "
ANNOTATION_COMMAND = "annotation "


def address(root):
    return f"\0jaro-dictation-{os.geteuid()}-{library_id(root)}"


def command_prefix(root, *, gnome=False):
    environment = ("env", f"RUMBLE_DATA_DIR={root}")
    from linux_direct import managed_root

    direct = managed_root(ENGINE_ROOT.parent)
    if direct or str(ENGINE_ROOT) == "/usr/lib/jaro/engine":
        cli = (str(direct / "jardctl") if direct else "/usr/bin/jardctl", "dictation")
        if gnome:
            environment += ("JARO_GNOME_SHORTCUT=1",)
    elif gnome:
        cli = (sys.executable, str(ENGINE_ROOT / "wayland_dictation_commands.py"))
    else:
        cli = (sys.executable, str(ENGINE_ROOT / "jardctl.py"), "dictation")
    return shlex.join((*environment, *cli))


def same_user(connection):
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEER_CREDENTIALS.size)
    _, uid, _ = PEER_CREDENTIALS.unpack(credentials)
    return uid == os.geteuid()


async def listen(root, perform, *, trash_meeting=None, meeting_command=None, annotation_command=None, onboarding=None):
    from linux_capture import CaptureError

    async def request(reader, writer):
        try:
            if not same_user(writer.get_extra_info("socket")):
                return
            async with asyncio.timeout(2):
                command = (await reader.readline()).decode("ascii")
            if not command.endswith("\n"):
                return
            action = command[:-1]
            try:
                if action.startswith(TRASH_MEETING) and trash_meeting is not None:
                    identifier = action.removeprefix(TRASH_MEETING)
                    if str(UUID(identifier)) != identifier:
                        return
                    result = await trash_meeting(identifier)
                elif action.startswith(MEETING_COMMAND) and meeting_command is not None:
                    result = await meeting_command(json.loads(action.removeprefix(MEETING_COMMAND)))
                elif action.startswith(ANNOTATION_COMMAND) and annotation_command is not None:
                    result = {"annotation": await annotation_command(json.loads(action.removeprefix(ANNOTATION_COMMAND)))}
                elif action == "onboarding-reset" and onboarding is not None:
                    result = await onboarding()
                elif action == PRINT_ACTION or action in ACTIONS or action in GNOME_ACTIONS or action in RAW_ACTIONS:
                    if perform is None:
                        raise CaptureError("System dictation is unavailable on this desktop.")
                    value = await perform(action)
                    result = value if action == PRINT_ACTION else {"status": value}
                else:
                    return
            except CaptureError as failure:
                result = {"error": str(failure)}
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (OSError, TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    # A full Unicode terminal path can expand to almost 48 KiB when JSON-escaped.
    return await asyncio.start_unix_server(request, address(root), limit=64 * 1024)


def dispatch(root, action, *, gnome=False, raw=False, printed=False):
    if printed:
        if raw or gnome or action != "stop":
            raise ValueError("Use print with stop.")
        action = PRINT_ACTION
    elif raw:
        if gnome or action + "-raw" not in RAW_ACTIONS:
            raise ValueError("Choose start or toggle for a raw dictation.")
        action += "-raw"
    elif gnome and "gnome-" + action in GNOME_ACTIONS:
        action = "gnome-" + action
    elif action not in ACTIONS:
        raise ValueError("Choose a Jaro desktop shortcut command.")
    return request_command(root, action, timeout=70 if printed else 5,
                           limit=1024 * 1024 if printed else 2048, purpose="use desktop dictation commands")


def dispatch_meeting_trash(root, identifier):
    return request_command(root, TRASH_MEETING + str(UUID(str(identifier))), timeout=125, limit=2048,
                           purpose="move a meeting to Trash")


def dispatch_meeting(root, action, *, title=""):
    command = MEETING_COMMAND + json.dumps({"action": action, "title": title})
    return request_command(root, command, timeout=2 if action == "live" else 60, limit=8192, purpose="use meeting commands")


def dispatch_annotation(root, action, **options):
    command = ANNOTATION_COMMAND + json.dumps({"action": action, **options})
    limit = 2 * 1024 * 1024 if action == "destinations" else 64 * 1024
    reply = request_command(root, command, timeout=180 if action in ("stop", "toggle") else 20, limit=limit,
                            purpose="use annotation commands")
    if not isinstance(reply.get("annotation"), dict):
        raise RuntimeError("Jaro returned an invalid annotation reply.")
    return reply["annotation"]


def dispatch_onboarding(root):
    return request_command(root, "onboarding-reset", timeout=5, limit=2048, purpose="replay dictation setup")


def request_command(root, action, *, timeout, limit, purpose):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(address(root))
            if not same_user(connection):
                raise ValueError("Unexpected socket owner")
            connection.settimeout(timeout)
            connection.sendall(action.encode("ascii") + b"\n")
            with connection.makefile("rb") as reply:
                result = json.loads(reply.readline(limit))
            if not isinstance(result, dict):
                raise ValueError("Invalid dictation reply.")
    except (OSError, ValueError) as failure:
        raise RuntimeError(f"Open Jaro for this library to {purpose}.") from failure
    if "error" in result:
        raise RuntimeError(result["error"])
    return result


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in GNOME_ACTIONS.values():
        print("Choose a GNOME shortcut.", file=sys.stderr)
        return 2
    try:
        dispatch(library_path(), sys.argv[1], gnome=True)
    except (RuntimeError, ValueError) as failure:
        print(str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
