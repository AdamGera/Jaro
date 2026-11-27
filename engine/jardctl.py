"""Local status, dictation, sessions, meetings, annotations, history, and file transcription for Jaro."""
import argparse
import asyncio
from contextlib import ExitStack, closing
import errno
import json
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import UUID

import httpx

from annotation_agent_skill import AGENTS, manage as manage_annotation_guide
from diagnostics import EVENT_LOG, PROCESS_LOG, tail_log
from dictation_settings import SETTINGS_LIMIT, validate_changes
from engine_lifecycle import PORT, URL, desktop_control_path, library_path, read_native_token, running_engine
from engine_paths import ENGINE_ROOT, WORKSPACE_ROOT
from managed_preferences import ManagedFeatureError
from model_installation import ModelUnavailable, ensure_model, install_model, model_status
from native_recordings import normalize_meeting_title
from saved_annotations import annotation_context, annotation_detail, frame_time
from saved_dictations import history_page, preferences_path, read_private_history_plist, render_history
from saved_meetings import (
    get_meeting,
    meeting_detail,
    meeting_search_report,
    meetings as saved_meetings,
    read_portable_live_meeting,
    render_export,
    timestamp,
    use_24_hour_clock,
)
from session_store import MissingSession, SessionStore

CONTROL_LIMIT = 4 * 1024 * 1024
MACOS_ONLY_COMMANDS = frozenset({
    "start", "quit", "restart", "window", "settings",
    "launch-at-login", "check-for-updates", "update-channel",
})


class DesktopUnavailable(RuntimeError):
    pass


def control(root, token, action, *, timeout=12, **options):
    message = json.dumps({"token": token, "action": action, **options}, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    try:
        path = desktop_control_path(root)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            connection.sendall(message)
            with connection.makefile("rb") as output:
                result = json.loads(output.readline(CONTROL_LIMIT))
    except (OSError, ValueError) as error:
        raise DesktopUnavailable("Open Jaro before using desktop controls.") from error
    if not result.get("ok"):
        if result.get("code") == "managed":
            raise ManagedFeatureError(result["error"])
        raise RuntimeError(result.get("error", "The desktop command failed."))
    return result["result"]


def desktop(root, action, *, timeout=12, **options):
    try:
        token = (root / ".native-token").read_text().strip()
    except OSError as error:
        raise DesktopUnavailable("Open Jaro before using desktop controls.") from error
    return control(root, token, action, timeout=timeout, **options)


def desktop_status(root):
    try:
        return desktop(root, "desktop-status", timeout=.75)
    except DesktopUnavailable:
        return None


def _portable_commands():
    if sys.platform == "win32":
        import windows_dictation_commands as commands
    else:
        import wayland_dictation_commands as commands
    return commands


def trash_meeting(root, identifier):
    if sys.platform in ("win32", "linux"):
        return _portable_commands().dispatch_meeting_trash(root, identifier)
    if not running_engine():
        raise RuntimeError("Open Jaro before moving a meeting to Trash.")
    with httpx.Client(base_url=URL, trust_env=False, timeout=httpx.Timeout(125, connect=3)) as client:
        response = client.delete(f"/api/native/meetings/{identifier}",
                                 headers={"Origin": URL, "X-Jard-Token": read_native_token(root)})
    if response.status_code == 404:
        raise MissingSession(str(identifier))
    if response.status_code not in (202, 204):
        try:
            detail = response.json().get("detail")
        except (AttributeError, ValueError):
            detail = None
        raise RuntimeError(detail if isinstance(detail, str) else "Jaro could not move the meeting to Trash.")
    return {"id": str(identifier), "trashPending": response.status_code == 202}


def wait_for_desktop(root, *, previous=None, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = desktop_status(root)
        if status and status.get("pid") != previous:
            return status
        time.sleep(.2)
    return None


def wait_for_desktop_exit(pid, *, timeout=20):
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("Jard returned an invalid desktop process identifier.")
    with closing(select.kqueue()) as queue:
        event = select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)
        try:
            queue.control([event], 0, 0)
        except OSError as error:
            if error.errno == errno.ESRCH:
                return True
            raise
        return bool(queue.control(None, 1, timeout))


def desktop_bundle():
    if override := os.environ.get("JARD_DESKTOP_BUNDLE_PATH"):
        return Path(override).resolve()
    app = Path("/Applications/Jaro.app")
    legacy = Path("/Applications/Jard Soundfolio Local.app")
    return legacy.resolve() if not (app / "Contents/Info.plist").is_file() and (legacy / "Contents/Info.plist").is_file() else app.resolve()


def transformation_workspace(args):
    from output_transform_runtime import execute_config
    from output_transform_workspace import config_directory, find_config, initialize_workspace

    directory = (args.directory or config_directory()).expanduser().resolve()
    if args.action == "init":
        result = initialize_workspace(directory)
        return result if args.json else result["path"]
    path = find_config(directory)
    if path is None:
        raise ValueError("No Jaro configuration in this folder. Run jardctl transforms init first.")
    helper = desktop_bundle() / "Contents" / "Helpers" / "jaro-transforms"
    if WORKSPACE_ROOT != ENGINE_ROOT and not os.environ.get("JARD_DESKTOP_BUNDLE_PATH"):
        source = WORKSPACE_ROOT / "desktop" / ".build" / "transform-target" / "release" / "jaro-transforms"
        if source.is_file():
            helper = source
    reviewed = asyncio.run(execute_config(helper, path, log_setup=True))
    reviewed.pop("files", None)
    if args.json:
        return {"path": str(path), **reviewed}
    names = [f"{row['id']} · {row['name']}" for row in reviewed["transforms"]]
    return "\n".join([str(path), *names])


def desktop_lifecycle(root, action, *, force=False, hidden=False):
    if action == "start":
        if status := desktop_status(root):
            return {**status, "status": "running", "changed": False}
        bundle = desktop_bundle()
        if not (bundle / "Contents/Info.plist").is_file():
            raise RuntimeError("Install Jaro before using jardctl start.")
        command = ["/usr/bin/open", "-g"]
        for key, value in (("RUMBLE_DATA_DIR", str(root)), ("RUMBLE_PORT", os.environ.get("RUMBLE_PORT")),
                           ("RUMBLE_LOG_DIR", os.environ.get("RUMBLE_LOG_DIR"))):
            if value is not None:
                if key == "RUMBLE_LOG_DIR":
                    value = str(Path(value).expanduser().resolve())
                command.extend(("--env", f"{key}={value}"))
        command.append(str(bundle))
        if hidden:
            command.extend(("--args", "--hidden"))
        if subprocess.run(command, check=False, timeout=10).returncode:
            raise RuntimeError("macOS could not open Jaro.")
        status = wait_for_desktop(root)
        return {**(status or {"bundle": str(bundle)}), "status": "running" if status else "starting", "changed": True}

    previous = desktop(root, "desktop-" + action, timeout=90 if force else 18, **({"force": True} if force else {}))
    if not wait_for_desktop_exit(previous["pid"]):
        raise RuntimeError("Jard is still finishing its shutdown.")
    if action == "quit":
        return {**previous, "status": "stopped"}
    status = wait_for_desktop(root, previous=previous["pid"])
    if status and Path(status["bundle"]).resolve() != Path(previous["bundle"]).resolve():
        raise RuntimeError("A different Jard bundle answered after restart.")
    return {**(status or {"bundle": previous["bundle"]}), "status": "running" if status else "starting",
            "previousPid": previous["pid"], "updateApplied": previous["updateApplied"]}


def setting_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Repeated setting key: {key}")
        result[key] = value
    return result


def apply_offline_settings(encoded):
    bundle = desktop_bundle()
    try:
        with (bundle / "Contents/Info.plist").open("rb") as manifest:
            info = plistlib.load(manifest)
        name = info["CFBundleExecutable"]
        if info.get("JardOfflineSettingsProtocol") != 1 or not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError("Unsupported app bundle.")
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as error:
        raise RuntimeError("Install a Jard build that supports offline settings first.") from error
    process = subprocess.run([bundle / "Contents/MacOS" / name, "--settings-apply-offline"],
                             input=encoded, capture_output=True, timeout=8)
    try:
        result = json.loads(process.stdout)
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("The app could not apply settings offline.") from error
    if process.returncode:
        raise RuntimeError(result.get("error", "The app could not apply settings offline."))
    return result


def desktop_settings(root, args):
    match args.action:
        case "list":
            return desktop(root, "settings-list")
        case "get":
            return desktop(root, "settings-get", key=args.key)
        case "set":
            metadata = desktop(root, "settings-list").get(args.key)
            if not metadata or metadata.get("readOnly"):
                raise ValueError(f"Unknown or read-only setting: {args.key}")
            kind = metadata["type"]
            value = args.value
            if kind == "boolean":
                choices = {"true": True, "on": True, "false": False, "off": False}
                if value.lower() not in choices:
                    raise ValueError("Provide true, false, on, or off for this setting.")
                value = choices[value.lower()]
            elif kind in ("array", "integer", "number"):
                value = json.loads(value)
            changes = {args.key: value}
        case "apply":
            if args.document == "-":
                raw = sys.stdin.buffer.read(SETTINGS_LIMIT + 1)
            else:
                with Path(args.document).open("rb") as source:
                    raw = source.read(SETTINGS_LIMIT + 1)
            if len(raw) > SETTINGS_LIMIT:
                raise ValueError("The settings document exceeds 56,000 bytes.")
            changes = json.loads(raw, object_pairs_hook=setting_pairs)
            if not isinstance(changes, dict) or not changes:
                raise ValueError("Provide a nonempty JSON object of settings.")
    encoded = validate_changes(changes)
    try:
        return desktop(root, "settings-apply", changes=changes)
    except DesktopUnavailable:
        if args.action != "apply":
            raise
        return apply_offline_settings(encoded)


def live_meeting(root, *, turns=True):
    try:
        if sys.platform in ("win32", "linux"):
            return read_portable_live_meeting(root, _portable_commands().dispatch_meeting(root, "live").get("meeting"), turns=turns)
        return control(root, (root / ".native-token").read_text().strip(), "meeting-live", timeout=1, turns=turns)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError):
        return None


