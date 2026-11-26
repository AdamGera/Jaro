"""Read and write independent copies of saved annotations."""
from datetime import datetime
from contextlib import suppress
import json
import math
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import tempfile

from annotation_assets import (KEYFRAME_KINDS, KEYFRAME_SIZE, MAX_TIMELINE_SECONDS, annotation_input,
                               annotation_png, annotation_timeline, number)
from annotation_storage import canonical_id
from saved_annotations import annotation_context, annotation_detail, frame_time
import session_images

PACKAGE_SUFFIX = ".jaroannotation"
MANIFEST = "annotation.json"
MAX_PACKAGE_MANIFEST = 64 * 1024 * 1024
UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def is_package_reference(value):
    path = Path(value)
    return path.suffix.lower() == PACKAGE_SUFFIX or path.name == MANIFEST


def _plain_stat(path, file_type):
    info = path.lstat()
    if not file_type(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Annotation packages require regular files and folders.")
    return info


def _json_number(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite annotation number.")
    return parsed


def _package_manifest(path):
    expected = _plain_stat(path, stat.S_ISREG)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise ValueError("The annotation manifest changed while opening it.")
        if opened.st_size > MAX_PACKAGE_MANIFEST:
            raise ValueError("The annotation manifest exceeds the size limit.")
        data = stream.read(MAX_PACKAGE_MANIFEST + 1)
    if len(data) > MAX_PACKAGE_MANIFEST:
        raise ValueError("The annotation manifest exceeds the size limit.")
    try:
        return json.loads(data.decode("utf-8"), parse_float=_json_number, parse_constant=_json_number)
    except (ValueError, RecursionError) as error:
        raise ValueError("The annotation package has an unreadable manifest.") from error


def _package_asset(folder, value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        raise ValueError("The annotation package has an invalid asset path.")
    parts = value.split("/")
    def is_reserved(part):
        if hasattr(ntpath, "isreserved"):
            return ntpath.isreserved(part)
        return PureWindowsPath(part).is_reserved()

    if any(not part or part.endswith((".", " ")) or UNSAFE_FILENAME.search(part) or is_reserved(part) for part in parts):
        raise ValueError("The annotation package has an invalid asset path.")
    path = folder
    try:
        for index, part in enumerate(parts, 1):
            path /= part
            _plain_stat(path, stat.S_ISDIR if index < len(parts) else stat.S_ISREG)
    except FileNotFoundError:
        return None  # The rest of the package remains readable after media is removed.
    return str(path)


def read_package(reference):
    folder = Path(reference).expanduser()
    if folder.name == MANIFEST:
        folder = folder.parent
    if folder.suffix.lower() != PACKAGE_SUFFIX:
        raise ValueError("Choose a .jaroannotation folder or its annotation.json manifest.")
    try:
        expected = _plain_stat(folder, stat.S_ISDIR)
        folder = folder.resolve(strict=True)
        if not os.path.samestat(expected, _plain_stat(folder, stat.S_ISDIR)):
            raise ValueError("The annotation folder changed while opening it.")
        manifest_path = folder / MANIFEST
        document = _package_manifest(manifest_path)
    except FileNotFoundError as error:
        raise ValueError("The annotation package or its manifest is missing.") from error
    if not isinstance(document, dict) or type(document.get("formatVersion")) is not int or document["formatVersion"] != 1:
        raise ValueError("Unsupported annotation package version.")
    capture = document.get("annotation")
    strokes = annotation_timeline(capture)
    transcript = capture.get("transcript")
    duration = capture.get("durationSeconds")
    created = capture.get("created")
    if (not canonical_id(capture.get("id")) or not isinstance(capture.get("title"), str)
            or not isinstance(created, str) or capture.get("status") not in ("complete", "interrupted")
            or (duration is not None and not number(duration, 0, MAX_TIMELINE_SECONDS))
            or not isinstance(transcript, dict) or not isinstance(transcript.get("text"), str)
            or not isinstance(transcript.get("words"), list) or not isinstance(transcript.get("segments"), list)
            or (transcript.get("interruption") is not None and not isinstance(transcript["interruption"], str))):
        raise ValueError("Invalid annotation package details.")
    if datetime.fromisoformat(created).utcoffset() is None:
        raise ValueError("The annotation package has an invalid recording date.")
    images = capture.get("images", [])
    if not isinstance(images, list) or len(images) > session_images.MAX_IMAGES:
        raise ValueError("The annotation package has an invalid image list.")
    keyframes = []
    for index in capture.get("keyframes", []):
        kind = KEYFRAME_KINDS[index - 1]
        if path := _package_asset(folder, f"frames/{kind}.png"):
            with suppress(ValueError, FileNotFoundError):
                annotation_png(Path(path), kind, KEYFRAME_SIZE)
                keyframes.append({"index": index, "time": capture["frames"][index - 1], "path": path})
    return document, {
        "version": 1, "id": capture["id"], "name": capture["title"], "created": created, "source": capture["source"],
        "status": capture["status"], "durationSeconds": duration, "strokeCount": len(strokes), "frames": capture["frames"],
        "transcript": transcript["text"], "interruption": transcript.get("interruption"), "route": None,
        "packagePath": str(folder), "manifestPath": str(manifest_path),
        **{f"{kind}Path": _package_asset(folder, capture[kind]) if kind in capture else None
           for kind in ("video", "audio", "contactSheet")},
        "imagePaths": [path for image in images if (path := _package_asset(folder, image))],
        **({"keyframes": keyframes} if keyframes else {}),
        **({"missingVideo": capture["missingVideo"]} if "missingVideo" in capture else {}),
    }


def capture_document(detail):
    source = Path(detail["manifestPath"]) if detail["manifestPath"] else None
    if source is None:
        raise ValueError("The saved annotation is unavailable. Reconnect the Annotations folder if needed.")
    manifest = json.loads(annotation_input(source, "manifest").read_bytes())
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("The saved annotation has an unreadable stroke timeline.")
    # Portable references are written by Jaro, never taken from a source manifest.
    capture = {"source": detail["source"], "coordinateSpace": manifest.get("coordinateSpace"),
               "events": manifest.get("events"), "frames": detail["frames"]}
    if missing_video := detail.get("missingVideo"):
        capture["missingVideo"] = missing_video
    annotation_timeline(capture)
    capture.update(id=detail["id"], title=detail["captureTitle"], created=detail["created"], status=detail["status"],
                   durationSeconds=detail["durationSeconds"], transcript=detail["timedTranscript"], images=[])
    return {"formatVersion": 1, "annotation": capture}


def stroke_times(document):
    strokes = {}
    for event in document["annotation"]["events"]:
        if event.get("clear") is True:
            continue
        key, time = event["stroke"], event["time"]
        stroke = strokes.setdefault(key, {"stroke": key, "start": time, "end": time, "segments": 0})
        stroke["end"] = max(stroke["end"], event.get("liftedAt", time))
        stroke["segments"] += 1
    return list(strokes.values())


def write_package(library, key, destination):
    destination = Path(destination).resolve(strict=True)
    if not destination.is_dir() or destination.is_relative_to(library.root.resolve()):
        raise ValueError("Choose an export folder outside the session library.")
    detail = annotation_detail(library, key, include_capture=True)
    if detail["status"] == "processing":
        raise ValueError("Wait for the annotation narration to finish before exporting.")
    document = capture_document(detail)
    capture = document["annotation"]
    if destination.is_relative_to(Path(detail["manifestPath"]).parent.resolve()):
        raise ValueError("Choose an export folder outside the saved annotation.")
    stem = UNSAFE_FILENAME.sub("_", capture["title"])[:45].strip(" .")
    folder = Path(tempfile.mkdtemp(prefix=f"Jaro - {stem or 'Annotation'} - ", suffix=PACKAGE_SUFFIX, dir=destination))
    try:
        for key_name, path_key, name in (
            ("video", "videoPath", "recording.mp4"),
            ("audio", "audioPath", "narration.wav"),
            ("contactSheet", "contactSheetPath", "contact-sheet.png"),
        ):
            if value := detail.get(path_key):
                path = Path(value)
                if key_name == "audio":
                    name = "narration" + path.suffix
                shutil.copyfile(path, folder / name)
                capture[key_name] = name
        saved_stills = []
        for frame in detail.get("keyframes", []):
            kind = KEYFRAME_KINDS[frame["index"] - 1]
            with suppress(ValueError, FileNotFoundError):
                path = annotation_png(Path(frame["path"]), kind, KEYFRAME_SIZE)
                (folder / "frames").mkdir(exist_ok=True)
                shutil.copyfile(path, folder / "frames" / f"{kind}.png")
                saved_stills.append(frame)
        if saved_stills:
            capture["keyframes"] = [frame["index"] for frame in saved_stills]
        with library.connection() as db:
            db.execute("BEGIN")
            for index, image in enumerate(session_images.list_images(db, key), 1):
                if data := session_images.get(db, key, image["id"]):
                    name = f"images/{index:02d}.jpg"
                    (folder / "images").mkdir(exist_ok=True)
                    (folder / name).write_bytes(data)
                    capture["images"].append(name)
        summary = annotation_context(detail)["markdown"]
        if saved_stills:
            summary += "\n## Saved stills\n\n" + "".join(
                f"- [Frame {frame['index']}](frames/frame-{frame['index']:02d}.png) at {frame_time(frame['time'])}\n"
                for frame in saved_stills)
        (folder / "summary.md").write_text(summary, encoding="utf-8")
        partial = folder / ".annotation.json.tmp"
        partial.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        if partial.stat().st_size > MAX_PACKAGE_MANIFEST:
            raise ValueError("The annotation manifest exceeds the size limit.")
        partial.replace(folder / MANIFEST)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return {"saved": True, "name": folder.name}
