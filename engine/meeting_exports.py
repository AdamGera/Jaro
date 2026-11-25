"""Publish managed meeting Markdown without overwriting an existing file."""

import asyncio
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_export_folder (
    id INTEGER PRIMARY KEY CHECK(id=1),
    folder TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_exports (
    id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    folder TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready','error')),
    filename TEXT, sha256 TEXT, stage TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS meeting_export_notes_policy (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    descriptive INTEGER NOT NULL DEFAULT 0 CHECK(descriptive IN (0,1))
);
CREATE TABLE IF NOT EXISTS meeting_clock_style (
    id INTEGER PRIMARY KEY CHECK(id=1),
    use_24_hour INTEGER NOT NULL CHECK(use_24_hour IN (0,1))
);
CREATE TABLE IF NOT EXISTS meeting_export_edits (
    id TEXT NOT NULL REFERENCES meeting_exports(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('transcript','notes')),
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, stage TEXT NOT NULL,
    replacing INTEGER NOT NULL CHECK(replacing IN (0,1)),
    PRIMARY KEY(id,kind)
);
CREATE TABLE IF NOT EXISTS meeting_export_trash (
    id TEXT NOT NULL REFERENCES meeting_trash(id) ON DELETE CASCADE,
    folder TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
    filename TEXT NOT NULL, sha256 TEXT NOT NULL, file_inode INTEGER,
    staging TEXT NOT NULL,
    PRIMARY KEY(id,filename)
);
CREATE TABLE IF NOT EXISTS meeting_export_renames (
    id TEXT PRIMARY KEY REFERENCES meeting_exports(id) ON DELETE CASCADE,
    previous TEXT NOT NULL, target TEXT NOT NULL,
    notes_previous TEXT, notes_target TEXT,
    applied INTEGER NOT NULL DEFAULT 0 CHECK(applied IN (0,1))
);
"""

UNSAFE_TITLE = str.maketrans({character: "-" for character in '/\\:*?"<>|'})

def migrate(db):
    db.executescript(SCHEMA)
    if "descriptive" not in {row[1] for row in db.execute("PRAGMA table_info(meeting_export_notes_policy)")}:
        db.execute("ALTER TABLE meeting_export_notes_policy ADD COLUMN descriptive INTEGER NOT NULL DEFAULT 0 CHECK(descriptive IN (0,1))")
    columns = {row[1] for row in db.execute("PRAGMA table_info(meeting_exports)")}
    for name, definition in {
        "split": "INTEGER NOT NULL DEFAULT 0",
        "refresh": "INTEGER NOT NULL DEFAULT 0",
        "notes_filename": "TEXT",
        "notes_sha256": "TEXT",
        "notes_removed": "INTEGER NOT NULL DEFAULT 0",
        "file_inode": "INTEGER",
        "notes_inode": "INTEGER",
    }.items():
        if name not in columns:
            db.execute(f"ALTER TABLE meeting_exports ADD COLUMN {name} {definition}")


def separate_notes(db):
    return bool(db.execute("SELECT 1 FROM meeting_export_notes_policy WHERE id=1 AND enabled").fetchone())


def set_separate_notes(db, enabled):
    db.execute("""INSERT INTO meeting_export_notes_policy(id,enabled) VALUES(1,?)
        ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled""", (enabled,))


def descriptive_names(db):
    return bool(db.execute("SELECT 1 FROM meeting_export_notes_policy WHERE id=1 AND descriptive").fetchone())


def set_descriptive_names(db, enabled):
    db.execute("""INSERT INTO meeting_export_notes_policy(id,enabled,descriptive) VALUES(1,0,?)
        ON CONFLICT(id) DO UPDATE SET descriptive=excluded.descriptive""", (enabled,))


def preferences(db):
    return {"folder": folder_preference(db), "separateNotes": separate_notes(db), "descriptiveNames": descriptive_names(db)}


def clock_24(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_clock_style'").fetchone():
        return False
    return bool(db.execute("SELECT 1 FROM meeting_clock_style WHERE id=1 AND use_24_hour").fetchone())


def set_clock_24(db, enabled):
    db.execute("""INSERT INTO meeting_clock_style VALUES(1,?)
        ON CONFLICT(id) DO UPDATE SET use_24_hour=excluded.use_24_hour
        WHERE meeting_clock_style.use_24_hour!=excluded.use_24_hour""", (enabled,))


def folder_preference(db):
    row = db.execute("SELECT folder FROM meeting_export_folder WHERE id=1").fetchone()
    return row["folder"] if row else None


def set_folder(db, library, selected):
    if selected is None:
        db.execute("DELETE FROM meeting_export_folder WHERE id=1")
        return None
    selected = Path(selected)
    if not selected.is_absolute():
        raise ValueError("Choose an existing folder in Finder.")
    folder = selected.resolve(strict=True)
    if folder.is_relative_to(library.resolve()):
        raise ValueError("Choose a folder outside the session library.")
    info = folder.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("Choose an existing folder in Finder.")
    db.execute("""INSERT INTO meeting_export_folder VALUES(1,?,?,?)
        ON CONFLICT(id) DO UPDATE SET folder=excluded.folder,device=excluded.device,inode=excluded.inode""",
               (str(folder), info.st_dev, info.st_ino))
    return str(folder)


def enqueue(db, key):
    db.execute("""INSERT OR IGNORE INTO meeting_exports(id,folder,device,inode,split)
        SELECT ?,folder,device,inode,? FROM meeting_export_folder WHERE id=1""", (key, separate_notes(db)))


def queue_refresh(db, key):
    db.execute("UPDATE meeting_exports SET refresh=refresh+1 WHERE id=? AND status IN ('pending','ready')", (key,))


def read(db, key):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='meeting_exports' AND type='table'").fetchone():
        return None
    row = db.execute("SELECT * FROM meeting_exports WHERE id=?", (key,)).fetchone()
    if row is None:
        return None
    return {"status": row["status"], "filename": row["filename"] if row["status"] == "ready" else None,
            "notesFilename": dict(row).get("notes_filename"), "error": row["error"],
            "refreshing": bool(dict(row).get("refresh")) and row["status"] == "ready"}


@contextmanager
def checked_folder(row):
    directory = os.open(row["folder"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino) != (row["device"], row["inode"]):
            raise OSError("Choose the export folder again in Settings.")
        yield directory
    finally:
        os.close(directory)


def file_stem(detail, existing=None):
    date = datetime.fromisoformat(detail["startedAt"] or detail["created"]).astimezone()
    stamp = re.match(r"^\d{4}-\d{2}-\d{2} \d{4}(?= )", existing or "")
    cleaned = "".join(character if character.isprintable() else " " for character in detail["title"].translate(UNSAFE_TITLE))
    title = " ".join(cleaned.split()).strip(" .-")[:80]
    title = title.encode("utf8")[:219].decode("utf8", errors="ignore").rstrip() or "Meeting"
    return f"{stamp[0] if stamp else date.strftime('%Y-%m-%d %H%M')} {title}"


def transcript_path(directory, row):
    info = os.stat(row["filename"], dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError("The original export is unavailable.")
    return Path(row["folder"]) / row["filename"]


def same_export(directory, row):
    if not row["filename"] or not row["stage"]:
        return False
    try:
        current = os.stat(row["filename"], dir_fd=directory, follow_symlinks=False)
        stage = os.stat(row["stage"], dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(stage.st_mode) and os.path.samestat(current, stage)


def mark_ready(store, row, directory):
    inode = os.stat(row["stage"], dir_fd=directory, follow_symlinks=False).st_ino
    os.fsync(directory)
    with store.connection() as db:
        db.execute("UPDATE meeting_exports SET status='ready',file_inode=?,error=NULL WHERE id=?", (inode, row["id"]))
    remove_stage(store, row, directory)


def remove_stage(store, row, directory):
    if row["stage"]:
        try:
            os.unlink(row["stage"], dir_fd=directory)
        except FileNotFoundError:
            pass
        with store.connection() as db:
            db.execute("UPDATE meeting_exports SET stage=NULL WHERE id=?", (row["id"],))


def write_stage(directory, stage, data, mode=0o600):
    descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        os.fchmod(output.fileno(), mode)
        output.flush()
        os.fsync(output.fileno())


def exists(directory, name):
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def candidate(stem, number):
    return stem if number == 1 else f"{stem} ({number})"


def publish(store, row, directory):
    from saved_meetings import meeting_detail, render_export

    if same_export(directory, row):
        mark_ready(store, row, directory)
        return
    remove_stage(store, row, directory)
    detail = meeting_detail(store.get(row["id"], include_turns=True))
    with store.connection() as db:
        use_24_hour = clock_24(db)
    data = render_export(detail, "md", use_24_hour=use_24_hour).encode("utf8")
    stage = f".jard-export-{uuid4().hex}.tmp"
    digest = hashlib.sha256(data).hexdigest()
    with store.connection() as db:
        db.execute("UPDATE meeting_exports SET filename=NULL,stage=?,sha256=? WHERE id=?", (stage, digest, row["id"]))
    write_stage(directory, stage, data)
    stem = file_stem(detail)
    for number in range(1, 101):
        base = candidate(stem, number)
        if row["split"] and exists(directory, f"{base}.notes.md"):
            continue
        name = f"{base}.transcript.md" if row["split"] else f"{base}.md"
        with store.connection() as db:
            db.execute("UPDATE meeting_exports SET filename=? WHERE id=?", (name, row["id"]))
        try:
            os.link(stage, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            continue
        mark_ready(store, {**row, "stage": stage}, directory)
        return
    raise OSError("Too many exports use the same meeting name.")


def disk_file(directory, name):
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            return "", None
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None, None
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if not os.path.samestat(before, opened):
            return "", None
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        try:
            after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        if not os.path.samestat(opened, after) or opened.st_ctime_ns != after.st_ctime_ns:
            return "", None
        return digest, opened


def is_owned(digest, info, expected, inode):
    return info is not None and digest == expected and (inode is None or info.st_ino == inode)


def recover_edit(store, edit, directory):
    filename, stage = edit["filename"], edit["stage"]
    if edit["replacing"]:
        digest, info = disk_file(directory, filename)
        published = not exists(directory, stage) and digest == edit["sha256"]
    else:
        published = same_export(directory, edit)
        info = os.stat(stage, dir_fd=directory, follow_symlinks=False) if published else None
    if published:
        os.fsync(directory)
        with store.connection() as db:
            if edit["kind"] == "notes":
                db.execute("UPDATE meeting_exports SET notes_filename=?,notes_sha256=?,notes_inode=? WHERE id=?",
                           (filename, edit["sha256"], info.st_ino, edit["id"]))
            else:
                db.execute("UPDATE meeting_exports SET sha256=?,file_inode=? WHERE id=?", (edit["sha256"], info.st_ino, edit["id"]))
    try:
        os.unlink(stage, dir_fd=directory)
    except FileNotFoundError:
        pass
    with store.connection() as db:
        db.execute("DELETE FROM meeting_export_edits WHERE id=? AND kind=?", (edit["id"], edit["kind"]))
    return published


def write_edit(store, directory, key, kind, filename, data, previous=None):
    digest = hashlib.sha256(data).hexdigest()
    original, info = previous or (None, None)
    if digest == original:
        return True
    stage = f".jard-export-{uuid4().hex}.tmp"
    with store.connection() as db:
        db.execute("INSERT INTO meeting_export_edits VALUES(?,?,?,?,?,?)",
                   (key, kind, filename, digest, stage, previous is not None))
    write_stage(directory, stage, data, stat.S_IMODE(info.st_mode) & 0o777 if info is not None else 0o600)
    try:
        if previous is None:
            os.link(stage, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        elif is_owned(*disk_file(directory, filename), original, info.st_ino):
            os.replace(stage, filename, src_dir_fd=directory, dst_dir_fd=directory)
    except FileExistsError:
        pass
    return recover_edit(store, {"id": key, "kind": kind, "filename": filename, "sha256": digest,
                                "stage": stage, "replacing": previous is not None}, directory)


def notes_markdown(detail, transcript, use_24_hour):
    from saved_meetings import render_export

    summary = {"summary": detail["notes"].get("summary"), "personalNotes": None, "overview": None}
    heading, body = render_export({**detail, "notes": summary}, "notes", use_24_hour=use_24_hour).split("\n\n", 1)
    return f"{heading}\n\n[Open transcript]({quote(transcript)})\n\n{body}".encode("utf8")


def forget_notes(store, key):
    with store.connection() as db:
        db.execute("""UPDATE meeting_exports SET notes_filename=NULL,notes_sha256=NULL,notes_inode=NULL,
            notes_removed=1 WHERE id=?""", (key,))


def refresh_notes(store, row, detail, directory, create, use_24_hour):
    name = row["notes_filename"]
    if name is not None:
        current, info = disk_file(directory, name)
        if current is None:
            forget_notes(store, row["id"])
        elif is_owned(current, info, row["notes_sha256"], row["notes_inode"]):
            write_edit(store, directory, row["id"], "notes", name, notes_markdown(detail, row["filename"], use_24_hour), (current, info))
        return
    if not create or row["notes_removed"]:
        return
    stem = row["filename"].removesuffix(".md").removesuffix(".transcript")
    data = notes_markdown(detail, row["filename"], use_24_hour)
    # A long older plain export can leave little room for an added suffix.
    base = stem.encode("utf8")[:240].decode("utf8", errors="ignore").rstrip()
    for number in range(1, 101):
        name = f"{candidate(base, number)}.notes.md"
        if not exists(directory, name) and write_edit(store, directory, row["id"], "notes", name, data):
            return
    raise OSError("Too many exports use the same meeting notes name.")


def recover_rename(store, plan, directory):
    pairs = [(plan["previous"], plan["target"])]
    if plan["notes_previous"]:
        pairs.append((plan["notes_previous"], plan["notes_target"]))
    changes = [(old, new) for old, new in pairs if old != new]
    if not plan["applied"]:
        with store.connection() as db:
            row = db.execute("SELECT * FROM meeting_exports WHERE id=?", (plan["id"],)).fetchone()
        case_changes = []
        renamed = []
        try:
            if not is_owned(*disk_file(directory, plan["previous"]), row["sha256"], row["file_inode"]):
                raise FileNotFoundError("The transcript changed during its rename.")
            if plan["notes_previous"] and not is_owned(
                *disk_file(directory, plan["notes_previous"]), row["notes_sha256"], row["notes_inode"]
            ):
                raise FileNotFoundError("The notes changed during their rename.")
            for old, new in changes:
                if same_export(directory, {"stage": old, "filename": new}):
                    names = os.listdir(directory)
                    if old in names and new not in names:
                        # On a case-insensitive volume these are already the same directory entry.
                        case_changes.append((old, new))
                else:
                    os.link(old, new, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            for old, new in case_changes:
                os.rename(old, new, src_dir_fd=directory, dst_dir_fd=directory)
                renamed.append((old, new))
            if not is_owned(*disk_file(directory, plan["target"]), row["sha256"], row["file_inode"]):
                raise FileNotFoundError("The transcript changed during its rename.")
            if plan["notes_target"] and not is_owned(
                *disk_file(directory, plan["notes_target"]), row["notes_sha256"], row["notes_inode"]
            ):
                raise FileNotFoundError("The notes changed during their rename.")
        except (FileExistsError, FileNotFoundError):
            for old, new in renamed:
                if same_export(directory, {"stage": old, "filename": new}):
                    os.rename(new, old, src_dir_fd=directory, dst_dir_fd=directory)
            names = os.listdir(directory)
            for old, new in changes:
                if old in names and new in names and same_export(directory, {"stage": old, "filename": new}):
                    os.unlink(new, dir_fd=directory)
            with store.connection() as db:
                db.execute("DELETE FROM meeting_export_renames WHERE id=?", (plan["id"],))
            raise
        os.fsync(directory)
        with store.connection() as db:
            db.execute("UPDATE meeting_exports SET filename=?,notes_filename=? WHERE id=?",
                       (plan["target"], plan["notes_target"], plan["id"]))
            db.execute("UPDATE meeting_export_renames SET applied=1 WHERE id=?", (plan["id"],))
    names = os.listdir(directory)
    for old, new in changes:
        if old in names and new in names and same_export(directory, {"stage": old, "filename": new}):
            os.unlink(old, dir_fd=directory)
    os.fsync(directory)
    with store.connection() as db:
        db.execute("UPDATE meeting_exports SET refresh=max(1,refresh) WHERE id=?", (plan["id"],))
        db.execute("DELETE FROM meeting_export_renames WHERE id=?", (plan["id"],))


def available_name(directory, original, target):
    return not exists(directory, target) or (
        original is not None and target.casefold() == original.casefold()
        and same_export(directory, {"stage": original, "filename": target}))


def rename_export(store, row, directory, detail):
    transcript, notes = row["filename"], row["notes_filename"]
    if notes:
        current, info = disk_file(directory, notes)
        if current is None:
            forget_notes(store, row["id"])
            notes = None
        elif not is_owned(current, info, row["notes_sha256"], row["notes_inode"]):
            return
    stem = file_stem(detail, existing=transcript)
    split = transcript.endswith(".transcript.md")
    for number in range(1, 101):
        base = candidate(stem, number)
        target = f"{base}.transcript.md" if split else f"{base}.md"
        notes_target = f"{base}.notes.md" if split or notes else None
        if not available_name(directory, transcript, target):
            continue
        if notes_target and not available_name(directory, notes, notes_target):
            continue
        if transcript == target and (notes is None or notes == notes_target):
            return
        plan = {"id": row["id"], "previous": transcript, "target": target,
                "notes_previous": notes, "notes_target": notes_target if notes else None, "applied": 0}
        with store.connection() as db:
            db.execute("INSERT INTO meeting_export_renames VALUES(:id,:previous,:target,:notes_previous,:notes_target,:applied)", plan)
        try:
            recover_rename(store, plan, directory)
        except FileExistsError:
            continue
        return
    raise OSError("Too many exports use the new meeting name.")


def refresh(store, row, directory, create_notes, descriptive):
    from saved_meetings import meeting_detail, render_export

    current, info = disk_file(directory, row["filename"])
    if current is None:
        return
    detail = meeting_detail(store.get(row["id"], include_turns=True))
    with store.connection() as db:
        use_24_hour = clock_24(db)
    if is_owned(current, info, row["sha256"], row["file_inode"]):
        if descriptive:
            rename_export(store, row, directory, detail)
            with store.connection() as db:
                row = db.execute("SELECT * FROM meeting_exports WHERE id=?", (row["id"],)).fetchone()
        write_edit(store, directory, row["id"], "transcript", row["filename"],
                   render_export(detail, "md", use_24_hour=use_24_hour).encode("utf8"), (current, info))
    if current and (row["notes_filename"] or detail["notes"].get("summary")):
        refresh_notes(store, row, detail, directory, create_notes, use_24_hour)


def settle_refresh(store, row, error=None):
    with store.connection() as db:
        db.execute("UPDATE meeting_exports SET refresh=0,error=? WHERE id=? AND status='ready' AND refresh=?",
                   (error, row["id"], row["refresh"]))


def sync_export(store, row, directory):
    if row["status"] == "pending":
        publish(store, row, directory)
    elif row["status"] == "error" and same_export(directory, row):
        mark_ready(store, row, directory)
    else:
        remove_stage(store, row, directory)
    with store.connection() as db:
        edits = db.execute("SELECT * FROM meeting_export_edits WHERE id=?", (row["id"],)).fetchall()
        plan = db.execute("SELECT * FROM meeting_export_renames WHERE id=?", (row["id"],)).fetchone()
    for edit in edits:
        recover_edit(store, edit, directory)
    if plan is not None:
        try:
            recover_rename(store, plan, directory)
        except (FileExistsError, FileNotFoundError):
            pass
    with store.connection() as db:
        current = db.execute("SELECT * FROM meeting_exports WHERE id=?", (row["id"],)).fetchone()
        create_notes = separate_notes(db)
        descriptive = descriptive_names(db)
    if current["status"] == "ready" and current["refresh"]:
        try:
            refresh(store, current, directory, create_notes, descriptive)
        except (OSError, ValueError) as error:
            settle_refresh(store, current, getattr(error, "strerror", None) or str(error))
        else:
            settle_refresh(store, current)


def flush(store, *, key=None):
    with store.export_lock:
        with store.connection() as db:
            rows = db.execute("""SELECT * FROM meeting_exports WHERE
                (status='pending' OR stage IS NOT NULL OR (status='ready' AND refresh>0)
                 OR EXISTS(SELECT 1 FROM meeting_export_edits e WHERE e.id=meeting_exports.id)
                 OR EXISTS(SELECT 1 FROM meeting_export_renames m WHERE m.id=meeting_exports.id)
                ) AND (? IS NULL OR id=?)""", (key, key)).fetchall()
        for item in rows:
            row = dict(item)
            try:
                with checked_folder(row) as directory:
                    sync_export(store, row, directory)
            except (OSError, ValueError) as error:
                reason = getattr(error, "strerror", None) or str(error)
                with store.connection() as db:
                    db.execute("UPDATE meeting_exports SET status='error',error=? WHERE id=? AND status='pending'",
                               (reason, row["id"]))
                settle_refresh(store, row, reason)


async def flush_async(store, *, key=None):
    # Keep the library lock until publication finishes even if a restart
    # cancels the caller while its thread holds the export lock.
    job = asyncio.create_task(asyncio.to_thread(flush, store, key=key))
    try:
        await asyncio.shield(job)
    except asyncio.CancelledError:
        await asyncio.gather(job, return_exceptions=True)
        raise


def queue_trash(db, key):
    row = db.execute("SELECT * FROM meeting_exports WHERE id=? AND status='ready'", (key,)).fetchone()
    if row is None:
        return
    for filename, digest, inode in (
        (row["filename"], row["sha256"], row["file_inode"]),
        (row["notes_filename"], row["notes_sha256"], row["notes_inode"]),
    ):
        if filename and digest:
            db.execute("INSERT INTO meeting_export_trash VALUES(?,?,?,?,?,?,?,?)",
                       (key, row["folder"], row["device"], row["inode"], filename, digest, inode,
                        f".jard-trash-{uuid4().hex}"))


def restore_unowned_file(directory, staged, filename):
    # A replacement raced the move. Put it back without overwriting another
    # file that may have taken its original name during the check.
    for number in range(1, 101):
        name = filename
        if number != 1:
            prefix = f"Recovered ({number}) "
            stem = filename.removesuffix(".md").encode("utf8")[:252-len(prefix)].decode("utf8", errors="ignore")
            name = f"{prefix}{stem}.md"
        try:
            os.link(filename, name, src_dir_fd=staged, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            continue
        os.unlink(filename, dir_fd=staged)
        return
    raise OSError("Could not restore a changed copy of meeting Markdown.")


def trash_export(store, row, directory, trash):
    filename, staging = row["filename"], row["staging"]
    if not exists(directory, staging):
        digest, info = disk_file(directory, filename)
        if not is_owned(digest, info, row["sha256"], row["file_inode"]):
            return
        os.mkdir(staging, mode=0o700, dir_fd=directory)
    staged = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        if not exists(staged, filename):
            digest, info = disk_file(directory, filename)
            if not is_owned(digest, info, row["sha256"], row["file_inode"]):
                return
            if row["file_inode"] is None:
                row["file_inode"] = info.st_ino
                with store.connection() as db:
                    db.execute("UPDATE meeting_export_trash SET file_inode=? WHERE id=? AND filename=?",
                               (info.st_ino, row["id"], filename))
            try:
                os.rename(filename, filename, src_dir_fd=directory, dst_dir_fd=staged)
            except FileNotFoundError:
                return
            os.fsync(staged)
            os.fsync(directory)
        digest, info = disk_file(staged, filename)
        if not is_owned(digest, info, row["sha256"], row["file_inode"]):
            if digest is not None:
                restore_unowned_file(directory, staged, filename)
            return
        path = Path(row["folder"]) / staging / filename
        if not os.path.samestat(info, path.lstat()):
            raise OSError("The staged Markdown moved before it could be sent to the Trash.")
        trash(path)
        if exists(staged, filename):
            raise OSError("The managed Markdown is still waiting to move to the Trash.")
    finally:
        try:
            if not exists(staged, filename):
                os.rmdir(staging, dir_fd=directory)
                os.fsync(directory)
        finally:
            os.close(staged)


def deliver_trash(store, key, trash):
    with store.connection() as db:
        pending = db.execute("SELECT * FROM meeting_export_trash WHERE id=?", (key,)).fetchall()
    failures = []
    for item in pending:
        row = dict(item)
        try:
            with checked_folder(row) as directory:
                trash_export(store, row, directory, trash)
        except OSError as error:
            failures.append(error)
        else:
            with store.connection() as db:
                db.execute("DELETE FROM meeting_export_trash WHERE id=? AND filename=?", (key, row["filename"]))
    if failures:
        raise failures[0]
