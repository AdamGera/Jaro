"""Owner-scoped SDK takes using the portable desktop's existing microphone recorder."""
import asyncio
import base64
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
import hmac
import logging
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from uuid import uuid4

import numpy as np
from fastapi import HTTPException

from portable_capture import BLOCK_BYTES, CaptureError
from recording_power import keep_awake_until
from rumble_model import SAMPLE_RATE


logger = logging.getLogger(__name__)
LEASE_SECONDS = 10


def failure(code, message, status=409):
    return HTTPException(status, {"code": code, "message": message})


@dataclass
class Take:
    id: str
    owner: str
    folder: TemporaryDirectory
    recorder: object
    languages: tuple
    expiry: asyncio.TimerHandle | None = None
    stopping: asyncio.Task | None = None
    terminal: asyncio.Task | None = None


class PortableSDKCapture:
    def __init__(self, root, recorder, transcribe):
        self.root = root
        self.recorder = recorder
        self.transcribe = transcribe
        self.active = None
        self.interrupted = None
        self.ending = {}
        self.completed = OrderedDict()

    @property
    def busy(self):
        return self.active is not None or bool(self.ending)

    async def start(self, languages, microphone):
        # The caller owns the shared portable recording admission lock.
        try:
            folder = TemporaryDirectory(prefix=".upload-sdk-", dir=self.root)
        except OSError as error:
            raise failure("recording-unavailable", "Jaro could not prepare the temporary recording.", 503) from error
        key = uuid4()
        try:
            recorder = self.recorder(key, Path(folder.name) / "audio.wav", "mic", seconds=120, microphone=microphone)
            recorder.dictation = True
            recorder.watch_microphone = True
            take = self.active = Take(str(key), secrets.token_hex(32), folder, recorder, tuple(languages))
            await recorder.start()
        except BaseException as error:
            self.active = None
            with suppress(OSError):
                folder.cleanup()
            if isinstance(error, (CaptureError, OSError)):
                raise failure("microphone-lost", "The selected microphone could not start.") from error
            raise
        recorder.worker.add_done_callback(lambda _: self.recorder_ended(take))
        keep_awake_until(recorder.worker, "Recording a dictation for a connected app")
        self.renew(take)
        return {"id": take.id, "ownerToken": take.owner, "sampleRate": SAMPLE_RATE}

    def renew(self, take):
        if take.expiry:
            take.expiry.cancel()
        take.expiry = asyncio.get_running_loop().call_later(LEASE_SECONDS, self.end, take, True)

    def recorder_ended(self, take):
        if self.active is take and not take.terminal:
            asyncio.create_task(self.retain(take))

    async def stop(self, take):
        if take.stopping is None:
            take.stopping = asyncio.create_task(take.recorder.stop())
        try:
            await asyncio.shield(take.stopping)
        except Exception:
            take.recorder.interrupted = True
            logger.warning("An SDK microphone ended unexpectedly.", exc_info=True)

    async def retain(self, take):
        await self.stop(take)
        if self.active is not take or take.terminal:
            return
        self.active = None
        lost = failure("microphone-lost", "The microphone stopped recording.")
        if not take.recorder.frames:
            self.complete(take, lost)
            return
        if self.interrupted:
            self.complete(self.interrupted, lost)
        self.interrupted = take

    def end(self, take, discard):
        if take.terminal:
            return
        if take.expiry:
            take.expiry.cancel()
        if self.interrupted is take:
            self.interrupted = None
        self.ending[take.id] = take
        take.terminal = asyncio.create_task(self.finalize(take, discard))

    async def finalize(self, take, discard):
        result = {"phase": "cancelled"}
        try:
            await self.stop(take)
            if not discard:
                if not take.recorder.frames:
                    code = "microphone-lost" if take.recorder.interrupted else "empty-recording"
                    raise failure(code, "The recording did not capture any audio.")
                transcript = await self.transcribe(take.recorder.path, take.languages)
                result = {"phase": "finished", "transcript": transcript,
                          "durationMs": round(take.recorder.frames * 1000 / SAMPLE_RATE)}
        except HTTPException as error:
            result = (error.status_code, error.detail)
        except Exception:
            logger.exception("Could not transcribe the SDK microphone take.")
            result = (503, {"code": "transcription-failed", "message": "The local model could not transcribe this recording."})
        finally:
            self.complete(take, result)
            if self.active is take:
                self.active = None
            self.ending.pop(take.id, None)

    def complete(self, take, result):
        if take.expiry:
            take.expiry.cancel()
        if isinstance(result, HTTPException):
            result = (result.status_code, result.detail)
        self.completed[take.id] = (take.owner, result)
        if len(self.completed) > 16:
            self.completed.popitem(last=False)
        with suppress(OSError):
            take.folder.cleanup()

    async def close(self):
        for take in (self.active, self.interrupted):
            if take:
                self.end(take, True)
        await asyncio.gather(*(take.terminal for take in self.ending.values()))

    def request(self, action, identifier, owner, cursor):
        take = next((take for take in (self.active, self.interrupted, self.ending.get(identifier))
                     if take and take.id == identifier and hmac.compare_digest(take.owner, owner)), None)
        if not take:
            cached = self.completed.get(identifier)
            if action in ("finish", "cancel") and cached and hmac.compare_digest(cached[0], owner):
                result = cached[1]
                if isinstance(result, tuple):
                    raise HTTPException(*result)
                return result
            raise failure("dictation-not-found", "This recording is unavailable.", 404)
        if action in ("finish", "cancel"):
            self.end(take, action == "cancel")
            return {"phase": "finishing"}
        if action == "heartbeat":
            if not take.terminal:
                self.renew(take)
            return {}
        if self.active is not take or take.terminal or take.recorder.worker.done():
            return {"done": True}
        recorder = take.recorder
        if cursor >= recorder.frames or not recorder.blocks:
            return {"cursor": cursor}
        if action == "levels":
            samples = np.frombuffer(recorder.blocks[-1][1], dtype="<i2").astype(np.float32) / 32768
            rms = float(np.sqrt(np.mean(samples ** 2)))
            peak = float(np.max(np.abs(samples)))
            return {"cursor": recorder.frames, "rmsDb": float(20 * np.log10(max(1e-6, rms))),
                    "peakDb": float(20 * np.log10(max(1e-6, peak)))}
        start, pcm = recorder.recent(max(cursor, recorder.frames - SAMPLE_RATE // 2))
        samples = np.frombuffer(pcm[:BLOCK_BYTES], dtype="<i2").astype("<f4") / 32768
        return {"cursor": start + len(samples), "samples": base64.b64encode(samples).decode("ascii")}
