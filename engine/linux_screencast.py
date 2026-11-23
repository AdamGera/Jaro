"""Record an approved PipeWire source and the selected local audio inputs."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import logging
import os
import re
import shutil
import signal
import time
from uuid import uuid4

from annotation_assets import annotation_movie_info
from audio_file import MAX_AUDIO_SECONDS
from linux_capture import CaptureError, default_computer_audio, default_microphone
from linux_desktop_portal import DBusError, PortalResponseError, ScreenCastPortal
from linux_pen import DesktopPen
from recording_power import keep_awake_until
from session_images import jpeg_dimensions
from screen_draft import ScreenDraft, validate_region, writer_lock
from screencast_ink import FRAME_BOUNDARY, RecordingDrawing


logger = logging.getLogger(__name__)
LEVEL_MESSAGE = re.compile(rb'from element "(mic|system)_level" \(element\): level,.*?peak=\([^)]*\)<([^>]*)>')
PREVIEW_BYTES = 16 * 1024 * 1024


def supported():
    return shutil.which("gst-launch-1.0") is not None


def audio_caps(channels):
    return f"audio/x-raw,format=S16LE,rate=48000,channels={channels}"


def encode_audio(index):
    return ["voaacenc", "bitrate=128000", "!", "aacparse", "!", "queue", "!", f"mux.audio_{index}"]


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int
    frame_width: int
    frame_height: int

    def __post_init__(self):
        validate_region((self.x, self.y, self.width, self.height), (self.frame_width, self.frame_height))

    def crop(self):
        return ["videoconvert", "!", f"video/x-raw,format=RGBx,width={self.frame_width},height={self.frame_height}", "!",
                "videocrop", f"left={self.x}", f"top={self.y}", f"right={self.frame_width - self.x - self.width}",
                f"bottom={self.frame_height - self.y - self.height}", "!"]


def recording_command(fd, target, path, devices, region=None, *, preview_fd):
    command = [
        "gst-launch-1.0", "-m" if devices else "-q", "-e",
        # Write playable fragments as we go. The compositor can revoke the stream
        # before GStreamer gets a chance to finalize a conventional MP4.
        "mp4mux", "name=mux", "fragment-duration=500", "!", "filesink", "buffer-mode=unbuffered", f"location={path}",
        "pipewiresrc", f"fd={fd}", target, "keepalive-time=250", "!",
        "queue", "!", "videorate", "!",
        *(region.crop() if region else []),
        "videoconvert", "!", "videoscale", "!",
        "video/x-raw,format=I420,framerate=24/1,pixel-aspect-ratio=1/1,width=(int)[2,2560,2],height=(int)[2,1440,2]", "!",
        "openh264enc", "bitrate=3000000", "gop-size=12", "!",
        "tee", "name=screen",
        "screen.", "!", "queue", "!", "h264parse", "!", "mux.video_0",
        # Branch after encoding so preview dimensions cannot negotiate a smaller source.
        "screen.", "!", "queue", "leaky=downstream", "max-size-buffers=24", "!", "h264parse", "!", "openh264dec", "!",
        "videorate", "drop-only=true", "!", "video/x-raw,framerate=8/1", "!",
        "jpegenc", "quality=70", "!", "multipartmux", f"boundary={FRAME_BOUNDARY}", "!",
        "fdsink", f"fd={preview_fd}", "sync=false", "async=false",
    ]
    mixed = len(devices) == 2
    if mixed:
        command.extend(["audiomixer", "name=mix", "start-time-selection=first", "latency=100000000",
                        "!", audio_caps(2), "!", *encode_audio(0)])
    for index, (source, device) in enumerate(devices.items()):
        channels = 1 if source == "mic" else 2
        command.extend([
            "pulsesrc", f"device={device}", "provide-clock=false", "!", "queue", "!", "audioconvert", "!",
            "audioresample", "!", audio_caps(channels), "!", "level", f"name={source}_level", "interval=200000000", "!",
        ])
        if mixed:
            command.extend(["tee", f"name={source}", f"{source}.", "!", "queue", "!", "audioconvert",
                            "!", audio_caps(2), "!", "volume", "volume=0.5", "!", "mix.",
                            f"{source}.", "!", "queue", "!"])
        command.extend(encode_audio(index + int(mixed)))
    return command


def input_level(line):
    match = LEVEL_MESSAGE.search(line)
    if match:
        channel, values = match.groups()
        with suppress(ValueError):
            peak = max(float(value) for value in values.split(b","))
            return channel.decode(), 10 ** (min(0, max(-80, peak)) / 20) if peak > -80 else 0
    return None


@asynccontextmanager
async def preview_pipe():
    descriptor, writer = os.pipe()
    reader = asyncio.StreamReader()
    with os.fdopen(descriptor, "rb") as source:
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), source)
            try:
                yield reader, writer
            finally:
                transport.close()
        finally:
            os.close(writer)


class ScreenRecording:
    def __init__(self, staging, source, microphone, computer_audio=False):
        audio = tuple(name for name, enabled in (("mic", microphone), ("system", computer_audio)) if enabled)
        self.draft = ScreenDraft(staging / str(uuid4()), source, audio)
        self.levels = dict.fromkeys(audio, 0)
        self.portal = ScreenCastPortal(self.portal_closed)
        self.phase = "choosing"
        self.started = None
        self.elapsed = 0
        self.error = None
        self.recorder = None
        self.stopped = False
        self.task = None
        self.saving = False
        self.preview_id = None
        self.preview = None
        self.preview_size = None
        self.selection = None
        self.target = None
        self.region = None
        self.drawing = RecordingDrawing(self.draft.ink)
        self.pen = DesktopPen(self.drawing)

    def recording_time(self):
        return self.elapsed if self.started is None else time.monotonic() - self.started

    def status(self):
        status = {"phase": "saving" if self.saving else self.phase, "seconds": self.recording_time(),
                  "error": self.error, "levels": self.levels}
        if self.phase == "selecting" and self.preview_size is not None:
            width, height = self.preview_size
            status["preview"] = {"id": self.preview_id, "width": width, "height": height}
        if self.phase == "recording":
            status["id"] = self.draft.key
            status["desktopDrawing"] = self.pen.monitor is not None
            status["desktopDrawingActive"] = self.pen.active
        return status

    def portal_closed(self):
        self.request_stop()
        if self.selection is not None and not self.selection.done():
            self.selection.set_exception(CaptureError("The desktop ended screen sharing."))

    async def snapshot(self, target):
        fd = await self.portal.open_remote()
        try:
            return await capture_preview(fd, target)
        finally:
            os.close(fd)

    async def choose_region(self, target):
        while True:
            self.phase = "previewing"
            self.preview, self.preview_size = await self.snapshot(target)
            if self.portal.session is None:
                raise CaptureError("The desktop ended screen sharing.")
            self.preview_id = str(uuid4())
            self.selection = asyncio.get_running_loop().create_future()
            self.phase = "selecting"
            try:
                async with asyncio.timeout(120):
                    region = await self.selection
            except TimeoutError as failure:
                raise CaptureError("Area selection timed out.") from failure
            finally:
                self.preview_id = self.preview = self.preview_size = self.selection = None
            if region is not None:
                return region
            # Give the user time to focus the content they want in the next preview.
            await asyncio.sleep(3)

    def select_region(self, bounds):
        if self.phase != "selecting" or self.selection is None or self.selection.done():
            raise CaptureError("There is no display preview ready.")
        region = None if bounds is None else Region(*bounds, *self.preview_size)
        self.phase = "previewing" if region is None else "starting"
        self.selection.set_result(region)

    async def read_levels(self):
        while line := await self.recorder.stdout.readline():
            reading = input_level(line)
            if reading is not None:
                source, level = reading
                self.levels[source] = level

    def request_stop(self):
        self.pen.request_stop()
        self.drawing.close_view()
        if not self.stopped and self.recorder and self.recorder.returncode is None:
            self.stopped = True
            with suppress(ProcessLookupError):
                self.recorder.send_signal(signal.SIGINT)

    async def stop(self):
        self.request_stop()
        if self.task is not None:
            try:
                async with asyncio.timeout(8):
                    await asyncio.shield(self.task)
            except TimeoutError:
                if self.recorder and self.recorder.returncode is None:
                    with suppress(ProcessLookupError):
                        self.recorder.kill()
                await self.task

    async def record(self):
        try:
            await self.portal.connect()
            area = self.draft.source == "area"
            self.target = await self.portal.select("display" if area else self.draft.source)
            self.region = await self.choose_region(self.target) if area else None
            async with preview_pipe() as (preview_reader, preview_fd):
                await self.start_encoder(preview_fd)
                readers = [asyncio.create_task(self.drawing.read_frames(preview_reader))]
                if self.recorder.stdout:
                    readers.append(asyncio.create_task(self.read_levels()))
                try:
                    await self.wait_until_finished(self.draft.movie)
                finally:
                    await self.finish_process()
                    for task in readers:
                        task.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
        except PortalResponseError as failure:
            self.phase = "cancelled" if failure.code == 1 else "error"
            if self.phase == "error":
                self.error = str(failure)
        except asyncio.CancelledError:
            self.phase = "cancelled"
            if asyncio.current_task().cancelling():
                raise
        except (CaptureError, DBusError, EOFError, OSError, TimeoutError, ValueError) as failure:
            self.phase = "error"
            self.error = str(failure) if isinstance(failure, CaptureError) else "Could not start screen recording on this desktop."
            logger.warning("Screen recording stopped during setup: %s", type(failure).__name__)
        finally:
            await self.pen.close()
            if self.started is not None:
                self.elapsed = time.monotonic() - self.started
                self.started = None
            self.drawing.frame = None
            await self.portal.close()

    async def start_encoder(self, preview_fd):
        fd = await self.portal.open_remote()
        try:
            self.phase = "starting"
            get_device = {"mic": default_microphone, "system": default_computer_audio}
            audio = self.draft.audio_sources
            names = await asyncio.gather(*(get_device[name]() for name in audio))
            devices = dict(zip(audio, names, strict=True))
            self.draft.create()
            movie = self.draft.movie
            with writer_lock(self.draft.folder, create=True) as lock, movie.with_suffix(".log").open("wb") as errors:
                self.recorder = await asyncio.create_subprocess_exec(
                    *recording_command(fd, self.target, movie, devices, self.region, preview_fd=preview_fd),
                    pass_fds=(fd, lock, preview_fd), env={**os.environ, "LC_ALL": "C"},
                    stdin=asyncio.subprocess.DEVNULL, stderr=errors,
                    stdout=asyncio.subprocess.PIPE if devices else asyncio.subprocess.DEVNULL,
                )
        finally:
            os.close(fd)

    async def wait_until_finished(self, movie):
        async with asyncio.timeout(12):
            while self.recorder.returncode is None:
                if movie.is_file() and movie.stat().st_size >= 1024:
                    break
                await asyncio.sleep(.05)
        if self.recorder.returncode is not None:
            raise CaptureError("The desktop stream could not start recording.")
        self.started = time.monotonic()
        # Fragmented MP4 already holds some video when the UI first goes live.
        with suppress(OSError, ValueError, TimeoutError):
            async with asyncio.timeout(1):
                captured, _ = await annotation_movie_info(movie)
            self.started = time.monotonic() - captured
        self.phase = "recording"
        keep_awake_until(asyncio.create_task(self.recorder.wait()), "Jaro is recording the screen")
        try:
            async with asyncio.timeout(MAX_AUDIO_SECONDS - 1):
                await self.pen.probe(self.portal.display_properties)
                await self.recorder.wait()
        except TimeoutError:
            await self.finish_process()
        self.phase = "recorded"

    async def finish_process(self):
        self.request_stop()
        if self.recorder and self.recorder.returncode is None:
            try:
                async with asyncio.timeout(4):
                    await self.recorder.wait()
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.recorder.kill()
                await self.recorder.wait()

    async def shutdown(self):
        if self.recorder is None:
            await self.discard()
            return
        await self.stop()
        if not self.draft.movie.is_file() or not self.draft.movie.stat().st_size:
            self.draft.discard()

    async def discard(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.draft.discard()


async def capture_preview(fd, target):
    process = await asyncio.create_subprocess_exec(
        "gst-launch-1.0", "-q", "pipewiresrc", f"fd={fd}", target, "!",
        "videoconvert", "!", "jpegenc", "snapshot=true", "quality=72", "!", "fdsink", "fd=1",
        pass_fds=(fd,), stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(8):
            try:
                await process.stdout.readexactly(PREVIEW_BYTES + 1)
            except asyncio.IncompleteReadError as data:
                image = data.partial
            else:
                raise CaptureError("The display preview is too large.")
            if await process.wait() or not (dimensions := jpeg_dimensions(image)):
                raise CaptureError("The desktop did not send a display preview.")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.communicate()
    return image, dimensions
