"""Own a temporary native drawing surface while the recorder is running."""

import asyncio
from contextlib import suppress
import json
import logging

from portable_capture import CaptureError


logger = logging.getLogger(__name__)


class PenProcess:
    def __init__(self, drawing):
        self.drawing = drawing
        self.process = None
        self.task = None
        self.stopping = False

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    async def start(self, args, keep_stroke, **options):
        if self.active or self.drawing.token is not None:
            raise CaptureError("Close the current drawing view first.")
        ready = asyncio.get_running_loop().create_future()
        self.stopping = False
        self.task = asyncio.create_task(self.run(args, keep_stroke, ready, options))
        try:
            return await asyncio.shield(ready)
        except asyncio.CancelledError:
            self.request_stop()
            raise

    async def run(self, args, keep_stroke, ready, options):
        try:
            self.process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **options,
            )
            if self.stopping:
                self.request_stop()
            async with asyncio.timeout(3):
                if (await self.process.stdout.readline()).rstrip(b"\r\n") != b"READY":
                    return
            ready.set_result(True)
            while line := await self.process.stdout.readline():
                try:
                    keep_stroke(json.loads(line))
                except (OSError, ValueError, TypeError, KeyError, OverflowError):
                    logger.warning("Could not keep a desktop drawing event.")
        except (OSError, TimeoutError, ValueError):
            logger.warning("Could not open the desktop drawing surface.")
        finally:
            try:
                if self.process and self.process.returncode is None:
                    try:
                        async with asyncio.timeout(.25):
                            await self.process.wait()
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
            finally:
                if not ready.done():
                    ready.set_result(False)
                self.process = None

    def request_stop(self):
        self.stopping = True
        if self.process:
            self.process.stdin.close()

    async def close(self):
        self.request_stop()
        if self.active:
            try:
                async with asyncio.timeout(4):
                    await asyncio.shield(self.task)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