def watch_meetings(token):
    with httpx.Client(base_url=URL, trust_env=False, timeout=30) as client:
        with client.stream("GET", "/api/meetings/events", headers={"Authorization": "Bearer " + token}) as response:
            if response.status_code == 409:
                raise RuntimeError("Another subscriber is already watching for finished meetings.")
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    print(line.removeprefix("data: "), flush=True)


def portable_engine_status(root):
    from sdk_discovery import read_sdk_record
    from sdk_embedded import sdk_service_status

    try:
        record = read_sdk_record(root)
    except RuntimeError as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return "stopped"
        raise
    return sdk_service_status(record) or "stopped"


def running_portable_transcriber(root):
    from sdk_discovery import read_sdk_record
    from sdk_embedded import sdk_service_status

    deadline = time.monotonic() + 120
    while True:
        try:
            record = read_sdk_record(root)
        except RuntimeError as error:
            if isinstance(error.__cause__, FileNotFoundError):
                return None
            raise
        status = sdk_service_status(record)
        if status is None:
            return None
        if status == "ready":
            return record
        if status != "loading" or time.monotonic() >= deadline:
            raise RuntimeError("The running Jaro speech model is unavailable.")
        time.sleep(.25)


def transcription_service(root, scope, *, allow_prompt):
    from sdk_discovery import read_sdk_record
    from sdk_embedded import owned_transcriber

    if sys.platform in ("linux", "win32"):
        if (record := running_portable_transcriber(root)) is not None:
            return record
        ensure_model(allow_prompt=allow_prompt)
        if (record := running_portable_transcriber(root)) is not None:
            return record
        return scope.enter_context(owned_transcriber())

    engine = running_engine()
    if engine is None:
        ensure_model(allow_prompt=allow_prompt)
        engine = running_engine()
        if engine is None:
            return scope.enter_context(owned_transcriber())
    deadline = time.monotonic() + 120
    while engine and engine["status"] == "loading" and time.monotonic() < deadline:
        time.sleep(.25)
        engine = running_engine()
    if not engine or engine["status"] != "ready":
        raise RuntimeError("The running Jaro speech model is unavailable.")
    return read_sdk_record(root, PORT)


def transcribe_file(root, args):
    from rumble_model import LANGUAGES, SAMPLE_RATE
    from sdk_embedded import sdk_transcription
    from transcription_pieces import SAMPLE_BYTES, audio_pieces, has_audible_samples, wav_piece

    if any(code not in LANGUAGES for code in args.language):
        raise ValueError("Choose a language from the list.")
    parts, transcript_bytes, samples = [], 0, 0
    service = None
    progress = not args.json and sys.stderr.isatty()
    try:
        with ExitStack() as scope, closing(audio_pieces(args.file)) as pieces:
            for pcm in pieces:
                if has_audible_samples(pcm):
                    if service is None:
                        service = transcription_service(root, scope, allow_prompt=not args.json)
                    text = sdk_transcription(service, wav_piece(pcm), args.language).strip()
                    if text:
                        transcript_bytes += len(text.encode("utf8")) + 1
                        if transcript_bytes > 16 * 1024 * 1024:
                            raise ValueError("This recording’s transcript exceeds the command’s output limit.")
                        parts.append(text)
                samples += len(pcm) // SAMPLE_BYTES
                if progress:
                    print(f"\rProcessed {samples // SAMPLE_RATE} seconds of audio…", end="", file=sys.stderr, flush=True)
    finally:
        if progress and samples:
            print(file=sys.stderr)
    if not parts:
        no_speech(args.json)
    text = " ".join(parts)
    print(json.dumps({"text": text}, ensure_ascii=False) if args.json else text)


