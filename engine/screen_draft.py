"""Keep portable screen recordings recoverable until they reach the session library."""
import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID

from annotation_assets import annotation_movie_tracks, number
from audio_file import codec_binary
from engine_paths import WORKSPACE_ROOT
from portable_capture import CaptureError, SAMPLE_RATE, draft_frames, draft_tracks
from screencast_ink import make_overlay


AUDIO_LABELS = {"mic": "Microphone", "system": "Computer audio"}


def validate_region(bounds, frame_size):
    x, y, width, height = bounds
    frame_width, frame_height = frame_size
    if min(x, y) < 0 or min(width, height) < 2 or x + width > frame_width or y + height > frame_height:
        raise ValueError("Select an area within the shared display.")


def audio_track_labels(sources):
    names = [AUDIO_LABELS[source] for source in sources]
    return ["Combined audio", *names] if len(names) == 2 else names


@lru_cache(maxsize=8)
def _ffmpeg_encoders(binary, check_output):
    return check_output(
        [binary, "-hide_banner", "-encoders"],
        stderr=subprocess.DEVNULL, text=True, timeout=5,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )


def windows_h264_encoder(prefer_software=False):
    candidates = ("libopenh264", "libx264") if prefer_software else ("h264_mf", "libopenh264", "libx264")
    try:
        output = _ffmpeg_encoders(codec_binary("ffmpeg"), subprocess.check_output)
    except (OSError, subprocess.SubprocessError):
        return None if prefer_software else "h264_mf"
    for name in candidates:
        if f" {name} " in output:
            return name
    return None if prefer_software else "h264_mf"


def windows_video_args(encoder):
    if encoder == "h264_mf":
        return ["-c:v", "h264_mf", "-b:v", "6M", "-pix_fmt", "nv12"]
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"]
    return ["-c:v", encoder, "-b:v", "6M", "-pix_fmt", "yuv420p"]


