"""Connect desktop key bindings to the running Wayland dictation workspace."""
import asyncio
from contextlib import suppress
import json
import os
import shlex
import socket
import struct
import sys

from engine_lifecycle import library_id
from engine_paths import ENGINE_ROOT
from linux_capture import CaptureError


ACTIONS = frozenset({"start", "stop", "toggle", "cancel", "paste-last"})
PEER_CREDENTIALS = struct.Struct("3i")


def address(root):
    return f"\0jaro-dictation-{os.geteuid()}-{library_id(root)}"


def command_prefix(root):
    return shlex.join(("env", f"RUMBLE_DATA_DIR={root}", sys.executable, str(ENGINE_ROOT / "jardctl.py"), "dictation"))


def same_user(connection):
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEER_CREDENTIALS.size)
    _, uid, _ = PEER_CREDENTIALS.unpack(credentials)
    return uid == os.geteuid()


async def listen(root, perform):
    async def request(reader, writer):
        try:
            if not same_user(writer.get_extra_info("socket")):
                return
            async with asyncio.timeout(2):
                command = (await reader.readline()).decode("ascii")
            if not command.endswith("\n"):
                return
            action = command[:-1]
            if action not in ACTIONS:
                return
            try:
                result = {"status": await perform(action)}
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

    return await asyncio.start_unix_server(request, address(root), limit=32)


def dispatch(root, action):
    if action not in ACTIONS:
        raise ValueError("Choose a Jaro dictation command.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(address(root))
            if not same_user(connection):
                raise ValueError("Unexpected socket owner")
            connection.sendall(action.encode("ascii") + b"\n")
            with connection.makefile("rb") as reply:
                result = json.loads(reply.readline(2048))
    except (OSError, ValueError) as failure:
        raise RuntimeError("Open Jaro for this library to use desktop dictation commands.") from failure
    if "error" in result:
        raise RuntimeError(result["error"])
    return result
