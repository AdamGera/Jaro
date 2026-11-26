"""Generate offline visual excerpts from a saved annotation movie."""
import asyncio
from contextlib import contextmanager
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from annotation_assets import MAX_TIMELINE_SECONDS, annotation_movie_info, number
from annotation_package import stroke_times
from audio_file import codec_binary, stop_process
from meeting_search import words
from saved_meetings import parse_offset
from screen_contact_sheet import frame_png, read_preview_frame, write_sheet

MAX_FRAMES = 240
MAX_SHEET_FRAMES = 48
REVIEW_TIMEOUT = 45
MAX_FRAME_PIXELS = 8192 * 4320


@contextmanager
def output_folder(parent, library_root, detail):
    parent = Path(parent or tempfile.gettempdir()).expanduser().resolve(strict=True)
    protected = [library_root.resolve(), Path(detail["videoPath"]).parent.resolve()]
    if package := detail.get("packagePath"):
        protected.append(Path(package))
    if not parent.is_dir() or any(parent.is_relative_to(path) for path in protected):
        raise ValueError("Choose an output folder outside the capture and Session library.")
    folder = Path(tempfile.mkdtemp(prefix="jaro-annotation-", dir=parent))
    try:
        yield folder
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


async def movie_info(detail):
    if not detail.get("videoPath"):
        raise ValueError("This annotation has no video available. Reconnect its storage folder if needed.")
    movie = Path(detail["videoPath"])
    duration, _ = await annotation_movie_info(movie)
    return movie, min(duration, MAX_TIMELINE_SECONDS)


def requested_range(start, end):
    start_ms, end_ms = parse_offset(start), parse_offset(end)
    if start_ms is None or end_ms is None or start_ms > end_ms:
        raise ValueError("Choose a frame range with the end at or after the start.")
    return start_ms / 1000, end_ms / 1000


def frame_range(start, end, fps, duration):
    start, end = requested_range(start, end)
    end = min(end, duration)
    if start >= duration:
        raise ValueError("The frame range starts at or beyond the end of the video.")
    count = math.ceil((end - start) * fps - 1e-9)
    if count > MAX_FRAMES:
        raise ValueError(f"Choose a shorter range or lower frame rate. One export writes up to {MAX_FRAMES} frames.")
    return start, end, max(1, count)


def saved_stills(detail, start, end, fps):
    margin = .5 / fps
    found = [frame for frame in detail.get("keyframes", [])
             if start - margin <= frame["time"] <= end + margin and Path(frame["path"]).is_file()]
    if not found:
        return None
    return {"id": detail["id"], "from": start, "to": end, "fps": fps, "source": "saved",
            "frames": [frame["path"] for frame in found], "frameTimes": [frame["time"] for frame in found]}


async def write_frames(stream, folder, count):
    paths = []
    for index in range(count):
        if not (magic := await stream.readline()):
            break
        try:
            width, height = map(int, (await stream.readline()).split())
        except ValueError as error:
            raise ValueError("The decoder returned an invalid video frame.") from error
        if magic != b"P6\n" or await stream.readline() != b"255\n" or not (0 < width * height <= MAX_FRAME_PIXELS and width > 0):
            raise ValueError("The decoder returned an invalid or oversized video frame.")
        stride = width * 3
        try:
            pixels = await stream.readexactly(stride * height)
        except asyncio.IncompleteReadError as error:
            raise ValueError("The decoder returned an incomplete video frame.") from error
        path = folder / f"frame-{index + 1:03d}.png"
        path.write_bytes(frame_png(width, height, pixels))
        paths.append(str(path))
    return paths


async def extract_frames(detail, library_root, args):
    if not detail.get("videoPath") and detail.get("keyframes"):
        start, end = requested_range(args.start, args.end)
        if result := saved_stills(detail, start, end, args.fps):
            return result
        raise ValueError("The requested range contains no saved stills.")
    try:
        async with asyncio.timeout(REVIEW_TIMEOUT):
            movie, duration = await movie_info(detail)
            start, end, count = frame_range(args.start, args.end, args.fps, duration)
            with output_folder(args.output, library_root, detail) as folder:
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
                sampling = ("-t", f"{end - start:.6f}", "-vf", f"fps={args.fps}:start_time=0:round=up") if end > start else ()
                process = await asyncio.create_subprocess_exec(
                    codec_binary("ffmpeg"), "-hide_banner", "-v", "error", "-nostdin", "-ss", f"{start:.6f}",
                    "-threads", "2", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov", "-i", movie,
                    "-map", "0:v:0", "-an", "-sn", "-dn", *sampling, "-frames:v", str(count),
                    "-c:v", "ppm", "-threads", "1", "-f", "image2pipe", "pipe:1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **options,
                )
                try:
                    paths = await write_frames(process.stdout, folder, count)
                    if await process.wait():
                        raise ValueError("Jaro could not extract frames from the annotation video.")
                finally:
                    await stop_process(process)
                if not paths:
                    if result := saved_stills(detail, start, end, args.fps):
                        folder.rmdir()
                        return result
                    raise ValueError("The requested range contains no video frames.")
                return {"id": detail["id"], "from": start, "to": end, "fps": args.fps,
                        "frames": paths}
    except TimeoutError as error:
        raise ValueError("The frame export took too long. Try a shorter video range.") from error


