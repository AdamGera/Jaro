"""Track Pulse microphones and independent playback routes during a meeting."""

import asyncio
from collections import deque
from contextlib import suppress
import json

import numpy as np

from linux_capture import default_computer_audio, default_microphone, pulse_info
from portable_capture import CaptureError, PortableRecording, SAMPLE_RATE


PACKET_FRAMES = SAMPLE_RATE // 50
CAPTURE_LAG = SAMPLE_RATE // 4
JITTER = SAMPLE_RATE // 8
ROUTE_INTERVAL = 1


def meeting_monitors(sinks, default):
    monitors = {}
    for sink in sinks:
        if not isinstance(sink, dict):
            continue
        name, source = sink.get("name"), sink.get("monitor_source")
        if not isinstance(name, str) or not isinstance(source, str) or source in ("", "n/a", "default"):
            continue
        properties = sink.get("properties")
        monitors[name] = (source, isinstance(properties, dict) and bool(properties.get("device.api")))
    # A virtual effect or combined sink can forward the same signal to hardware.
    hardware = [source for source, device_backed in monitors.values() if device_backed]
    return tuple(dict.fromkeys(hardware or ([monitors[default][0]] if default in monitors else [])))


async def read_monitors():
    data = json.loads(await pulse_info("--format=json", "list", "sinks"))
    if not isinstance(data, list):
        raise ValueError("Expected Linux output devices.")
    default = (await pulse_info("get-default-sink")).strip()
    return meeting_monitors(data, default)


class Source:
    def __init__(self, name, clock, *, optional=False):
        self.name = name
        self.clock = clock
        self.optional = optional
        self.packets = deque(maxlen=200)
        self.through = None
        self.received = False
        self.task = asyncio.create_task(self.read())

    def push(self, data):
        samples = np.frombuffer(data, dtype="<i2")
        arrived = self.clock() - len(samples)
        # Small dispatch jitter should not insert gaps into adjacent packets.
        # A delayed or restarted stream is placed back on the meeting clock.
        if self.through is not None and abs(arrived - self.through) <= JITTER:
            start = self.through
        else:
            start = max(arrived, self.through or 0)
        self.through = start + len(samples)
        self.packets.append((start, samples))
        self.received = True

    async def read(self):
        delay = 0.5
        while True:
            process = None
            self.through = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-v", "error", "-nostdin", "-probesize", "32", "-analyzeduration", "0",
                    "-f", "pulse", "-sample_rate", str(SAMPLE_RATE), "-channels", "1",
                    "-fragment_size", str(PACKET_FRAMES * 2), "-i", self.name, "-flush_packets", "1",
                    "-f", "s16le", "pipe:1",
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                while True:
                    read = process.stdout.readexactly(PACKET_FRAMES * 2)
                    data = await (read if self.optional else asyncio.wait_for(read, timeout=3))
                    self.push(data)
                    delay = 0.5
            except (OSError, EOFError, TimeoutError):
                pass
            finally:
                if process is not None:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    await process.wait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)

    def add(self, target, start):
        end = start + len(target)
        while self.packets:
            at, samples = self.packets[0]
            last = at + len(samples)
            if at >= end:
                break
            left, right = max(start, at), min(end, last)
            if left < right:
                target[left - start:right - start] += samples[left - at:right - at]
            if last > end:
                break
            self.packets.popleft()


class LinuxMeetingRecording(PortableRecording):
    def __init__(self, key, path, mode, **kwargs):
        super().__init__(key, path, mode, **kwargs)
        self.mic = None
        self.outputs = {}
        self.end_frame = None

    def clock(self):
        return round((asyncio.get_running_loop().time() - self.epoch) * SAMPLE_RATE)

    async def spawn(self):
        self.epoch = asyncio.get_running_loop().time()

    async def watch_routes(self):
        while self.end_frame is None:
            try:
                microphone = await default_microphone()
            except CaptureError:
                pass
            else:
                if self.mic is None or self.mic.name != microphone:
                    if self.mic:
                        await self.close_sources((self.mic,))
                    self.mic = Source(microphone, self.clock)
            try:
                monitors = await read_monitors()
            except (CaptureError, ValueError):
                monitors = tuple(self.outputs)
                if not monitors:
                    with suppress(CaptureError):
                        monitors = (await default_computer_audio(),)
            for name in monitors:
                if name not in self.outputs:
                    self.outputs[name] = Source(name, self.clock, optional=True)
            await self.close_sources([self.outputs.pop(name) for name in self.outputs.keys() - set(monitors)])
            await asyncio.sleep(ROUTE_INTERVAL)

    @staticmethod
    async def close_sources(sources):
        tasks = [source.task for source in sources]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def capture_blocks(self, block_bytes, frame_bytes):
        watcher = asyncio.create_task(self.watch_routes())
        try:
            # Silent playback monitors do not emit packets until audio plays;
            # begin as soon as the microphone (or any output) delivers audio.
            while not ((self.mic and self.mic.received) or any(source.received for source in self.outputs.values())):
                if self.end_frame is not None:
                    return
                await asyncio.sleep(0.02)
            frames = block_bytes // frame_bytes
            written = 0
            limit = self.seconds * SAMPLE_RATE
            while written < limit:
                if self.end_frame is not None:
                    limit = min(limit, self.end_frame)
                count = min(frames, limit - written)
                if self.clock() - CAPTURE_LAG < written + count:
                    await asyncio.sleep(0.02)
                    continue
                system = np.zeros(count, dtype=np.int32)
                for source in self.outputs.values():
                    source.add(system, written)
                mic = np.zeros(count, dtype=np.int32)
                if self.mic:
                    self.mic.add(mic, written)
                interleaved = np.empty((count, 2), dtype="<i2")
                interleaved[:, 0] = mic
                interleaved[:, 1] = np.clip(system, -32768, 32767)
                written += count
                if count:
                    yield interleaved.tobytes()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await self.close_sources([source for source in [self.mic, *self.outputs.values()] if source])

    async def stop(self):
        if not self.worker:
            return
        if self.end_frame is None:
            self.end_frame = self.clock()
        await self.worker
