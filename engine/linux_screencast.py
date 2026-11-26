"""Record a native X11 display or an approved PipeWire source with local audio."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import time
from uuid import uuid4

from annotation_assets import annotation_movie_info
from audio_file import MAX_AUDIO_SECONDS
from linux_capture import CaptureError, default_computer_audio, resolve_microphone
from linux_desktop_portal import DBusError, PortalResponseError, ScreenCastPortal
from linux_pen import DesktopPen
from linux_x11 import recording_target
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


def gstreamer_environment():
    return {**os.environ, "LC_ALL": "C", "GST_XINITTHREADS": "1"}


def recording_command(video, path, devices, region=None, *, preview_fd):
    command = [
        # Write playable fragments as we go. The compositor can revoke the stream
        # before GStreamer gets a chance to finalize a conventional MP4.
        "mp4mux", "name=mux", "fragment-duration=500", "!", "filesink", "buffer-mode=unbuffered", f"location={path}",
        *video, "!", "queue", "!", "videorate", "!",
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
    routes = {source: (device, audio_caps(1 if source == "mic" else 2)) for source, device in devices.items()}
    for index, (source, (_, caps)) in enumerate(routes.items()):
        command.extend([
            "interaudiosrc", f"channel={source}", "buffer-time=250000000", "!", caps, "!",
            "level", f"name={source}_level", "interval=200000000", "!",
        ])
        if mixed:
            command.extend(["tee", f"name={source}", f"{source}.", "!", "queue", "!", "audioconvert",
                            "!", audio_caps(2), "!", "volume", "volume=0.5", "!", "mix.",
                            f"{source}.", "!", "queue", "!"])
        command.extend(encode_audio(index + int(mixed)))
    if routes:
        helper = Path(__file__).with_name("linux_screen_pipeline.py")
        return ["/usr/bin/python3", "-IB", str(helper), json.dumps(routes), *command]
    return ["gst-launch-1.0", "-q", "-e", *command]


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
    def __init__(self, staging, source, microphone, computer_audio=False, *, microphone_device="", native_x11=False, notice=None,
                 draw_on_start=False, x11_area=None, show_area_picker=None, annotation_location=None):
        audio = tuple(name for name, enabled in (("mic", microphone), ("system", computer_audio)) if enabled)
        self.draft = ScreenDraft(staging / str(uuid4()), source, audio, annotation_location)
        self.microphone_device = microphone_device
        self.levels = dict.fromkeys(audio, 0)
        self.unavailable_audio = set()
        self.portal = None if native_x11 else ScreenCastPortal(self.portal_closed)
        self.phase = "choosing"
        if native_x11:
            self.phase = "previewing" if source == "area" and x11_area is None else "starting"
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
        self.x11_area = x11_area
        self.show_area_picker = show_area_picker
        self.region = None
        self.drawing = RecordingDrawing(self.draft.ink)
        self.pen = DesktopPen(self.drawing)
        self.notice = notice
        self.draw_on_start = draw_on_start
        self.drawing_view = None

    def recording_time(self):
        return self.elapsed if self.started is None else time.monotonic() - self.started

    def status(self):
        status = {"phase": "saving" if self.saving else self.phase, "seconds": self.recording_time(),
                  "id": self.draft.key, "error": self.error, "levels": self.levels}
        if self.phase == "selecting" and self.preview_size is not None:
            width, height = self.preview_size
            status["preview"] = {"id": self.preview_id, "width": width, "height": height}
        if self.phase == "recording":
            status["desktopDrawing"] = self.pen.monitor is not None
            status["desktopDrawingActive"] = self.pen.active
            if self.drawing_view:
                status["drawingView"] = self.drawing_view
            if self.unavailable_audio and not self.error:
                sources = {"mic": "microphone", "system": "computer audio"}
                missing = " and ".join(label for source, label in sources.items() if source in self.unavailable_audio)
                status["error"] = f"Waiting for {missing} to reconnect. Screen recording continues."
        return status

    async def toggle_drawing(self):
        if self.drawing_view:
            self.drawing_view = None
            self.drawing.close_view()
        elif self.pen.active:
            await self.pen.close()
        elif self.pen.monitor is not None:
            await self.pen.open(self.started, self.region)
        elif self.drawing.token is not None:
            raise CaptureError("Close the current drawing view first.")
        else:
            tray = Path(os.environ.get("JARO_DESKTOP_TRAY", ""))
            if not tray.is_absolute() or not tray.is_dir():
                raise CaptureError("Open Jaro’s workspace to draw in the recording preview.")
            try:
                (tray / "open").touch()
            except OSError as failure:
                raise CaptureError("Could not open the recording preview.") from failure
            self.drawing_view = str(uuid4())

    def portal_closed(self):
        self.request_stop()
        if self.selection is not None and not self.selection.done():
            self.selection.set_exception(CaptureError("The desktop ended screen sharing."))

    @asynccontextmanager
    async def video_input(self, *, preview=False):
        if self.portal is None:
            x, y, width, height = self.target
            caps = "video/x-raw,framerate=24/1"
            if self.x11_area is not None:
                caps += f",width={width},height={height}"
            yield ["ximagesrc", f"startx={x}", f"starty={y}", f"endx={x + width - 1}", f"endy={y + height - 1}",
                   "!", caps], ()
            return
        fd = await self.portal.open_remote()
        try:
            video = ["pipewiresrc", f"fd={fd}", self.target]
            if not preview:
                video.append("keepalive-time=250")
            yield video, (fd,)
        finally:
            os.close(fd)

    async def snapshot(self):
        async with self.video_input(preview=True) as (video, descriptors):
            return await capture_preview(video, descriptors)

    async def choose_region(self):
        while True:
            self.phase = "previewing"
            self.preview, self.preview_size = await self.snapshot()
            if self.portal is not None and self.portal.session is None:
                raise CaptureError("The desktop ended screen sharing.")
            self.preview_id = str(uuid4())
            self.selection = asyncio.get_running_loop().create_future()
            self.phase = "selecting"
            try:
                if self.show_area_picker:
                    self.show_area_picker()
                    self.show_area_picker = None
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
            fields = line.split()
            if len(fields) == 3 and fields[0] == b"jaro-screen-audio" and fields[1] in (b"mic", b"system"):
                source = fields[1].decode("ascii")
                if fields[2] == b"unavailable":
                    self.unavailable_audio.add(source)
                elif fields[2] == b"ready":
                    self.unavailable_audio.discard(source)
                continue
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
            area = self.draft.source == "area"
            if self.portal is None:
                self.target = self.x11_area or await asyncio.to_thread(recording_target)
            else:
                await self.portal.connect()
                self.target = await self.portal.select("display" if area else self.draft.source)
            if area and self.x11_area is None:
                self.region = await self.choose_region()
            if self.notice:
                await self.notice.show(self)
            async with preview_pipe() as (preview_reader, preview_fd):
                await self.start_encoder(preview_fd)
                readers = [asyncio.create_task(self.drawing.read_frames(preview_reader))]
                if self.recorder.stdout:
                    readers.append(asyncio.create_task(self.read_levels()))
                try:
                    if self.notice and self.notice.lost:
                        raise CaptureError("The X11 recording control was closed.")
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
            if self.notice:
                await self.notice.close()
            await self.pen.close()
            if self.started is not None:
                self.elapsed = time.monotonic() - self.started
                self.started = None
            self.drawing.frame = None
            if self.portal is not None:
                await self.portal.close()

    async def start_encoder(self, preview_fd):
        async with self.video_input() as (video, descriptors):
            self.phase = "starting"
            audio = self.draft.audio_sources
            names = await asyncio.gather(*(resolve_microphone(self.microphone_device) if name == "mic"
                                           else default_computer_audio() for name in audio))
            if self.notice and self.notice.lost:
                raise CaptureError("The X11 recording control was closed.")
            devices = dict(zip(audio, names, strict=True))
            self.draft.create()
            movie = self.draft.movie
            with writer_lock(self.draft.folder, create=True) as lock, movie.with_suffix(".log").open("wb") as errors:
                self.recorder = await asyncio.create_subprocess_exec(
                    *recording_command(video, movie, devices, self.region, preview_fd=preview_fd),
                    pass_fds=(*descriptors, lock, preview_fd), env=gstreamer_environment(),
                    stdin=asyncio.subprocess.DEVNULL, stderr=errors,
                    stdout=asyncio.subprocess.PIPE if devices else asyncio.subprocess.DEVNULL,
                )

    async def wait_until_finished(self, movie):
        async with asyncio.timeout(12):
            while self.recorder.returncode is None:
                if movie.is_file() and movie.stat().st_size >= 1024:
                    break
                await asyncio.sleep(.05)
        if self.recorder.returncode is not None:
            if self.draft.audio_sources and self.recorder.returncode == 3:
                raise CaptureError("Install python3-gi and gir1.2-gstreamer-1.0 to include audio in screen recordings.")
            raise CaptureError("The desktop stream could not start recording.")
        self.started = time.monotonic()
        # Fragmented MP4 already holds some video when the UI first goes live.
        with suppress(OSError, ValueError, TimeoutError):
            async with asyncio.timeout(1):
                captured, _ = await annotation_movie_info(movie)
            self.started = time.monotonic() - captured
        keep_awake_until(asyncio.create_task(self.recorder.wait()), "Jaro is recording the screen")
        try:
            async with asyncio.timeout(MAX_AUDIO_SECONDS - 1):
                if self.portal is not None:
                    await self.pen.probe(self.portal.display_properties)
                self.phase = "recording"
                if self.draw_on_start and not self.stopped:
                    try:
                        await self.toggle_drawing()
                    except CaptureError as failure:
                        self.error = str(failure)
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


async def capture_preview(video, descriptors):
    process = await asyncio.create_subprocess_exec(
        "gst-launch-1.0", "-q", *video, "!",
        "videoconvert", "!", "jpegenc", "snapshot=true", "quality=72", "!", "fdsink", "fd=1",
        pass_fds=descriptors, env=gstreamer_environment(), stdin=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
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
