"""Read the local path selected in the portable desktop file dialog."""
import asyncio
import os
from pathlib import Path

from audio_file import stop_process
from engine_paths import WORKSPACE_ROOT


def file_dialog():
    name = "jaro-save-video.exe" if os.name == "nt" else "jaro-save-video"
    helper = Path(os.environ.get("JARO_SAVE_VIDEO_DIALOG") or WORKSPACE_ROOT / "bin" / name)
    return helper if helper.is_file() else None


async def choose_file(dialog, argument):
    process = await asyncio.create_subprocess_exec(
        dialog, argument, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        data, _ = await process.communicate()
    finally:
        await stop_process(process)
    if process.returncode:
        raise ValueError("Jaro could not open the desktop file dialog.")
    if data:
        path = Path(os.fsdecode(data) if os.name != "nt" else data.decode("utf-8"))
        if not path.is_absolute():
            raise ValueError("The desktop file dialog returned an invalid path.")
        return path
    return None
