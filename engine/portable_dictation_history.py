"""Dictation history and retention preferences shared by portable Jaro and jardctl."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
from time import time
from typing import get_args
from uuid import UUID

from saved_dictations import HISTORY_LIMIT, HISTORY_TEXT_BUDGET, RETENTION_LABELS, history_page
from dictation_output import validate_dictation_vocabulary
from portable_audio_control import AudioBehavior, AudioPreferences
from portable_dictation_sounds import SoundPreferences, SoundStyle


DEFAULT = {"retention": 7, "previousRetention": 7, "terminal": 0, "revision": 0, "clearEpoch": 0}
SECONDS_PER_DAY = 86_400
# A two-minute dictation is at most 3.84 MB of 16 kHz mono PCM plus its WAV header.
MAX_CLIP_BYTES = 4 * 1024 * 1024
logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    retention INTEGER NOT NULL DEFAULT 7 CHECK (retention IN (0,1,7,30)),
    previousRetention INTEGER NOT NULL DEFAULT 7 CHECK (previousRetention IN (1,7,30)),
    terminal INTEGER NOT NULL DEFAULT 0 CHECK (terminal IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 0,
    clearEpoch INTEGER NOT NULL DEFAULT 0,
    desktopCommands INTEGER NOT NULL DEFAULT 0 CHECK (desktopCommands IN (0,1)),
    microphone TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    windowsShortcutMode TEXT NOT NULL DEFAULT 'hold',
    linuxShortcutMode TEXT NOT NULL DEFAULT 'tap',
    shortcutEnabled INTEGER NOT NULL DEFAULT 0 CHECK (shortcutEnabled IN (0,1)),
    portalShortcutEnabled INTEGER NOT NULL DEFAULT 0 CHECK (portalShortcutEnabled IN (0,1)),
    pasteRestoreToken TEXT NOT NULL DEFAULT '',
    vocabularyEntries TEXT NOT NULL DEFAULT '[]',
    replacements TEXT NOT NULL DEFAULT '',
    outputProfiles TEXT NOT NULL DEFAULT '',
    indicatorPosition TEXT NOT NULL DEFAULT 'top' CHECK (indicatorPosition IN ('top','bottom','off')),
    soundEnabled INTEGER NOT NULL DEFAULT 1 CHECK (soundEnabled IN (0,1)),
    soundStyle TEXT NOT NULL DEFAULT 'classic',
    soundVolume INTEGER NOT NULL DEFAULT 70 CHECK (soundVolume BETWEEN 0 AND 100),
    otherAudio TEXT NOT NULL DEFAULT 'none',
    otherAudioPercent INTEGER NOT NULL DEFAULT 20 CHECK (otherAudioPercent BETWEEN 0 AND 100),
    noiseCleanup INTEGER NOT NULL DEFAULT 0 CHECK (noiseCleanup IN (0,1)),
    boostMicrophone INTEGER NOT NULL DEFAULT 0 CHECK (boostMicrophone IN (0,1))
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
        self.audio_dir = self.path.with_name("dictation-audio")

    @staticmethod
    def _private(attributes):
        if os.name == "nt":
            return not attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        return attributes.st_uid == os.geteuid() and not attributes.st_mode & 0o077

    @contextmanager
    def connection(self, *, write=False, create=False, required=False):
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        flags |= os.O_RDWR if write else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        try:
            if os.name == "nt" and self.path.is_symlink():
                raise ValueError("Dictation history must be a private file owned by the current user.")
            fd = os.open(self.path, flags, 0o600)
        except FileNotFoundError:
            if create or required:
                raise
            yield None
            return
        try:
            attributes = os.fstat(fd)
            if not stat.S_ISREG(attributes.st_mode) or not self._private(attributes):
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(preferences)")}
            if "desktopCommands" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN desktopCommands INTEGER NOT NULL DEFAULT 0 CHECK (desktopCommands IN (0,1))")
            if "microphone" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN microphone TEXT NOT NULL DEFAULT ''")
            if "language" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN language TEXT NOT NULL DEFAULT ''")
            if "windowsShortcutMode" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN windowsShortcutMode TEXT NOT NULL DEFAULT 'hold'")
            if "linuxShortcutMode" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN linuxShortcutMode TEXT NOT NULL DEFAULT 'tap'")
            if "pasteRestoreToken" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN pasteRestoreToken TEXT NOT NULL DEFAULT ''")
            if "vocabularyEntries" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN vocabularyEntries TEXT NOT NULL DEFAULT '[]'")
            if "replacements" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN replacements TEXT NOT NULL DEFAULT ''")
            if "outputProfiles" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN outputProfiles TEXT NOT NULL DEFAULT ''")
            if "indicatorPosition" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN indicatorPosition TEXT NOT NULL DEFAULT 'top' "
                           "CHECK (indicatorPosition IN ('top','bottom','off'))")
            if "soundEnabled" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN soundEnabled INTEGER NOT NULL DEFAULT 1 CHECK (soundEnabled IN (0,1))")
            if "soundStyle" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN soundStyle TEXT NOT NULL DEFAULT 'classic'")
            if "soundVolume" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN soundVolume INTEGER NOT NULL DEFAULT 70 CHECK (soundVolume BETWEEN 0 AND 100)")
            if "otherAudio" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN otherAudio TEXT NOT NULL DEFAULT 'none'")
            if "otherAudioPercent" not in columns:
                db.execute("ALTER TABLE preferences ADD COLUMN otherAudioPercent INTEGER NOT NULL DEFAULT 20 "
                           "CHECK (otherAudioPercent BETWEEN 0 AND 100)")
            for column in ("shortcutEnabled", "portalShortcutEnabled", "noiseCleanup", "boostMicrophone"):
                if column not in columns:
                    db.execute(f"ALTER TABLE preferences ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0 CHECK ({column} IN (0,1))")
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
        removed = db.execute("DELETE FROM dictations WHERE time < ? RETURNING id", (self.cutoff(settings, now),)).fetchall()
        if removed:
            db.execute("UPDATE preferences SET revision=revision+1")
            self._delete_audio(removed)

    def expire(self):
        with self.connection(write=True) as db:
            if db is not None:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    now = time()
                    self._expire(db, self.preferences(db), now)
                    changed, next_expiry = self._sweep_audio(db, now)
                    if changed:
                        db.execute("UPDATE preferences SET revision=revision+1")
                    return next_expiry

    def _audio_directory(self):
        attributes = self.audio_dir.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or not self._private(attributes):
            raise ValueError("Dictation audio must be stored in a private directory.")

    @staticmethod
    def _clip_name(identifier):
        if str(UUID(identifier)) != identifier:
            raise ValueError("Invalid dictation identifier.")
        return f"{identifier}.wav"

    def _clip_stat(self, attributes):
        return (stat.S_ISREG(attributes.st_mode) and self._private(attributes)
                and 44 < attributes.st_size <= MAX_CLIP_BYTES)

    def _save_audio(self, identifier, source):
        try:
            target = self.audio_dir / self._clip_name(identifier)
            if not self._clip_stat(source.lstat()):
                return
            self.audio_dir.mkdir(mode=0o700, exist_ok=True)
            self._audio_directory()
            # The original recording is already private and on this library's filesystem.
            os.replace(source, target)
        except (OSError, ValueError):
            logger.warning("Could not keep the original dictation audio.", exc_info=True)

    def _unlink_audio(self, clip):
        try:
            clip.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            logger.warning("Could not remove retained dictation audio.", exc_info=True)
            return False

    def _delete_audio(self, removed):
        try:
            self._audio_directory()
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("Could not check retained dictation audio.", exc_info=True)
            return
        for row in removed:
            try:
                name = self._clip_name(row["id"])
            except ValueError:
                continue
            self._unlink_audio(self.audio_dir / name)

    def _sweep_audio(self, db, now):
        """Run under the history writer lock, including when invoked by jardctl."""
        try:
            self._audio_directory()
        except FileNotFoundError:
            return False, None
        except (OSError, ValueError):
            logger.warning("Could not check retained dictation audio.", exc_info=True)
            return False, None
        keep = {f'{row["id"]}.wav': row["time"] + SECONDS_PER_DAY for row in db.execute(
            "SELECT id, time FROM dictations WHERE time > ?", (now - SECONDS_PER_DAY,))}
        changed, next_expiry = False, None
        for clip in self.audio_dir.glob("*.wav"):
            try:
                self._clip_name(clip.stem)
            except ValueError:
                continue
            expiry = keep.get(clip.name)
            if expiry is not None:
                next_expiry = min(next_expiry, expiry) if next_expiry is not None else expiry
                continue
            changed = self._unlink_audio(clip) or changed
        return changed, next_expiry

    @contextmanager
    def open_audio(self, identifier):
        with self.connection() as db:
            entry = db is not None and db.execute(
                "SELECT time FROM dictations WHERE id=?", (identifier,)).fetchone()
        if not entry or entry["time"] + SECONDS_PER_DAY <= time():
            raise FileNotFoundError
        self._audio_directory()
        path = self.audio_dir / self._clip_name(identifier)
        if not self._clip_stat(path.lstat()):
            raise FileNotFoundError
        flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, os.O_RDONLY | flags)
        with os.fdopen(fd, "rb") as stream:
            attributes = os.fstat(stream.fileno())
            if not self._clip_stat(attributes) or entry["time"] + SECONDS_PER_DAY <= time():
                raise FileNotFoundError
            yield stream, attributes.st_size

    def commands_enabled(self):
        with self.connection(required=True) as db:
            return bool(db.execute("SELECT desktopCommands FROM preferences").fetchone()[0])

    def microphone(self):
        with self.connection(required=True) as db:
            return db.execute("SELECT microphone FROM preferences").fetchone()[0]

    def language(self):
        with self.connection(required=True) as db:
            return db.execute("SELECT language FROM preferences").fetchone()[0]

    def windows_shortcut_mode(self):
        with self.connection(required=True) as db:
            return db.execute("SELECT windowsShortcutMode FROM preferences").fetchone()[0]

    def linux_shortcut_mode(self):
        with self.connection(required=True) as db:
            return db.execute("SELECT linuxShortcutMode FROM preferences").fetchone()[0]

    def shortcut_enabled(self, *, portal=False):
        column = "portalShortcutEnabled" if portal else "shortcutEnabled"
        with self.connection(required=True) as db:
            return bool(db.execute(f"SELECT {column} FROM preferences").fetchone()[0])

    def save_shortcut_enabled(self, enabled, *, portal=False):
        column = "portalShortcutEnabled" if portal else "shortcutEnabled"
        with self.connection(write=True, required=True) as db, db:
            db.execute(f"UPDATE preferences SET {column}=?", (int(enabled),))

    def consume_paste_grant(self):
        with self.connection(write=True, required=True) as db, db:
            db.execute("BEGIN IMMEDIATE")
            token = db.execute("SELECT pasteRestoreToken FROM preferences").fetchone()[0]
            if token:
                db.execute("UPDATE preferences SET pasteRestoreToken=''")
            return token

    def save_paste_grant(self, token):
        with self.connection(write=True, required=True) as db, db:
            db.execute("UPDATE preferences SET pasteRestoreToken=?", (token,))

    def forget_paste_grant(self, token):
        with self.connection(write=True, required=True) as db, db:
            db.execute("UPDATE preferences SET pasteRestoreToken='' WHERE pasteRestoreToken=?", (token,))

    def voice_input(self):
        with self.connection(required=True) as db:
            row = db.execute("SELECT noiseCleanup, boostMicrophone FROM preferences").fetchone()
            return {key: bool(row[key]) for key in row.keys()}

    def indicator_position(self):
        with self.connection(required=True) as db:
            return db.execute("SELECT indicatorPosition FROM preferences").fetchone()[0]

    def sound_preferences(self):
        with self.connection(required=True) as db:
            enabled, style, volume = db.execute("SELECT soundEnabled, soundStyle, soundVolume FROM preferences").fetchone()
            if style not in get_args(SoundStyle):
                style = "classic"
            return SoundPreferences(bool(enabled), style, volume)

    def save_sound_preferences(self, settings):
        with self.connection(write=True, required=True) as db, db:
            db.execute("UPDATE preferences SET soundEnabled=?, soundStyle=?, soundVolume=?",
                       (int(settings.enabled), settings.style, settings.volume))

    def audio_preferences(self):
        with self.connection(required=True) as db:
            behavior, percent = db.execute("SELECT otherAudio, otherAudioPercent FROM preferences").fetchone()
            return AudioPreferences(behavior if behavior in get_args(AudioBehavior) else "none", percent)

    def save_audio_preferences(self, settings):
        with self.connection(write=True, required=True) as db, db:
            db.execute("UPDATE preferences SET otherAudio=?, otherAudioPercent=?", (settings.behavior, settings.percent))

    def output_preferences(self):
        with self.connection(required=True) as db:
            row = db.execute("SELECT vocabularyEntries, replacements, outputProfiles FROM preferences").fetchone()
            return validate_dictation_vocabulary(json.loads(row["vocabularyEntries"])), row["replacements"], row["outputProfiles"]

    def save_vocabulary(self, base, entries):
        with self.connection(write=True, required=True) as db, db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT vocabularyEntries FROM preferences").fetchone()[0]
            if json.loads(stored) != base:
                return False
            db.execute("UPDATE preferences SET vocabularyEntries=?", (json.dumps(entries, ensure_ascii=False),))
            return True

    def save_replacements(self, value):
        with self.connection(write=True, required=True) as db, db:
            db.execute("UPDATE preferences SET replacements=?", (value,))

    def save_output_profiles(self, base, value):
        with self.connection(write=True, required=True) as db, db:
            return db.execute("UPDATE preferences SET outputProfiles=? WHERE outputProfiles=?", (value, base)).rowcount == 1

    def configure(self, *, retention=None, terminal=None, commands=None, microphone=None, language=None,
                  windows_shortcut_mode=None, linux_shortcut_mode=None, noise_cleanup=None, boost_microphone=None,
                  indicator_position=None):
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
            if microphone is not None:
                db.execute("UPDATE preferences SET microphone=?", (microphone,))
            if language is not None:
                db.execute("UPDATE preferences SET language=?", (language,))
            if windows_shortcut_mode is not None:
                db.execute("UPDATE preferences SET windowsShortcutMode=?", (windows_shortcut_mode,))
            if linux_shortcut_mode is not None:
                db.execute("UPDATE preferences SET linuxShortcutMode=?", (linux_shortcut_mode,))
            if noise_cleanup is not None:
                db.execute("UPDATE preferences SET noiseCleanup=?", (int(noise_cleanup),))
            if boost_microphone is not None:
                db.execute("UPDATE preferences SET boostMicrophone=?", (int(boost_microphone),))
            if indicator_position is not None:
                db.execute("UPDATE preferences SET indicatorPosition=?", (indicator_position,))

    def record(self, entry, audio=None):
        with self.connection(write=True, required=True) as db, db:
            db.execute("BEGIN IMMEDIATE")
            settings = self.preferences(db)
            now = time()
            self._expire(db, settings, now)
            if not settings["retention"]:
                return False, settings["clearEpoch"]
            db.execute("""INSERT INTO dictations(id,time,text,app,seconds,inferenceMs,totalMs)
                VALUES(:id,:time,:text,:app,:seconds,:inferenceMs,:totalMs)""", entry)
            trimmed = db.execute("""DELETE FROM dictations WHERE id IN (
                SELECT id FROM (
                    SELECT id, row_number() OVER ordered AS sequence,
                        sum(length(CAST(text AS BLOB))) OVER ordered AS bytes
                    FROM dictations
                    WINDOW ordered AS (ORDER BY time DESC, id DESC ROWS UNBOUNDED PRECEDING)
                ) WHERE sequence > ? OR bytes > ?
            ) RETURNING id""", (HISTORY_LIMIT, HISTORY_TEXT_BUDGET)).fetchall()
            if trimmed:
                self._delete_audio(trimmed)
            if (audio is not None and entry["time"] + SECONDS_PER_DAY > now
                    and not any(row["id"] == entry["id"] for row in trimmed)):
                self._save_audio(entry["id"], audio)
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
                self._sweep_audio(db, time())
                return removed

    def page(self, *, include_timing=False, include_audio=False, **filters):
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
        if include_audio:
            self._add_audio_availability(result["entries"], now)
        return result

    def _add_audio_availability(self, entries, now):
        try:
            self._audio_directory()
        except (OSError, ValueError):
            return
        for entry in entries:
            expiry = entry["time"] + SECONDS_PER_DAY
            if expiry > now:
                try:
                    path = self.audio_dir / self._clip_name(entry["id"])
                    if self._clip_stat(path.lstat()):
                        entry["audioExpiresAt"] = expiry
                except (OSError, ValueError):
                    pass
