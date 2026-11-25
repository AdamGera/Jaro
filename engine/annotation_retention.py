"""Two-stage retention for settled screen annotations in the Session library."""
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import annotation_storage
from personal_notes import as_context
from meeting_retention import add_archive_images, snapshot_images, sync_directory, trash_path, write_archive

SCHEMA = """
CREATE TABLE IF NOT EXISTS annotation_retention (
    id INTEGER PRIMARY KEY CHECK(id=1),
    media_days INTEGER CHECK(media_days BETWEEN 1 AND 3650),
    record_days INTEGER CHECK(record_days BETWEEN 1 AND 3650),
    include_existing INTEGER NOT NULL CHECK(include_existing IN (0,1)),
    CHECK(media_days IS NULL OR record_days IS NULL OR media_days<=record_days)
);
INSERT OR IGNORE INTO annotation_retention VALUES(1,7,30,0);
CREATE TABLE IF NOT EXISTS annotation_video_cleanup (
    id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE
);
"""

# Ignore active transcription and queued writing work. Recheck around the
# filesystem operation so a settled recording can safely re-enter processing.
ELIGIBLE = """
    SELECT s.*, CASE
      WHEN julianday(s.created) + p.record_days <= julianday('now') THEN 'record'
      WHEN julianday(s.created) + p.media_days <= julianday('now')
        AND json_extract(s.options,'$.annotation.missingVideo') IS NULL
        AND json_extract(s.options,'$.audioRemovedAt') IS NULL THEN 'video'
    END AS expiry
    FROM sessions s JOIN annotation_retention p ON p.id=1
    WHERE json_type(s.options,'$.annotation')='object'
      AND (p.media_days IS NOT NULL OR p.record_days IS NOT NULL)
      AND (p.include_existing OR json_extract(s.options,'$.annotationRetentionEligible')=1)
      AND s.status IN ('complete','interrupted')
      AND NOT EXISTS(SELECT 1 FROM overviews o WHERE o.id=s.id AND o.status='queued')
      AND NOT EXISTS(SELECT 1 FROM chat_turns c WHERE c.id=s.id AND c.status='streaming')
      AND (? IS NULL OR s.id=?)
"""

SNAPSHOT_TABLES = {
    "sessions": "id", "chunks": "session_id", "notes": "id", "overviews": "id",
    "window_notes": "id", "personal_notes": "id", "chat_turns": "id",
}


def policy(db):
    row = db.execute("SELECT media_days,record_days,include_existing FROM annotation_retention WHERE id=1").fetchone()
    return {"mediaDays": row[0], "recordDays": row[1], "includeExisting": bool(row[2])}


def set_policy(db, media_days, record_days, include_existing):
    if any(days is not None and (type(days) is not int or not 1 <= days <= 3650)
           for days in (media_days, record_days)):
        raise ValueError("Retention must be between 1 and 3650 days.")
    if record_days is not None and media_days is not None and record_days < media_days:
        raise ValueError("The annotation must stay at least as long as its video.")
    if type(include_existing) is not bool:
        raise ValueError("Choose whether to include older annotations.")
    db.execute("UPDATE annotation_retention SET media_days=?,record_days=?,include_existing=? WHERE id=1",
               (media_days, record_days, include_existing))
    return policy(db)


