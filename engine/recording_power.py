"""Keep the computer awake for the lifetime of a portable recording."""

import asyncio
from contextlib import suppress
from functools import partial
import logging
import os
import sys


logger = logging.getLogger(__name__)


async def inhibit_linux(reason):
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.errors import DBusError

    from linux_desktop_portal import close_abandoned_reply, close_descriptors

    try:
        bus = MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True)
    except (DBusError, OSError, ValueError):
        return None
    try:
        await asyncio.wait_for(bus.connect(), .5)
        for scope in ("idle:sleep", "idle"):
            call = asyncio.create_task(bus.call(Message(
                destination="org.freedesktop.login1", path="/org/freedesktop/login1",
                interface="org.freedesktop.login1.Manager", member="Inhibit",
                signature="ssss", body=[scope, "Jaro", reason, "block"],
            )))
            try:
                reply = await asyncio.wait_for(asyncio.shield(call), .5)
            except (TimeoutError, asyncio.CancelledError):
                call.add_done_callback(close_abandoned_reply)
                raise
            if reply is None:
                return None
            try:
                if reply.message_type == MessageType.METHOD_RETURN and reply.signature == "h":
                    index, = reply.body
                    if 0 <= index < len(reply.unix_fds):
                        return partial(os.close, os.dup(reply.unix_fds[index]))
            finally:
                close_descriptors(reply)
    except (DBusError, EOFError, OSError, TimeoutError, ValueError):
        return None
    finally:
        bus.disconnect()
    return None


def inhibit_windows(reason):
    import ctypes
    from ctypes import wintypes

    class DetailedReason(ctypes.Structure):
        _fields_ = [("module", wintypes.HMODULE), ("id", wintypes.ULONG), ("count", wintypes.ULONG),
                    ("strings", ctypes.POINTER(wintypes.LPWSTR))]

    class Reason(ctypes.Union):
        _fields_ = [("simple", wintypes.LPWSTR), ("detailed", DetailedReason)]

    class ReasonContext(ctypes.Structure):
        _fields_ = [("version", wintypes.ULONG), ("flags", wintypes.DWORD), ("reason", Reason)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.PowerCreateRequest.argtypes = [ctypes.POINTER(ReasonContext)]
    kernel.PowerCreateRequest.restype = wintypes.HANDLE
    for action in (kernel.PowerSetRequest, kernel.PowerClearRequest):
        action.argtypes = [wintypes.HANDLE, ctypes.c_int]
        action.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    simple_string = 1  # POWER_REQUEST_CONTEXT_SIMPLE_STRING
    system_required = 1  # PowerRequestSystemRequired
    request = kernel.PowerCreateRequest(ctypes.byref(ReasonContext(0, simple_string, Reason(simple=reason))))
    if not request or request == ctypes.c_void_p(-1).value:
        return None
    if not kernel.PowerSetRequest(request, system_required):
        kernel.CloseHandle(request)
        return None

    def release():
        kernel.PowerClearRequest(request, system_required)
        kernel.CloseHandle(request)

    return release


def keep_awake_until(recording, reason, *, delay=0, while_valid=None):
    if sys.platform not in ("linux", "win32"):
        return None
    return asyncio.create_task(hold_during_recording(recording, reason, delay, while_valid))


async def hold_during_recording(recording, reason, delay, while_valid):
    if delay:
        await asyncio.wait((recording,), timeout=delay)
    if recording.done() or (while_valid is not None and not while_valid()):
        return
    release = None
    try:
        if sys.platform == "linux":
            release = await inhibit_linux(reason)
        else:
            release = await asyncio.to_thread(inhibit_windows, reason)
        if release is not None:
            await asyncio.wait((recording,))
    except (AttributeError, ImportError, OSError):
        logger.debug("Could not prevent idle sleep during recording.", exc_info=True)
    finally:
        if release is not None:
            with suppress(OSError):
                release()
