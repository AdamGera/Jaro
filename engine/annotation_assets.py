"""Bounded native annotation artifacts published with their recorded Session."""
import json
import math
from pathlib import Path, PurePosixPath
import re
import struct

ASSETS = {
    "manifest": ("annotation.json", ".annotation.json", "application/json", 24 * 1024 * 1024),
    "contact-sheet": ("contact-sheet.png", ".annotation.png", "image/png", 12 * 1024 * 1024),
}
MAX_TIMELINE_SECONDS = 3 * 60 * 60 + 2
MISSING_VIDEO_REASONS = {
    "noFrames": "The screen recording produced no video frames.",
    "permissionDenied": "macOS declined Screen Recording access.",
    "screenInterrupted": "The video source stopped before it saved a usable picture.",
}


def staged_annotation_paths(source):
    return {kind: source.with_suffix(extension) for kind, (_, extension, _, _) in ASSETS.items()}


def annotation_input(path, kind):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= ASSETS[kind][3]:
        raise ValueError("An annotation asset is missing or exceeds its size limit.")
    return path


def number(value, lower, upper):
    return type(value) in (int, float) and lower <= value <= upper and math.isfinite(value)


def point(value):
    return isinstance(value, list) and len(value) == 2 and all(number(axis, 0, 1) for axis in value)




TERMINAL_APPS = frozenset((
    "com.apple.terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
    "com.github.wez.wezterm", "org.alacritty", "dev.warp.warp-stable", "dev.warp.warp-preview",
))


def terminal_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//") or len(value) > 4096:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    path = PurePosixPath(value)
    if ".." in path.parts or path in (PurePosixPath("/"), Path.home(), Path.home().resolve()):
        return None
    return path


def terminal_metadata(value):
    if not isinstance(value, dict):
        return None
    folder, project = terminal_path(value.get("folder")), terminal_path(value.get("project"))
    if not folder or not project or not folder.is_relative_to(project):
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
        if terminal := terminal_metadata(value.get("terminal")):
            origin["terminal"] = terminal
        return origin
    site = value.get("site")
    if isinstance(site, str) and len(site) <= 255:
        try:
            host = site.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            host = ""
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", host):
            origin["site"] = host
    return origin

def read_staged_annotation(source):
    paths = staged_annotation_paths(source)
    with annotation_input(paths["manifest"], "manifest").open("rb") as data:
        manifest = json.load(data)
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1
            or manifest.get("coordinateSpace") != "normalized-bottom-left" or manifest.get("source") not in ("display", "window", "area", "phone")):
        raise ValueError("Invalid annotation manifest.")
    events, frames = manifest.get("events"), manifest.get("frames")
    if not isinstance(events, list) or len(events) > 100_000 or not isinstance(frames, list) or len(frames) > 6:
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
    if frames:
        with annotation_input(paths["contact-sheet"], "contact-sheet").open("rb") as sheet:
            header = sheet.read(24)
        if len(header) != 24 or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            raise ValueError("Invalid annotation contact sheet.")
        width, height = struct.unpack(">II", header[16:24])
        if not 0 < width <= 872 or not 0 < height <= 846:
            raise ValueError("Invalid annotation contact sheet size.")
    result = {"version": 1, "source": manifest["source"], "strokeCount": len(strokes), "frames": frames}
    if reason:
        result["missingVideo"] = reason
    if origin := annotation_origin(manifest.get("origin")):
        result["origin"] = origin
    return result
