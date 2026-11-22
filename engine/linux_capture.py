"""Capture Linux audio through the local Pulse server."""
import asyncio
from contextlib import suppress
import json

from portable_capture import CaptureError, SAMPLE_RATE, PortableRecording


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


async def default_microphone():
    source = await default_device("source")
    if (not source or source["name"].endswith(".monitor") or (source.get("properties") or {}).get("device.class") == "monitor"
            or source.get("monitor_of_sink") not in (None, "n/a", 4294967295)):
        raise CaptureError("Select a microphone in your system sound settings, then try again.")
    return source["name"]


async def default_computer_audio():
    sink = await default_device("sink")
    monitor = sink.get("monitor_source") if sink else None
    if not isinstance(monitor, str) or monitor in ("", "n/a"):
        raise CaptureError("Select an audio output in your system sound settings, then try again.")
    return monitor


async def capture_args(mode):
    if mode != "both":
        source = await (default_microphone() if mode == "mic" else default_computer_audio())
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
        options = await capture_args(self.mode)
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", *options, "-t", str(self.seconds),
            "-f", "s16le", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
