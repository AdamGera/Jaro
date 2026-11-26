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

from annotation_assets import KEYFRAME_KINDS, KEYFRAME_SIZE, MAX_TIMELINE_SECONDS, number
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


async def read_preview_frame(movie, seconds, codec, *, crop=None, max_size=None):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    limit_width, limit_height = max_size or (TILE_WIDTH, TILE_HEIGHT)
    scale = (f"w='min(iw,{limit_width})':h='min(ih,{limit_height})'" if max_size
             else f"{limit_width}:{limit_height}")
    filters = f"scale={scale}:force_original_aspect_ratio=decrease,format=rgb24,showinfo"
    if crop is not None:
        left, top, width, height = crop
        filters = f"crop=iw*{width:.8f}:ih*{height:.8f}:iw*{left:.8f}:ih*{top:.8f}:exact=1," + filters
    process = await asyncio.create_subprocess_exec(
        codec, "-hide_banner", "-nostdin", "-loglevel", "info", "-copyts", "-start_at_zero",
        "-ss", f"{seconds:.6f}", "-threads", "2", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "mov", "-i", movie, "-map", "0:v:0",
        "-vf", filters,
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
    if (not number(time, 0, MAX_TIMELINE_SECONDS) or not 0 < width <= limit_width
            or not 0 < height <= limit_height or len(pixels) != width * height * 3):
        return None
    return round(time, 3), width, height, pixels


def png_chunk(kind, content):
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))


def rgb_png(width, height, rows):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header) + png_chunk(b"IDAT", zlib.compress(rows, 3)) + png_chunk(b"IEND", b"")


def frame_png(width, height, pixels):
    stride = width * 3
    rows = bytearray((stride + 1) * height)
    for row in range(height):
        start = row * (stride + 1) + 1
        rows[start:start + stride] = pixels[row * stride:(row + 1) * stride]
    return rgb_png(width, height, rows)


def thumbnail(frame):
    time, width, height, pixels = frame
    factor = min(TILE_WIDTH / width, TILE_HEIGHT / height)
    if factor == 1:
        return frame
    out_width, out_height = max(1, int(width * factor)), max(1, int(height * factor))
    columns = [3 * ((2 * column + 1) * width // (2 * out_width)) for column in range(out_width)]
    stride = width * 3
    resized = bytearray()
    for row in range(out_height):
        start = ((2 * row + 1) * height // (2 * out_height)) * stride
        line = pixels[start:start + stride]
        resized.extend(b"".join(line[column:column + 3] for column in columns))
    return time, out_width, out_height, resized


def write_sheet(output, frames, *, columns=2):
    columns = min(columns, len(frames))
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
    file = NamedTemporaryFile(dir=output.parent, prefix=".contact-sheet-", delete=False)
    temporary = Path(file.name)
    try:
        with file:
            file.write(rgb_png(width, height, canvas))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def write_preview_assets(paths, frames):
    write_sheet(paths["contact-sheet"], [thumbnail(frame) for frame in frames])
    stills = []
    for index, (_, width, height, pixels) in enumerate(frames, 1):
        path = paths[KEYFRAME_KINDS[index - 1]]
        try:
            path.write_bytes(frame_png(width, height, pixels))
            stills.append(index)
        except OSError:
            with suppress(OSError):
                path.unlink(missing_ok=True)
    return stills


async def make_contact_sheet(movie, paths, duration, events):
    frames = []
    try:
        codec = codec_binary("ffmpeg")
        async with asyncio.timeout(PREVIEW_TIMEOUT):
            for seconds in frame_candidates(min(duration, MAX_TIMELINE_SECONDS), events):
                frame = await read_preview_frame(movie, seconds, codec, max_size=KEYFRAME_SIZE)
                if frame and (not frames or int(frame[0] * 10 + .0001) > int(frames[-1][0] * 10 + .0001)):
                    frames.append(frame)
    except (OSError, TimeoutError):
        # A preview failure must never prevent saving the recording.
        pass
    if frames:
        with suppress(OSError):
            keyframes = await run_in_threadpool(write_preview_assets, paths, frames)
            return [frame[0] for frame in frames], keyframes
    return [], []
