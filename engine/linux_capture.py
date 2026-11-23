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


async def audio_stream_apps(kind, sources=None):
    try:
        streams = json.loads(await pulse_info("--format=json", "list", kind))
        if not isinstance(streams, list):
            raise ValueError("Expected a list of audio streams.")
        binaries = set()
        for stream in streams:
            if stream.get("corked") not in (False, "false", "no"):
                continue
            if sources is not None and str(stream.get("source")) not in sources:
                continue
            properties = stream.get("properties") or {}
            pid = str(properties.get("application.process.id", ""))
            if not pid.isascii() or not pid.isdigit():
                continue
            process = Path("/proc") / pid
            with suppress(OSError):
                if process.stat().st_uid == os.getuid():
                    executable = (process / "exe").readlink().name
                    reported = properties.get("application.process.binary")
                    command = (process / "comm").read_text().rstrip("\n")
                    binaries.add(command if command == reported else executable)
        return binaries
    except (AttributeError, ValueError):
        raise CaptureError("Jaro could not identify the active audio streams.") from None


async def microphone_users():
    sources = await microphone_sources()
    try:
        inputs = {str(item["index"]) for item in sources}
    except KeyError:
        raise CaptureError("Jaro could not identify an active microphone.") from None
    return await audio_stream_apps("source-outputs", inputs)


async def playback_users():
    return await audio_stream_apps("sink-inputs")


async def default_microphone():
    source = await default_device("source")
    if not microphone_source(source):
        raise CaptureError("Select a microphone in your system sound settings, then try again.")
    return source["name"]


async def default_computer_audio():
    sink = await default_device("sink")
    monitor = sink.get("monitor_source") if sink else None
    if not isinstance(monitor, str) or monitor in ("", "n/a"):
        raise CaptureError("Select an audio output in your system sound settings, then try again.")
    return monitor


async def capture_args(mode, microphone="", *, dictation=False):
    if mode != "both":
        if mode == "mic" and microphone:
            if not any(item["name"] == microphone for item in await microphone_sources()):
                raise CaptureError("The selected microphone is unavailable.")
            source = microphone
        else:
            source = await (default_microphone() if mode == "mic" else default_computer_audio())
        if dictation:
            return ("-f", "pulse", "-sample_rate", str(SAMPLE_RATE), "-channels", "1",
                    "-fragment_size", str(VOICE_FRAME * FRAME_BYTES),
                    "-analyzeduration", "0", "-probesize", "32", "-i", source, "-flush_packets", "1")
        return ("-f", "pulse", "-i", source, "-ac", "1", "-ar", str(SAMPLE_RATE))

    microphone, monitor = await asyncio.gather(default_microphone(), default_computer_audio())
    input_options = ("-thread_queue_size", "128", "-analyzeduration", "0", "-probesize", "32", "-f", "pulse")
    # Use the Pulse packet clocks to align the inputs before joining them into one pipe.
    resample = f"aresample={SAMPLE_RATE}:async=1000:first_pts=0,aformat=channel_layouts=mono"
    return ("-copyts", "-start_at_zero",
            *input_options, "-i", microphone,
            *input_options, "-isync", "0", "-i", monitor,
            "-filter_complex", f"[0:a]{resample}[mic];[1:a]{resample}[system];"
            "[mic][system]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR")


class LinuxRecording(PortableRecording):
    async def spawn(self):
        options = await capture_args(self.mode, self.microphone, dictation=self.dictation)
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", *options, "-t", str(self.seconds),
            "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