def recover_stage(staging):
    # These directories only ever contain duplicate files. The originals are
    # preserved until macOS has accepted the staged copy and SQLite commits.
    if staging.is_symlink():
        raise OSError("Annotation Trash staging must be a local directory.")
    if staging.is_dir():
        for path in staging.glob("transfer-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)


def snapshot(db, key):
    return {table: [dict(row) for row in db.execute(
        f"SELECT * FROM {table} WHERE {column}=? ORDER BY rowid", (key,))]
        for table, column in SNAPSHOT_TABLES.items()}


def movie_path(library, db, row, options):
    folder = library.media_folder(db, row["id"], options)
    path = folder / row["filename"]
    if folder.is_symlink() or path.parent != folder or path.is_symlink():
        raise OSError("Annotation media is outside its managed directory.")
    return path


def identity(path):
    if not path.exists():
        return None
    info = path.stat()
    if not path.is_file():
        raise OSError("The annotation movie is unavailable.")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def staging_folder(library, root=None):
    if root is not None:
        library_id = hashlib.sha256(os.fsencode(library.root.resolve())).hexdigest()[:24]
        return root / (".jaro-annotation-trash-" + library_id)
    return library.root / ".annotation-trash"


def transfer(library, key, trash):
    """Move an archive to Trash, then retire the corresponding live data."""
    with library.connection() as db:
        initial = db.execute(ELIGIBLE, (key, key)).fetchone()
        if initial is None or not initial["expiry"]:
            return False
        options = json.loads(initial["options"])
        location = options.get("annotationLocation")
        root = annotation_storage.location_root(db, location) if location else None
    staging = staging_folder(library, root)
    staging.mkdir(mode=0o700, exist_ok=True)
    if staging.is_symlink():
        raise OSError("Annotation Trash staging must be a local directory.")
    with TemporaryDirectory(dir=staging, prefix="transfer-") as directory:
        directory = Path(directory)
        with library.connection() as db:
            db.execute("BEGIN")
            row = db.execute(ELIGIBLE, (key, key)).fetchone()
            if row is None or not row["expiry"]:
                return False
            kind = row["expiry"]
            options = json.loads(row["options"])
            movie = movie_path(library, db, row, options)
            original = identity(movie)
            if kind == "record":
                saved = snapshot(db, key)
                images = snapshot_images(db, key)
                transcript, _ = library._result(db, row)
                path = directory / f"Jaro Annotation {key}"
                files = {"Narration.md": transcript["text"] + "\n",
                         "Notes.md": as_context(db, key) + "\n"}
                tables = dict(saved)
                add_archive_images(files, tables, key, images)
                files["Annotation.json"] = json.dumps({"version": 1, "id": key, "tables": tables}, ensure_ascii=False) + "\n"
                write_archive(path, files, movie.parent, media_folder="Capture")
            else:
                path = directory / f"Jaro Annotation Video {key}{movie.suffix}"
                if original is not None:
                    annotation_storage.duplicate(movie, path)
                    sync_directory(directory)
                    sync_directory(staging)

        if path.exists():
            trash(path)
            if path.exists():
                raise OSError("The annotation is still waiting to move to Trash.")

        with library.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(ELIGIBLE, (key, key)).fetchone()
            if current is None or current["expiry"] != kind:
                return False
            options = json.loads(current["options"])
            if movie_path(library, db, current, options) != movie:
                return False
            if identity(movie) != original:
                return False
            if kind == "record":
                if snapshot(db, key) != saved or snapshot_images(db, key) != images:
                    return False
                library._delete_session(db, key, current["status"], options)
            else:
                when = datetime.now(timezone.utc).isoformat()
                db.execute("UPDATE sessions SET options=json_set(options,'$.audioRemovedAt',?) WHERE id=?", (when, key))
                db.execute("INSERT INTO annotation_video_cleanup VALUES(?)", (key,))
    return True


def cleanup_videos(library):
    with library.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("""SELECT s.id,s.filename,s.options FROM annotation_video_cleanup c JOIN sessions s ON s.id=c.id
            WHERE json_type(s.options,'$.annotation')='object' AND json_extract(s.options,'$.audioRemovedAt') IS NOT NULL""").fetchall()
        failures = []
        for row in rows:
            try:
                path = movie_path(library, db, row, json.loads(row["options"]))
                path.unlink(missing_ok=True)
                if path.parent.is_dir():
                    sync_directory(path.parent)
                db.execute("DELETE FROM annotation_video_cleanup WHERE id=?", (row["id"],))
            except OSError:
                failures.append(row["id"])
    return failures


def sweep(library, trash=trash_path):
    with library.retention_lock:
        library.recover_annotation_files()
        recover_stage(staging_folder(library))
        with library.connection() as db:
            locations = db.execute("SELECT id,folder FROM annotation_locations").fetchall()
        for location, path in locations:
            try:
                recover_stage(staging_folder(library, annotation_storage.root_path(path, location)))
            except OSError:
                continue
        changed = 0
        failed = set(cleanup_videos(library))
        with library.connection() as db:
            keys = [row[0] for row in db.execute(f"SELECT id FROM ({ELIGIBLE}) WHERE expiry IS NOT NULL", (None, None))]
        for key in keys:
            try:
                changed += transfer(library, key, trash)
            except OSError:
                failed.add(key)
        return changed, sorted(failed | set(cleanup_videos(library)))
