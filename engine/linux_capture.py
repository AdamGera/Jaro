"""Capture Linux audio through the local Pulse server."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path

from portable_capture import CaptureError, FRAME_BYTES, SAMPLE_RATE, VOICE_FRAME, PortableRecording


async def pulse_info(*arguments):
    try:
        process = await asyncio.create_subprocess_exec(
            "pactl", *arguments, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 2)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if process.returncode == 0:
            return output.decode()
    except (OSError, TimeoutError, UnicodeError):
        pass
    raise CaptureError("Jaro could not reach the Linux audio service.")


async def default_device(kind):
    name = (await pulse_info(f"get-default-{kind}")).strip()
    try:
        devices = json.loads(await pulse_info("--format=json", "list", kind + "s"))
    except ValueError:
        return None
    if not name or not isinstance(devices, list):
        return None
    return next((item for item in devices if isinstance(item, dict) and item.get("name") == name), None)


def microphone_source(source):
    name = source.get("name") if isinstance(source, dict) else None
    return (isinstance(name, str) and bool(name) and not name.endswith(".monitor")
            and (source.get("properties") or {}).get("device.class") != "monitor"
            and source.get("monitor_of_sink") in (None, "n/a", 4294967295))


async def microphone_sources():
    try:
        sources = json.loads(await pulse_info("--format=json", "list", "sources"))
        if not isinstance(sources, list):
            raise ValueError("Expected a list of Pulse sources.")
        return [source for source in sources if microphone_source(source)]
    except (ValueError, AttributeError):
        raise CaptureError("Jaro could not read the available microphones.") from None


async def microphone_inputs():
    sources = await microphone_sources()
    try:
        default = (await pulse_info("get-default-source")).strip()
    except CaptureError:
        default = ""
    return [{"id": source["name"], "label": source.get("description") or source["name"],
             "default": source["name"] == default} for source in sources]


async def audio_stream_processes(kind, sources=None):
    try:
        streams = json.loads(await pulse_info("--format=json", "list", kind))
        if not isinstance(streams, list):
            raise ValueError("Expected a list of audio streams.")
        binaries = {}
        for stream in streams:
            if stream.get("corked") not in (False, "false", "no"):
                continue
            if sources is not None and str(stream.get("source")) not in sources:
                continue
            properties = stream.get("properties") or {}
            pid = str(properties.get("application.process.id", ""))
            if not pid.isascii() or not pid.isdigit() or len(pid) > 10 or int(pid) <= 0:
                continue
            process = Path("/proc") / pid
            with suppress(OSError):
                if process.stat().st_uid == os.getuid():
                    executable = (process / "exe").readlink().name
                    reported = properties.get("application.process.binary")
                    command = (process / "comm").read_text().rstrip("\n")
                    binary = command if command == reported else executable
                    binaries.setdefault(binary, set()).add(int(pid))
        return binaries
    except (AttributeError, ValueError):
        raise CaptureError("Jaro could not identify the active audio streams.") from None


async def microphone_users():
    sources = await microphone_sources()
    try:
        inputs = {str(item["index"]) for item in sources}
    except KeyError:
        raise CaptureError("Jaro could not identify an active microphone.") from None
    return await audio_stream_processes("source-outputs", inputs)


async def playback_users():
    return set(await audio_stream_processes("sink-inputs"))


async def default_microphone():
    source = await default_device("source")
    if not microphone_source(source):
        raise CaptureError("Select a microphone in your system sound settings, then try again.")
    return source["name"]


async def resolve_microphone(selected):
    if not selected:
        return await default_microphone()
    if not any(source["name"] == selected for source in await microphone_sources()):
        raise CaptureError("The selected microphone is unavailable.")
    return selected


async def default_computer_audio():
    sink = await default_device("sink")
    monitor = sink.get("monitor_source") if sink else None
    if not isinstance(monitor, str) or monitor in ("", "n/a"):
        raise CaptureError("Select an audio output in your system sound settings, then try again.")
    return monitor


async def capture_args(mode, microphone="", *, dictation=False):
    if mode not in ("mic", "system"):
        raise ValueError("Single-source Linux capture requires microphone or computer audio.")
    source = await (resolve_microphone(microphone) if mode == "mic" else default_computer_audio())
    return pulse_capture_options(source, dictation)


def pulse_capture_options(source, dictation):
    if dictation:
        return ("-f", "pulse", "-sample_rate", str(SAMPLE_RATE), "-channels", "1",
                "-fragment_size", str(VOICE_FRAME * FRAME_BYTES),
                "-analyzeduration", "0", "-probesize", "32", "-i", source, "-flush_packets", "1")
    return ("-f", "pulse", "-i", source, "-ac", "1", "-ar", str(SAMPLE_RATE))


class LinuxRecording(PortableRecording):
    async def spawn(self):
        if self.watch_microphone:
            self.capture_source = await resolve_microphone(self.microphone)
            options = pulse_capture_options(self.capture_source, self.dictation)
        else:
            options = await capture_args(self.mode, self.microphone, dictation=self.dictation)
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", *options, "-t", str(self.seconds),
            "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    async def collect(self):
        if not self.watch_microphone:
            await super().collect()
            return
        monitor = asyncio.create_task(self.watch_input())
        try:
            await super().collect()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def watch_input(self):
        # Pulse can move a capture to another source without ending the stream.
        while not self.stopping and self.process.returncode is None:
            await asyncio.sleep(.25)
            try:
                await pulse_info("get-source-volume", self.capture_source)
            except CaptureError:
                if not self.stopping:
                    self.interrupted = True
                    self.request_stop()
                return
