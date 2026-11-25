"""Keep portable workspace drafts and preferences across loopback port changes."""

import json
import re

from pydantic import BaseModel, Field, field_validator


LOCATION = "jard.lastSession"
SESSION = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
DRAFT = re.compile(rf"(?:jard\.personalNotes\.(?P<notes>{SESSION})|jard\.overviewDraft\.(?P<overview>{SESSION})\.[A-Za-z0-9-]{{1,32}})")
TABS = {"overview", "timeline", "transcript", "notes", "analytics", "annotation"}
PREFERENCES = {
    "jard.recordingMode": {"mic", "system", "both"},
    "jard.sidebar": {"expanded", "collapsed"},
}


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_drafts (
        key TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, value TEXT NOT NULL
    )""")
    db.execute("CREATE TABLE IF NOT EXISTS workspace_preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def session_for(key, value):
    if key == LOCATION:
        if value is None:
            return None
        session = value.get("id")
        tab = value.get("tab")
        if isinstance(session, str) and re.fullmatch(SESSION, session) and isinstance(tab, str) and tab in TABS:
            return session
        raise ValueError("Invalid workspace location.")
    match = DRAFT.fullmatch(key)
    if match is None:
        raise ValueError("Invalid workspace draft.")
    if value is not None:
        revision, document = value.get("revision"), value.get("document")
        if not isinstance(document, dict) or document.get("type") != "doc":
            raise ValueError("Invalid workspace draft document.")
        if match["notes"]:
            valid = type(revision) is int and revision >= 0
        else:
            valid = isinstance(revision, str) and bool(re.fullmatch(r"[a-f0-9]{64}", revision))
        if not valid:
            raise ValueError("Invalid workspace draft revision.")
    return match["notes"] or match["overview"]


class DraftChanges(BaseModel):
    values: dict[str, dict | str | None] = Field(min_length=1, max_length=64)

    @field_validator("values")
    @classmethod
    def validate_values(cls, values):
        for key, value in values.items():
            if key in PREFERENCES:
                if not isinstance(value, str) or value not in PREFERENCES[key]:
                    raise ValueError("Invalid workspace preference.")
                continue
            if value is not None and not isinstance(value, dict):
                raise ValueError("Invalid workspace draft.")
            session_for(key, value)
            if value is not None and len(encode(value)) > 1_200_000:
                raise ValueError("The draft is too large to keep.")
        return values


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def write(db, changes):
    db.execute("BEGIN IMMEDIATE")
    for key, value in changes.values.items():
        if key in PREFERENCES:
            db.execute("""INSERT INTO workspace_preferences (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))
        elif value is None:
            db.execute("DELETE FROM workspace_drafts WHERE key=?", (key,))
        else:
            session = session_for(key, value)
            db.execute("""
                INSERT INTO workspace_drafts (key, session, value)
                SELECT ?, id, ? FROM sessions WHERE id=?
                ON CONFLICT(key) DO UPDATE SET session=excluded.session, value=excluded.value
            """, (key, encode(value), session))


def boot_data(db):
    values = {key: json.loads(value) for key, value in db.execute("SELECT key, value FROM workspace_drafts")}
    values.update(db.execute("SELECT key, value FROM workspace_preferences"))
    return encode(values).replace("<", "\\u003c").encode("ascii")
