"""Transcript history and retention preferences shared by portable Jaro and jardctl."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat
from time import time

from saved_dictations import HISTORY_LIMIT, HISTORY_TEXT_BUDGET, RETENTION_LABELS, history_page


DEFAULT = {"retention": 7, "previousRetention": 7, "terminal": 0, "revision": 0, "clearEpoch": 0}
SECONDS_PER_DAY = 86_400
SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    retention INTEGER NOT NULL DEFAULT 7 CHECK (retention IN (0,1,7,30)),
    previousRetention INTEGER NOT NULL DEFAULT 7 CHECK (previousRetention IN (1,7,30)),
    terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 0,
    clearEpoch INTEGER NOT NULL DEFAULT 0,
    desktopCommands INTEGER NOT NULL DEFAULT 0 CHECK (desktopCommands IN (0,1))
);
INSERT OR IGNORE INTO preferences(singleton) VALUES (1);
CREATE TABLE IF NOT EXISTS dictations (
    id TEXT PRIMARY KEY, time REAL NOT NULL, text TEXT NOT NULL, app TEXT NOT NULL,
    seconds REAL NOT NULL, inferenceMs INTEGER NOT NULL, totalMs INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS dictations_recent ON dictations(time DESC, id DESC);
"""


class PortableDictationHistory:
    def __init__(self, root):
        self.path = Path(root) / "dictation.sqlite3"

    @contextmanager
    def connection(self, *, write=False, create=False, required=False):
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        flags |= os.O_RDWR if write else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileNotFoundError:
            if create or required:
                raise
            yield None
            return
        try:
            attributes = os.fstat(fd)
            if not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != os.geteuid() or attributes.st_mode & 0o077:
                raise ValueError("Dictation history must be a private file owned by the current user.")
        finally:
            os.close(fd)

        path = self.path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro")
        with closing(sqlite3.connect(path, timeout=2, uri=True)) as db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("PRAGMA secure_delete=ON")
            yield db

    def initialize(self):
        with self.connection(write=True, create=True) as db, db:
            db.executescript(SCHEMA)
            if not any(row["name"] == "desktopCommands" for row in db.execute("PRAGMA table_info(preferences)")):
                db.execute("ALTER TABLE preferences ADD COLUMN desktopCommands INTEGER NOT NULL DEFAULT 0 CHECK (desktopCommands IN (0,1))")
        self.expire()

    @staticmethod
    def preferences(db):
        return dict(db.execute(
            "SELECT retention, previousRetention, terminal, revision, clearEpoch FROM preferences"
        ).fetchone()) if db is not None else DEFAULT.copy()

    def status(self, last_id=None):
        with self.connection() as db:
            if db is not None:
                db.execute("BEGIN")
            settings = self.preferences(db)
            found = last_id is None or (db is not None and db.execute(
                "SELECT 1 FROM dictations WHERE id=? AND time>=?", (last_id, self.cutoff(settings, time()))
            ).fetchone() is not None)
            return settings, found

    @staticmethod
    def cutoff(settings, now):
        return now - (settings["retention"] or settings["previousRetention"]) * SECONDS_PER_DAY

    def _expire(self, db, settings, now):
        removed = db.execute("DELETE FROM dictations WHERE time < ?", (self.cutoff(settings, now),)).rowcount
        if removed:
            db.execute("UPDATE preferences SET revision=revision+1")

    def expire(self):
        with self.connection(write=True) as db:
            if db is not None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    self._expire(db, self.preferences(db), time())

    def commands_enabled(self):
        with self.connection(required=True) as db:
            return bool(db.execute("SELECT desktopCommands FROM preferences").fetchone()[0])

    def configure(self, *, retention=None, terminal=None, commands=None):
        if retention is not None and retention not in RETENTION_LABELS:
            raise ValueError("Choose History off, 1 day, 7 days, or 30 days.")
        with self.connection(write=True, required=True) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if retention is not None:
                db.execute("""UPDATE preferences SET retention=?, previousRetention=
                    CASE WHEN ?=0 THEN previousRetention ELSE ? END, revision=revision+1""",
                    (retention, retention, retention))
                self._expire(db, self.preferences(db), time())
            if terminal is not None:
                db.execute("UPDATE preferences SET terminal=?", (int(terminal),))
            if commands is not None:
                db.execute("UPDATE preferences SET desktopCommands=?", (int(commands),))

    def record(self, entry):
        with self.connection(write=True, required=True) as db, db:
            db.execute("BEGIN IMMEDIATE")
            settings = self.preferences(db)
            self._expire(db, settings, entry["time"])
            if not settings["retention"]:
                return False, settings["clearEpoch"]
            db.execute("""INSERT INTO dictations(id,time,text,app,seconds,inferenceMs,totalMs)
                VALUES(:id,:time,:text,:app,:seconds,:inferenceMs,:totalMs)""", entry)
            db.execute("""DELETE FROM dictations WHERE id IN (
                SELECT id FROM (
                    SELECT id, row_number() OVER ordered AS sequence,
                        sum(length(CAST(text AS BLOB))) OVER ordered AS bytes
                    FROM dictations
                    WINDOW ordered AS (ORDER BY time DESC, id DESC ROWS UNBOUNDED PRECEDING)
                ) WHERE sequence > ? OR bytes > ?
            )""", (HISTORY_LIMIT, HISTORY_TEXT_BUDGET))
            db.execute("UPDATE preferences SET revision=revision+1")
            return True, settings["clearEpoch"]

    def remove(self, ids=None):
        with self.connection(write=True) as db:
            if db is None:
                return 0
            with db:
                db.execute("BEGIN IMMEDIATE")
                if ids is None:
                    removed = db.execute("DELETE FROM dictations").rowcount
                    db.execute("UPDATE preferences SET revision=revision+1, clearEpoch=clearEpoch+1")
                else:
                    removed = db.executemany("DELETE FROM dictations WHERE id=?", ((key,) for key in ids)).rowcount
                    if removed:
                        db.execute("UPDATE preferences SET revision=revision+1")
                return removed

    def page(self, *, include_timing=False, **filters):
        now = time()
        with self.connection() as db:
            if db is not None:
                db.execute("BEGIN")
            settings = self.preferences(db)
            entries = [] if db is None else [dict(row) for row in db.execute(
                "SELECT * FROM dictations WHERE time>=? ORDER BY time DESC, id DESC LIMIT ?",
                (self.cutoff(settings, now), HISTORY_LIMIT))]
        snapshot = {"JardSystemDictation": {**settings, "vocabulary": ""},
                    "JardSystemDictationHistory": [{**entry, "kind": "dictation"} for entry in entries]}
        result = history_page(None, snapshot=snapshot, now=datetime.fromtimestamp(now, timezone.utc), **filters)
        if include_timing:
            details = {entry["id"]: entry for entry in entries}
            for entry in result["entries"]:
                entry.update(inferenceMs=details[entry["id"]]["inferenceMs"], totalMs=details[entry["id"]]["totalMs"], raw="")
        return result
