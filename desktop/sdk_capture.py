"""Temporary, owner-scoped microphone takes for the running desktop SDK."""
import asyncio
from array import array
import base64
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
import hmac
import json
import math
from pathlib import Path
import secrets
import tempfile

import httpx

from native_recordings import repair_wav

RATE = 48_000
LEASE_SECONDS = 10


class CaptureError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Take:
    identifier: str
    owner: str
    directory: tempfile.TemporaryDirectory
    path: Path
    child: asyncio.subprocess.Process
    languages: list[str]
    ready: asyncio.Event
    events: asyncio.Task | None = None
    expiry: asyncio.TimerHandle | None = None
    terminal: asyncio.Task | None = None
    started: bool = False
    failed: bool = False
    level: tuple[int, float, float] = (0, -120., -120.)


def dbfs(value):
    return 20 * math.log10(max(1e-6, min(1., float(value))))


class SDKCapture:
    def __init__(self, executable, transcribe, on_release):
        self.executable = executable
        self.transcribe = transcribe
        self.on_release = on_release
        self.active: Take | None = None
        self.interrupted: Take | None = None
        self.recovering: Take | None = None
        self.completed = OrderedDict()

    @property
    def busy(self):
        return self.active is not None or self.recovering is not None

    async def start(self, identifier, root, options, languages):
        if self.active:
            raise CaptureError("dictation-busy", "A recording is already active.")
        directory = tempfile.TemporaryDirectory(prefix="capture-", dir=root)
        path = Path(directory.name) / "audio.wav"
        try:
            child = await asyncio.create_subprocess_exec(
                str(self.executable), "record", "--mode", "mic", "--output", str(path), "--meter", *options,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            )
        except BaseException:
            directory.cleanup()
            raise
        take = self.active = Take(identifier, secrets.token_hex(32), directory, path, child, languages, asyncio.Event())
        take.events = asyncio.create_task(self.read_events(take))
        try:
            await asyncio.wait_for(take.ready.wait(), 5)
            if not take.started:
                raise CaptureError("microphone-lost", "The selected microphone could not start.")
        except TimeoutError as error:
            await self.close_active(take)
            raise CaptureError("microphone-lost", "The selected microphone could not start.") from error
        except BaseException:
            await self.close_active(take)
            raise
        if take is self.active or take is self.interrupted:
            self.renew(take)
        return {"id": take.identifier, "ownerToken": take.owner, "sampleRate": RATE}

    async def read_events(self, take):
        try:
            async for raw in take.child.stdout:
                event = json.loads(raw)
                kind = event["event"]
                if kind == "started":
                    take.started = True
                    take.ready.set()
                elif kind == "levels":
                    take.level = (take.level[0] + 1, dbfs(event["micRms"]), dbfs(event["mic"]))
                elif (kind == "error" or (kind == "finished" and event.get("reason") != "Stopped")
                      or (kind == "sourceChanged" and event.get("microphone") == "Unavailable")):
                    take.failed = True
        except (OSError, ValueError, KeyError, TypeError):
            take.failed = True
            if take.child.returncode is None:
                with suppress(ProcessLookupError):
                    take.child.kill()
        finally:
            take.failed |= bool(await take.child.wait())
            if self.active is take and take.started and not take.terminal:
                self.retain_interrupted(take)
            take.ready.set()

    def retain_interrupted(self, take):
        self.active = None
        take.failed = True
        try:
            with suppress(OSError):
                if take.path.stat().st_size >= 46:
                    if self.interrupted:
                        self.drop_interrupted(self.interrupted, CaptureError("microphone-lost", "The microphone was interrupted again."))
                    self.interrupted = take
                    if not take.expiry:
                        self.renew(take)
                    return
            self.complete(take, CaptureError("microphone-lost", "The recording did not capture any audio."))
        finally:
            self.on_release(take.identifier)

    def complete(self, take, result):
        if take.expiry:
            take.expiry.cancel()
        self.completed[take.identifier] = (take.owner, result)
        if len(self.completed) > 16:
            self.completed.popitem(last=False)
        with suppress(OSError):
            take.directory.cleanup()

    def drop_interrupted(self, take, result):
        self.interrupted = None
        self.complete(take, result)

    def renew(self, take):
        if take.expiry:
            take.expiry.cancel()
        take.expiry = asyncio.get_running_loop().call_later(LEASE_SECONDS, self.expire, take)

    def expire(self, take):
        if self.active is take and not take.terminal:
            self.end(take, discard=True)
        elif self.interrupted is take:
            self.drop_interrupted(take, {"phase": "cancelled"})

    def end(self, take, *, discard):
        if take.expiry:
            take.expiry.cancel()
        take.terminal = asyncio.create_task(self.finalize(take, discard=discard))

    async def close_active(self, take):
        if take is not None and self.active is take:
            if not take.terminal:
                self.end(take, discard=True)
            await asyncio.shield(take.terminal)

    async def close(self):
        if self.interrupted:
            self.drop_interrupted(self.interrupted, {"phase": "cancelled"})
        await self.close_active(self.active)
        if self.recovering:
            await asyncio.shield(self.recovering.terminal)

    async def stop_for_exit(self):
        if self.interrupted:
            self.drop_interrupted(self.interrupted, {"phase": "cancelled"})
        take = self.active
        if take and take.terminal:
            try:
                await asyncio.wait_for(asyncio.shield(take.events), 5)
            except TimeoutError as error:
                raise CaptureError("dictation-busy", "The SDK recorder is still closing.") from error
        else:
            await self.close_active(take)

    async def finalize(self, take, *, discard):
        result = {"phase": "cancelled"}
        try:
            if take.child.returncode is None:
                with suppress(ConnectionError):
                    take.child.stdin.write(b"stop\n")
                    await take.child.stdin.drain()
            try:
                await asyncio.wait_for(asyncio.shield(take.events), 4)
            except TimeoutError:
                take.failed = True
                with suppress(ProcessLookupError):
                    take.child.kill()
                await take.events
            if not discard:
                try:
                    frames = await asyncio.to_thread(repair_wav, take.path)
                except (OSError, ValueError) as error:
                    code = "microphone-lost" if take.failed else "empty-recording"
                    raise CaptureError(code, "The recording did not capture any audio.") from error
                text = await self.transcribe(take.path, take.languages)
                result = {"phase": "finished", "transcript": text, "durationMs": round(frames * 1000 / RATE)}
        except CaptureError as error:
            result = CaptureError(error.code, str(error), error.status)
        except (OSError, httpx.HTTPError, ValueError, KeyError):
            result = CaptureError("transcription-failed", "The local model could not transcribe this recording.", 503)
        finally:
            self.complete(take, result)
            if self.active is take:
                self.active = None
                self.on_release(take.identifier)
            elif self.recovering is take:
                self.recovering = None

    def request(self, action, identifier, owner, cursor):
        take = next((take for take in (self.active, self.interrupted, self.recovering)
                     if take and take.identifier == identifier and hmac.compare_digest(take.owner, owner)), None)
        if not take:
            cached = self.completed.get(identifier)
            if action in ("finish", "cancel") and cached and hmac.compare_digest(cached[0], owner):
                if isinstance(cached[1], CaptureError):
                    error = cached[1]
                    raise CaptureError(error.code, str(error), error.status)
                return cached[1]
            raise CaptureError("dictation-not-found", "This recording is unavailable.", 404)
        if action in ("finish", "cancel"):
            if not take.terminal:
                if take is self.interrupted:
                    if action == "cancel":
                        result = {"phase": "cancelled"}
                        self.drop_interrupted(take, result)
                        return result
                    if self.recovering:
                        # Keep the second clip available while the first uses the speech model.
                        self.renew(take)
                        return {"phase": "finishing"}
                    self.interrupted = None
                    self.recovering = take
                self.end(take, discard=action == "cancel")
            return {"phase": "finishing"}
        if take.terminal or take.events.done():
            if action != "heartbeat":
                return {"done": True}
        if action == "heartbeat":
            if not take.terminal:
                self.renew(take)
            return {}
        if action == "levels":
            sequence, rms, peak = take.level
            return {"cursor": sequence, "rmsDb": rms, "peakDb": peak} if sequence > cursor else {"cursor": cursor}
        return self.audio(take, cursor)

    @staticmethod
    def audio(take, cursor):
        with suppress(FileNotFoundError):
            with take.path.open("rb") as file:
                frames = max(0, (file.seek(0, 2) - 44) // 2)
                # Slow readers pick up within the last half second of committed audio.
                start = min(frames, max(cursor, frames - RATE // 2))
                file.seek(44 + start * 2)
                pcm = array("h", file.read(min(frames - start, RATE // 10) * 2))
                if pcm:
                    samples = array("f", (sample / 32768 for sample in pcm))
                    return {"cursor": start + len(pcm), "samples": base64.b64encode(samples).decode("ascii")}
        return {"cursor": cursor}
