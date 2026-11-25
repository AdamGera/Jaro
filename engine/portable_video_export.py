"""Export a separate copy of a portable annotation from the local library."""
import asyncio
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from annotation_assets import annotation_movie_tracks
from audio_file import codec_binary, stop_process
from desktop_file_dialog import choose_file
from screen_draft import windows_h264_encoder, windows_video_args


async def trim_video(source, target, start_ms, end_ms):
    duration, tracks = await annotation_movie_tracks(source, container_duration=True)
    if not 0 <= start_ms <= end_ms - 500 or end_ms / 1000 > duration + .001 or start_ms / 1000 >= duration:
        raise ValueError("The selection is outside the recording.")
    length = min(duration, end_ms / 1000) - start_ms / 1000
    options = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"]
    attempts = [options]
    windows = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    if windows:
        preferred = await asyncio.to_thread(windows_h264_encoder)
        attempts = [windows_video_args(preferred)]
        if preferred == "h264_mf" and (fallback := await asyncio.to_thread(windows_h264_encoder, True)):
            attempts.append(windows_video_args(fallback))
    for encoding in attempts:
        # Accurate seeking decodes from the preceding keyframe and drops that lead-in.
        process = await asyncio.create_subprocess_exec(
            codec_binary("ffmpeg"), "-v", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
            "-ss", f"{start_ms / 1000:.3f}", "-i", source, "-t", f"{length:.6f}",
            "-map", "0:v:0", "-map", "0:a?", *encoding, "-threads", "4", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", target, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, **windows,
        )
        try:
            if await process.wait() == 0:
                _, exported_tracks = await annotation_movie_tracks(target)
                if exported_tracks == tracks:
                    return
        finally:
            await stop_process(process)
    raise ValueError("Jaro could not export the selected video range.")


async def save_annotation_video(library, dialog, key, span):
    session = await asyncio.to_thread(library.get, key)
    annotation = session.get("annotation")
    if not annotation or annotation.get("missingVideo") or not session.get("audioUrl"):
        raise ValueError("The annotation video is unavailable.")
    source, mime = await asyncio.to_thread(library.media, key)
    if mime != "video/mp4":
        raise ValueError("The annotation video is unavailable.")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(session["audioName"]).stem)[:120].strip(" .")
    name = (stem or "Screen recording") + ("-clip.mp4" if span else ".mp4")
    destination = await choose_file(dialog, name)
    if destination is None:
        return {"saved": False}
    resolved = destination.resolve()
    if resolved.is_relative_to(library.root.resolve()) or resolved.is_relative_to(source.parent.resolve()):
        raise ValueError("Choose an export destination outside the session library.")
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".jaro-export-") as folder:
        partial = Path(folder) / "video.mp4"
        if span:
            await trim_video(source, partial, *span)
        else:
            await asyncio.to_thread(shutil.copyfile, source, partial)
        partial.replace(destination)
    return {"saved": True}