def no_speech(json_output):
    message = "No speech was detected."
    print(json.dumps({"code": "no-speech", "error": message}) if json_output else message, file=sys.stderr)
    raise SystemExit(4)


def read_meetings(library, args, live, *, meetings_only=True):
    if args.action == "list":
        rows = saved_meetings(library, args.query, args.limit, since=args.since, until=args.until, live=live,
                              include_in_progress=args.include_in_progress, meetings_only=meetings_only)
        return rows if args.json else "\n".join(
            f"{row['id']}  {(row.get('meetingStartedAt') or row['created'])[:10]}  {row['name']}"
            + (f" ({row['status']})" if row["status"] != "complete" else "") for row in rows)
    if args.action == "search":
        target = getattr(args, "meeting", None) if meetings_only else getattr(args, "session", None)
        report = meeting_search_report(library, args.query, args.limit, identifier=target, since=args.since,
                                       until=args.until, live=live, include_in_progress=args.include_in_progress,
                                       fuzzy=args.fuzzy, meetings_only=meetings_only)
        return report["passages"] if args.json else render_meeting_search(report, meetings_only=meetings_only)
    if args.json and args.format:
        raise ValueError("Choose a single meeting output format.")
    detail = meeting_detail(get_meeting(library, args.id, live=live, meetings_only=meetings_only), args.start, args.end)
    if args.json:
        return detail
    kind = args.format or "md"
    if kind in ("srt", "vtt"):
        return render_export(detail, kind)
    return render_export(detail, kind, use_24_hour=use_24_hour_clock(library), companion=kind == "md")


def render_meeting_search(report, *, meetings_only=True):
    def offset(turn):
        start = turn["start"]
        return timestamp(start) if start is not None else "—"

    rows, total = report["passages"], report["totalMatches"]
    matched, searched = len(report["matchingMeetings"]), report["searchedMeetings"]
    passage_label = "passage" if total == 1 else "passages"
    noun = "meeting" if meetings_only else "session"
    meeting_label = noun if searched == 1 else f"{noun}s"
    lines = [f"{total} matching {passage_label} across {matched} of {searched} {meeting_label}."]
    if len(rows) < total:
        lines.append(f"Showing the top {len(rows)}.")
    for meeting in report["matchingMeetings"]:
        lines.append(f"{meeting['id']}  {meeting['title']}  {meeting['matchCount']} matches")
    for hit in rows:
        when = f"{offset(hit)} @ {hit['absoluteTime']}" if "absoluteTime" in hit else offset(hit)
        source = hit.get("transcriptSource") or hit["source"]
        speaker = f"  {hit['speaker']}" if hit["speaker"] else ""
        lines.extend(("", f"{hit['id']}  {when}  {hit['title']}  {source}{speaker}"))
        for direction in ("before", "hit", "after"):
            if direction == "hit":
                lines.append(f"  Hit: {hit['snippet']}")
            elif excerpt := hit.get(direction):
                speaker = f" {excerpt['speaker']}" if excerpt["speaker"] else ""
                lines.append(f"  {direction.capitalize()} ({offset(excerpt)}{speaker}): {excerpt['text']}")
    return "\n".join(lines)


def read_history_snapshot(root, **filters):
    if sys.platform in ("linux", "win32"):
        from portable_dictation_history import PortableDictationHistory

        return PortableDictationHistory(root).page(**filters)
    path = preferences_path()
    snapshot = None
    try:
        checkpoint = desktop(root, "history-checkpoint")
    except DesktopUnavailable:
        checkpoint = None
    except RuntimeError as error:
        if str(error) not in ("Unknown Jard control action.", "Unknown desktop control."):
            raise
        checkpoint = None
    if checkpoint is not None:
        if not isinstance(checkpoint, str) or not re.fullmatch(r"history-[0-9a-f-]{36}\.plist", checkpoint):
            raise RuntimeError("Jaro returned an invalid live history file.")
        snapshot_path = desktop_control_path(root).with_name(checkpoint)
        try:
            saved = read_private_history_plist(snapshot_path)
            if "JardSystemDictationHistory" not in saved:
                raise RuntimeError("Jaro's live history file is unavailable.")
            if saved.get("bundleId") == path.stem:
                snapshot = saved
        finally:
            snapshot_path.unlink(missing_ok=True)
    return history_page(path, **filters, snapshot=snapshot)


def read_history(args):
    if args.text and args.json:
        raise ValueError("Choose either JSON or text output.")
    legacy = not (
        any(value is not None for value in (args.since, args.until, args.app, args.grep, args.search, args.limit))
        or args.raw or args.oldest_first or args.text
    )
    page_size = 15 if legacy else 50
    result = read_history_snapshot(
        library_path(),
        since=args.since, until=args.until, app=args.app, grep=args.grep, search=args.search,
        oldest_first=args.oldest_first, query=args.query, offset=args.offset,
        limit=page_size if args.limit is None else args.limit, raw=args.raw, legacy=legacy,
    )
    return json.dumps(result, ensure_ascii=False) if args.json or legacy else render_history(result)


def portable_vocabulary(root, action, term="", sounds_like=None):
    from dictation_output import validate_dictation_vocabulary
    from portable_dictation_history import PortableDictationHistory

    history = PortableDictationHistory(root)
    if action == "list":
        return {"entries": history.vocabulary_entries()}
    term = term.strip()
    if not term:
        raise ValueError("Enter one term of up to 80 characters.")
    if action == "remove" and not history.path.is_file():
        return {"changed": False, "entries": []}
    history.initialize()
    base, _, _ = history.output_preferences()
    key = term.casefold()
    updated = [dict(entry, soundsLike=list(entry["soundsLike"])) for entry in base]
    if action == "add":
        requested = sounds_like or []
        existing = next((entry for entry in updated if entry["term"].casefold() == key), None)
        if existing is not None:
            known = {key, *(alias.casefold() for alias in existing["soundsLike"])}
            for alias in requested:
                cleaned = alias.strip()
                if cleaned and cleaned.casefold() not in known:
                    known.add(cleaned.casefold())
                    existing["soundsLike"].append(cleaned)
        else:
            updated.append({"term": term, "soundsLike": list(requested)})
    else:
        updated = [entry for entry in updated if entry["term"].casefold() != key]
    validated = validate_dictation_vocabulary(updated)
    changed = validated != base
    if changed and not history.save_vocabulary(base, validated):
        raise RuntimeError("Vocabulary changed while updating it. Retry the command.")
    return {"changed": changed, "entries": validated}


