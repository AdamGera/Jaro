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


async def choose_file(dialog, argument, *arguments):
    process = await asyncio.create_subprocess_exec(
        dialog, argument, *arguments, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
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


async def reveal_folder(folder):
    if os.name == "nt":
        await asyncio.to_thread(os.startfile, folder, "explore")
        return
    process = await asyncio.create_subprocess_exec("gio", "open", folder, stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(10):
            if await process.wait():
                raise OSError("Could not reveal the Annotations folder.")
    finally:
        await stop_process(process)
