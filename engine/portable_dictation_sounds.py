"""Play Jaro's bundled dictation cues on the desktop's default output."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal
import wave

SoundStyle = Literal["classic", "signature", "click", "soft-tone", "wood", "analog", "thock"]
DONE_STYLES = {"signature", "click", "soft-tone", "wood", "analog"}
MAX_WAV = 512 * 1024
ROOT = Path(__file__).resolve().parents[1] / "desktop" / "assets" / "dictation-sounds"


@dataclass(frozen=True)
class SoundPreferences:
    enabled: bool = True
    style: SoundStyle = "classic"
    volume: int = 70


def sound_path(style: SoundStyle, cue: str):
    # Classic uses the system error sound on macOS. Use a bundled error elsewhere.
    if style == "classic" and cue == "error":
        style = "soft-tone"
    folder = ROOT if style == "classic" else ROOT / ("soft" if style == "soft-tone" else style)
    return folder / (cue + ".wav")


def scaled_wav(path, volume):
    import numpy as np

    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getnframes() * source.getnchannels() * 2 > MAX_WAV - 44:
            raise ValueError("Invalid dictation sound.")
        frames = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        scaled = np.rint(frames.astype(np.float32) * (volume / 100)).astype("<i2")
        data = BytesIO()
        with wave.open(data, "wb") as writer:
            writer.setparams(source.getparams())
            writer.writeframes(scaled.tobytes())
    return data.getvalue()


class DictationSounds:
    def __init__(self):
        self.pulse_player = shutil.which("paplay") if sys.platform == "linux" else None
        self.available = (self.pulse_player is not None or os.name == "nt") and sound_path("classic", "start").is_file()
        self.task = None

    def play(self, cue, preferences, *, delay=0, while_valid=None, ready=None, after=None):
        if not self.available or not preferences.enabled or not preferences.volume:
            return None
        self.cancel()
        async def scheduled():
            if ready is not None:
                await ready.wait()
            if after is not None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(after), .8)
            return await self._play(cue, preferences, 0 if ready is not None else delay, while_valid)

        self.task = asyncio.create_task(scheduled())
        return self.task

    def cancel(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def stop(self):
        self.cancel()
        task = self.task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _play(self, cue, preferences, delay, while_valid):
        process = None
        try:
            if delay:
                await asyncio.sleep(delay)
            if while_valid is not None and not while_valid():
                return False
            path = sound_path(preferences.style, cue)
            if not path.is_file():
                return False
            payload = None
            options = {}
            if self.pulse_player is not None:
                # Pulse's nominal gain is 65536 and its software volume is cubic.
                volume = round(65536 * (preferences.volume / 100) ** (1 / 3))
                command = (self.pulse_player, "--client-name=Jaro", "--stream-name=Dictation cue",
                           f"--volume={volume}", path)
            else:
                # A short-lived child can stop memory-backed WinMM playback on
                # cancellation without touching the system output level.
                payload = scaled_wav(path, preferences.volume)
                command = (sys.executable, "-B", __file__)
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE if payload else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **options,
            )
            await asyncio.wait_for(process.communicate(payload), 3)
            return process.returncode == 0
        except (OSError, EOFError, ValueError, wave.Error, TimeoutError):
            return False
        finally:
            if process is not None and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


if __name__ == "__main__" and os.name == "nt":
    import winsound

    audio = sys.stdin.buffer.read(MAX_WAV + 1)
    if not 0 < len(audio) <= MAX_WAV:
        sys.exit(2)
    winsound.PlaySound(audio, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
