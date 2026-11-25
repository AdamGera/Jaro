"""Start the WASAPI helper shipped beside the Windows desktop host."""
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import struct
import subprocess

from portable_capture import CaptureError, PortableRecording
from portable_call_pages import BrowserPage, page_identity, select_call


def helper_path():
    configured = os.environ.get("JARO_WINDOWS_CAPTURE")
    if configured:
        path = Path(configured)
        if path.is_absolute() and path.is_file():
            return path
    return None


async def capture_inventory(command, *arguments, partial=False, timeout=None):
    helper = helper_path()
    if helper is None:
        raise CaptureError("Jaro’s Windows audio recorder is unavailable.")
    try:
        process = await asyncio.create_subprocess_exec(
            helper, command, *arguments, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        reply = asyncio.create_task(process.communicate())
        try:
            ready, _ = await asyncio.wait((reply,), timeout=timeout if timeout is not None else 2 if partial else 3)
            if not ready and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            output, _ = await reply
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if partial or process.returncode == 0:
            return output
    except OSError:
        pass
    raise CaptureError("Jaro could not read the Windows audio service.")


async def microphone_inputs():
    try:
        return parse_microphones(await capture_inventory("microphones"))
    except (UnicodeError, ValueError):
        raise CaptureError("Jaro could not read the available microphones.") from None


async def audio_stream_apps(command):
    try:
        return set((await capture_inventory(command)).decode().splitlines())
    except UnicodeError:
        raise CaptureError("Jaro could not identify the active audio streams.") from None


async def microphone_users():
    return await audio_stream_apps("microphone-users")


async def playback_users():
    return await audio_stream_apps("playback-users")


async def browser_call_pages(sources, _microphones=None):
    # Each executable is resolved to a source before the native helper runs.
    executables = [(source, f"{binary}.exe") for source, binaries in sources.items() for binary in binaries]
    if not executables:
        return {}
    output = await capture_inventory("browser-pages", *(binary for _, binary in executables), partial=True)
    return parse_browser_calls(output, tuple(source for source, _ in executables))


def parse_browser_calls(output, sources):
    pages = {}
    complete = output.endswith(b"done\n")
    for line in output.split(b"\n"):
        fields = line.split(b"\t", 3)
        if len(fields) != 4:
            continue
        kind, index, window, text = fields
        if (kind not in (b"T", b"U") or len(index) > 2 or not index.isdigit()
                or window not in (b"0", b"1", b"2", b"3", b"4", b"5")):
            continue
        browser = int(index)
        if browser >= len(sources):
            continue
        try:
            value = text.decode("utf-8")
        except UnicodeError:
            continue
        source = sources[browser]
        fields = pages.setdefault(source, {}).setdefault((browser, window), {})
        fields["title" if kind == b"T" else "address"] = value
    result = {}
    for source, windows in pages.items():
        candidates = [BrowserPage(**fields) for fields in windows.values()]
        # An unread or unrelated window could own the browser's microphone.
        identified = complete and all(page_identity(page.address) for page in candidates)
        call = select_call(source, candidates, identified)
        if call:
            result[source] = call
    return result


def parse_microphones(output):
    result = []
    for line in output.decode("utf-8").rstrip("\n").split("\n") if output else ():
        default, device, label = line.split("\t", 2)
        if default not in ("0", "1") or not device or not label:
            raise ValueError("Invalid microphone description.")
        result.append({"id": device, "label": label, "default": default == "1"})
    return result


class WindowsRecording(PortableRecording):
    helper_mode = None

    async def spawn(self, *options, stderr=asyncio.subprocess.DEVNULL):
        helper = helper_path()
        if helper is None:
            raise CaptureError("Jaro’s Windows audio recorder is missing. Rebuild the desktop host.")
        mode = "dictation" if self.dictation else self.helper_mode or self.mode
        if self.microphone and mode in ("mic", "dictation", "both"):
            options = (*options, "device", self.microphone)
        return await asyncio.create_subprocess_exec(
            helper, mode, str(self.seconds), *options, creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
        )

    def request_stop(self):
        self.process.stdin.close()

    async def collect(self):
        if self.dictation:
            try:
                stamp = await self.process.stdout.readexactly(8)
            except asyncio.IncompleteReadError:
                self.ready.set()
                return
            # WASAPI and CPython's Windows monotonic clock both use QPC.
            self.pcm_epoch = struct.unpack("<q", stamp)[0] / 10_000_000
        await super().collect()


class WindowsMeetingRecording(WindowsRecording):
    helper_mode = "meeting"

    def __init__(self, key, path, mode, *, call_binaries=(), **options):
        super().__init__(key, path, mode, **options)
        self.call_binaries = tuple(f"{binary}.exe" for binary in call_binaries)

    async def spawn(self):
        arguments = ("call", *self.call_binaries) if self.call_binaries else ()
        return await super().spawn(*arguments)


class WindowsScreenAudio(WindowsRecording):
    started_ticks = None

    def __init__(self, *args, **options):
        super().__init__(*args, **options)
        self.unavailable = set()

    async def spawn(self):
        return await super().spawn("clock", stderr=asyncio.subprocess.PIPE)

    async def read_source_status(self):
        while line := await self.process.stderr.readline():
            fields = line.split()
            if len(fields) != 3 or fields[0] != b"jaro-screen-audio" or fields[1] not in (b"mic", b"system"):
                continue
            source = fields[1].decode("ascii")
            if fields[2] == b"unavailable":
                self.unavailable.add(source)
            elif fields[2] == b"ready":
                self.unavailable.discard(source)

    async def collect(self):
        status = asyncio.create_task(self.read_source_status())
        try:
            try:
                stamp = await self.process.stdout.readexactly(8)
                self.started_ticks, = struct.unpack("<q", stamp)
            except asyncio.IncompleteReadError:
                self.ready.set()
            else:
                await super().collect()
        finally:
            status.cancel()
            await asyncio.gather(status, return_exceptions=True)
