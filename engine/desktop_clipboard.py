"""Copy explicit text on the portable desktops without sending keystrokes."""

import asyncio
from contextlib import suppress
import os
import shutil


async def copy_text(text):
    if not text.strip():
        raise ValueError("There is no text to copy.")
    if os.name == "nt":
        await asyncio.to_thread(copy_windows, text)
        return
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    tool = shutil.which("wl-copy" if wayland else "xclip")
    if not tool:
        raise OSError("The desktop clipboard helper is unavailable.")
    arguments = ("--type", "text/plain;charset=utf-8") if wayland else ("-selection", "clipboard", "-in")
    proc = await asyncio.create_subprocess_exec(tool, *arguments, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.communicate(text.encode()), 3)
        if proc.returncode != 0:
            raise OSError("The desktop refused the clipboard update.")
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def copy_windows(text):
    import ctypes as c
    from ctypes import wintypes as w
    from time import sleep

    user = c.WinDLL("user32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)

    def api(lib, name, result, *arguments):
        method = getattr(lib, name)
        method.restype, method.argtypes = result, arguments
        return method

    create = api(user, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                 c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
    destroy = api(user, "DestroyWindow", w.BOOL, w.HWND)
    opened = api(user, "OpenClipboard", w.BOOL, w.HWND)
    empty = api(user, "EmptyClipboard", w.BOOL)
    close = api(user, "CloseClipboard", w.BOOL)
    put = api(user, "SetClipboardData", w.HANDLE, w.UINT, w.HANDLE)
    alloc = api(kernel, "GlobalAlloc", w.HGLOBAL, w.UINT, c.c_size_t)
    lock = api(kernel, "GlobalLock", w.LPVOID, w.HGLOBAL)
    unlock = api(kernel, "GlobalUnlock", w.BOOL, w.HGLOBAL)
    free = api(kernel, "GlobalFree", w.HGLOBAL, w.HGLOBAL)
    window = create(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
    if not window:
        raise c.WinError(c.get_last_error())
    data = (text.replace("\x00", "") + "\x00").encode("utf-16-le")
    block = None
    try:
        block = alloc(2, len(data))  # GMEM_MOVEABLE
        pointer = lock(block) if block else None
        if not pointer:
            raise c.WinError(c.get_last_error())
        c.memmove(pointer, data, len(data))
        unlock(block)
        for _ in range(8):
            if opened(window):
                break
            sleep(.02)
        else:
            raise c.WinError(c.get_last_error())
        try:
            if not empty() or not put(13, block):  # CF_UNICODETEXT
                raise c.WinError(c.get_last_error())
            block = None  # The system now owns the block.
        finally:
            close()
    finally:
        if block:
            free(block)
        destroy(window)
