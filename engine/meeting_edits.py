"""Delete published turns from a finished meeting as a single library transaction."""
import hashlib
import json

import meeting_brief
import meeting_exports
import meeting_retention
import session_notes


SCHEMA = """CREATE TABLE IF NOT EXISTS meeting_edit_policy (
    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))"""


def enabled(db):
    return bool(db.execute("SELECT 1 FROM meeting_edit_policy WHERE id=1 AND enabled").fetchone())


def revision(turns):
    data = json.dumps(turns, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def filter_chunk(chunk, removed):
    turns = chunk["segments"]
    # Older chunks can contain text that was never attributed to a turn.
    if "".join(chunk["text"].split()) != "".join("".join(turn["text"].split()) for turn in turns):
        raise ValueError("These transcript lines could not be matched to all of the saved text.")
    kept = [turn for i, turn in enumerate(turns) if i not in removed]
    words, pos = chunk["words"], 0
    remaining = []
    # Producers emit words in turn order, even when speakers overlap or timestamps are absent.
    # Match exact text and speaker before changing anything in an older library.
    if words and kept:
        for i, turn in enumerate(turns):
            text = turn["text"].strip()
            cursor = 0
            while cursor < len(text):
                if pos >= len(words):
                    raise ValueError("These transcript lines could not be matched to their saved words.")
                word = words[pos]
                token = word["text"]
                if not token or word.get("speaker") != turn.get("speaker") or not text.startswith(token, cursor):
                    raise ValueError("These transcript lines could not be matched to their saved words.")
                if i not in removed:
                    remaining.append(word)
                cursor += len(token)
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                pos += 1
        if pos != len(words):
            raise ValueError("These transcript lines could not be matched to their saved words.")
    # Speaker activity describes the audio itself and remains valid after editing the text.
    return {**chunk, "segments": kept, "words": remaining, "text": " ".join(t["text"] for t in kept)}


def delete_lines(store, key, expected, indices):
    from session_store import MissingSession, encode

    with store.export_lock, store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if not enabled(db):
            raise ValueError("Turn on Delete transcript lines in Meetings settings first.")
        row = db.execute(f"SELECT * FROM {meeting_retention.readable_source(db)} WHERE id=?", (key,)).fetchone()
        if not row:
            raise MissingSession(key)
        if (row["status"] != "complete" or not db.execute("SELECT 1 FROM archivable_meetings WHERE id=?", (key,)).fetchone()):
            raise ValueError("Wait for this meeting and its writing and export jobs to finish.")
        if db.execute("SELECT 1 FROM meeting_rolling WHERE session_id=?", (key,)).fetchone():
            raise ValueError("This recovered transcript contains provisional lines and cannot be edited.")
        if db.execute("SELECT 1 FROM overviews WHERE id=? AND json_extract(body,'$.manuallyEdited')", (key,)).fetchone():
            raise ValueError("This meeting has a manually edited overview. Its transcript is protected.")
        chunks = [(r[0], json.loads(r[1])) for r in db.execute(
            "SELECT seconds,payload FROM chunks WHERE session_id=? ORDER BY seconds", (key,))]
        turns = [t for _, chunk in chunks for t in chunk["segments"]]
        if revision(turns) != expected:
            raise ValueError("The transcript changed. Reopen the meeting and select the lines again.")
        selected = set(indices)
        if not selected or min(selected) < 0 or max(selected) >= len(turns):
            raise ValueError("Select one or more existing transcript lines.")
        offset = 0
        for seconds, chunk in chunks:
            size = len(chunk["segments"])
            removed = {i for i in range(size) if offset + i in selected}
            if removed:
                db.execute("UPDATE chunks SET payload=? WHERE session_id=? AND seconds=?",
                           (encode(filter_chunk(chunk, removed)), key, seconds))
            offset += size
        transcript, _ = store._result(db, row, rebuild=True)
        db.execute("""UPDATE sessions SET transcript=?,search_text=?,options=json_remove(options,'$.provisionalTitle')
                      WHERE id=?""", (encode(transcript), transcript["text"], key))
        db.execute("DELETE FROM meeting_briefs WHERE id=?", (key,))
        session_notes.initialize(db, key, refinalize=True)
        if transcript["text"] and meeting_brief.automatic_policy(db)[0]:
            meeting_brief.enqueue(db, key, automatic=True)
        meeting_exports.queue_refresh(db, key)
