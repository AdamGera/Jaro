"""Generate a portable annotation contact sheet from the saved video."""
import asyncio
from contextlib import suppress
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile
import zlib

from starlette.concurrency import run_in_threadpool

from annotation_assets import MAX_TIMELINE_SECONDS, number
from audio_file import codec_binary


TILE_WIDTH = 420
TILE_HEIGHT = 248
CELL_WIDTH = 436
CELL_HEIGHT = 282
PREVIEW_TIMEOUT = 12
FRAME_INFO = re.compile(rb"\bn:\s*0\s+pts:\s*-?\d+\s+pts_time:(\S+)[^\n]*?\bs:(\d+)x(\d+)\b")


def frame_candidates(duration, events):
    if duration < .4:
        return [0.]
    end = duration - min(.12, duration / 8)
    marks = []
    previous = None
    for event in events:
        if event.get("clear"):
            if previous is not None:
                marks.append(min(event["time"], previous + .08))
            previous = None
        else:
            time = min(end, event["time"])
            if previous is not None and time - previous > .65:
                marks.append(previous + .08)
            previous = time
    if previous is not None:
        marks.append(previous + .08)
    # Keep the start and end, then sample drawing clusters across the recording.
    selected = [0., end]
    count = min(4, len(marks))
    for index in range(count):
        time = marks[int((index + .5) * len(marks) / count)]
        if all(abs(time - other) >= .18 for other in selected) and 0 <= time <= end:
            selected.append(time)
    target = min(6, math.ceil(duration / 2) + 1)
    for index in range(1, target - 1):
        time = end * index / (target - 1)
        if len(selected) < target and all(abs(time - other) >= .18 for other in selected):
            selected.append(time)
    return sorted(selected)


async def read_preview_frame(movie, seconds, codec):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = await asyncio.create_subprocess_exec(
        codec, "-hide_banner", "-nostdin", "-loglevel", "info", "-copyts", "-start_at_zero",
        "-ss", f"{seconds:.6f}", "-threads", "2", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "mov", "-i", movie, "-map", "0:v:0",
        "-vf", f"scale={TILE_WIDTH}:{TILE_HEIGHT}:force_original_aspect_ratio=decrease,format=rgb24,showinfo",
        "-frames:v", "1", "-threads", "1", "-c:v", "rawvideo", "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options,
    )
    try:
        pixels, report = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    info = FRAME_INFO.search(report)
    if process.returncode or not info:
        return None
    seconds, width, height = info.groups()
    try:
        time = float(seconds)
    except ValueError:
        return None
    width, height = int(width), int(height)
    if (not number(time, 0, MAX_TIMELINE_SECONDS) or not 0 < width <= TILE_WIDTH
            or not 0 < height <= TILE_HEIGHT or len(pixels) != width * height * 3):
        return None
    return round(time, 3), width, height, pixels


def png_chunk(kind, content):
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))


def write_sheet(output, frames):
    columns = min(2, len(frames))
    rows = math.ceil(len(frames) / columns)
    width, height = columns * CELL_WIDTH, rows * CELL_HEIGHT
    stride = width * 3 + 1
    # RGB PNG with unfiltered rows. The fixed sheet dimensions bound memory use.
    canvas = bytearray((b"\0" + b"\x1c\x21\x29" * width) * height)
    for index, (_, frame_width, frame_height, pixels) in enumerate(frames):
        left = index % columns * CELL_WIDTH + (CELL_WIDTH - frame_width) // 2
        top = index // columns * CELL_HEIGHT + (CELL_HEIGHT - frame_height) // 2
        line_size = frame_width * 3
        for row in range(frame_height):
            start = (top + row) * stride + left * 3 + 1
            canvas[start:start + line_size] = pixels[row * line_size:(row + 1) * line_size]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    file = NamedTemporaryFile(dir=output.parent, prefix=".contact-sheet-", delete=False)
    temporary = Path(file.name)
    try:
        with file:
            file.write(b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
                       + png_chunk(b"IDAT", zlib.compress(canvas, 3)) + png_chunk(b"IEND", b""))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


async def make_contact_sheet(movie, output, duration, events):
    frames = []
    try:
        codec = codec_binary("ffmpeg")
        async with asyncio.timeout(PREVIEW_TIMEOUT):
            for seconds in frame_candidates(min(duration, MAX_TIMELINE_SECONDS), events):
                frame = await read_preview_frame(movie, seconds, codec)
                if frame and (not frames or int(frame[0] * 10 + .0001) > int(frames[-1][0] * 10 + .0001)):
                    frames.append(frame)
    except (OSError, TimeoutError):
        # A preview failure must never prevent saving the recording.
        pass
    if frames:
        with suppress(OSError):
            await run_in_threadpool(write_sheet, output, frames)
            return [frame[0] for frame in frames]
    return []
