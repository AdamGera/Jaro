"""Local Wayland data-control exchange for system-wide dictation."""
import asyncio
from contextlib import suppress
import os
from pathlib import Path


async def run_tool(*args, timeout=1.2):
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, _ = await process.communicate()
        return stdout if process.returncode == 0 else None
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


def installed_helper():
    helper = Path(os.environ.get("JARO_WAYLAND_CLIPBOARD", ""))
    return helper if helper.is_absolute() and helper.is_file() else None


async def clipboard_capabilities():
    helper = installed_helper()
    if helper is None:
        return None, False
    try:
        result = await run_tool(helper, "probe")
    except (OSError, TimeoutError):
        return None, False
    if result in (b"virtual\n", b"clipboard\n"):
        return helper, result == b"virtual\n"
    return None, False


async def paste_keycodes(group=None):
    helper = installed_helper()
    if helper is None:
        return None
    try:
        args = () if group is None else (str(group),)
        response = await run_tool(helper, "keycodes", *args, timeout=.25)
        codes = tuple(map(int, (response or b"").split()))
    except (OSError, TimeoutError, ValueError):
        return None
    if len(codes) == 4 and all(-1 <= code <= 65535 for code in codes):
        return codes
    return None


class ClipboardLease:
    def __init__(self, process):
        self.process = process
        self.status = None

    @classmethod
    async def start(cls, helper):
        process = await asyncio.create_subprocess_exec(
            helper, "paste", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024,
        )
        return cls(process)

    def owned(self):
        return self.status is not None and not self.status.done() and self.process.returncode is None

    async def offer(self, data):
        if self.status is None:
            self.status = asyncio.create_task(self.process.stdout.readline())
        elif not self.owned():
            return False
        async with asyncio.timeout(2.5):
            self.process.stdin.write(f"{len(data)}\n".encode("ascii") + data)
            await self.process.stdin.drain()
            ready = await asyncio.shield(self.status) == b"ready\n"
        if ready:
            self.status = asyncio.create_task(self.process.stdout.readline())
        return ready

    async def close(self):
        # EOF tells the helper to restore the selection only if it still owns it.
        process = self.process
        process.stdin.close()
        try:
            async with asyncio.timeout(2):
                if self.status is not None:
                    await asyncio.shield(self.status)
                await process.wait()
        except TimeoutError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
