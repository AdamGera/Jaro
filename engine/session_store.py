"""SQLite session metadata and inference output, with managed audio alongside it."""
import json
import mimetypes
import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

import annotation_retention
import annotation_storage
import meeting_brief
import meeting_exports
import meeting_retention
import personal_notes
import session_images
import session_notes
import writing_cache
from annotation_assets import ASSETS as ANNOTATION_ASSETS, staged_annotation_paths
from managed_preferences import meetings_managed
from meeting_rolling import MIXED_SPEAKERS, RATE as RECORDING_RATE, SOURCE_LABELS, mixed_speaker_timeline, timeline_chunks
from native_recordings import normalize_meeting_title

SHUTDOWN_INTERRUPTION = "Processing stopped when Jaro closed."
LEGACY_SHUTDOWN_INTERRUPTION = "Processing stopped when Jard Soundfolio Local closed."
RECOVERY_LIMIT = 2


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def transcript_from_chunks(chunks, seconds, speakers, interruption=None, *, meeting=False, reconcile=False):
    text, languages, turns, words = [], [], [], []
    intervals = {}
    suppressed = 0
    for chunk in chunks:
        if chunk["text"]:
            text.append(chunk["text"])
            for language in chunk.get("languages", [chunk.get("language")]):
                if language and language not in languages:
                    languages.append(language)
        suppressed += chunk.get("echoSuppressedUtterances", 0)
        words.extend(chunk["words"])
        turns.extend(chunk["segments"])
        for turn in chunk.get("activity", []):
            intervals.setdefault(turn["speaker"], []).append((turn["start"], turn["end"]))
    if reconcile:
        turns.sort(key=lambda turn: turn["start"] or 0)
        words.sort(key=lambda word: word["start"] if word.get("start") is not None else float("inf"))
        if turns:
            text = [turn["text"] for turn in turns]
    segments = []
    for turn in turns:
        if segments and segments[-1]["speaker"] == turn["speaker"]:
            segments[-1]["text"] += " " + turn["text"]
            if turn["end"] is not None:
                segments[-1]["end"] = max(turn["end"], segments[-1]["end"] or 0)
        else:
            segments.append(dict(turn))
    rows = []
    for speaker, spans in sorted(intervals.items()):
        merged = []
        for start, end in sorted(spans):
            start, end = max(0, start), min(seconds, end)
            if end <= start:
                continue
            if merged and start <= merged[-1]["end"]:
                merged[-1]["end"] = max(end, merged[-1]["end"])
            else:
                merged.append({"start": start, "end": end})
        if merged:
            rows.append(dict(speaker=speaker, intervals=merged, seconds=sum(s["end"] - s["start"] for s in merged)))
    total = sum(row["seconds"] for row in rows)
    for row in rows:
        row["share"] = row["seconds"] / total
    result = dict(text=" ".join(text), durationSeconds=seconds, detectedLanguages=languages,
                  segments=segments, words=words, speakerActivity=rows if speakers else None)
    if meeting:
        result.update(speakerNames=SOURCE_LABELS, echoSuppressedUtterances=suppressed)
    if interruption:
        result["interruption"] = interruption
    return result


class MissingSession(KeyError):
    pass


@contextmanager
def open_library(root):
    # Recovery can only run while this server owns the library.
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".server.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = SessionStore(root)
        for upload in root.glob(".upload-*"):
            if upload.is_dir():
                shutil.rmtree(upload)
        yield store