@dataclass(frozen=True)
class ScreenDraft:
    folder: Path
    source: str
    audio_sources: tuple[str, ...]
    annotation_location: str | None = None

    @property
    def key(self):
        return self.folder.name

    @property
    def movie(self):
        return self.folder / "screen.mp4"

    @property
    def ink(self):
        return self.folder / "ink.jsonl"

    @property
    def narration(self):
        return self.folder / "narration.wav"

    def set_audio_offset(self, seconds):
        stage = self.folder / "audio-offset.tmp"
        with stage.open("x", encoding="ascii") as file:
            json.dump(seconds, file)
            file.flush()
            os.fsync(file.fileno())
        stage.replace(self.folder / "audio-offset.json")

    def narration_frames(self):
        path = self.folder / "audio-offset.json"
        if not self.audio_sources or path.is_symlink() or not path.is_file():
            return 0
        return draft_frames(self.narration)

    def audio_sidecars(self):
        """Use committed standalone WAV tracks when the platform captures sound separately."""
        if not (frames := self.narration_frames()):
            return None, []
        with (self.folder / "audio-offset.json").open("rb") as file:
            offset = json.loads(file.read(128))
        if not number(offset, 0, 30):
            raise ValueError("The narration offset is invalid.")
        labels = audio_track_labels(self.audio_sources)
        sidecars = [(self.narration, labels[0])]
        if len(self.audio_sources) == 2:
            sidecars.extend((track, AUDIO_LABELS[source]) for source, track in draft_tracks(self.narration, frames).items())
        return offset, sidecars

    def create(self):
        self.folder.mkdir(mode=0o700)
        try:
            self.write_metadata()
        except OSError:
            with suppress(OSError):
                self.discard()
            raise

    def write_metadata(self):
        pending = self.folder / "draft.tmp"
        with pending.open("xb") as file:
            file.write(json.dumps({"version": 1, "source": self.source, "audio": self.audio_sources,
                                  "annotationLocation": self.annotation_location}).encode())
            file.flush()
            os.fsync(file.fileno())
        pending.replace(self.folder / "draft.json")

    @classmethod
    def read(cls, folder):
        try:
            if str(UUID(folder.name)) != folder.name or folder.is_symlink():
                return None
            with writer_lock(folder):
                metadata_path = folder / "draft.json"
                if metadata_path.is_symlink():
                    return None
                flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
                descriptor = os.open(metadata_path, os.O_RDONLY | flags)
                with os.fdopen(descriptor, "rb") as file:
                    metadata = json.loads(file.read(256))
                if not isinstance(metadata, dict) or metadata.get("version") != 1:
                    return None
                source, audio = metadata.get("source"), metadata.get("audio")
                if source not in ("display", "window", "area") or audio not in ([], ["mic"], ["system"], ["mic", "system"]):
                    return None
                location = metadata.get("annotationLocation")
                if location is not None and (not isinstance(location, str) or str(UUID(location)) != location):
                    return None
                draft = cls(folder, source, tuple(audio), location)
                if draft.movie.is_file() and not draft.movie.is_symlink():
                    return draft
        except (OSError, ValueError):
            return None
        return None

    def discard(self):
        try:
            shutil.rmtree(self.folder)
        except FileNotFoundError:
            pass

    async def prepare_movie(self, events=()):
        """Publish a seekable MP4 from the fragments that reached disk."""
        if not self.movie.is_file():
            raise CaptureError("The screen recording contains no saved video.")
        output = self.folder / "recording.mp4"
        # Desktop pen strokes are already visible in the captured frames.
        events = [event for event in events if event.get("captured") is not True]
        with writer_lock(self.folder) as lock:
            encoding = ["-c:v", "copy"]
            if any("stroke" in event for event in events):
                font_family = "Sans"
                overlay = "ass=ink.ass"
                if os.name == "nt":
                    # libass needs a font for vector drawings, including on Windows without system fonts.
                    font = WORKSPACE_ROOT / "bin/ink-fonts/LiberationSans-Regular.ttf"
                    if font.is_file():
                        font_dir = self.folder / "ink-fonts"
                        font_dir.mkdir(exist_ok=True)
                        shutil.copyfile(font, font_dir / font.name)
                        overlay += ":fontsdir=ink-fonts"
                        font_family = "Liberation Sans"
                    encoding = ["-vf", overlay, *windows_video_args(windows_h264_encoder())]
                else:
                    encoding = ["-vf", overlay, "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"]
                await make_overlay(self.folder / "ink.ass", self.movie, events, font_family)
            offset, sidecars = self.audio_sidecars()
            duration, embedded_audio_tracks = await annotation_movie_tracks(self.movie)
            inputs = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov", "-i", self.movie.name]
            output_audio = ["-map", "0:a?", "-c:a", "copy"]
            labels = audio_track_labels(self.audio_sources)[:embedded_audio_tracks]
            if sidecars:
                output_audio = ["-c:a", "aac", "-b:a", "128k", "-t", str(duration)]
                labels = [label for _, label in sidecars]
                samples = round(offset * SAMPLE_RATE)
                for index, (path, _) in enumerate(sidecars, 1):
                    inputs.extend(["-protocol_whitelist", "file,pipe", "-f", "wav", "-i", path.name])
                    output_audio.extend(["-map", f"{index}:a:0", f"-filter:a:{index - 1}", f"adelay={samples}S,apad"])
            for index, label in enumerate(labels):
                output_audio.extend([f"-metadata:s:a:{index}", f"handler_name={label}",
                                     f"-disposition:a:{index}", "default" if index == 0 else "0"])
            inheritance = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"pass_fds": (lock,)}
            attempts = [encoding]
            if os.name == "nt" and "h264_mf" in encoding:
                fallback_encoder = windows_h264_encoder(prefer_software=True)
                if fallback_encoder:
                    attempts.append(["-vf", overlay, *windows_video_args(fallback_encoder)])
            succeeded = False
            for video_args in attempts:
                process = await asyncio.create_subprocess_exec(
                    codec_binary("ffmpeg"), "-v", "error", "-nostdin", "-y", *inputs,
                    "-map", "0:v:0", *video_args, *output_audio, "-movflags", "+faststart", output.name,
                    **inheritance,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    cwd=self.folder,
                )
                try:
                    if await process.wait() == 0:
                        succeeded = True
                        break
                finally:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
            if not succeeded:
                raise CaptureError("The screen recording contains no playable video.")
        return output, duration


@contextmanager
def writer_lock(folder, *, create=False):
    # POSIX workers inherit the lock. Windows workers live in the engine’s job
    # and terminate with it.
    path = folder / "writer.lock"
    if path.is_symlink():
        raise OSError("Invalid screen draft lock.")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor
    finally:
        os.close(descriptor)
