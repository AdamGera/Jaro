import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
import json
from pathlib import Path
import sys
import wave

from rumble_model import MAX_SAMPLES, SAMPLE_RATE
from engine_paths import WORKSPACE_ROOT

MAX_AUDIO_SECONDS = 3 * 60 * 60
FAST_WAV_MAX_SECONDS = 60
FILE_ERROR = "This audio could not be opened. Try a WAV, MP3, or M4A file."
DURATION_ERROR = "Choose audio up to 3 hours long."
PAUSE_AWARE_CHUNKING = "quiet-window-v1"
QUIET_SEARCH = SAMPLE_RATE * 5
QUIET_SPAN = SAMPLE_RATE // 10
INPUT_OPTIONS = (
    "-protocol_whitelist", "file,pipe",
    "-format_whitelist", "aac,aiff,caf,flac,matroska,webm,mov,mp3,ogg,wav",
)


def codec_binary(name):
    if sys.platform == "win32":
        bundled = WORKSPACE_ROOT / "bin" / f"{name}.exe"
        if bundled.is_file():
            return str(bundled)
    return name


async def stop_process(process: asyncio.subprocess.Process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()


async def audio_duration(path: Path) -> float | None:
    process = await asyncio.create_subprocess_exec(
        codec_binary("ffprobe"), "-v", "error", *INPUT_OPTIONS, "-select_streams", "a:0",
        "-show_entries", "stream=index,duration:format=duration", "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await process.communicate()
    finally:
        await stop_process(process)
    if process.returncode:
        raise ValueError(FILE_ERROR)
    metadata = json.loads(output)
    if not metadata.get("streams"):
        raise ValueError(FILE_ERROR)
    stream = metadata["streams"][0]
    duration = stream.get("duration", metadata.get("format", {}).get("duration"))
    return float(duration) if duration and duration != "N/A" else None


def split_at_pauses(pcm: bytes):
    """Keep all samples and give clearly separated utterances their own ASR context."""
    import numpy as np

    audio = np.frombuffer(pcm, dtype="<f4")
    frame = SAMPLE_RATE // 50  # 20 ms
    frames = audio[:len(audio) // frame * frame].reshape(-1, frame)
    quiet = np.mean(frames * frames, axis=1) < 0.003 ** 2
    edges = np.flatnonzero(np.diff(np.pad(quiet, 1))).reshape(-1, 2)
    start = 0
    for left, right in edges:
        if left == 0 or right == len(frames) or right - left < 14:
            continue
        cut = int(left + right) * frame // 2
        if min(cut - start, len(audio) - cut) >= 3 * SAMPLE_RATE:
            yield pcm[start * 4:cut * 4]
            start = cut
    yield pcm[start * 4:]


def quiet_sample_boundary(samples):
    """Cut at the midpoint of the quietest span near the end of a full model window."""
    import numpy as np

    if len(samples) <= 1:
        return len(samples)
    search = min(QUIET_SEARCH, len(samples) // 2)
    span = min(QUIET_SPAN, search)
    energy = np.square(samples[-search:], dtype=np.float64)
    if not np.isfinite(energy).all():
        return len(samples)
    # Only the energy difference from the first span matters. Accumulating
    # deltas keeps equal-energy spans equal even on float PCM.
    differences = np.concatenate(([0], np.cumsum(energy[span:] - energy[:-span])))
    quiet_start = len(differences) - 1 - differences[::-1].argmin()
    return max(1, int(len(samples) - search + quiet_start + span // 2))


@asynccontextmanager
async def open_audio_pcm(path: Path):
    process = await asyncio.create_subprocess_exec(
        codec_binary("ffmpeg"), "-v", "error", "-nostdin", *INPUT_OPTIONS, "-i", path,
        "-map", "0:a:0", "-t", str(MAX_AUDIO_SECONDS + 1), "-ac", "1",
        "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        await stop_process(process)


@lru_cache(maxsize=1)
def _decimate_48k_kernel():
    import numpy as np

    taps = 31
    half = taps // 2
    positions = np.arange(-half, half + 1, dtype=np.float64)
    cutoff = 7_200.0 / 48_000.0
    kernel = np.sinc(2.0 * cutoff * positions) * np.hamming(taps)
    kernel /= kernel.sum()
    return kernel.astype(np.float32)


def _read_fast_wav_pcm(path: Path) -> bytes | None:
    """Decode short mono 16-bit 16/48 kHz PCM WAVs in-process without spawning ffmpeg."""
    import numpy as np

    try:
        with wave.open(str(path), "rb") as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getcomptype()) != (1, 2, "NONE"):
                return None
            rate = stream.getframerate()
            if rate not in (SAMPLE_RATE, 48_000):
                return None
            frames = stream.getnframes()
            if frames <= 0 or frames > FAST_WAV_MAX_SECONDS * rate:
                return None
            raw = stream.readframes(frames)
    except (wave.Error, OSError, EOFError):
        return None
    if len(raw) != frames * 2:
        return None
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) * np.float32(1.0 / 32768.0)
    if rate == 48_000:
        kernel = _decimate_48k_kernel()
        half = len(kernel) // 2
        padded = np.pad(samples, half, mode="edge")
        samples = np.convolve(padded, kernel, mode="valid")[::3]
    return np.ascontiguousarray(samples, dtype="<f4").tobytes()


async def audio_blocks(path: Path, *, multilingual=False, pause_aware=False, fast_wav=False):
    import numpy as np

    limit = MAX_SAMPLES * 4
    read_size = limit + 4 if pause_aware else limit
    fast_pcm = await asyncio.to_thread(_read_fast_wav_pcm, path) if fast_wav else None
    if fast_pcm is not None:
        offset = 0
        total = len(fast_pcm)
        while offset < total:
            block = fast_pcm[offset:offset + read_size]
            if len(block) > limit:
                cut = quiet_sample_boundary(np.frombuffer(block, dtype="<f4", count=MAX_SAMPLES)) * 4
                block = block[:cut]
                offset += cut
            else:
                offset += len(block)
            if multilingual:
                for part in split_at_pauses(block):
                    yield part
            else:
                yield block
        return
    pending = b""
    async with open_audio_pcm(path) as process:
        while True:
            try:
                block = pending + await process.stdout.readexactly(read_size - len(pending))
            except asyncio.IncompleteReadError as error:
                block = pending + error.partial
            finished = len(block) < read_size
            if len(block) % 4:
                raise ValueError(FILE_ERROR)
            pending = b""
            if len(block) > limit:
                # A one-sample lookahead distinguishes the last window from a
                # full one that needs a cut. Keep the entire suffix for the
                # next read, including the sample used for lookahead.
                cut = quiet_sample_boundary(np.frombuffer(block, dtype="<f4", count=MAX_SAMPLES)) * 4
                block, pending = block[:cut], block[cut:]
            if block:
                if multilingual:
                    for part in split_at_pauses(block):
                        yield part
                else:
                    yield block
            if finished:
                break
        if await process.wait():
            raise ValueError(FILE_ERROR)


def dictation_has_signal(path: Path, through_frames: int | None = None, gain: float = 1.0) -> bool:
    """Reject near-silent recorder output before decoding or speech recognition."""
    import numpy as np

    full_scale = 32768 / gain
    fixed = full_scale * 10 ** (-50 / 20)
    level_floor = full_scale * 10 ** (-120 / 20)
    minimum_relative = full_scale * 10 ** (-66 / 20)
    levels = []
    try:
        with wave.open(str(path), "rb") as stream:
            rate = stream.getframerate()
            if (stream.getnchannels(), stream.getsampwidth()) != (1, 2) or rate not in (SAMPLE_RATE, 48_000):
                return True  # Leave other audio layouts to the existing decoder.
            window = rate * 30 // 1_000
            remaining = min(stream.getnframes(), (MAX_AUDIO_SECONDS + 1) * rate)
            if through_frames is not None:
                remaining = min(remaining, through_frames)
            while remaining > 0:
                count = min(64 * window, remaining)
                pcm = stream.readframes(count)
                if len(pcm) != count * 2:
                    return True
                samples = np.frombuffer(pcm, dtype="<i2")
                remaining -= count
                full = len(samples) // window * window
                rms = np.std(samples[:full].reshape(-1, window), axis=1)
                if full < len(samples):
                    rms = np.append(rms, np.std(samples[full:]))
                # Removing each window's DC offset avoids treating a microphone's bias as audio.
                if np.any(rms >= fixed):
                    return True
                levels.extend(rms)
    except (OSError, EOFError, ValueError, wave.Error):
        return True  # Decode errors and unfamiliar headers retain the usual error path.

    audible = sorted(level for level in levels if level > level_floor)
    if not audible:
        return False
    floor = audible[(len(audible) - 1) // 10]
    threshold = max(minimum_relative, floor * 10 ** (12 / 20))
    run = 0
    gap = False
    for level in levels:
        if level >= threshold:
            run += 1
            gap = False
            if run >= 6:  # At least 180 ms of above-floor sound.
                return True
        elif run:
            if gap:
                run = 0
            else:
                gap = True
    return False


async def make_playable(source: Path, target: Path):
    """Make a seekable browser copy for unsupported or unindexed media."""
    duration = await audio_duration(source)
    if duration and duration > MAX_AUDIO_SECONDS + 1:
        raise ValueError(DURATION_ERROR)
    process = await asyncio.create_subprocess_exec(
        codec_binary("ffmpeg"), "-v", "error", "-nostdin", *INPUT_OPTIONS, "-i", source,
        "-map", "0:a:0", "-t", str(MAX_AUDIO_SECONDS + 1), "-c:a", "aac",
        "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", target,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        if await process.wait():
            raise ValueError(FILE_ERROR)
    finally:
        await stop_process(process)
