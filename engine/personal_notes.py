"""User-authored session documents and their immutable writing inputs."""

import json
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

EMPTY = {"type": "doc", "content": [{"type": "paragraph"}]}
MARKS = {"bold", "italic", "underline", "strike", "link"}
INLINE = {"text", "hardBreak"}
BLOCKS = {"paragraph", "heading", "blockquote", "bulletList", "orderedList", "taskList", "horizontalRule"}
CHILDREN = {
    "doc": BLOCKS, "paragraph": INLINE, "heading": INLINE, "blockquote": BLOCKS,
    "bulletList": {"listItem"}, "orderedList": {"listItem"}, "listItem": BLOCKS,
    "taskList": {"taskItem"}, "taskItem": BLOCKS,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_notes (
    id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    document TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_note_drafts (
    id TEXT PRIMARY KEY, document TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created INTEGER NOT NULL DEFAULT (unixepoch())
);
"""


def clean_node(value, allowed, depth=0):
    if not isinstance(value, dict) or depth > 32 or value.get("type") not in allowed:
        raise ValueError("Unsupported note formatting.")
    kind = value["type"]
    node = {"type": kind}
    attrs = value.get("attrs") or {}
    if kind == "text":
        if not isinstance(value.get("text"), str) or not value["text"]:
            raise ValueError("Invalid note text.")
        node["text"] = value["text"]
        marks = []
        for mark in value.get("marks", []):
            name = mark["type"]
            if name not in MARKS:
                raise ValueError("Unsupported note formatting.")
            if name == "link":
                href = mark["attrs"]["href"]
                if not isinstance(href, str) or not href.lower().startswith(("https://", "http://", "mailto:", "tel:")):
                    continue
                marks.append({"type": name, "attrs": {"href": href}})
            else:
                marks.append({"type": name})
        if marks:
            node["marks"] = marks
    if kind in CHILDREN:
        children = [clean_node(child, CHILDREN[kind], depth + 1) for child in value.get("content", [])]
        if kind == "doc" and not children:
            return EMPTY
        if kind in {"listItem", "taskItem"} and (not children or children[0]["type"] != "paragraph"):
            raise ValueError("A list item needs a paragraph.")
        if kind in {"blockquote", "bulletList", "orderedList", "taskList"} and not children:
            raise ValueError("Invalid empty block.")
        if children:
            node["content"] = children
    if kind == "heading":
        level = attrs.get("level", 1)
        if type(level) is not int or not 1 <= level <= 6:
            raise ValueError("Invalid heading level.")
        node["attrs"] = {"level": level}
    if kind == "taskItem":
        checked = attrs.get("checked", False)
        if type(checked) is not bool:
            raise ValueError("Invalid checkbox state.")
        node["attrs"] = {"checked": checked}
    if kind == "orderedList":
        start = attrs.get("start", 1)
        if type(start) is not int or not 1 <= start <= 1_000_000:
            raise ValueError("Invalid list numbering.")
        node["attrs"] = {"start": start}
    if kind in {"paragraph", "heading"} and attrs.get("dir") in ("auto", "ltr", "rtl"):
        node.setdefault("attrs", {})["dir"] = attrs["dir"]
    return node


def encode(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class NoteDocument(BaseModel):
    document: dict

    @field_validator("document")
    @classmethod
    def validate_document(cls, document):
        try:
            if len(encode(document).encode()) > 256_000:
                raise ValueError("My notes is too large to save.")
            return clean_node(document, {"doc"})
        except (AttributeError, KeyError, RecursionError, TypeError, UnicodeError) as error:
            raise ValueError("Invalid note document.") from error


class NoteEdit(NoteDocument):
    revision: int = Field(ge=0)


def migrate(db):
    db.executescript(SCHEMA)
    draft_columns = {row["name"] for row in db.execute("PRAGMA table_info(personal_note_drafts)")}
    if "revision" not in draft_columns:
        db.execute("ALTER TABLE personal_note_drafts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(overviews)")}
    for name in ("queued_personal", "published_personal"):
        if name not in columns:
            db.execute(f"ALTER TABLE overviews ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    db.execute("DELETE FROM personal_note_drafts WHERE created < unixepoch() - 604800")


def get(db, key):
    row = db.execute("SELECT document,revision FROM personal_notes WHERE id=?", (key,)).fetchone()
    return {"document": json.loads(row[0]), "revision": row[1]} if row else {"document": EMPTY, "revision": 0}


def save(db, key, edit):
    current = get(db, key)
    if current["document"] == edit.document:
        return current["revision"]
    if current["revision"] != edit.revision:
        return None
    revision = edit.revision + 1
    db.execute("""INSERT INTO personal_notes VALUES(?,?,?) ON CONFLICT(id) DO UPDATE
                  SET document=excluded.document,revision=excluded.revision""", (key, encode(edit.document), revision))
    return revision


def get_draft(db, key):
    if db.execute("SELECT 1 FROM sessions WHERE id=?", (key,)).fetchone():
        return {"promoted": True, **get(db, key)}
    row = db.execute("SELECT document,revision FROM personal_note_drafts WHERE id=?", (key,)).fetchone()
    if row:
        return {"document": json.loads(row[0]), "revision": row[1], "promoted": False}
    return {"document": EMPTY, "revision": 0, "promoted": False}


def save_draft(db, key, edit):
    if db.execute("SELECT 1 FROM sessions WHERE id=?", (key,)).fetchone():
        return save(db, key, edit), True
    row = db.execute("SELECT document,revision FROM personal_note_drafts WHERE id=?", (key,)).fetchone()
    current_doc = json.loads(row[0]) if row else EMPTY
    current_rev = row[1] if row else 0
    if current_doc == edit.document:
        return current_rev, False
    if current_rev != edit.revision:
        return None, False
    revision = edit.revision + 1
    db.execute("DELETE FROM personal_note_drafts WHERE created < unixepoch() - 604800 AND id!=?", (key,))
    db.execute("""INSERT INTO personal_note_drafts(id,document,revision,created) VALUES(?,?,?,unixepoch())
                  ON CONFLICT(id) DO UPDATE SET document=excluded.document,revision=excluded.revision,
                  created=excluded.created""", (key, encode(edit.document), revision))
    return revision, False


def delete_draft(db, key):
    db.execute("DELETE FROM personal_note_drafts WHERE id=?", (key,))


def stage(db, document):
    key = str(uuid4())
    db.execute("DELETE FROM personal_note_drafts WHERE created < unixepoch() - 604800")
    db.execute("INSERT INTO personal_note_drafts(id,document,revision) VALUES(?,?,1)", (key, encode(document)))
    return key


def attach(db, key, draft, *, required=True):
    if not draft:
        return
    row = db.execute("SELECT document,revision FROM personal_note_drafts WHERE id=?", (draft,)).fetchone()
    if not row:
        if required:
            raise ValueError("The notes draft is unavailable. Try starting the session again.")
        return
    db.execute("INSERT INTO personal_notes(id,document,revision) VALUES(?,?,?)", (key, row[0], row[1]))
    db.execute("DELETE FROM personal_note_drafts WHERE id=?", (draft,))


def as_context(db, key):
    return document_text(get(db, key)["document"])


def document_text(document):
    def render(node):
        kind = node["type"]
        children = node.get("content", [])
        if kind == "text":
            return node["text"]
        if kind == "hardBreak":
            return "\n"
        if kind in {"paragraph", "heading"}:
            text = "".join(map(render, children))
            return ("#" * node["attrs"]["level"] + " " + text) if kind == "heading" and text.strip() else text
        if kind in {"bulletList", "orderedList"}:
            start = node.get("attrs", {}).get("start", 1)
            return "\n".join(
                ("- " if kind == "bulletList" else f"{index}. ") + item.replace("\n", "\n  ")
                for index, item in enumerate(map(render, children), start) if item.strip()
            )
        if kind == "taskList":
            items = []
            for child in children:
                item = render(child)
                if item.strip():
                    mark = "x" if child["attrs"]["checked"] else " "
                    items.append(f"- [{mark}] " + item.replace("\n", "\n  "))
            return "\n".join(items)
        text = "\n\n".join(filter(str.strip, map(render, children)))
        return "\n".join("> " + line for line in text.splitlines()) if kind == "blockquote" else text

    return render(document).strip()


def fingerprint(context):
    return sha256(context.encode()).hexdigest() if context else ""