class SessionStore:
    def __init__(self, root, *, read_only=False):
        self.root = Path(root).expanduser()
        self.audio = self.root / "audio"
        self.path = self.root / "sessions.sqlite3"
        self.read_only = read_only
        self.retention_lock = Lock()
        self.annotation_file_lock = Lock()
        self.export_lock = Lock()
        if read_only:
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audio.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL,
                    filename TEXT NOT NULL, mime TEXT NOT NULL, options TEXT NOT NULL,
                    duration REAL, processed REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'processing',
                    error TEXT, transcript TEXT, search_text TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    seconds REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id, seconds)
                );
                CREATE TABLE IF NOT EXISTS meeting_rolling (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    start_frame INTEGER NOT NULL, end_frame INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(session_id, start_frame)
                );
                CREATE TABLE IF NOT EXISTS meeting_finalization (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    next_frame INTEGER NOT NULL DEFAULT 0 CHECK(next_frame>=0),
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS meeting_audio_retention (
                    id INTEGER PRIMARY KEY CHECK (id=1), days INTEGER NOT NULL CHECK (days IN (1,7,30))
                );
            """)
            db.executescript(session_notes.SCHEMA)
            db.executescript(session_images.SCHEMA)
            meeting_brief.migrate(db)
            meeting_exports.migrate(db)
            writing_cache.migrate(db)
            personal_notes.migrate(db)
            db.executescript(meeting_retention.SCHEMA)
            db.executescript(annotation_retention.SCHEMA)
            db.executescript(annotation_storage.SCHEMA)
            db.execute("INSERT OR IGNORE INTO meeting_audio_policy SELECT * FROM meeting_audio_retention")
            db.execute("UPDATE chat_turns SET status='stopped' WHERE status='streaming'")
            db.execute("""INSERT OR IGNORE INTO meeting_finalization(session_id,next_frame)
                       SELECT id,max(0,round(processed * ?)) FROM sessions
                       WHERE (status='processing' OR (status='interrupted' AND error IN (?,?)))
                         AND coalesce(json_extract(options,'$.meeting'),json_extract(options,'$.sourceSpeakerTurns'),0)""",
                       (RECORDING_RATE, SHUTDOWN_INTERRUPTION, LEGACY_SHUTDOWN_INTERRUPTION))
            db.execute("UPDATE sessions SET status='interrupted',error=? WHERE status='processing'",
                       (SHUTDOWN_INTERRUPTION,))
            ids = {row[0]: json.loads(row[1]).get("audioRemovedAt") for row in db.execute("SELECT id,options FROM sessions")}
            audio_pending = {row[0] for row in db.execute("SELECT id FROM meeting_audio_trash")}
        # Finish filesystem changes interrupted between a rename and a database commit.
        audio_prefix = "Jard Meeting Audio "
        for path in (self.root / ".meeting-trash").glob(audio_prefix + "*"):
            name = path.name.removeprefix(audio_prefix)
            if name in ids and not ids[name] and name not in audio_pending and not (self.audio / name).exists():
                path.rename(self.audio / name)
        for path in self.audio.iterdir():
            name = path.name.removesuffix(".deleting").removesuffix(".removing-audio")
            try:
                if str(UUID(name)) != name:
                    continue
            except ValueError:
                continue
            if path.name.endswith(".removing-audio") and (name not in ids or ids[name]):
                # Older builds left committed audio here until cleanup.
                with self.connection() as db:
                    db.execute("INSERT OR IGNORE INTO meeting_audio_trash VALUES(?)", (name,))
            elif name not in ids:
                shutil.rmtree(path)
            elif path.name != name:
                path.rename(self.audio / name)
        self.recover_annotation_files()

    @contextmanager
    def connection(self):
        path = self.path.absolute().as_uri() + "?mode=ro" if self.read_only else self.path
        with closing(sqlite3.connect(path, timeout=15, uri=self.read_only)) as db:
            db.row_factory = sqlite3.Row
            if self.read_only:
                db.execute("PRAGMA query_only=ON")
            else:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db

    def recover_annotation_files(self, key=None):
        with self.annotation_file_lock, self.connection() as db:
            annotation_storage.recover(db, key=key)

    @contextmanager
    def new_media_folder(self, key, location):
        if location is None:
            folder = self.audio / key
            folder.mkdir(mode=0o700)
            try:
                yield folder
            except BaseException:
                shutil.rmtree(folder)
                raise
            return
        with self.annotation_file_lock:
            with self.connection() as db:
                root = annotation_storage.location_root(db, location)
                annotation_storage.recover(db, key=key)
                db.execute("INSERT INTO annotation_file_operations VALUES(?,?,'import')", (key, location))
            try:
                folder = annotation_storage.allocate(root, key)
            except FileExistsError:
                with self.connection() as db:
                    db.execute("DELETE FROM annotation_file_operations WHERE id=?", (key,))
                raise
            try:
                yield folder
            except BaseException:
                with self.connection() as db:
                    annotation_storage.recover(db, key=key)
                raise

    def create(self, source, name, mime, options, duration, *, key=None, notes_draft=None, tracks=None, rolling=(),
               annotation=None, annotation_location=None):
        staged = key is not None
        if annotation_location is not None and (not annotation or not staged):
            raise ValueError("Only a staged annotation can use a separate storage folder.")
        key = key or str(uuid4())
        if options.get("meeting", options.get("sourceSpeakerTurns")):
            options = {"retentionApplies": 1, **options}
        created = datetime.now(timezone.utc).isoformat()
        name = Path(name or "").name.strip()[:240] or ("Recording" if options["recording"] else "Audio")
        mime = mime.partition(";")[0].lower()
        suffix = Path(name).suffix.lower()
        if options["recording"] or not suffix[1:].isalnum() or len(suffix) > 10:
            suffix = {"audio/mp4": ".m4a", "audio/wav": ".wav", "video/webm": ".webm", "audio/webm": ".webm"}.get(mime) or mimetypes.guess_extension(mime) or ".audio"
        mime = mimetypes.guess_type("source" + suffix)[0] or mime
        if not mime.startswith("audio/") and mime not in ("video/webm", "video/mp4"):
            mime = "application/octet-stream"
        filename = "source" + suffix
        with self.new_media_folder(key, annotation_location) as folder:
            files = {source: folder / filename}
            if tracks:
                files.update({path: folder / f"{track}.wav" for track, path in tracks.items()})
                options = {**options, "recordingTracks": {track: f"/api/sessions/{key}/audio?track={track}" for track in tracks}}
            if annotation:
                base = f"/api/sessions/{key}/annotation/"
                paths = staged_annotation_paths(source)
                files[paths["manifest"]] = folder / ANNOTATION_ASSETS["manifest"][0]
                if annotation["frames"]:
                    files[paths["contact-sheet"]] = folder / ANNOTATION_ASSETS["contact-sheet"][0]
                options = {**options, "annotationRetentionEligible": 1, "annotation": {**annotation, "manifestUrl": base + "manifest",
                           "contactSheetUrl": base + "contact-sheet" if annotation["frames"] else None}}
            if annotation_location is not None:
                options["annotationLocation"] = annotation_location
            moved = []
            try:
                for original, destination in files.items():
                    if staged and annotation_location is not None:
                        annotation_storage.duplicate(original, destination)
                    elif staged:
                        # Keep stage data in place until the Session is committed.
                        os.link(original, destination, follow_symlinks=False)
                    else:
                        os.replace(original, destination)
                        moved.append((original, destination))
                if annotation_location is not None:
                    meeting_retention.sync_directory(folder)
                with self.connection() as db:
                    db.execute("""INSERT INTO sessions(id,name,created,filename,mime,options,duration)
                                  VALUES (?,?,?,?,?,?,?)""", (key, name, created, filename, mime, encode(options), duration))
                    db.executemany("INSERT INTO meeting_rolling VALUES(?,?,?,?)",
                                   ((key, start, end, encode(result)) for start, end, result in rolling))
                    if options.get("meeting", options.get("sourceSpeakerTurns")):
                        db.execute("INSERT INTO meeting_finalization(session_id) VALUES(?)", (key,))
                    personal_notes.attach(db, key, notes_draft)
                    if annotation_location is not None:
                        db.execute("DELETE FROM annotation_file_operations WHERE id=?", (key,))
            except BaseException:
                for original, destination in reversed(moved):
                    os.replace(destination, original)
                raise
        return key, folder / filename

    def append(self, key, chunk, seconds, *, next_frame=None):
        with self.connection() as db:
            db.execute("INSERT INTO chunks VALUES(?,?,?)", (key, seconds, encode(chunk)))
            db.execute("UPDATE sessions SET processed=?,search_text=trim(search_text || ' ' || ?) WHERE id=?",
                       (seconds, chunk["text"], key))
            if next_frame is not None:
                db.execute("UPDATE meeting_finalization SET next_frame=? WHERE session_id=?", (next_frame, key))

    def claim_meeting_finalization(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            exhausted = """SELECT f.session_id FROM meeting_finalization f JOIN sessions s ON s.id=f.session_id
                            WHERE s.status='interrupted' AND f.attempts>=?"""
            db.execute(f"UPDATE sessions SET error=?,transcript=NULL WHERE id IN ({exhausted})",
                       ("Meeting recovery did not finish after two attempts. Recorded audio and available text are saved.",
                        RECOVERY_LIMIT))
            db.execute(f"DELETE FROM meeting_finalization WHERE session_id IN ({exhausted})", (RECOVERY_LIMIT,))
            row = db.execute("""SELECT s.id,s.filename,s.duration,s.options,f.next_frame FROM meeting_finalization f
                JOIN sessions s ON s.id=f.session_id WHERE s.status='interrupted' ORDER BY s.created,s.id LIMIT 1""").fetchone()
            if row:
                db.execute("UPDATE meeting_finalization SET attempts=attempts+1 WHERE session_id=?", (row["id"],))
                db.execute("UPDATE sessions SET status='processing',error=NULL,transcript=NULL WHERE id=?", (row["id"],))
                return {**dict(row), "options": json.loads(row["options"])}
            return None

    def has_pending_meetings(self):
        with self.connection() as db:
            return bool(db.execute("""SELECT 1 FROM meeting_finalization f JOIN sessions s ON s.id=f.session_id
                                      WHERE s.status='interrupted' LIMIT 1""").fetchone())

    def _result(self, db, row, *, rebuild=False, include_turns=False):
        options = json.loads(row["options"])
        cached = row["transcript"] and not rebuild
        if cached and not include_turns:
            return json.loads(row["transcript"]), []
        key = row["id"]
        chunks = [json.loads(c[0]) for c in db.execute("SELECT payload FROM chunks WHERE session_id=? ORDER BY seconds", (key,))]
        source_turns = options.get("sourceSpeakerTurns", False)
        is_meeting = options.get("meeting", source_turns)
        mixed_speakers = options.get("meetingSpeakerMode") == MIXED_SPEAKERS
        # Offline consumers may open an older library before it has been migrated.
        rolling = db.execute("SELECT end_frame,payload FROM meeting_rolling WHERE session_id=? ORDER BY start_frame", (key,)).fetchall() if (
            is_meeting and db.execute("SELECT 1 FROM sqlite_master WHERE name='meeting_rolling'").fetchone()) else []
        if rolling:
            provisional = [json.loads(draft["payload"]) for draft in rolling]
            chunks = (mixed_speaker_timeline(chunks, provisional, row["processed"]) if mixed_speakers else
                      timeline_chunks(chunks, provisional, mixed=not source_turns))
        seconds = (row["duration"] or row["processed"]) if is_meeting else row["processed"]
        if rolling:
            seconds = max(seconds, rolling[-1]["end_frame"] / RECORDING_RATE)
        result = json.loads(row["transcript"]) if cached else transcript_from_chunks(
            chunks, seconds, options["diarization"] or bool(rolling and not mixed_speakers), row["error"],
            meeting=source_turns or bool(rolling and not mixed_speakers),
            reconcile=bool(rolling))
        if is_meeting and not cached:
            result["transcriptSource"] = ("final" if row["status"] == "complete" else
                                          "rolling" if rolling or row["status"] == "processing" else "draft")
        turns = [turn for chunk in chunks for turn in chunk["segments"]] if include_turns else []
        return result, sorted(turns, key=lambda turn: turn["start"] or 0) if rolling else turns

    def finish(self, key, error=None, *, closing=False, recovered=False):
        with self.connection() as db:
            updated = db.execute("""UPDATE sessions SET status=?,error=?,duration=
                       CASE WHEN ? IS NULL THEN processed ELSE coalesce(duration,processed) END
                       WHERE id=? AND (NOT ? OR status='processing')""",
                       ("interrupted" if error else "complete", error, error, key, closing))
            if not updated.rowcount:
                return
            row = db.execute("SELECT * FROM sessions WHERE id=?", (key,)).fetchone()
            options = json.loads(row["options"])
            if not error and (options.get("sourceSpeakerTurns") or options.get("meetingSpeakerMode") == MIXED_SPEAKERS):
                db.execute("DELETE FROM meeting_rolling WHERE session_id=?", (key,))
            if not closing:
                db.execute("DELETE FROM meeting_finalization WHERE session_id=?", (key,))
            elif recovered:
                db.execute("UPDATE meeting_finalization SET attempts=max(0,attempts-1) WHERE session_id=?", (key,))
            result, _ = self._result(db, row, rebuild=True)
            db.execute("UPDATE sessions SET transcript=?,search_text=? WHERE id=?", (encode(result), result["text"], key))

            if not error:
                session_notes.initialize(db, key, refinalize=True)
                if result.get("transcriptSource") == "final":
                    meeting_exports.enqueue(db, key)
                    if result["text"].strip() and meeting_brief.automatic_policy(db)[0]:
                        meeting_brief.enqueue(db, key, refinalize=True, automatic=True)

    def list(self, query, *, meetings_only=False):
        with self.connection() as db:
            db.create_function("fold", 1, str.casefold, deterministic=True)
            pending = "EXISTS(SELECT 1 FROM meeting_finalization f WHERE f.session_id=s.id)" if self.has_finalization_table(db) else "0"
            source = meeting_retention.readable_source(db)
            retention = "coalesce((SELECT days FROM meeting_retention WHERE id=1),json_extract(s.options,'$.retentionApplies'))" if source == "readable_sessions" else "NULL"
            meeting = "coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)"
            briefs = meeting_brief.installed(db)
            title = "b.title" if briefs else "NULL"
            join = "LEFT JOIN meeting_briefs b ON b.id=s.id" if briefs else ""
            pending_brief = "coalesce(b.status='queued',0)" if briefs else "0"
            provisional = "json_extract(s.options,'$.provisionalTitle')"
            renamed = "json_extract(s.options,'$.meetingTitle')"
            personal_search = ""
            if meetings_only and query and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_notes'"
            ).fetchone():
                personal_search = """OR EXISTS(SELECT 1 FROM personal_notes p, json_tree(p.document) j
                    WHERE p.id=s.id AND j.key='text' AND j.type='text' AND instr(fold(CAST(j.atom AS TEXT)),:folded))"""
            brief_search = "OR instr(fold(coalesce(b.summary,'')),:folded)" if meetings_only and briefs else ""
            rows = db.execute(f"""SELECT s.id,coalesce({renamed},json_extract(s.options,'$.meetingCalendar.title'),{title},{provisional},o.title,s.name) AS name,s.created,s.duration,s.processed,s.status,
                {meeting} AS meeting,
                json_extract(s.options,'$.meetingProvider') AS meetingProvider,
                json_extract(s.options,'$.meetingStartedAt') AS meetingStartedAt,
                json_extract(s.options,'$.meetingEndedAt') AS meetingEndedAt,
                json_extract(s.options,'$.audioRemovedAt') AS audioRemovedAt,
                json_type(s.options,'$.annotation')='object' AS annotation,
                CASE WHEN {meeting} THEN NULLIF({retention},0) END AS meetingRetentionDays,
                {pending} AS finalizationPending, {title} AS summaryTitle, {provisional} AS provisionalTitle,
                (SELECT count(*) FROM overviews v WHERE v.id=s.id AND v.status='queued')+{pending_brief} AS pendingOverview
                FROM {source} s LEFT JOIN notes n ON n.id=s.id LEFT JOIN overviews o ON o.id=s.id AND o.lang=n.displayed {join}
                WHERE (NOT :meetings_only OR meeting) AND (:query='' OR instr(fold(s.name),:folded) OR instr(fold(coalesce(o.title,'')),:folded)
                  OR instr(fold(s.search_text),:folded) OR instr(fold(coalesce(meetingProvider,'')),:folded)
                  OR instr(fold(coalesce(json_extract(s.options,'$.meetingCalendar.title'),'')),:folded)
                  OR instr(fold(coalesce({title},'')),:folded) OR instr(fold(coalesce({provisional},'')),:folded)
                  OR instr(fold(coalesce({renamed},'')),:folded) {personal_search} {brief_search})
                ORDER BY s.created DESC,s.id""", {"meetings_only": meetings_only, "query": query, "folded": query.casefold()})
            return [{**row, "meeting": bool(row["meeting"]), "annotation": bool(row["annotation"]),
                     "finalizationPending": bool(row["finalizationPending"])} for row in rows]

    def exists(self, key):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM sessions WHERE id=?", (key,)).fetchone():
                return True
            return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_trash'").fetchone()
                        and db.execute("SELECT 1 FROM meeting_trash WHERE id=?", (key,)).fetchone())

    def get(self, key, *, include_turns=False):
        with self.connection() as db:
            db.execute("BEGIN" if self.read_only else "BEGIN IMMEDIATE")
            source = meeting_retention.readable_source(db)
            row = db.execute(f"SELECT * FROM {source} WHERE id=?", (key,)).fetchone()
            if not row:
                raise MissingSession(key)
            result = self._metadata(db, row)
            include_turns = include_turns and result["meeting"]
            result["transcript"], turns = self._result(db, row, include_turns=include_turns)
            if include_turns:
                result["meetingTurns"] = turns
            return result

    def _metadata(self, db, row, *, initialize_notes=True):
        key = row["id"]
        options = json.loads(row["options"])
        custom_location = options.pop("annotationLocation", None)
        has_audio = not options.get("audioRemovedAt")
        if custom_location is not None:
            options["annotationCustomStorage"] = True
            try:
                annotation_storage.folder_for(db, self.audio, key, custom_location)
            except annotation_storage.Unavailable:
                has_audio = False
                options["annotationStorageUnavailable"] = True
                options["annotation"] = {**options["annotation"], "manifestUrl": None, "contactSheetUrl": None}
        is_meeting = bool(options.get("meeting", options.get("sourceSpeakerTurns")))
        managed = is_meeting and meetings_managed()
        overview = session_notes.selection(db, key, initialize_missing=initialize_notes and not self.read_only and not managed)
        brief = meeting_brief.read(db, key) if is_meeting else None
        pending = bool(options.get("meeting", options.get("sourceSpeakerTurns")) and self.has_finalization_table(db)
                       and db.execute("SELECT 1 FROM meeting_finalization WHERE session_id=?", (key,)).fetchone())
        configured = (db.execute("SELECT days FROM meeting_retention WHERE id=1").fetchone()
                      if is_meeting and meeting_retention.readable_source(db) == "readable_sessions" else None)
        return {**{k: row[k] for k in ("id", "name", "created", "duration", "processed", "status")},
                **options, "meeting": is_meeting,
                **({"meetingsManaged": True} if managed else {}),
                "meetingRetentionDays": (configured[0] if configured else options.get("retentionApplies")) or None,
                "finalizationPending": pending,
                "overview": overview, "personalNotes": personal_notes.get(db, key), "meetingBrief": brief,
                "meetingExport": meeting_exports.read(db, key) if is_meeting else None,
                "name": options.get("meetingTitle") or
                    (options.get("meetingCalendar") or {}).get("title") or
                    (brief or {}).get("title") or
                    options.get("provisionalTitle") or
                    (overview["version"]["title"] if overview and overview["version"] else row["name"]),
                "audioName": row["name"] + Path(row["filename"]).suffix if options["recording"] else row["name"],
                "audioPath": f"audio/{key}/{row['filename']}" if has_audio and custom_location is None else None,
                "audioUrl": f"/api/sessions/{key}/audio" if has_audio else None}

    def rename_meeting(self, key, title):
        title = normalize_meeting_title(title)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = meeting_retention.readable_source(db)
            row = db.execute(f"""SELECT status, json_extract(options,'$.meetingTitle') AS title
                FROM {source} WHERE id=? AND
                coalesce(json_extract(options,'$.meeting'),json_extract(options,'$.sourceSpeakerTurns'),0)""", (key,)).fetchone()
            if row is None:
                raise MissingSession(key)
            if row["status"] != "complete":
                raise ValueError("Finish the meeting before renaming it.")
            if row["title"] != title:
                db.execute("UPDATE sessions SET options=json_set(options,'$.meetingTitle',?) WHERE id=?", (title, key))
                meeting_exports.queue_refresh(db, key)
        return title

    @staticmethod
    def has_finalization_table(db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_finalization'").fetchone())

    def media(self, key, *, track=None):
        with self.connection() as db:
            source = meeting_retention.readable_source(db)
            row = db.execute(f"SELECT filename,mime,options FROM {source} WHERE id=?", (key,)).fetchone()
            if not row:
                raise MissingSession(key)
            options = json.loads(row["options"])
            if options.get("audioRemovedAt"):
                raise MissingSession(key)
            if track is not None:
                if track not in ("mic", "system") or track not in options.get("recordingTracks", {}):
                    raise MissingSession(key)
                path = self.audio / key / f"{track}.wav"
                if not path.is_file():
                    raise MissingSession(key)
                return path, "audio/wav"
            folder = self.media_folder(db, key, options)
            path = folder / row["filename"]
            if path.parent != folder or path.is_symlink() or not path.is_file():
                raise MissingSession(key)
            return path, row["mime"]

    def meeting_recording_folder(self, key):
        with self.connection() as db:
            source = meeting_retention.readable_source(db)
            row = db.execute(f"""SELECT filename FROM {source} s WHERE s.id=? AND s.status IN ('complete','interrupted')
                AND coalesce(json_extract(s.options,'$.meeting'),json_extract(s.options,'$.sourceSpeakerTurns'),0)
                AND json_extract(s.options,'$.audioRemovedAt') IS NULL
                AND NOT EXISTS(SELECT 1 FROM meeting_finalization f WHERE f.session_id=s.id)""", (key,)).fetchone()
        if row is None:
            raise MissingSession(key)
        folder = self.audio / key
        media = folder / row["filename"]
        if folder.is_symlink() or media.parent != folder or media.is_symlink() or not media.is_file():
            raise MissingSession(key)
        return folder

    def annotations(self, *, limit=20, prefix=""):
        if self.read_only and not self.path.is_file():
            return []
        with self.connection() as db:
            source = meeting_retention.readable_source(db)
            rows = db.execute(f"""SELECT id,name,created,status,duration AS durationSeconds,
                json_extract(options,'$.annotation.source') AS source,
                json_extract(options,'$.annotation.strokeCount') AS strokeCount,
                json_extract(options,'$.annotation.frames') AS frames
                FROM {source} WHERE json_type(options,'$.annotation')='object' AND id LIKE ?
                ORDER BY created DESC,id LIMIT ?""", (prefix + "%", limit))
            return [{**row, "frames": json.loads(row["frames"] or "[]")} for row in rows]

    def annotation_asset(self, key, kind):
        with self.connection() as db:
            source = meeting_retention.readable_source(db)
            row = db.execute(f"SELECT options FROM {source} WHERE id=?", (key,)).fetchone()
            options = json.loads(row["options"]) if row else {}
            if not options.get("annotation") or kind not in ANNOTATION_ASSETS:
                raise MissingSession(key)
            name, _, mime, _ = ANNOTATION_ASSETS[kind]
            path = self.media_folder(db, key, options) / name
            if path.is_symlink() or not path.is_file():
                raise MissingSession(key)
            return path, mime

    def media_folder(self, db, key, options):
        location = options.get("annotationLocation") if options.get("annotation") else None
        return annotation_storage.folder_for(db, self.audio, key, location)

    def remove_meeting_audio(self, key, *, trash=meeting_retention.trash_path):
        with self.retention_lock:
            when = self._queue_meeting_audio(key)
            self._deliver_audio_trash(trash, only=key)
            return when

    def _queue_meeting_audio(self, key, *, expired=False):
        folder = self.audio / key
        staged = meeting_retention.audio_trash_path(self.root, key)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if expired and not db.execute("SELECT 1 FROM expired_meeting_audio WHERE id=?", (key,)).fetchone():
                return None
            source = meeting_retention.readable_source(db)
            row = db.execute(f"SELECT status,options FROM {source} WHERE id=?", (key,)).fetchone()
            if not row:
                raise MissingSession(key)
            options = json.loads(row["options"])
            if not options.get("meeting", options.get("sourceSpeakerTurns")):
                raise MissingSession(key)
            if options.get("audioRemovedAt"):
                return options["audioRemovedAt"]
            if row["status"] != "complete":
                raise ValueError("Wait for the meeting to finish before removing its audio.")

            when = datetime.now(timezone.utc).isoformat()
            options["audioRemovedAt"] = when
            options.pop("recordingTracks", None)
            moved = False
            try:
                if folder.is_dir():
                    staged.parent.mkdir(mode=0o700, exist_ok=True)
                    if staged.exists():
                        raise OSError("A previous audio transfer is still staged.")
                    folder.rename(staged)
                    moved = True
                    meeting_retention.sync_directory(self.audio)
                    meeting_retention.sync_directory(staged.parent)
                    meeting_retention.sync_directory(self.root)
                    db.execute("INSERT INTO meeting_audio_trash VALUES(?)", (key,))
                db.execute("UPDATE sessions SET options=? WHERE id=?", (encode(options), key))
                db.commit()
            except BaseException:
                if moved:
                    staged.rename(folder)
                raise
            return when

    def _deliver_audio_trash(self, trash, *, only=None):
        with self.connection() as db:
            pending = [row[0] for row in db.execute(
                "SELECT id FROM meeting_audio_trash WHERE ? IS NULL OR id=?", (only, only))]
        failed = []
        for key in pending:
            staged = meeting_retention.audio_trash_path(self.root, key)
            legacy = self.audio / (key + ".removing-audio")
            try:
                if legacy.exists() and not staged.exists():
                    staged.parent.mkdir(mode=0o700, exist_ok=True)
                    legacy.rename(staged)
                    meeting_retention.sync_directory(self.audio)
                    meeting_retention.sync_directory(staged.parent)
                    meeting_retention.sync_directory(self.root)
                if staged.exists():
                    trash(staged)
                    if staged.exists() or legacy.exists():
                        raise OSError("Recorded audio is still waiting to move to the Trash.")
                with self.connection() as db:
                    db.execute("DELETE FROM meeting_audio_trash WHERE id=?", (key,))
            except OSError:
                failed.append(key)
        return failed

    def meeting_audio_days(self):
        with self.connection() as db:
            row = db.execute("SELECT days FROM meeting_audio_policy WHERE id=1").fetchone()
            return (row[0] or None) if row else None

    def set_meeting_audio_days(self, days):
        with self.retention_lock, self.connection() as db:
            db.execute("INSERT INTO meeting_audio_policy VALUES(1,?) ON CONFLICT(id) DO UPDATE SET days=excluded.days", (days or 0,))
            # Keep the old finite preference readable by earlier app versions. In
            # particular, an explicit Forever value must never enter that table.
            if days in (1, 7, 30):
                db.execute("INSERT INTO meeting_audio_retention VALUES(1,?) ON CONFLICT(id) DO UPDATE SET days=excluded.days", (days,))
            else:
                db.execute("DELETE FROM meeting_audio_retention WHERE id=1")

    def expire_meeting_audio(self, trash=meeting_retention.trash_path):
        with self.retention_lock:
            with self.connection() as db:
                expired = [row[0] for row in db.execute("SELECT id FROM expired_meeting_audio")]
            failed = []
            for key in expired:
                try:
                    self._queue_meeting_audio(key, expired=True)
                except OSError:
                    failed.append(key)
            return failed + self._deliver_audio_trash(trash)

    def meeting_retention_days(self):
        with self.connection() as db:
            row = db.execute("SELECT days FROM meeting_retention WHERE id=1").fetchone()
            return row[0] if row else None

    def set_meeting_retention_days(self, days):
        with self.retention_lock, self.connection() as db:
            db.execute("INSERT INTO meeting_retention VALUES(1,?) ON CONFLICT(id) DO UPDATE SET days=excluded.days", (days,))

    def expire_meetings(self, trash=meeting_retention.trash_path):
        with self.retention_lock:
            with self.connection() as db:
                expired = [row[0] for row in db.execute("SELECT id FROM expired_meetings")]
                queued = [row[0] for row in db.execute("SELECT id FROM meeting_trash WHERE sent=0")]
            failures = []
            for key in expired:
                try:
                    with self.export_lock:
                        if self._archive_expired_meeting(key):
                            queued.append(key)
                except OSError:
                    failures.append(key)
            for key in queued:
                try:
                    self._deliver_meeting_archive(key, trash)
                except OSError:
                    failures.append(key)
            return failures

    def _archive_expired_meeting(self, key):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE id=? AND id IN (SELECT id FROM expired_meetings)", (key,)).fetchone()
            if row is None:
                return False
            self._archive_meeting(db, row)
            return True

    def _archive_meeting(self, db, row):
        from saved_meetings import meeting_detail, render_export

        key = row["id"]
        snapshot = self._metadata(db, row, initialize_notes=False)
        snapshot["transcript"], snapshot["meetingTurns"] = self._result(db, row, include_turns=True)
        detail = meeting_detail(snapshot)
        archive = meeting_retention.archive_path(self.root, key)
        archive.parent.mkdir(mode=0o700, exist_ok=True)
        if archive.exists():
            # A crash before the transaction committed can leave a redundant snapshot.
            shutil.rmtree(archive)
        use_24_hour = meeting_exports.clock_24(db)
        meeting_retention.write_snapshot(db, key, archive, self.audio / key,
                                         render_export(detail, "md", use_24_hour=use_24_hour),
                                         render_export(detail, "notes", use_24_hour=use_24_hour))
        db.execute("INSERT INTO meeting_trash(id) VALUES(?)", (key,))
        meeting_exports.queue_trash(db, key)
        db.execute("DELETE FROM sessions WHERE id=?", (key,))

    def _deliver_meeting_archive(self, key, trash):
        meeting_exports.deliver_trash(self, key, trash)
        archive = meeting_retention.archive_path(self.root, key)
        if archive.exists():
            trash(archive)
            if archive.exists():
                raise OSError("The meeting archive is still waiting to move to the Trash.")
        audio = self.audio / key
        if audio.exists():
            shutil.rmtree(audio)
        with self.connection() as db:
            db.execute("UPDATE meeting_trash SET sent=1 WHERE id=?", (key,))

    def delete(self, key, *, trash=meeting_retention.trash_path):
        """Delete a session and return whether a committed meeting archive is still waiting for Trash."""
        with self.retention_lock:
            with self.export_lock, self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM readable_sessions WHERE id=?", (key,)).fetchone()
                if row is None:
                    if not db.execute("SELECT 1 FROM meeting_trash WHERE id=? AND sent=0", (key,)).fetchone():
                        raise MissingSession(key)
                else:
                    options = json.loads(row["options"])
                    if not options.get("meeting", options.get("sourceSpeakerTurns")):
                        self._delete_session(db, key, row["status"], options)
                        return False
                    if not db.execute("SELECT 1 FROM archivable_meetings WHERE id=?", (key,)).fetchone():
                        raise ValueError("Wait for the meeting and its notes or exports to finish before moving it to the Trash.")
                    self._archive_meeting(db, row)
            try:
                self._deliver_meeting_archive(key, trash)
            except (OSError, sqlite3.Error):
                return True
            return False

    def _delete_session(self, db, key, status, options):
        if status == "processing":
            raise ValueError("This session is still processing.")
        folder = self.media_folder(db, key, options)
        location = options.get("annotationLocation") if options.get("annotation") else None
        if location is not None:
            db.execute("INSERT INTO annotation_file_operations VALUES(?,?,'remove')", (key, location))
            db.execute("DELETE FROM sessions WHERE id=?", (key,))
            db.commit()
            self.recover_annotation_files(key)
            return
        staged = self.audio / (key + ".deleting")
        if folder.exists():
            folder.rename(staged)
        try:
            db.execute("DELETE FROM sessions WHERE id=?", (key,))
            db.commit()
        except BaseException:
            if staged.exists():
                staged.rename(folder)
            raise
        if staged.exists():
            shutil.rmtree(staged)
