"""Read saved annotations and render portable context for an explicit handoff."""
import base64
import re

from annotation_assets import MISSING_VIDEO_REASONS, annotation_origin
from session_store import MissingSession

MAX_INLINE_SHEET = 9 * 1024 * 1024  # 12 MiB after base64 encoding.


def annotation_detail(library, identifier):
    identifier = identifier.lower()
    if identifier == "latest":
        matches = library.annotations(limit=1)
    else:
        if not re.fullmatch(r"[0-9a-f][0-9a-f-]{5,35}", identifier):
            raise MissingSession(identifier)
        matches = library.annotations(limit=2, prefix=identifier)
    if len(matches) > 1:
        raise ValueError("That annotation ID prefix matches more than one Session.")
    if not matches:
        raise MissingSession(identifier)
    row = matches[0]
    key = row["id"]
    session = library.get(key)
    transcript = session["transcript"]

    def path_for(read, *parameters):
        try:
            path, _ = read(key, *parameters)
        except (MissingSession, OSError):
            return None
        return str(path) if not path.is_symlink() and not path.parent.is_symlink() else None

    origin = annotation_origin(session["annotation"].get("origin"))
    missing_video = session["annotation"].get("missingVideo")
    media_path = path_for(library.media)
    return {"version": 1, **row, **({"origin": origin} if origin else {}),
            "status": session["status"], "durationSeconds": session["duration"],
            "route": f"/?session={key}&tab=overview",
            "transcript": transcript["text"], "interruption": transcript.get("interruption"),
            **({"missingVideo": missing_video, "audioPath": media_path} if missing_video else {}),
            "videoPath": None if missing_video else media_path, "manifestPath": path_for(library.annotation_asset, "manifest"),
            "contactSheetPath": path_for(library.annotation_asset, "contact-sheet")}


def frame_time(seconds):
    whole, tenth = divmod(int(seconds * 10 + .0001), 10)
    minutes, seconds = divmod(whole, 60)
    hours, minutes_of_hour = divmod(minutes, 60)
    clock = f"{hours}:{minutes_of_hour:02d}" if hours else str(minutes)
    return f"{clock}:{seconds:02d}.{tenth}"


def annotation_context(detail, *, cursor=0, max_chars=None):
    transcript = detail["transcript"]
    if not 0 <= cursor <= len(transcript):
        raise ValueError("The cursor is beyond the annotation narration.")
    end = len(transcript) if max_chars is None else min(len(transcript), cursor + max_chars)
    lines = ["# Screen annotation", "", f"- Capture ID: {detail['id']}", f"- Source: {detail['source']}",
             f"- Recorded: {detail['created']}", f"- Narration status: {detail['status']}", f"- Strokes: {detail['strokeCount']}"]
    if duration := detail["durationSeconds"]:
        lines.append(f"- Duration: {frame_time(duration)}")
    if detail["frames"]:
        lines.extend(["", "## Contact sheet times", "", ", ".join(frame_time(time) for time in detail["frames"])])
    if reason := MISSING_VIDEO_REASONS.get(detail.get("missingVideo")):
        lines.extend(["", reason])
    if detail["interruption"]:
        lines.extend(["", detail["interruption"]])
    lines.extend(["", "## Narration", "", transcript[cursor:end] if transcript else "No narration is available."])
    if end < len(transcript):
        lines.extend(["", "The narration continues on the next page."])
    return {"id": detail["id"], "route": detail["route"], "markdown": "\n".join(lines) + "\n",
            "next_cursor": end if end < len(transcript) else None}


def inline_contact_sheet(library, key):
    try:
        path, _ = library.annotation_asset(key, "contact-sheet")
        if path.parent.is_symlink():
            return None
        with path.open("rb") as source:
            data = source.read(MAX_INLINE_SHEET + 1)
    except (MissingSession, OSError):
        return None
    if len(data) > MAX_INLINE_SHEET or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return base64.b64encode(data).decode("ascii")
