"""Optional preprocessing for finalized system dictation audio."""

import asyncio
from contextlib import suppress
import os
from pathlib import Path
import signal
import subprocess
import sys

import numpy as np

from stupase_runner import HEADER, REVISION, checkpoints_ready


MIC_GAIN = 10 ** (6 / 20)
CLEANUP_TIMEOUT = 20
WORKER_IDLE = 90
RUNNER = Path(__file__).with_name("stupase_runner.py")
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class CleanupFailed(Exception):
    pass


def boost_microphone(pcm):
    """Apply +6 dB, with a 5 ms look-ahead envelope and smooth release."""
    audio = np.frombuffer(pcm, dtype="<f4") * np.float32(MIC_GAIN)
    if not len(audio):
        return pcm
    block = 80  # 5 ms at 16 kHz.
    peaks = np.maximum.reduceat(np.abs(audio), np.arange(0, len(audio), block))
    caps = 0.98 / np.maximum(peaks, 0.98)
    gains = np.minimum(np.r_[1, caps], np.r_[caps, 1])
    release = 1 - np.exp(-0.005 / 0.075)
    for i in range(1, len(gains)):
        gains[i] = min(gains[i], gains[i - 1] + (1 - gains[i - 1]) * release)
    if not np.all(gains == 1):
        audio *= np.interp(np.arange(len(audio)), np.arange(len(gains)) * block, gains)
    return audio.astype("<f4", copy=False).tobytes()


def replace_mixed_microphone(mixed, original, replacement):
    """Update the microphone's half of a two-source meeting mix for ASR."""
    mix, mic, enhanced = (np.frombuffer(value, dtype="<f4") for value in (mixed, original, replacement))
    return np.clip(mix + (enhanced - mic) * np.float32(0.5), -1, 1).astype("<f4", copy=False).tobytes()


class StuPASECleanup:
    def __init__(self, library):
        self.root = library / "models" / "stupase" / REVISION
        self.downloading = None
        self.error = False
        self.worker = None
        self.idle = None
        self.lock = asyncio.Lock()

    def status(self):
        if checkpoints_ready(self.root):
            return "ready"
        if self.downloading:
            return "downloading"
        return "error" if self.error else "notInstalled"

    def prepare(self, *, retry=False):
        status = self.status()
        if status == "notInstalled" or (retry and status == "error"):
            self.error = False
            self.downloading = asyncio.create_task(self.download())
        return self.status()

    async def download(self):
        child = None
        try:
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(RUNNER), "download", str(self.root),
                start_new_session=os.name != "nt", creationflags=CREATION_FLAGS,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            self.error = await child.wait() != 0
        except OSError:
            self.error = True
        finally:
            if child and child.returncode is None:
                # Stop curl with its installer and leave its partial file resumable.
                with suppress(ProcessLookupError):
                    if os.name == "nt":
                        child.terminate()
                    else:
                        os.killpg(child.pid, signal.SIGTERM)
                await child.wait()
            self.downloading = None

    async def clean(self, pcm, *, wait=True):
        if self.prepare() != "ready":
            raise CleanupFailed()
        if not wait and self.lock.locked():
            raise CleanupFailed()
        async with self.lock:
            if self.idle:
                self.idle.cancel()
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT):
                    if self.worker is None or self.worker.returncode is not None:
                        self.worker = await asyncio.create_subprocess_exec(
                            sys.executable, str(RUNNER), "serve", str(self.root),
                            creationflags=CREATION_FLAGS,
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        )
                    worker = self.worker
                    worker.stdin.write(HEADER.pack(len(pcm)) + pcm)
                    await worker.stdin.drain()
                    size, = HEADER.unpack(await worker.stdout.readexactly(HEADER.size))
                    if size != len(pcm):
                        raise CleanupFailed()
                    return await worker.stdout.readexactly(size)
            except (OSError, TimeoutError, EOFError, CleanupFailed) as error:
                await self.stop_worker()
                raise CleanupFailed() from error
            except asyncio.CancelledError:
                await self.stop_worker()
                raise
            finally:
                if self.worker:
                    self.idle = asyncio.get_running_loop().call_later(
                        WORKER_IDLE, lambda: asyncio.create_task(self.retire_worker()),
                    )

    async def retire_worker(self):
        async with self.lock:
            await self.stop_worker()

    async def stop_worker(self):
        if self.idle:
            self.idle.cancel()
            self.idle = None
        if worker := self.worker:
            self.worker = None
            if worker.returncode is None:
                with suppress(ProcessLookupError):
                    worker.kill()
            await worker.communicate()

    async def close(self):
        if task := self.downloading:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.retire_worker()