def read_annotations(library, args):
    if args.action == "list":
        return {"version": 1, "annotations": library.annotations(limit=args.limit)}
    from annotation_package import capture_document, is_package_reference, read_package, stroke_times

    document = None
    inspect_capture = args.action == "inspect" and (args.json or args.strokes)
    if is_package_reference(args.id):
        document, detail = read_package(args.id)
    else:
        detail = annotation_detail(library, args.id, include_capture=inspect_capture or args.action == "zoom")
    if args.action in ("frames", "zoom"):
        from annotation_review import extract_frames, zoom

        if args.action == "frames":
            return asyncio.run(extract_frames(detail, library.root, args))
        return asyncio.run(zoom(detail, document or capture_document(detail), library.root, args))
    if inspect_capture:
        if document is None:
            document = capture_document(detail)
        if args.strokes:
            return {"id": detail["id"], "strokes": stroke_times(document)}
        return document
    if args.action in ("context", "inspect"):
        return {**annotation_context(detail), "contactSheetPath": detail["contactSheetPath"]}
    return detail


def annotation_area(value):
    try:
        bounds = [int(part) for part in value.split(",")]
    except ValueError:
        bounds = []
    if len(bounds) != 4:
        raise argparse.ArgumentTypeError("Use four integer pixel values as X,Y,WIDTH,HEIGHT.")
    return bounds


def _add_recording_read_subparsers(subparsers, *, target_flag, json_parent):
    listing = subparsers.add_parser("list", parents=[json_parent])
    listing.add_argument("--query", default="")
    listing.add_argument("--limit", type=int, choices=range(1, 201), metavar="1–200", default=20)
    search = subparsers.add_parser("search", parents=[json_parent])
    search.add_argument("query")
    search.add_argument(target_flag, help=f"Search a {target_flag.lstrip('-')} UUID, unique prefix, or latest.")
    search.add_argument("--no-fuzzy", action="store_false", dest="fuzzy")
    search.add_argument("--limit", type=int, choices=range(1, 51), metavar="1–50", default=10)
    for action in (listing, search):
        action.add_argument("--since", help="Include items overlapping this date or time onward.")
        action.add_argument("--until", help="Include items starting before this time. A date includes that entire day.")
        action.add_argument("--no-in-progress", action="store_false", dest="include_in_progress")
    show = subparsers.add_parser("show")
    show.add_argument("id", help=f"{target_flag.lstrip('-').capitalize()} UUID, unique prefix, or latest.")
    show.add_argument("--from", dest="start", help="Start offset in seconds, MM:SS, HH:MM:SS, or with an s, m, or h suffix.")
    show.add_argument("--to", dest="end", help="End offset in the same format as --from.")
    formats = show.add_mutually_exclusive_group()
    formats.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    for kind in ("md", "notes", "srt", "vtt"):
        formats.add_argument("--" + kind, dest="format", action="store_const", const=kind)


