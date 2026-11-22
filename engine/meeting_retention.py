"""Whole-meeting retention admission and recoverable macOS Trash snapshots."""
import json
import os
import shutil
import subprocess
from pathlib import Path

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS meeting_retention (
    id INTEGER PRIMARY KEY CHECK(id=1), days INTEGER NOT NULL CHECK(days IN (0,1,7,30))
);
CREATE TABLE IF NOT EXISTS meeting_trash (
    id TEXT PRIMARY KEY, sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meeting_audio_policy (
    id INTEGER PRIMARY KEY CHECK(id=1), days INTEGER NOT NULL CHECK(days IN (-1,0,1,7,30))
);
CREATE TABLE IF NOT EXISTS meeting_audio_trash (
    id TEXT PRIMARY KEY
);
DROP VIEW IF EXISTS expired_meeting_audio;
DROP VIEW IF EXISTS readable_sessions;
DROP VIEW IF EXISTS expired_meetings;
DROP VIEW IF EXISTS archivable_meetings;
CREATE VIEW archivable_meetings AS
    SELECT s.id FROM sessions s
    WHERE s.status IN ('complete','interrupted')
      AND coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)
      AND NOT EXISTS(SELECT 1 FROM meeting_finalization f WHERE f.session_id=s.id)
      AND NOT EXISTS(SELECT 1 FROM overviews o WHERE o.id=s.id AND o.status='queued')
      AND NOT EXISTS(SELECT 1 FROM meeting_briefs b WHERE b.id=s.id AND b.status='queued')
      AND NOT EXISTS(SELECT 1 FROM meeting_exports x WHERE x.id=s.id AND
          (x.status='pending' OR (x.status='ready' AND x.refresh>0)))
      AND NOT EXISTS(SELECT 1 FROM meeting_export_renames m WHERE m.id=s.id)
      AND NOT EXISTS(SELECT 1 FROM meeting_export_edits e WHERE e.id=s.id)
      AND NOT EXISTS(SELECT 1 FROM chat_turns c WHERE c.id=s.id AND c.status='streaming');
CREATE VIEW expired_meetings AS
    SELECT s.id FROM archivable_meetings a JOIN sessions s ON s.id=a.id LEFT JOIN meeting_retention r ON r.id=1
    WHERE s.status='complete' AND coalesce(r.days,json_extract(s.options,'$.retentionApplies'),0)>0
      AND julianday('now') > coalesce(julianday(json_extract(s.options,'$.meetingEndedAt')),julianday(s.created))
                               + coalesce(r.days,json_extract(s.options,'$.retentionApplies'),0);
CREATE VIEW readable_sessions AS
    SELECT s.* FROM sessions s WHERE NOT EXISTS(SELECT 1 FROM expired_meetings e WHERE e.id=s.id);
CREATE VIEW expired_meeting_audio AS
    SELECT s.id FROM readable_sessions s JOIN meeting_audio_policy p ON p.id=1
    WHERE p.days>0 AND s.status='complete'
      AND coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)
      AND json_extract(s.options,'$.audioRemovedAt') IS NULL
      AND julianday('now') > coalesce(julianday(json_extract(s.options,'$.meetingEndedAt')),julianday(s.created)) + p.days
      AND NOT EXISTS(SELECT 1 FROM meeting_finalization f WHERE f.session_id=s.id)
      AND NOT EXISTS(SELECT 1 FROM overviews o WHERE o.id=s.id AND o.status='queued')
      AND NOT EXISTS(SELECT 1 FROM meeting_briefs b WHERE b.id=s.id AND b.status='queued')
      AND NOT EXISTS(SELECT 1 FROM chat_turns c WHERE c.id=s.id AND c.status='streaming');
COMMIT;
"""

OWNED_TABLES = {
    "sessions": "id", "chunks": "session_id", "meeting_rolling": "session_id",
    "notes": "id", "overviews": "id", "window_notes": "id", "personal_notes": "id", "chat_turns": "id",
    "meeting_briefs": "id",
}


def readable_source(db):
    # Offline clients can also open libraries that the upgraded engine has yet to migrate.
    return ("readable_sessions" if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='readable_sessions'").fetchone() else "sessions")


def trash_path(path):
    helper = shutil.which("jard-dialog")
    if helper is None:
        # The source checkout has no bundled helper. JXA calls Foundation directly.
        command = ["/usr/bin/osascript", "-l", "JavaScript", "-e", """
ObjC.import('Foundation')
function run(argv) {
  const path = $.NSURL.fileURLWithPath(argv[0])
  if (!$.NSFileManager.defaultManager.trashItemAtURLResultingItemURLError(path, null, null))
    throw Error('Could not move the meeting to the Trash')
}
""", str(path)]
    else:
        command = [helper, "trash", str(path)]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (subprocess.SubprocessError, OSError) as error:
        raise OSError("Could not move the meeting to the macOS Trash.") from error


def write_snapshot(db, identifier, directory, source_audio, transcript, notes):
    tables = {
        table: [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE {column}=?", (identifier,))]
        for table, column in OWNED_TABLES.items()
    }
    files = {
        "Meeting.json": json.dumps({"version": 1, "id": identifier, "tables": tables}, ensure_ascii=False) + "\n",
        "Transcript.md": transcript,
        "Notes.md": notes,
    }
    write_archive(directory, files, source_audio, media_folder="Audio")


def write_archive(directory, files, source_media, *, media_folder):
    directory.mkdir(mode=0o700)
    for name, text in files.items():
        with (directory / name).open("x", encoding="utf8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())

    if source_media.is_dir() and not source_media.is_symlink():
        audio = directory / media_folder
        audio.mkdir(mode=0o700)
        for file in source_media.iterdir():
            if file.is_file() and not file.is_symlink():
                os.link(file, audio / file.name, follow_symlinks=False)
        sync_directory(audio)
    sync_directory(directory)
    sync_directory(directory.parent)
    sync_directory(directory.parent.parent)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_path(root, identifier):
    return Path(root) / ".meeting-trash" / f"Jard Meeting {identifier}"


def audio_trash_path(root, identifier):
    return Path(root) / ".meeting-trash" / f"Jard Meeting Audio {identifier}"
