"""Durable stroke events and their timed video overlay."""

import asyncio
from contextlib import suppress
import json
import logging
import math
import os
import subprocess
import sys

from annotation_assets import MAX_TIMELINE_SECONDS, number
from audio_file import codec_binary


logger = logging.getLogger(__name__)
INK_HOLD = 0.65
INK_LIFETIME = 1.30
MAX_EVENTS = 100_000
FRAME_BOUNDARY = "jaro-live-frame"
BOUNDARY_LINE = f"--{FRAME_BOUNDARY}\r\n".encode()


class RecordingDrawing:
    def __init__(self, journal):
        self.journal = journal
        self.token = None
        self.frame = None
        self.updated = asyncio.Event()
        self.last_ink = 0
        self.ink_count = 0
        self.stroke_number = 0

    def add(self, points, released, limit, *, captured=False):
        events = stroke_events(points, released, self.stroke_number, self.last_ink, min(MAX_TIMELINE_SECONDS, limit))
        if captured:
            for event in events:
                event["captured"] = True
        if self.ink_count + len(events) > MAX_EVENTS:
            raise ValueError("This recording has reached its drawing limit.")
        append_events(self.journal, events)
        self.ink_count += len(events)
        self.stroke_number += bool(points)
        self.last_ink = released

    async def read_frames(self, reader):
        try:
            while line := await reader.readline():
                if line != BOUNDARY_LINE:
                    continue
                media = await reader.readline()
                if not media:
                    break
                length, blank = await reader.readline(), await reader.readline()
                size = int(length.lower().removeprefix(b"content-length: "))
                if media.lower() != b"content-type: image/jpeg\r\n" or blank != b"\r\n" or not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError("Unexpected drawing preview frame.")
                image = await reader.readexactly(size + 2)
                self.frame = BOUNDARY_LINE + media + length + blank + image
                self.updated.set()
        except asyncio.IncompleteReadError:
            pass
        except ValueError:
            # Keep reading even after a broken preview so recording can finish.
            logger.warning("Could not decode the drawing preview stream.")
            while await reader.read(65_536):
                pass
        finally:
            self.close_view()

    def close_view(self):
        self.token = None
        self.updated.set()

    async def stream(self, is_live):
        if self.token is not None or not is_live():
            return
        token = self.token = object()
        try:
            previous = None
            while self.token is token and is_live():
                self.updated.clear()
                frame = self.frame
                if frame is not None and frame is not previous:
                    yield frame
                    previous = frame
                else:
                    async with asyncio.timeout(5):
                        await self.updated.wait()
        except TimeoutError:
            pass
        finally:
            if self.token is token:
                self.close_view()


def stroke_events(points, released, stroke, previous, limit):
    if not number(released, previous, limit):
        raise ValueError("The stroke belongs outside the current recording.")
    if not points:
        return [{"time": round(released, 3), "clear": True}]
    timed = []
    for x, y, seconds in points:
        if not number(x, 0, 1) or not number(y, 0, 1) or not number(seconds, previous, released):
            raise ValueError("A drawing point is outside the video or recording time.")
        timed.append((round(seconds, 3), [round(x, 5), round(1 - y, 5)]))
        previous = seconds
    return [
        {"time": seconds, "stroke": stroke, "from": timed[max(0, index - 1)][1], "to": point,
         "liftedAt": round(released, 3)}
        for index, (seconds, point) in enumerate(timed)
    ]


def append_events(path, events):
    if path.is_symlink():
        raise OSError("Invalid drawing journal.")
    line = json.dumps(events, separators=(",", ":")).encode("ascii") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(line)
        file.flush()
        os.fsync(file.fileno())


def load_events(path):
    if path.is_symlink():
        raise ValueError("The annotation timeline is invalid.")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                             | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        return []
    events = []
    with os.fdopen(descriptor, "rb") as file:
        while line := file.readline(128 * 1024):
            if not line.endswith(b"\n"):
                # A process interruption can leave the final write unfinished.
                break
            chunk = json.loads(line)
            if not isinstance(chunk, list) or len(events) + len(chunk) > MAX_EVENTS:
                raise ValueError("The annotation timeline is invalid.")
            events.extend(chunk)
    return events


def ass_time(seconds):
    hours, rest = divmod(round(seconds * 100), 360_000)
    minutes, rest = divmod(rest, 6000)
    seconds, fraction = divmod(rest, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def polygon(points):
    return "m " + " l ".join(f"{round(x)} {round(y)}" for x, y in points)


def segment_path(start, end, radius):
    # Draw each edge as a capsule so stroke endpoints stay round at any scale.
    x, y = start
    end_x, end_y = end
    angle = math.atan2(end_y - y, end_x - x)
    points = []
    for (cx, cy), offset in ((end, -math.pi / 2), (start, math.pi / 2)):
        for step in range(9):
            turn = angle + offset + math.pi * step / 8
            points.append((cx + radius * math.cos(turn), cy + radius * math.sin(turn)))
    return polygon(points)


def overlay_script(events, width, height, font="Sans"):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Ink,{font},16,&H003866FF,&H003866FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues = []
    clear = MAX_TIMELINE_SECONDS
    radius = max(1.5, min(width, height) * 0.0035)
    for event in reversed(events):
        start = event["time"]
        if event.get("clear"):
            clear = start
            continue
        release = event["liftedAt"]
        fade = release + INK_HOLD
        end = min(clear, release + INK_LIFETIME)
        if ass_time(end) <= ass_time(start):
            continue
        a, b = ([point[0] * width, (1 - point[1]) * height] for point in (event["from"], event["to"]))
        delay = round((fade - start) * 1000)
        finish = round((release + INK_LIFETIME - start) * 1000)
        tags = fr"{{\an7\pos(0,0)\p1\fade(0,0,255,0,0,{delay},{finish})}}"
        dialogues.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Ink,,0,0,0,,{tags}{segment_path(a,b,radius)}\n")
    return header + "".join(reversed(dialogues))


async def make_overlay(path, movie, events, font="Sans"):
    windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = await asyncio.create_subprocess_exec(
        codec_binary("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
        "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", movie,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **windows,
    )
    try:
        output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    try:
        stream = json.loads(output)["streams"][0]
        dimensions = stream["width"], stream["height"]
        if process.returncode or min(dimensions) <= 0:
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError) as failure:
        raise ValueError("Could not read the video canvas for drawing.") from failure
    path.write_text(overlay_script(events, *dimensions, font), encoding="ascii")
