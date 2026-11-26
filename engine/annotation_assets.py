"""Bounded native annotation artifacts published with their recorded Session."""
import asyncio
from contextlib import suppress
import json
import math
from pathlib import Path, PureWindowsPath
import re
import struct
import subprocess
import sys

from audio_file import codec_binary
from annotation_projects import project_path
from annotation_sites import annotation_site, site_scope

ASSETS = {
    "manifest": ("annotation.json", ".annotation.json", "application/json", 24 * 1024 * 1024),
    "contact-sheet": ("contact-sheet.png", ".annotation.png", "image/png", 12 * 1024 * 1024),
}
KEYFRAME_KINDS = tuple(f"frame-{index:02d}" for index in range(1, 7))
KEYFRAME_SIZE = (1280, 800)
ASSETS.update({kind: (f"{kind}.png", f".annotation.{kind}.png", "image/png", 5 * 1024 * 1024)
               for kind in KEYFRAME_KINDS})
MAX_TIMELINE_SECONDS = 3 * 60 * 60 + 2
MISSING_VIDEO_REASONS = {
    "noFrames": "The screen recording produced no video frames.",
    "permissionDenied": "macOS declined Screen Recording access.",
    "screenInterrupted": "The video source stopped before it saved a usable picture.",
}


def staged_annotation_paths(source):
    return {kind: source.with_suffix(extension) for kind, (_, extension, _, _) in ASSETS.items()}


async def annotation_movie_info(path, probe=None):
    duration, audio_tracks = await annotation_movie_tracks(path, probe)
    return duration, audio_tracks > 0


