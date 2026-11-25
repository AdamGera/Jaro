"""Record a desktop source selected in Jaro through Windows Graphics Capture."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import re
import struct
import subprocess
import time
from uuid import uuid4

from annotation_assets import number
from audio_file import MAX_AUDIO_SECONDS, codec_binary
from desktop_pen import PenProcess
from portable_capture import CaptureError
from recording_power import keep_awake_until
from screen_draft import ScreenDraft, validate_region, windows_h264_encoder, windows_video_args, writer_lock
from screencast_ink import FRAME_BOUNDARY, RecordingDrawing
from session_images import jpeg_dimensions
from windows_capture import WindowsScreenAudio


logger = logging.getLogger(__name__)
TARGET_ID = re.compile(r"[1-9a-f][0-9a-f]{0,15}:[0-9]{1,10}\Z")
FRAME_HEADER = struct.Struct("<IIq")
SNAPSHOT_HEADER = struct.Struct("<IIII")
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
CAPTURE_FPS = 24


@dataclass(frozen=True)
class Snapshot:
    jpeg: bytes
    size: tuple[int, int]
    native_size: tuple[int, int]

    def region(self, bounds):
        validate_region(bounds, self.size)
        x, y, width, height = bounds
        preview_width, preview_height = self.size
        frame_width, frame_height = self.native_size
        left, top = x * frame_width // preview_width, y * frame_height // preview_height
        right = (x + width) * frame_width // preview_width
        bottom = (y + height) * frame_height // preview_height
        return left, top, right - left, bottom - top, frame_width, frame_height


def helper_path():
    path = Path(os.environ.get("JARO_WINDOWS_SCREEN", ""))
    return path if path.is_absolute() and path.is_file() else None


async def output(*arguments, timeout=4):
    helper = helper_path()
    if helper is None:
        raise CaptureError("The Windows screen recorder is missing.")
    process = await asyncio.create_subprocess_exec(
        helper, *arguments, creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout):
            data, _ = await process.communicate()
        if process.returncode:
            raise CaptureError("Windows screen capture is unavailable on this PC.")
        return data
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


async def sources():
    try:
        await output("probe")
    except (CaptureError, OSError, TimeoutError):
        return []
    return ["display", "window", "area"]


def capture_targets(data, source):
    result = []
    for line in data.decode("utf-8").split("\n"):
        parts = line.rstrip("\r").split("\t", 2)
        if len(parts) != 3:
            continue
        handle, pid, label = parts
        target = f"{handle}:{pid}"
        if not TARGET_ID.fullmatch(target) or (source == "display") != (pid == "0"):
            continue
        label = " ".join(label.split())
        if source == "display" and (display := re.fullmatch(r"\\\\\.\\DISPLAY([0-9]+)", label)):
            label = f"Display {display[1]}"
        if label:
            result.append({"id": target, "label": label})
    return result[:100]


async def targets(source):
    try:
        return capture_targets(await output("targets", source), source)
    except (OSError, TimeoutError, UnicodeError) as error:
        raise CaptureError("Could not list the available screen sources.") from error


async def validate_target(source, target):
    if not TARGET_ID.fullmatch(target) or (source == "display") != target.endswith(":0"):
        raise ValueError("Choose a display or window in Jaro’s capture picker.")
    if not any(item["id"] == target for item in await targets(source)):
        raise LookupError("That screen source changed. Choose it again.")


async def cursor_display():
    try:
        target = (await output("cursor-display")).decode("ascii").strip()
    except (OSError, TimeoutError, UnicodeError) as failure:
        raise CaptureError("Could not find the display under the pointer.") from failure
    if not TARGET_ID.fullmatch(target) or not target.endswith(":0"):
        raise CaptureError("Could not find the display under the pointer.")
    return target


async def snapshot(source, target):
    handle, pid = target.split(":")
    data = await output("snapshot", source, handle, pid, timeout=10)
    if not SNAPSHOT_HEADER.size < len(data) <= MAX_SNAPSHOT_BYTES:
        raise CaptureError("Windows sent an invalid screenshot.")
    width, height, native_width, native_height = SNAPSHOT_HEADER.unpack_from(data)
    jpeg = data[SNAPSHOT_HEADER.size:]
    if not (2 <= width <= min(native_width, 1920) and 2 <= height <= min(native_height, 1080)
            and max(native_width, native_height) <= 16384 and jpeg_dimensions(jpeg) == (width, height)):
        raise CaptureError("Windows sent an invalid screenshot.")
    return Snapshot(jpeg, (width, height), (native_width, native_height))


def encoder_command(movie, width, height, encoder=None):
    selected = encoder or windows_h264_encoder()
    pix_fmt = "nv12" if selected == "h264_mf" else "yuv420p"
    video_args = windows_video_args(selected)
    return [
        codec_binary("ffmpeg"), "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pixel_format", "bgra", "-video_size", f"{width}x{height}",
        "-framerate", str(CAPTURE_FPS), "-i", "pipe:0",
        "-filter_complex_threads", "2", "-filter_complex",
        f"[0:v]split[record][preview];[record]format={pix_fmt}[main];"
        "[preview]fps=8,scale=w='min(iw,960)':h='min(ih,540)':force_original_aspect_ratio=decrease,format=yuvj420p[thumb]",
        "-map", "[main]", "-an", *video_args, "-g", "24",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1", str(movie),
        "-map", "[thumb]", "-an", "-c:v", "mjpeg", "-q:v", "6", "-f", "mpjpeg",
        "-boundary_tag", FRAME_BOUNDARY, "-flush_packets", "1", "pipe:1",
    ]


class WindowsPen(PenProcess):
    def __init__(self, drawing, target, source, recording_time):
        super().__init__(drawing)
        self.binary = helper_path() if source in ("display", "area") else None
        self.target = target
        self.recording_time = recording_time
        self.video_clock = None

    @property
    def available(self):
        return self.binary is not None and self.video_clock is not None

    async def open(self, _started, region):
        if not self.available:
            raise CaptureError("Use the recording preview to draw on this desktop.")
        handle, pid = self.target.split(":")
        args = (self.binary, "pen", "display", handle, pid, *map(str, region))
        if not await self.start(args, self.keep_stroke, creationflags=subprocess.CREATE_NO_WINDOW):
            self.binary = None
            raise CaptureError("Could not draw on this display. You can still draw in the preview.")

    def keep_stroke(self, payload):
        released = payload["released"] / 1_000_000 - self.video_clock
        points = payload["points"]
        if not 0 < len(points) <= 1024 or not number(released, 0, MAX_AUDIO_SECONDS + 2):
            raise ValueError("Invalid desktop drawing stroke.")
        previous = self.drawing.last_ink
        # Windows Graphics Capture and the pen use the same performance counter.
        # Keep strokes within the video even if encoding has fallen behind.
        end = max(previous, min(self.recording_time() + .5, released))
        timed = []
        for x, y, timestamp in points:
            seconds = timestamp / 1_000_000 - self.video_clock
            if not number(seconds, 0, released):
                raise ValueError("The desktop stroke belongs outside the current recording.")
            timed.append((x, y, max(previous, min(seconds, end))))
        # The native overlay is excluded from the capture. Render these strokes at export.
        self.drawing.add(timed, end, end)


class WindowsScreenRecording:
    def __init__(self, staging, source, target, microphone=False, computer_audio=False, *, microphone_device="", notice=None):
        audio_sources = tuple(name for name, enabled in (("mic", microphone), ("system", computer_audio)) if enabled)
        self.draft = ScreenDraft(staging / str(uuid4()), source, audio_sources)
        self.target = target
        self.microphone_device = microphone_device
        self.phase = "previewing" if source == "area" else "starting"
        self.started = None
        self.elapsed = 0
        self.error = None
        self.recorder = None
        self.encoder = None
        self.task = None
        self.saving = False
        self.stopped = False
        self.audio = None
        self.picture = None
        self.preview_id = None
        self.preview = None
        self.selection = None
        self.region = ()
        self.drawing = RecordingDrawing(self.draft.ink)
        self.pen = WindowsPen(self.drawing, target, source, self.recording_time)
        self.frame_bytes = 0
        self.bytes_encoded = 0
        self.notice = notice

    def recording_time(self):
        # Follow the frames sent to the encoder so slow encodes cannot move ink ahead of the video.
        if self.started is None:
            return self.elapsed
        return (self.bytes_encoded // self.frame_bytes) / CAPTURE_FPS

    def status(self):
        status = {"phase": "saving" if self.saving else self.phase, "seconds": self.recording_time(),
                  "id": self.draft.key, "error": self.error}
        if self.phase == "recording":
            status["desktopDrawing"] = self.pen.available
            status["desktopDrawingActive"] = self.pen.active
            if self.audio and self.audio.unavailable and not self.error:
                sources = {"mic": "Microphone", "system": "Computer audio"}
                missing = " and ".join(label for source, label in sources.items() if source in self.audio.unavailable)
                status["error"] = f"{missing} unavailable. Jaro is trying to reconnect."
        if self.phase == "selecting" and self.preview_id:
            width, height = self.picture.size
            status["preview"] = {"id": self.preview_id, "width": width, "height": height}
        if self.audio and self.audio.blocks:
            levels = self.audio.levels
            if levels is None:
                _, block = self.audio.blocks[-1]
                levels = (max(map(abs, memoryview(block).cast("h")), default=0) / 32_768,)
            status["levels"] = dict(zip(self.draft.audio_sources, levels, strict=True))
        return status

    def request_stop(self):
        self.stopped = True
        self.pen.request_stop()
        self.drawing.close_view()
        if self.selection is not None and not self.selection.done():
            self.selection.set_result(None)
        if self.recorder:
            self.recorder.stdin.close()
        if self.audio and self.audio.process and self.audio.process.returncode is None:
            self.audio.request_stop()

    async def stop(self):
        self.request_stop()
        if self.task:
            try:
                async with asyncio.timeout(12):
                    await asyncio.shield(self.task)
            except TimeoutError:
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task

    async def shutdown(self):
        await self.stop()

    async def discard(self):
        await self.stop()
        self.draft.discard()

    async def choose_region(self):
        while not self.stopped:
            self.picture = await snapshot("display", self.target)
            if self.stopped:
                break
            self.preview_id = str(uuid4())
            self.preview = self.picture.jpeg
            self.selection = asyncio.get_running_loop().create_future()
            self.phase = "selecting"
            try:
                async with asyncio.timeout(120):
                    region = await self.selection
            except TimeoutError as failure:
                raise CaptureError("Area selection timed out.") from failure
            finally:
                self.preview_id = self.preview = self.selection = self.picture = None
            if self.stopped:
                break
            if region is not None:
                return region
            # Give the user time to focus the content they want in the next preview.
            await asyncio.sleep(3)
        raise CaptureError("Screen recording cancelled.")

    def select_region(self, bounds):
        if self.phase != "selecting" or self.selection is None or self.selection.done():
            raise CaptureError("There is no display preview ready.")
        region = None if bounds is None else self.picture.region(bounds)
        self.phase = "previewing" if region is None else "starting"
        self.selection.set_result(region)

    async def write_video(self):
        try:
            while chunk := await self.recorder.stdout.read(262_144):
                self.encoder.stdin.write(chunk)
                await self.encoder.stdin.drain()
                self.bytes_encoded += len(chunk)
        finally:
            self.encoder.stdin.close()

    async def start_audio(self, video_ticks):
        names = self.draft.audio_sources
        if not names or self.stopped:
            return
        audio = self.audio = WindowsScreenAudio(self.draft.key, self.draft.narration, "both" if len(names) == 2 else names[0],
                                               microphone=self.microphone_device)
        try:
            await audio.start()
            delay = (audio.started_ticks - video_ticks) / 10_000_000
            if not 0 <= delay <= 30:
                raise CaptureError("Windows returned an invalid audio clock.")
            self.draft.set_audio_offset(delay)
        except (CaptureError, OSError) as error:
            self.error = str(error) if isinstance(error, CaptureError) else "Could not save the screen recording’s audio."
            with suppress(OSError):
                await audio.stop()
            self.audio = None
            self.draft = replace(self.draft, audio_sources=())
            try:
                self.draft.write_metadata()
            except OSError:
                logger.warning("Could not update the saved screen draft after audio setup failed.")

    async def record(self):
        try:
            helper = helper_path()
            if helper is None:
                raise CaptureError("The Windows screen recorder is missing.")
            if self.stopped:
                self.phase = "cancelled"
                return
            if self.draft.source == "area":
                self.region = await self.choose_region()
            if self.notice is not None:
                await self.notice.show(self)
                if self.stopped or self.notice.lost:
                    self.phase = "cancelled"
                    return
            self.draft.create()
            with writer_lock(self.draft.folder, create=True), self.draft.movie.with_suffix(".log").open("xb") as diagnostics:
                await self.run_encoder(helper, diagnostics)
        except (CaptureError, OSError, TimeoutError, ValueError, asyncio.IncompleteReadError, BrokenPipeError) as error:
            self.phase = "cancelled" if self.stopped and not self.started else "error"
            if self.phase == "error":
                self.error = str(error) if isinstance(error, CaptureError) else "Could not start or finish the Windows recording."
                logger.warning("Windows screen recording stopped: %s", type(error).__name__)
        finally:
            if self.notice is not None:
                await self.notice.close()
            await self.pen.close()
            self.picture = None
            self.drawing.frame = None
            if self.started is not None:
                self.elapsed = self.recording_time()
                self.started = None

    async def run_encoder(self, helper, diagnostics):
        handle, pid = self.target.split(":")
        source = "display" if self.region else self.draft.source
        writer = preview = None
        try:
            self.recorder = await asyncio.create_subprocess_exec(
                helper, "capture", source, handle, pid, *map(str, self.region),
                creationflags=subprocess.CREATE_NO_WINDOW, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=diagnostics,
            )
            if self.stopped:
                self.request_stop()
            async with asyncio.timeout(10):
                header = await self.recorder.stdout.readexactly(FRAME_HEADER.size)
            width, height, video_ticks = FRAME_HEADER.unpack(header)
            if not (2 <= width <= 1920 and 2 <= height <= 1080 and width % 2 == height % 2 == 0 and video_ticks > 0):
                raise CaptureError("Windows sent an invalid screen size.")
            self.frame_bytes = width * height * 4
            keep_awake_until(asyncio.create_task(self.recorder.wait()), "Jaro is recording the screen")
            self.pen.video_clock = video_ticks / 10_000_000
            self.started = time.monotonic()
            self.encoder = await asyncio.create_subprocess_exec(
                *encoder_command(self.draft.movie, width, height),
                creationflags=subprocess.CREATE_NO_WINDOW, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=diagnostics,
            )
            writer = asyncio.create_task(self.write_video())
            preview = asyncio.create_task(self.drawing.read_frames(self.encoder.stdout))
            await self.start_audio(video_ticks)
            async with asyncio.timeout(15):
                while not writer.done() and not self.movie_ready():
                    await asyncio.sleep(.05)
            if self.movie_ready():
                self.phase = "recording"
            try:
                async with asyncio.timeout(MAX_AUDIO_SECONDS):
                    await asyncio.shield(writer)
            except TimeoutError:
                self.request_stop()
                async with asyncio.timeout(10):
                    await writer
            self.request_stop()
            await self.pen.close()
            async with asyncio.timeout(10):
                result, _ = await asyncio.gather(self.encoder.wait(), self.recorder.wait())
            if result or not self.movie_ready():
                raise CaptureError("Windows could not encode this screen recording. Install the Windows Media Feature Pack if H.264 encoding is unavailable.")
            self.phase = "recorded"
        finally:
            if self.audio:
                with suppress(OSError):
                    await self.audio.stop()
            workers = [task for task in (writer, preview) if task is not None]
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for process in (self.recorder, self.encoder):
                if process and process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    if process.stdout:
                        await process.stdout.read()
                    await process.wait()

    def movie_ready(self):
        path = self.draft.movie
        return path.is_file() and path.stat().st_size >= 1024
