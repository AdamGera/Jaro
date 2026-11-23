"""Own the temporary Wayland drawing surface for an approved recording."""

import asyncio
from contextlib import suppress
import os
import time

from annotation_assets import number
from desktop_pen import PenProcess
from portable_capture import CaptureError


def monitor_arguments(properties):
    mapping = properties.get("mapping_id")
    connector = mapping.value if mapping is not None and mapping.signature == "s" else ""
    position, size = (properties.get(key) for key in ("position", "size"))
    geometry = ""
    if all(item is not None and item.signature == "(ii)" for item in (position, size)) and min(size.value) > 0:
        geometry = ",".join(str(value) for value in (*position.value, *size.value))
    return connector, geometry


class DesktopPen(PenProcess):
    def __init__(self, drawing):
        super().__init__(drawing)
        self.binary = os.environ.get("JARO_LINUX_PEN")
        self.monitor = None

    async def probe(self, properties):
        if not self.binary or not os.environ.get("WAYLAND_DISPLAY") or properties is None:
            return
        monitor = monitor_arguments(properties)
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, "probe", *monitor, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(2):
                    if await process.wait() == 0:
                        self.monitor = monitor
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        except (OSError, TimeoutError, ValueError):
            pass

    async def open(self, started, region):
        if self.monitor is None:
            raise CaptureError("Use the recording preview to draw on this desktop.")
        crop = (region.x / region.frame_width, region.y / region.frame_height,
                (region.x + region.width) / region.frame_width,
                (region.y + region.height) / region.frame_height) if region else (0, 0, 1, 1)
        args = (self.binary, "draw", *self.monitor, ",".join(map(str, crop)))
        if not await self.start(args, lambda payload: self.keep_stroke(payload, started)):
            self.monitor = None
            raise CaptureError("Could not draw on this display. You can still draw in the preview.")

    def keep_stroke(self, payload, started):
        released = payload["released"] / 1_000_000 - started
        points = payload["points"]
        if not 0 < len(points) <= 1024:
            raise ValueError("Invalid desktop drawing stroke.")
        # Preview clients can estimate slightly ahead of the native clock.
        previous = self.drawing.last_ink
        limit = time.monotonic() - started + .5
        if not number(released, 0, limit):
            raise ValueError("The desktop stroke belongs outside the current recording.")
        timed = [(x, y, max(previous, timestamp / 1_000_000 - started)) for x, y, timestamp in points]
        self.drawing.add(timed, max(previous, released), max(previous, limit), captured=True)