def build_parser():
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(prog="jardctl", description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    actions = parser.add_subparsers(dest="command", required=True)

    actions.add_parser("status", parents=[json_parent], help="Check local engine and desktop status.")
    logs = actions.add_parser("logs", help="Read local diagnostic logs without opening Jaro.")
    logs.add_argument("--process", action="store_true", help="Read the engine process log.")
    logs.add_argument("-n", "--lines", type=int, default=50, metavar="COUNT", help="Show 0 to 10,000 recent lines.")
    logs.add_argument("-f", "--follow", action="store_true", help="Print new lines until interrupted.")

    start = actions.add_parser("start", parents=[json_parent], help="Open Jaro if it is closed (macOS).")
    start.add_argument("--hidden", action="store_true", help="Start in the menu bar without opening the workspace.")
    quit_app = actions.add_parser("quit", parents=[json_parent], help="Close the running desktop app (macOS).")
    restart = actions.add_parser("restart", parents=[json_parent], help="Reopen the running desktop app at the same path (macOS).")
    for command in (quit_app, restart):
        command.add_argument("--force", action="store_true", help="Stop active captures and finish saving before closing.")

    window = actions.add_parser("window", help="Show or hide the running app's workspace (macOS).")
    window_actions = window.add_subparsers(dest="action", required=True)
    show_window = window_actions.add_parser("show", parents=[json_parent])
    show_window.add_argument("pane", nargs="?", default="settings",
                             choices=("settings", "general", "dictation", "history", "output", "meetings", "meeting-library", "annotate", "home"))
    window_actions.add_parser("hide", parents=[json_parent])

    settings = actions.add_parser("settings", help="Read or atomically change running desktop preferences (macOS).")
    settings_actions = settings.add_subparsers(dest="action", required=True)
    settings_actions.add_parser("list", parents=[json_parent])
    settings_actions.add_parser("get", parents=[json_parent]).add_argument("key")
    setting = settings_actions.add_parser("set", parents=[json_parent])
    setting.add_argument("key")
    setting.add_argument("value")
    settings_actions.add_parser("apply", parents=[json_parent]).add_argument("document", help="Path to a JSON settings object, or - for stdin.")

    transforms = actions.add_parser("transforms", help="Create or check a local transformation configuration.")
    transform_actions = transforms.add_subparsers(dest="action", required=True)
    for action in ("init", "check"):
        command = transform_actions.add_parser(action, parents=[json_parent])
        command.add_argument("--directory", type=Path, help="Use a configuration folder other than Jaro's default.")

    vocabulary = actions.add_parser("vocabulary", help="Manage the shared dictation vocabulary.")
    vocabulary_actions = vocabulary.add_subparsers(dest="action", required=True)
    for name in ("list", "add", "remove"):
        command = vocabulary_actions.add_parser(name, parents=[json_parent])
        if name != "list":
            command.add_argument("term")
        if name == "add":
            command.add_argument("--sounds-like", metavar="SPELLINGS", help="Comma-separated alternate spellings.")

    login = actions.add_parser("launch-at-login", parents=[json_parent], help="Read or change macOS Launch at Login.")
    login.add_argument("enabled", nargs="?", choices=("on", "off"))
    actions.add_parser("check-for-updates", parents=[json_parent], help="Ask the desktop updater to check now (macOS).")
    update_channel = actions.add_parser("update-channel", parents=[json_parent], help="Read or select a configured desktop update channel (macOS).")
    update_channel.add_argument("channel", nargs="?", choices=("stable", "dev"))
    update_channel.add_argument("--confirm", metavar="OFFER", help="Confirm the displayed release digest before downloading a channel switch.")

    model = actions.add_parser("model", help="Check or install Jaro’s pinned local models.")
    model_commands = model.add_subparsers(dest="action", required=True)
    speech_status = model_commands.add_parser("status", parents=[json_parent])
    speech_install = model_commands.add_parser("install", parents=[json_parent])
    speech_install.add_argument("--from", dest="source", type=Path, help="Verify and copy from a local folder.")
    speech_install.add_argument("--repair", action="store_true", help="Verify installed files and replace damaged copies.")
    for command in (speech_status, speech_install):
        command.add_argument("--all", dest="all_models", action="store_true", help="Include speaker labels and local writing.")

    file_cmd = actions.add_parser("transcribe", parents=[json_parent], help="Transcribe a local audio or video file.")
    file_cmd.add_argument("file", type=Path)
    file_cmd.add_argument("--language", action="append", default=[])

    dictate = actions.add_parser("dictation", parents=[json_parent], help="Control live system dictation.")
    dictate.add_argument("action", choices=("start", "stop", "toggle", "cancel", "paste-last"))
    dictate.add_argument("--raw", action="store_true", help="Start without applying output processing.")
    dictate.add_argument("--print", dest="print_transcript", action="store_true",
                         help="Write the transcript to stdout only (stop on all desktops, start on macOS).")

    sessions = actions.add_parser("sessions", help="List, search, and read all saved sessions and recordings.")
    session_actions = sessions.add_subparsers(dest="action", required=True)
    _add_recording_read_subparsers(session_actions, target_flag="--session", json_parent=json_parent)

    meetings = actions.add_parser("meetings", help="Control meeting recordings and read saved meetings.")
    meeting_actions = meetings.add_subparsers(dest="action", required=True)
    for action in ("status", "stop", "copy-last"):
        meeting_actions.add_parser(action, parents=[json_parent])
    meeting_record = meeting_actions.add_parser("record", parents=[json_parent])
    meeting_record.add_argument("--title", help="Name the meeting when recording starts.")
    meeting_delete = meeting_actions.add_parser("delete", parents=[json_parent], help="Move a saved meeting and its files to Trash.")
    meeting_delete.add_argument("id", type=UUID, help="Full meeting UUID from meetings list.")
    meeting_actions.add_parser("watch", help="Stream finished meeting IDs as JSON lines (macOS).")
    _add_recording_read_subparsers(meeting_actions, target_flag="--meeting", json_parent=json_parent)

    annotate = actions.add_parser("annotate", aliases=["capture"], help="Control live annotations and read saved captures.")
    annotation_actions = annotate.add_subparsers(dest="action", required=True)
    annotation_list = annotation_actions.add_parser("list", parents=[json_parent])
    annotation_list.add_argument("--limit", type=int, choices=range(1, 201), metavar="1–200", default=20)
    annotation_skill = annotation_actions.add_parser("skill", parents=[json_parent], help="Print or install the guide agents use to review captures.")
    skill_options = annotation_skill.add_mutually_exclusive_group()
    skill_options.add_argument("--status", action="store_true", help="List guide locations and installation status.")
    skill_options.add_argument("--install", choices=AGENTS, help="Install the guide for a configured agent.")
    skill_options.add_argument("--update", choices=AGENTS, help="Replace this agent's regular guide with the bundled version.")
    annotation_show = annotation_actions.add_parser("show", parents=[json_parent], help="Show annotation details and available file paths.")
    annotation_prompt = annotation_actions.add_parser("context", parents=[json_parent], help="Write a saved annotation's narration and frame times as Markdown.")
    annotation_inspect = annotation_actions.add_parser("inspect", parents=[json_parent], help="Read the narration, full JSON, or recorded strokes.")
    annotation_inspect.add_argument("--strokes", action="store_true", help="List recorded strokes with their times.")
    for action in (annotation_show, annotation_prompt, annotation_inspect):
        action.add_argument("id", nargs="?", default="latest",
                            help="Session UUID, unique prefix, latest (the default), or an exported package path.")
    annotation_frames = annotation_actions.add_parser("frames", parents=[json_parent], help="Export a range of video stills as PNG files.")
    annotation_frames.add_argument("--from", dest="start", required=True, help="Start time in seconds, MM:SS, or HH:MM:SS.")
    annotation_frames.add_argument("--to", dest="end", required=True, help="End time in the same format as --from.")
    annotation_frames.add_argument("--fps", type=int, choices=range(1, 241), default=4, metavar="1–240")
    annotation_zoom = annotation_actions.add_parser("zoom", parents=[json_parent], help="Build a contact sheet around a moment in the recording.")
    focus = annotation_zoom.add_mutually_exclusive_group(required=True)
    focus.add_argument("--at", help="A timestamp or phrase from the timed narration.")
    focus.add_argument("--mark", type=int, help="A stroke ID from annotate inspect --strokes.")
    annotation_zoom.add_argument("--span", type=float, default=2, help="Seconds of video around that moment.")
    annotation_zoom.add_argument("--frames", type=int, choices=range(1, 49), default=12, metavar="1–48")
    annotation_zoom.add_argument("--full", action="store_true", help="Include the whole picture when reviewing a stroke.")
    for action in (annotation_frames, annotation_zoom):
        action.add_argument("id", nargs="?", default="latest", help="Session UUID, unique prefix, latest, or an exported package path.")
        action.add_argument("--output", type=Path, help="Write a new folder inside this directory. Defaults to the system temporary folder.")
    annotation_start = annotation_actions.add_parser("start", parents=[json_parent], help="Record the display under the pointer or choose a source.")
    annotation_start.add_argument("--source", choices=("display", "window", "area", "phone"),
                                  help="explicit source (phone requires a USB iPhone or iPad)")
    annotation_start.add_argument("--language", action="append", help="Override the saved dictation language hints.")
    annotation_start.add_argument("--target", help="Windows display or window ID returned by annotate targets.")
    annotation_start.add_argument("--area", type=annotation_area, metavar="X,Y,WIDTH,HEIGHT", help="Exact capture area on native Linux X11.")
    annotation_targets = annotation_actions.add_parser("targets", parents=[json_parent], help="List Windows screen sources or report the desktop picker.")
    annotation_targets.add_argument("--source", choices=("display", "window"), default="display")
    annotation_actions.add_parser("status", parents=[json_parent], help="Report the active or most recent capture.")
    annotation_stop = annotation_actions.add_parser("stop", parents=[json_parent], help="Stop and save the active capture.")
    annotation_stop.add_argument("--from-project", action="store_true",
                                 help="Match OpenCode rules using this terminal's working folder (Linux and Windows).")
    annotation_actions.add_parser("toggle", parents=[json_parent], help="Start a display capture or stop and save the active capture.")
    annotation_actions.add_parser("draw", parents=[json_parent], help="Start drawing or finish drawing in an active capture (Linux).")
    annotation_actions.add_parser("cancel", parents=[json_parent], help="Discard the unsaved capture.")
    annotation_storage = annotation_actions.add_parser("storage", parents=[json_parent], help="Read or change the folder for new annotations.")
    folder_options = annotation_storage.add_mutually_exclusive_group()
    folder_options.add_argument("--folder", type=Path, help="Save new annotations in this existing directory.")
    folder_options.add_argument("--default", action="store_true", help="Use the Jaro Session library.")
    folder_options.add_argument("--reveal", action="store_true", help="Open the current Annotations folder in the desktop file manager.")
    annotation_destinations = annotation_actions.add_parser("destinations", parents=[json_parent], help="List open local OpenCode v2 sessions.")
    annotation_destinations.add_argument("--query", default="", help="Match a session title, directory, or ID.")
    annotation_destinations.add_argument("--capture", help="Include the saved destination and suggestions for this annotation.")
    annotation_remember = annotation_actions.add_parser("remember", parents=[json_parent], help="Remember an OpenCode destination for a captured app, site, or terminal project.")
    annotation_remember.add_argument("id", help="Annotation Session UUID, unique prefix, or latest.")
    annotation_remember.add_argument("--scope", choices=("app", "site", "terminal"), required=True)
    annotation_remember.add_argument("--scope-key", help="Exact capture scope from annotate destinations, such as a website project path.")
    annotation_remember.add_argument("--to", required=True, help="Exact OpenCode session ID.")
    annotation_remember.add_argument("--service", required=True, help="Service identity returned by annotate destinations.")
    annotation_remember.add_argument("--automatic", action="store_true", help="Send future matching captures after a visible countdown.")
    annotation_route = annotation_actions.add_parser("route", parents=[json_parent], help="Read or cancel a pending automatic annotation send.")
    annotation_route.add_argument("id", help="Exact annotation Session UUID.")
    annotation_route.add_argument("--cancel", action="store_true")
    annotation_forget = annotation_actions.add_parser("forget", parents=[json_parent], help="Remove a saved OpenCode destination rule.")
    annotation_forget.add_argument("id", help="Annotation Session UUID, unique prefix, or latest.")
    annotation_forget.add_argument("--rule", required=True, help="Rule ID returned by annotate destinations.")
    annotation_send = annotation_actions.add_parser("send", parents=[json_parent], help="Queue a saved annotation in the selected OpenCode session.")
    annotation_send.add_argument("id", help="Annotation Session UUID, unique prefix, or latest.")
    annotation_send.add_argument("--to", required=True, metavar="SESSION", help="Exact OpenCode session ID.")
    annotation_send.add_argument("--service", help="Service identity returned by annotate destinations.")
    annotation_deliveries = annotation_actions.add_parser("deliveries", parents=[json_parent], help="Read recent OpenCode receipts for a capture.")
    annotation_deliveries.add_argument("id", help="Annotation Session UUID, unique prefix, or latest.")
    annotation_undo = annotation_actions.add_parser("undo", parents=[json_parent], help="Withdraw an annotation or roll back its isolated OpenCode turn.")
    annotation_undo.add_argument("delivery_id", help="Receipt ID returned by annotate send or annotate deliveries.")
    annotation_undo.add_argument("--restore-files", action="store_true",
                                 help="Allow OpenCode to restore changed files when the turn ran a successful file edit.")
    annotation_correct = annotation_actions.add_parser("correct", parents=[json_parent], help="Ask OpenCode to disregard a delivered annotation.")
    annotation_correct.add_argument("delivery_id", help="Receipt ID returned by annotate send or annotate deliveries.")

    actions.add_parser("mcp", help="Serve read-only session, meeting, annotation, and dictation tools over stdio.")

    history = actions.add_parser("history", help="Read or clear saved dictations.")
    history.add_argument("--query", help="Filter a history page by text or application.")
    history.add_argument("--offset", type=int, default=0, help="Skip this many matching entries.")
    history.add_argument("--since", help="Include dictations from this date or time.")
    history.add_argument("--until", help="Stop before this time. A date includes that entire day.")
    history.add_argument("--app", help="Match the exact application name, ignoring case.")
    history.add_argument("--grep", help="Match a literal substring in raw or final text.")
    history.add_argument("--search", help="Rank raw and final text with fuzzy transcript search.")
    history.add_argument("--oldest-first", action="store_true")
    history.add_argument("--limit", type=int, metavar="1–10000")
    history.add_argument("--raw", action="store_true", help="Include the original transcript.")
    output = history.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    output.add_argument("--text", action="store_true", help="Read history as text.")
    history_actions = history.add_subparsers(dest="action")
    clear_history = history_actions.add_parser("clear", parents=[json_parent], help="Remove all retained dictations and reset Paste Last.")
    clear_history.add_argument("--yes", action="store_true", help="Confirm deletion of the entire dictation history.")

    actions.add_parser("setup", parents=[json_parent], help="Replay dictation setup.")
    onboarding = actions.add_parser("onboarding", help="Replay the dictation tour.")
    onboarding_actions = onboarding.add_subparsers(dest="action", required=True)
    onboarding_actions.add_parser("reset", parents=[json_parent], help="Reopen setup with your existing permissions and preferences.")

    return parser, dictate, annotate, annotation_start


def run_history(args):
    if args.action != "clear":
        print(read_history(args))
        return
    if not args.yes:
        raise ValueError("This deletes every retained dictation. Pass --yes to clear history.")
    if (args.offset or args.oldest_first or args.raw or args.text or any(
        value is not None for value in (args.query, args.since, args.until, args.app, args.grep, args.search, args.limit)
    )):
        raise ValueError("Clear history removes all entries and does not accept read filters.")
    if sys.platform in ("linux", "win32"):
        from portable_dictation_history import PortableDictationHistory

        removed = PortableDictationHistory(library_path()).remove()
        result = {"removed": removed, "changed": bool(removed), "pasteLastCleared": True}
    else:
        result = desktop(library_path(), "history-clear", confirm=True)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["removed"]:
        count = result["removed"]
        print(f"Cleared {count} dictation{'s' if count != 1 else ''}.")
    else:
        print("Cleared Paste Last." if result["changed"] or result.get("pasteLastCleared") else "History is already empty.")


def run_vocabulary(root, args):
    sounds_like = None
    if args.action == "add" and args.sounds_like is not None:
        sounds_like = [cleaned for value in args.sounds_like.split(",") if (cleaned := value.strip())]
    if sys.platform in ("linux", "win32"):
        result = portable_vocabulary(root, args.action, term=getattr(args, "term", ""), sounds_like=sounds_like)
    else:
        options = {}
        if args.action != "list":
            options["term"] = args.term.strip()
        if sounds_like is not None:
            options["soundsLike"] = sounds_like
        result = desktop(root, "vocabulary-" + args.action, **options)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif args.action == "list":
        for entry in result["entries"]:
            spellings = ", ".join(entry["soundsLike"])
            print(entry["term"] + (f"\t{spellings}" if spellings else ""))
    else:
        print("Vocabulary updated." if result["changed"] else "Vocabulary unchanged.")


def run_annotations(root, args):
    if args.action == "skill":
        action = "install" if args.install else "update" if args.update else "status" if args.status else "guide"
        value = manage_annotation_guide(action, args.install or args.update)
        if args.json:
            print(json.dumps(value))
        elif action == "guide":
            sys.stdout.buffer.write(value["markdown"].encode("utf-8"))
        else:
            for row in value["agents"]:
                state = row["state"] if row["found"] else "agent not configured"
                print(f"{row['agent']}  {state}  {row['path']}")
        return

    portable_action = args.action in ("start", "status", "stop", "toggle", "cancel", "targets", "draw")
    handoff = args.action in ("destinations", "send", "undo", "correct", "remember", "forget", "route")
    if args.action in ("list", "show", "context", "inspect", "frames", "zoom"):
        value = read_annotations(SessionStore(root, read_only=True), args)
    elif portable_action and sys.platform in ("linux", "win32"):
        options = {}
        if args.action in ("start", "targets"):
            options["source"] = args.source or "display"
        if args.action == "start":
            options.update(target=args.target or "", languages=args.language)
            if args.area is not None:
                options["area"] = args.area
        if args.action == "stop" and args.from_project:
            options["terminalFolder"] = str(Path.cwd())
        value = _portable_commands().dispatch_annotation(root, args.action, **options)
    elif args.action == "stop" and args.from_project:
        raise ValueError("--from-project is available on Linux and Windows.")
    elif args.action == "targets" or (args.action == "start" and args.target is not None):
        raise ValueError("Use Jaro’s macOS capture picker to choose a source.")
    elif args.action == "deliveries":
        from annotation_routing import recent_deliveries
        value = recent_deliveries(root, args.id)
    else:
        options = {}
        if args.action == "start":
            if args.source is not None:
                options["source"] = args.source
            if args.language is not None:
                options["languages"] = args.language
        elif args.action == "destinations":
            options["query"] = args.query
            if args.capture is not None:
                options["id"] = args.capture
        elif args.action == "storage":
            options["storageAction"] = "set" if args.folder else "reset" if args.default else "reveal" if args.reveal else "status"
            if args.folder:
                options["folder"] = str(args.folder.expanduser().resolve())
        elif args.action == "remember":
            options = {"id": args.id, "scope": args.scope, "target": args.to,
                       "service": args.service, "automatic": args.automatic}
            if args.scope_key is not None:
                options["scopeKey"] = args.scope_key
        elif args.action == "route":
            options = {"id": args.id, "routeAction": "cancel" if args.cancel else "status"}
        elif args.action == "forget":
            options = {"id": args.id, "ruleId": args.rule}
        elif args.action == "send":
            options = {"id": args.id, "target": args.to}
            if args.service is not None:
                options["service"] = args.service
        elif args.action in ("undo", "correct"):
            options["deliveryId"] = args.delivery_id
            if args.action == "undo" and args.restore_files:
                options["restoreFiles"] = True
        try:
            if sys.platform in ("linux", "win32") and (handoff or args.action == "storage"):
                if options.get("restoreFiles"):
                    raise ValueError("Use Undo without --restore-files to withdraw a queued annotation.")
                if args.action in ("undo", "correct"):
                    options = {"id": args.delivery_id}
                value = _portable_commands().dispatch_annotation(root, args.action, **options)
            else:
                timeout = {"undo": 26, "send": 18, "correct": 18, "destinations": 18, "remember": 18}
                value = desktop(root, "annotation-" + args.action, timeout=timeout.get(args.action, 12), **options)
        except RuntimeError as error:
            if args.action != "route" or not (isinstance(error, DesktopUnavailable) or
                    (sys.platform in ("linux", "win32") and error.__cause__ is not None)):
                raise
            from annotation_automation import stored_route_status
            from annotation_deliveries import AnnotationDeliveries
            capture = str(UUID(args.id))
            outbox = AnnotationDeliveries(root)
            if args.cancel:
                outbox.set_pending_route(capture, "cancelled", "Automatic send cancelled.")
            value = stored_route_status(outbox, outbox.get_route(capture), capture)

    if args.action in ("context", "inspect") and not args.json:
        if args.action == "inspect" and args.strokes:
            lines = [f"Stroke {stroke['stroke']}  {frame_time(stroke['start'])}–{frame_time(stroke['end'])}"
                     for stroke in value["strokes"]]
            print("\n".join(lines) or "No strokes recorded.")
        else:
            print(value["markdown"], end="")
    elif args.action == "frames" and not args.json:
        print("\n".join(value["frames"]))
    elif args.action == "zoom" and not args.json:
        print(value["path"])
        print("Frame times " + ", ".join(f"{second:.3f}s" for second in value["frames"]))
        if value["narration"]:
            print(value["narration"])
    else:
        print(json.dumps(value, ensure_ascii=False))


def run_status(root):
    if sys.platform in ("linux", "win32"):
        value = {"engine": portable_engine_status(root), "model": "Reson8/Rumble-2-Flash"}
    else:
        engine = running_engine()
        value = {"engine": engine["status"] if engine else "stopped",
                 "model": engine["model"] if engine else "Reson8/Rumble-2-Flash"}
        if engine:
            try:
                value["desktop"] = desktop(root, "status")
            except RuntimeError:
                pass
    print(json.dumps(value, ensure_ascii=False))


def main():
    parser, dictate, annotate, annotation_start = build_parser()
    args = parser.parse_args()
    annotation_command = args.command in ("annotate", "capture")
    if annotation_command and args.action == "start" and args.area is not None:
        if sys.platform != "linux":
            annotation_start.error("Exact coordinates require native Linux X11. Use --source area to select the region.")
        if args.source not in (None, "area") or args.target is not None:
            annotation_start.error("--area can only be combined with --source area and cannot be combined with --target.")
        args.source = "area"
    if annotation_command and args.action == "draw" and sys.platform != "linux":
        annotate.error("Use the recording controls to draw on this desktop.")
    if args.command == "dictation":
        if args.raw and args.action not in ("start", "toggle"):
            dictate.error("--raw requires start or toggle")
        if args.print_transcript and args.action not in ("start", "stop"):
            dictate.error("--print requires start or stop")
    if sys.platform != "darwin" and (
        args.command in MACOS_ONLY_COMMANDS
        or (args.command == "meetings" and args.action == "watch")
        or (os.name == "nt" and args.command == "transforms")
    ):
        parser.error(f"`jardctl {args.command}` is only available on macOS.")

    try:
        if annotation_command and args.action == "skill":
            run_annotations(None, args)
            return
        if args.command == "dictation" and args.print_transcript and args.action == "start" and sys.platform != "darwin":
            raise RuntimeError("Starting a print-only dictation is available on macOS. Use `dictation stop --print` here.")
        if args.command == "model":
            result = (install_model(args.source, progress=not args.json and sys.stderr.isatty(),
                                    all_models=args.all_models, repair=args.repair)
                      if args.action == "install" else model_status(all_models=args.all_models))
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif args.all_models:
                print("Jaro’s local models are ready." if result["ready"] else
                      "Run `jardctl model install --all` to set up the full local model set.")
            else:
                print("Rumble2 Flash is ready." if result["ready"] else str(ModelUnavailable()))
            return
        if args.command == "logs":
            if not 0 <= args.lines <= 10_000:
                raise ValueError("Choose a log line count from 0 to 10,000.")
            name = PROCESS_LOG if args.process else EVENT_LOG
            if hasattr(signal, "SIGPIPE"):
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
            try:
                tail_log(name, args.lines, args.follow, sys.stdout.buffer)
            except FileNotFoundError as error:
                raise ValueError("Jaro has no process log yet." if args.process else "Jaro has no event log yet.") from error
            return
        if args.command == "history":
            run_history(args)
            return
        if args.command == "transforms":
            result = transformation_workspace(args)
            print(json.dumps(result, ensure_ascii=False) if args.json else result)
            return

        root = library_path()
        if args.command == "status":
            run_status(root)
            return
        if args.command in ("setup", "onboarding"):
            result = _portable_commands().dispatch_onboarding(root) if sys.platform in ("win32", "linux") else desktop(root, "setup")
            print(json.dumps(result, ensure_ascii=False))
            return
        if args.command == "dictation" and sys.platform in ("win32", "linux"):
            result = _portable_commands().dispatch(root, args.action, raw=args.raw, printed=args.print_transcript)
            if args.print_transcript and result.get("status") == "no-speech":
                no_speech(args.json)
            if args.print_transcript and not args.json:
                print(result["text"])
            elif args.json:
                print(json.dumps(result, ensure_ascii=False))
            return
        if annotation_command:
            run_annotations(root, args)
            return
        if args.command == "window":
            options = {"pane": args.pane} if args.action == "show" else {}
            print(json.dumps(desktop(root, "window-" + args.action, **options), sort_keys=True))
            return
        if args.command == "vocabulary":
            run_vocabulary(root, args)
            return
        if args.command in ("start", "quit", "restart", "settings", "launch-at-login", "check-for-updates", "update-channel"):
            if args.command in ("start", "quit", "restart"):
                value = desktop_lifecycle(root, args.command, force=getattr(args, "force", False), hidden=getattr(args, "hidden", False))
            elif args.command == "settings":
                value = desktop_settings(root, args)
            elif args.command == "update-channel":
                if args.channel and args.confirm:
                    raise ValueError("Select a channel first, then confirm its release offer.")
                options = {"channel": args.channel} if args.channel else {"offer": args.confirm} if args.confirm else {}
                value = desktop(root, args.command, **options)
            else:
                options = {"enabled": args.enabled == "on"} if args.command == "launch-at-login" and args.enabled else {}
                value = desktop(root, args.command, **options)
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return
        if args.command == "mcp":
            from meeting_mcp import serve

            serve(
                SessionStore(root, read_only=True),
                lambda turns=True: live_meeting(root, turns=turns),
                history_reader=lambda **filters: read_history_snapshot(root, **filters),
            )
            return
        if args.command in ("sessions", "meetings") and args.action in ("list", "show", "search"):
            meetings_only = args.command == "meetings"
            live = live_meeting(root, turns=args.action != "list") if getattr(args, "include_in_progress", True) else None
            result = read_meetings(SessionStore(root, read_only=True), args, live, meetings_only=meetings_only)
            if args.action == "show" and args.format in ("srt", "vtt"):
                sys.stdout.write(result)
            else:
                print(result.rstrip("\n") if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
            return
        if args.command == "meetings" and args.action == "delete":
            result = trash_meeting(root, args.id)
            if args.json:
                print(json.dumps(result))
            else:
                print("The meeting is archived and waiting to move to Trash." if result["trashPending"]
                      else "Moved the meeting to Trash.")
            return
        if args.command == "meetings" and args.action in ("status", "record", "stop", "copy-last") and sys.platform in ("win32", "linux"):
            title = getattr(args, "title", None) or ""
            result = _portable_commands().dispatch_meeting(root, args.action, title=normalize_meeting_title(title) if title.strip() else "")
            print(json.dumps(result, ensure_ascii=False))
            return
        if args.command == "transcribe":
            transcribe_file(root, args)
            return

        engine = running_engine()
        if not engine:
            raise RuntimeError("Open Jaro to start the local engine.")
        token = (root / ".native-token").read_text().strip()
        if args.command == "meetings":
            if args.action == "watch":
                watch_meetings(token)
                return
            options = {}
            if args.action == "record" and args.title and args.title.strip():
                options["title"] = normalize_meeting_title(args.title)
            value = control(root, token, "meeting-" + args.action, **options)
        elif args.command == "dictation":
            # An older desktop must reject these actions instead of treating print
            # as an ordinary stop that can insert into the focused application.
            action = args.action
            timeout = 12
            if args.raw or args.print_transcript:
                flags = ("-raw" if args.raw else "") + ("-print" if args.print_transcript else "")
                action = f"dictation-{action}{flags}"
                if args.print_transcript:
                    timeout = 31 * 60 + 10 if args.action == "start" else 70
            value = control(root, token, action, timeout=timeout)
            if args.print_transcript:
                if value.get("status") == "no-speech":
                    no_speech(args.json)
                if not args.json:
                    print(value["text"])
                    return
        print(json.dumps(value, ensure_ascii=False))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ManagedFeatureError as error:
        print(json.dumps({"code": "managed", "error": str(error)}) if args.json else str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except ModelUnavailable as error:
        print(json.dumps({"code": "model-unavailable", "error": str(error)}) if args.json else str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, OSError, RuntimeError, httpx.HTTPError, MissingSession, sqlite3.Error, subprocess.TimeoutExpired) as error:
        subject = "annotation" if annotation_command else "session" if args.command == "sessions" else "meeting"
        message = (f"{subject.capitalize()} not found." if isinstance(error, MissingSession) else
                   f"The saved {subject} library is unavailable." if isinstance(error, sqlite3.Error) else str(error))
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        else:
            print(message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