async def annotation_movie_tracks(path, probe=None, *, container_duration=False):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = await asyncio.create_subprocess_exec(
        probe or codec_binary("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
        "-show_entries", "stream=codec_type,duration:format=duration", "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        **options,
    )
    try:
        output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    if process.returncode:
        raise ValueError("The annotation video could not be opened.")
    try:
        metadata = json.loads(output)
        streams = metadata["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        duration = metadata.get("format", {}).get("duration") if container_duration else video.get("duration")
        if not duration or duration == "N/A":
            duration = video.get("duration") if container_duration else metadata.get("format", {}).get("duration")
        duration = float(duration)
    except (ValueError, TypeError, KeyError, StopIteration) as error:
        raise ValueError("The annotation video could not be opened.") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The annotation video has no valid duration.")
    return duration, sum(stream["codec_type"] == "audio" for stream in streams)


def annotation_input(path, kind):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= ASSETS[kind][3]:
        raise ValueError("An annotation asset is missing or exceeds its size limit.")
    return path


def annotation_png(path, kind, max_size):
    with annotation_input(path, kind).open("rb") as image:
        header = image.read(24)
    label = "contact sheet" if kind == "contact-sheet" else "still"
    if len(header) != 24 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        raise ValueError(f"Invalid annotation {label}.")
    if not all(0 < size <= limit for size, limit in zip(struct.unpack(">II", header[16:24]), max_size)):
        raise ValueError(f"Invalid annotation {label} size.")
    return path


def number(value, lower, upper):
    return type(value) in (int, float) and lower <= value <= upper and math.isfinite(value)


def point(value):
    return isinstance(value, list) and len(value) == 2 and all(number(axis, 0, 1) for axis in value)


LINUX_TERMINAL_BINARIES = {
    "xterm": ("xterm", "uxterm"), "uxterm": ("xterm",),
    "gnome-terminal": ("gnome-terminal-server",), "gnome-terminal-server": ("gnome-terminal-server",),
    "org.gnome.terminal": ("gnome-terminal-server",), "org.gnome.console": ("kgx",), "kgx": ("kgx",),
    "kitty": ("kitty",), "net.kovidgoyal.kitty": ("kitty",), "konsole": ("konsole",), "org.kde.konsole": ("konsole",),
    "alacritty": ("alacritty",), "org.alacritty": ("alacritty",), "ghostty": ("ghostty",), "com.mitchellh.ghostty": ("ghostty",),
    "wezterm": ("wezterm-gui",), "org.wezfurlong.wezterm": ("wezterm-gui",),
    "foot": ("foot",), "footclient": ("foot",),
    "xfce4-terminal": ("xfce4-terminal",), "lxterminal": ("lxterminal",), "tilix": ("tilix",),
    "terminator": ("terminator",),
}
WINDOWS_TERMINALS = frozenset(("windowsterminal", "windowsterminalpreview", "cmd", "powershell", "pwsh", "conhost", "openconsole", "wezterm-gui", "alacritty"))
TERMINAL_APPS = frozenset((
    "com.apple.terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
    "com.github.wez.wezterm", "org.alacritty", "dev.warp.warp-stable", "dev.warp.warp-preview", "linux.shell", "windows.shell",
)) | {f"linux.{app}" for app in LINUX_TERMINAL_BINARIES} | {f"windows.{app}" for app in WINDOWS_TERMINALS}


def terminal_path(value):
    if not isinstance(value, str):
        return None
    path = project_path(value, windows=not value.startswith("/"))
    if path in (Path.home(), Path.home().resolve()):
        return None
    return path


def terminal_scope(path):
    key = str(path).lower() if isinstance(path, PureWindowsPath) else str(path)
    return {"kind": "terminal", "key": key, "label": path.name}


def terminal_metadata(value, *, windows=False):
    if not isinstance(value, dict):
        return None
    folder, project = terminal_path(value.get("folder")), terminal_path(value.get("project"))
    if not folder or not project or isinstance(folder, PureWindowsPath) != windows or not folder.is_relative_to(project):
        return None
    result = {"folder": str(folder), "project": str(project)}
    title, session = value.get("title"), value.get("session")
    if isinstance(title, str) and title.strip():
        result["title"] = " ".join(title.split())[:160]
    if isinstance(session, str) and re.fullmatch(r"ses_[A-Za-z0-9_-]{1,123}", session):
        result["session"] = session
    return result


def annotation_origin(value):
    if not isinstance(value, dict):
        return None
    app = value.get("app")
    if not isinstance(app, str) or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", app) or len(app) > 255:
        return None
    name = value.get("name")
    name = " ".join(name.split())[:160] if isinstance(name, str) else ""
    origin = {"app": app.lower(), "name": name or app}
    if origin["app"] in TERMINAL_APPS:
        if terminal := terminal_metadata(value.get("terminal"), windows=app.lower().startswith("windows.")):
            origin["terminal"] = terminal
        return origin
    site = annotation_site(value.get("site"))
    location = site_scope(value.get("siteUrl"), observed=True, strip_www=origin["app"].startswith(("windows.", "linux.")))
    if location and (not site or site == location.host):
        origin["site"] = location.host
        origin["siteUrl"] = location.key()
    elif site:
        origin["site"] = site
    return origin


def annotation_timeline(manifest):
    if (not isinstance(manifest, dict) or manifest.get("coordinateSpace") != "normalized-bottom-left"
            or manifest.get("source") not in ("display", "window", "area", "phone")):
        raise ValueError("Invalid annotation manifest.")
    events, frames = manifest.get("events"), manifest.get("frames")
    if not isinstance(events, list) or len(events) > 100_000 or not isinstance(frames, list) or len(frames) > len(KEYFRAME_KINDS):
        raise ValueError("Invalid annotation timeline.")
    reason = manifest.get("missingVideo")
    if "missingVideo" in manifest and (not isinstance(reason, str) or reason not in MISSING_VIDEO_REASONS or frames):
        raise ValueError("Invalid missing video state.")
    last = 0
    strokes = set()
    for event in events:
        if not isinstance(event, dict) or not number(event.get("time"), last, MAX_TIMELINE_SECONDS):
            raise ValueError("Annotation event times must be in order.")
        last = event["time"]
        if event.get("clear") is True:
            continue
        stroke = event.get("stroke")
        if not point(event.get("from")) or not point(event.get("to")) or type(stroke) is not int or not 0 <= stroke <= 2**53 - 1:
            raise ValueError("Invalid annotation stroke.")
        if "liftedAt" in event and not number(event["liftedAt"], event["time"], MAX_TIMELINE_SECONDS):
            raise ValueError("Invalid annotation stroke release time.")
        strokes.add(stroke)
    if any(not number(frame, 0, MAX_TIMELINE_SECONDS) for frame in frames) or any(a >= b for a, b in zip(frames, frames[1:])):
        raise ValueError("Invalid annotation frame times.")
    stills = manifest.get("keyframes", [])
    if (not isinstance(stills, list) or len(stills) > len(frames)
            or any(type(index) is not int or not 1 <= index <= len(frames) for index in stills)
            or any(a >= b for a, b in zip(stills, stills[1:]))):
        raise ValueError("Invalid annotation keyframe indexes.")
    return strokes


def read_staged_annotation(source):
    paths = staged_annotation_paths(source)
    with annotation_input(paths["manifest"], "manifest").open("rb") as data:
        manifest = json.load(data)
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("Invalid annotation manifest.")
    strokes = annotation_timeline(manifest)
    if manifest["frames"]:
        annotation_png(paths["contact-sheet"], "contact-sheet", (872, 846))
    result = {"version": 1, "source": manifest["source"], "strokeCount": len(strokes), "frames": manifest["frames"]}
    stills = []
    for index in manifest.get("keyframes", []):
        kind = KEYFRAME_KINDS[index - 1]
        with suppress(ValueError, OSError):
            annotation_png(paths[kind], kind, KEYFRAME_SIZE)
            stills.append(index)
    if stills:
        result["keyframes"] = stills
    if "missingVideo" in manifest:
        result["missingVideo"] = manifest["missingVideo"]
    if origin := annotation_origin(manifest.get("origin")):
        result["origin"] = origin
    return result