def timed_text(items):
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        start = item.get("start")
        if number(start, 0, MAX_TIMELINE_SECONDS):
            end = item.get("end")
            yield start, end if number(end, start, MAX_TIMELINE_SECONDS) else start, item["text"]


def moment(transcript, text):
    try:
        offset = parse_offset(text)
        if offset is not None:
            return offset / 1000
    except ValueError:
        pass
    query = " ".join(words(text))
    if query:
        query_tokens = query.split()
        terms = [(term, start) for start, _, text in timed_text(transcript["words"]) for term in words(text)]
        tokens = [term for term, _ in terms]
        for index, token in enumerate(tokens):
            if token == query_tokens[0] and tokens[index:index + len(query_tokens)] == query_tokens:
                return terms[index][1]
        for start, _, text in timed_text(transcript["segments"]):
            if f" {query} " in f" {' '.join(words(text))} ":
                return start
    raise ValueError("Use a timestamp or a phrase in the timed narration.")


def mark_crop(document, mark):
    xs, ys = zip(*(point for event in document["annotation"]["events"] if event.get("stroke") == mark
                   and event.get("clear") is not True for point in (event["from"], event["to"])))

    def axis(low, high):
        size = min(1., max(.35, high - low + .16))
        return max(0., min(1. - size, (low + high - size) / 2)), size

    left, width = axis(min(xs), max(xs))
    top, height = axis(1 - max(ys), 1 - min(ys))
    return left, top, width, height


async def zoom(detail, document, library_root, args):
    if not number(args.span, .2, MAX_TIMELINE_SECONDS):
        raise ValueError("Choose a span of at least 0.2 seconds within the annotation timeline.")
    if not 1 <= args.frames <= MAX_SHEET_FRAMES:
        raise ValueError(f"Choose between 1 and {MAX_SHEET_FRAMES} contact sheet frames.")
    capture = document["annotation"]
    transcript = capture["transcript"]
    crop = None
    mark = None
    if args.mark is not None:
        mark = next((stroke for stroke in stroke_times(document) if stroke["stroke"] == args.mark), None)
        if mark is None:
            raise ValueError("Stroke not found. Use annotate inspect --strokes to list the recorded strokes.")
        focus = mark["start"]
        if not args.full:
            crop = mark_crop(document, args.mark)
    else:
        focus = moment(transcript, args.at)
    try:
        async with asyncio.timeout(REVIEW_TIMEOUT):
            movie, duration = await movie_info(detail)
            if focus > duration:
                raise ValueError("The selected moment is beyond the end of the video.")
            span = min(duration, max(args.span, mark["end"] - focus + 1) if mark else args.span)
            center = (focus + mark["end"]) / 2 if mark else focus
            start = max(0., min(duration - span, center - span / (2 if mark else 4)))
            end = start + span
            codec = codec_binary("ffmpeg")
            frames = []
            for index in range(args.frames):
                # Sample within each cell of the window and report decoded presentation times.
                sample = start + span * (index + .25) / args.frames
                frame = await read_preview_frame(movie, sample, codec, crop=crop)
                if frame and (not frames or frame[0] > frames[-1][0]):
                    frames.append(frame)
            if not frames:
                raise ValueError("No frames could be decoded around that moment.")
            with output_folder(args.output, library_root, detail) as folder:
                sheet = folder / "contact-sheet.png"
                write_sheet(sheet, frames, columns=4)
    except TimeoutError as error:
        raise ValueError("The contact sheet took too long. Try fewer frames.") from error
    spoken = [text for first, last, text in timed_text(transcript["segments"]) if first < end and last >= start]
    if not spoken:
        spoken = [text for first, last, text in timed_text(transcript["words"]) if first < end and last >= start]
    return {"id": detail["id"], "path": str(sheet), "from": round(start, 3), "to": round(end, 3),
            "frames": [frame[0] for frame in frames], "narration": " ".join(spoken),
            **({"stroke": mark["stroke"]} if mark else {})}
