"""Write live portable desktop audio to recoverable drafts."""
import asyncio
from collections import deque
from contextlib import ExitStack, contextmanager, suppress
import os
import struct
import wave

import numpy as np

from native_recordings import source_tracks
from rumble_model import SAMPLE_RATE


FRAME_BYTES = 2
BLOCK_BYTES = SAMPLE_RATE * FRAME_BYTES // 10
HISTORY_BLOCKS = 30


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
    def __init__(self, key, path, mode, *, seconds=3 * 3600):
        self.key = key
        self.path = path
        self.mode = mode
        self.seconds = seconds
        self.blocks = deque(maxlen=HISTORY_BLOCKS)
        self.frames = 0
        self.levels = None
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

    async def collect(self):
        dual = self.mode == "both"
        frame_bytes = FRAME_BYTES * (2 if dual else 1)
        block_bytes = BLOCK_BYTES * (2 if dual else 1)
        try:
            with ExitStack() as files:
                writer, mixed_file = files.enter_context(private_wav(self.path))
                if dual:
                    tracks = source_tracks(self.path)
                    mic, mic_file = files.enter_context(private_wav(tracks["mic"]))
                    system, system_file = files.enter_context(private_wav(tracks["system"]))
                while True:
                    try:
                        data = await self.process.stdout.readexactly(block_bytes)
                    except asyncio.IncompleteReadError as error:
                        data = error.partial[:len(error.partial) // frame_bytes * frame_bytes]
                    block = data
                    if data and dual:
                        block, microphone, computer_audio, self.levels = split_audio(data)
                        mic.writeframes(microphone)
                        system.writeframes(computer_audio)
                        mic_file.flush()
                        system_file.flush()
                    if block:
                        # Commit the mix last. A recovered take ends at the last committed mix frame.
                        writer.writeframes(block)
                        mixed_file.flush()
                        self.blocks.append((self.frames, block))
                        self.frames += len(block) // FRAME_BYTES
                        self.ready.set()
                    if len(data) < block_bytes:
                        break
        finally:
            self.ready.set()

    def recent(self, offset):
        start = min(self.frames, max(offset, self.blocks[0][0] if self.blocks else 0))
        data = b"".join(block[max(0, start - at) * FRAME_BYTES:] for at, block in self.blocks)
        return start, data

    async def stop(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.request_stop()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()
        if self.worker is not None:
            await self.worker


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
            pcm_size = committed_frames * FRAME_BYTES
            # private_wav writes a fixed 44-byte header for mono PCM.
            with track.open("r+b") as file:
                file.seek(4)
                file.write(struct.pack("<I", pcm_size + 36))
                file.seek(40)
                file.write(struct.pack("<I", pcm_size))
                file.truncate(pcm_size + 44)
        tracks[kind] = track
    return tracks


def clear_draft(path):
    for file in (*source_tracks(path).values(), path):
        file.unlink(missing_ok=True)
