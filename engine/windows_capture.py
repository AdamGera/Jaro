"""Start the WASAPI helper shipped beside the Windows desktop host."""
import asyncio
import os
from pathlib import Path
import subprocess

from portable_capture import CaptureError, PortableRecording


def helper_path():
    configured = os.environ.get("JARO_WINDOWS_CAPTURE")
    if configured:
        path = Path(configured)
        if path.is_absolute() and path.is_file():
            return path
    return None


class WindowsRecording(PortableRecording):
    async def spawn(self):
        helper = helper_path()
        if helper is None:
            raise CaptureError("Jaro’s Windows audio recorder is missing. Rebuild the desktop host.")
        return await asyncio.create_subprocess_exec(
            helper, self.mode, str(self.seconds), creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )

    def request_stop(self):
        self.process.stdin.close()
