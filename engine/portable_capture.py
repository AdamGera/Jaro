"""Write live portable desktop audio to recoverable drafts."""
import asyncio
from collections import deque
from contextlib import ExitStack, aclosing, contextmanager, suppress
import os
import struct
from time import monotonic
import wave

import numpy as np

from native_recordings import recording_marker, recording_metadata, source_tracks, write_recording_metadata
from rumble_model import SAMPLE_RATE


FRAME_BYTES = 2
BLOCK_BYTES = SAMPLE_RATE * FRAME_BYTES // 10
HISTORY_BLOCKS = 30
VOICE_FRAME = SAMPLE_RATE // 50
RELEASE_BASE = SAMPLE_RATE // 5
RELEASE_CAP = 3 * RELEASE_BASE
DICTATION_STALL = 4


class CaptureError(RuntimeError):
    pass


@contextmanager
def private_wav(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as file, wave.open(file, "wb") as writer:
        writer.setparams((1, FRAME_BYTES, SAMPLE_RATE, 0, "NONE", ""))
        yield writer, file


def split_audio(block):
    channels = np.frombuffer(block, dtype="<i2").reshape(-1, 2)
    pcm = channels.astype(np.float32)
    mixed = np.rint(pcm.mean(axis=1)).astype("<i2").tobytes()
    levels = np.minimum(1, np.sqrt(np.mean(pcm ** 2, axis=0)) * (5 / 32_768))
    return mixed, channels[:, 0].tobytes(), channels[:, 1].tobytes(), tuple(map(float, levels))


class PortableRecording:
    def __init__(self, key, path, mode, *, seconds=3 * 3600, microphone=""):
        self.key = key
        self.path = path
        self.mode = mode
        self.meeting = False
        self.seconds = seconds
        self.microphone = microphone
        self.dictation = False
        self.interrupted = False
        self.stopping = False
        self.pcm_epoch = None
        self.pcm_updated = asyncio.Event()
        self.blocks = deque(maxlen=HISTORY_BLOCKS)
        self.frames = 0
        self.levels = None
        self.measure_level = False
        self.ready = asyncio.Event()
        self.process = None
        self.worker = None

    async def start(self):
        try:
            self.process = await self.spawn()
            self.worker = asyncio.create_task(self.collect())
            await asyncio.wait_for(self.ready.wait(), 5)
            if self.frames and not self.worker.done():
                return
        except (OSError, TimeoutError):
            pass
        except asyncio.CancelledError:
            await self.stop()
            raise
        with suppress(OSError):
            await self.stop()
        if not self.frames:
            clear_draft(self.path)
        label = {"mic": "microphone audio", "system": "computer audio", "both": "both audio sources"}[self.mode]
        raise CaptureError(f"Jaro could not capture {label}. Check your system sound settings and try again.")

    async def spawn(self):
        raise NotImplementedError

    def request_stop(self):
        with suppress(ProcessLookupError):
            self.process.terminate()

    async def capture_blocks(self, block_bytes, frame_bytes):
        while True:
            try:
                async with asyncio.timeout(DICTATION_STALL if self.dictation else None):
                    data = await self.process.stdout.readexactly(block_bytes)
            except TimeoutError:
                self.interrupted = not self.stopping
                return
            except asyncio.IncompleteReadError as error:
                data = error.partial[:len(error.partial) // frame_bytes * frame_bytes]
                if self.dictation and not self.stopping:
                    filled = self.frames + len(data) // frame_bytes
                    self.interrupted = filled + VOICE_FRAME < self.seconds * SAMPLE_RATE
            if data:
                yield data
            if len(data) < block_bytes:
                return

    async def collect(self):
        dual = self.mode == "both"
        frame_bytes = FRAME_BYTES * (2 if dual else 1)
        block_bytes = VOICE_FRAME * FRAME_BYTES if self.dictation else BLOCK_BYTES * (2 if dual else 1)
        if self.dictation:
            self.blocks = deque(maxlen=HISTORY_BLOCKS * 5)
        try:
            async with aclosing(self.capture_blocks(block_bytes, frame_bytes)) as blocks:
                with ExitStack() as files:
                    writer, mixed_file = files.enter_context(private_wav(self.path))
                    if dual:
                        tracks = source_tracks(self.path)
                        mic, mic_file = files.enter_context(private_wav(tracks["mic"]))
                        system, system_file = files.enter_context(private_wav(tracks["system"]))
                    async for block in blocks:
                        if dual:
                            block, microphone, computer_audio, self.levels = split_audio(block)
                            mic.writeframes(microphone)
                            system.writeframes(computer_audio)
                            mic_file.flush()
                            system_file.flush()
                        if self.measure_level and not dual:
                            samples = np.frombuffer(block, dtype="<i2").astype(np.float32)
                            self.levels = (min(1.0, float(np.sqrt(np.mean(samples ** 2))) * (5 / 32_768)),)
                        # Commit the mix last. A recovered take ends at the last committed mix frame.
                        writer.writeframes(block)
                        mixed_file.flush()
                        if self.dictation and self.pcm_epoch is None:
                            self.pcm_epoch = monotonic() - len(block) / (SAMPLE_RATE * FRAME_BYTES)
                        self.blocks.append((self.frames, block))
                        self.frames += len(block) // FRAME_BYTES
                        self.ready.set()
                        if self.dictation:
                            self.pcm_updated.set()
        finally:
            self.ready.set()
            self.pcm_updated.set()

    def recent(self, offset):
        start = min(self.frames, max(offset, self.blocks[0][0] if self.blocks else 0))
        data = b"".join(block[max(0, start - at) * FRAME_BYTES:] for at, block in self.blocks)
        return start, data

    async def stop(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.stopping = True
            self.request_stop()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()
        if self.worker is not None:
            await self.worker
        if self.dictation:
            normal = (0,) if os.name == "nt" or not self.stopping else (0, -15, 255)
            self.interrupted |= self.process.returncode not in normal

    async def finish_dictation(self, released_at):
        if self.pcm_epoch is None:
            await self.stop()
            return
        release = max(0, round((released_at - self.pcm_epoch) * SAMPLE_RATE))
        window = max(0, (release - 2 * SAMPLE_RATE) // VOICE_FRAME * VOICE_FRAME)
        deadline = released_at + .7  # Includes time for the final audio packet.
        cutoff = None
        while True:
            offset, data = self.recent(window)
            cutoff = release_cutoff(offset, data, release)
            left = deadline - monotonic()
            if cutoff is not None or self.worker.done() or left <= 0:
                break
            self.pcm_updated.clear()
            try:
                await asyncio.wait_for(self.pcm_updated.wait(), left)
            except TimeoutError:
                break
        await self.stop()
        if self.interrupted:
            return
        frames = min(self.frames, cutoff if cutoff is not None else release + RELEASE_CAP)
        if frames < self.frames:
            trim_pcm(self.path, frames)
            self.frames = frames


def release_cutoff(offset, data, release):
    """Keep 200 ms, extending up to 600 ms for a word still in progress."""
    captured = offset + len(data) // FRAME_BYTES
    base, cap = release + RELEASE_BASE, release + RELEASE_CAP
    if captured < base:
        return None
    start = max(0, (base - offset) // VOICE_FRAME)
    count = len(data) // (VOICE_FRAME * FRAME_BYTES)
    samples = np.frombuffer(data, dtype="<i2", count=count * VOICE_FRAME).reshape(-1, VOICE_FRAME)
    levels = 20 * np.log10(np.maximum(1e-9, np.std(samples, axis=1) / 32768))
    voice = []
    floor = levels[0] if len(levels) else -90
    for level in levels:
        floor = max(-90, min(level, floor + .06))
        voice.append(level > max(-55, floor + 12))
    heard = gap = 0
    for speaking in reversed(voice[:start]):
        if speaking:
            gap = 0
            heard += 1
            if heard == 5:
                break
        else:
            gap += 1
            if gap > 2:
                break
    if heard < 5:
        return base
    quiet = 0
    for index in range(start, min(count, (cap - offset) // VOICE_FRAME)):
        quiet = 0 if voice[index] else quiet + 1
        if quiet == 8:
            return max(base, offset + (index + 1) * VOICE_FRAME)
    return cap if captured >= cap else None


def trim_pcm(path, frames):
    size = frames * FRAME_BYTES
    # private_wav writes a fixed 44-byte header for mono PCM.
    with path.open("r+b") as file:
        file.seek(4)
        file.write(struct.pack("<I", size + 36))
        file.seek(40)
        file.write(struct.pack("<I", size))
        file.truncate(size + 44)


def draft_frames(path):
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (SAMPLE_RATE, 1, FRAME_BYTES):
                return 0
            return min(audio.getnframes(), max(0, path.stat().st_size - 44) // FRAME_BYTES)
    except (EOFError, OSError, wave.Error):
        return 0


def draft_tracks(path, committed_frames):
    tracks = {}
    for kind, track in source_tracks(path).items():
        frames = draft_frames(track)
        if frames < committed_frames:
            continue
        if frames > committed_frames:
            trim_pcm(track, committed_frames)
        tracks[kind] = track
    return tracks


def save_draft_metadata(path, metadata):
    fd = os.open(recording_marker(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            write_recording_metadata(file, metadata)
    except OSError:
        with suppress(OSError):
            recording_marker(path).unlink()
        raise


def draft_metadata(path):
    marker = recording_marker(path)
    if marker.is_symlink():
        return {}
    try:
        with marker.open("rb") as file:
            return recording_metadata(file)
    except OSError:
        return {}


def clear_draft(path):
    for file in (*source_tracks(path).values(), path, recording_marker(path)):
        file.unlink(missing_ok=True)
