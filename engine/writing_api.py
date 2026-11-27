"""Session-local routes for overview variants and text-only transcript conversations."""

import asyncio
import json
import logging
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from session_notes import LANGUAGES, RegenerationConflict, edit_overview, initialize, selection
from meeting_retention import readable_source
import chat_context
import meeting_brief
from managed_preferences import meetings_managed, require_meetings
import personal_notes
import writing_cache
from session_store import MissingSession

router = APIRouter()
logger = logging.getLogger("uvicorn.error")


class OverviewChoice(BaseModel):
    language: str = Field(max_length=32)
    retry: bool = False
    updatePersonal: bool = False
    regenerate: bool | Literal["overview", "timeline"] = False


class OverviewEdit(BaseModel):
    language: str = Field(max_length=32)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(max_length=32_000)
    markdown: str = Field(max_length=256_000)


class ChatQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=1, max_length=4000)


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Open Jaro from its local launcher.")


def session_status(db, key):
    source = readable_source(db)
    row = db.execute(f"""SELECT status,length(search_text) AS size,
                        coalesce(json_extract(options,'$.meeting'),json_extract(options,'$.sourceSpeakerTurns'),0) AS meeting
                        FROM {source} WHERE id=?""", (key,)).fetchone()
    if not row:
        raise MissingSession(key)
    return row


def meeting_brief_response(request, key, *, enqueue=False):
    state = request.app.state
    with state.library.connection() as db:
        if enqueue:
            db.execute("BEGIN IMMEDIATE")
        row = db.execute(f"""SELECT status,length(trim(search_text)) AS size FROM {readable_source(db)}
            WHERE id=? AND coalesce(json_extract(options,'$.meeting'),json_extract(options,'$.sourceSpeakerTurns'),0)""",
                         (key,)).fetchone()
        if not row:
            raise MissingSession(key)
        if enqueue:
            if state.draining:
                raise HTTPException(409, "Jard is preparing to restart.")
            if row["status"] != "complete" or not row["size"]:
                raise HTTPException(409, "A finished meeting with speech is required to write a summary.")
            meeting_brief.enqueue(db, key)
        result = meeting_brief.read(db, key)
    if enqueue:
        state.insights.wake.set()
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/api/sessions/{key}/meeting-brief")
def get_meeting_brief(key: UUID, request: Request):
    return meeting_brief_response(request, str(key))


@router.post("/api/sessions/{key}/meeting-brief")
async def summarize_meeting(key: UUID, request: Request):
    check_origin(request)
    require_meetings()
    return meeting_brief_response(request, str(key), enqueue=True)


def refresh_pending_overview_personal(db, key):
    from insights import GUIDE, SUMMARY

    if db.execute("SELECT 1 FROM window_notes WHERE id=? AND part IN (?,?)", (key, GUIDE, SUMMARY)).fetchone():
        return
    context = personal_notes.as_context(db, key)
    db.execute(
        """UPDATE overviews SET queued_personal=?
           WHERE id=? AND status='queued' AND body IS NULL AND generation=0""",
        (context, key),
    )


@router.get("/api/sessions/{key}/personal-notes")
def get_personal_notes(key: UUID, request: Request):
    with request.app.state.library.connection() as db:
        session_status(db, str(key))
        return JSONResponse(personal_notes.get(db, str(key)), headers={"Cache-Control": "no-store"})


@router.put("/api/sessions/{key}/personal-notes")
def save_personal_notes(key: UUID, body: personal_notes.NoteEdit, request: Request):
    check_origin(request)
    with request.app.state.library.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        session_status(db, str(key))
        revision = personal_notes.save(db, str(key), body)
        if revision is None:
            raise HTTPException(409, "My notes changed in another window.")
        refresh_pending_overview_personal(db, str(key))
        return {"revision": revision}


@router.post("/api/personal-note-drafts", status_code=201)
def stage_personal_notes(body: personal_notes.NoteDocument, request: Request):
    check_origin(request)
    with request.app.state.library.connection() as db:
        return {"id": personal_notes.stage(db, body.document)}


@router.get("/api/personal-note-drafts/{key}")
def get_personal_note_draft(key: UUID, request: Request):
    with request.app.state.library.connection() as db:
        return JSONResponse(personal_notes.get_draft(db, str(key)), headers={"Cache-Control": "no-store"})


@router.put("/api/personal-note-drafts/{key}")
def save_personal_note_draft(key: UUID, body: personal_notes.NoteEdit, request: Request):
    check_origin(request)
    with request.app.state.library.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        revision, promoted = personal_notes.save_draft(db, str(key), body)
        if revision is None:
            raise HTTPException(409, "My notes changed in another window.")
        if promoted:
            refresh_pending_overview_personal(db, str(key))
        return {"revision": revision, "promoted": promoted}


@router.get("/api/overview-languages")
def overview_languages():
    return LANGUAGES


def overview_response(request, key, choice=None):
    with request.app.state.library.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        managed = session_status(db, key)["meeting"] and meetings_managed()
        if managed and choice is not None:
            require_meetings()
        try:
            value = selection(db, key, choice.language if choice else None, choice.retry if choice else False,
                              regenerate=choice.regenerate if choice else False,
                              update_personal=choice.updatePersonal if choice else False, initialize_missing=not managed)
        except RegenerationConflict as error:
            raise HTTPException(409, str(error)) from error
    if value is None:
        raise HTTPException(409, "The overview is available after transcription finishes.")
    if value["status"] == "queued" and not managed:
        request.app.state.insights.wake.set()
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@router.get("/api/sessions/{key}/overview")
async def get_overview(key: UUID, request: Request):
    return overview_response(request, str(key))


@router.get("/api/sessions/{key}/overview/stream")
async def overview_stream(key: UUID, language: str, request: Request):
    key = str(key)
    library = request.app.state.library
    if language != "und" and language not in LANGUAGES:
        raise HTTPException(400, "Choose a supported overview language.")
    with library.connection() as db:
        if session_status(db, key)["meeting"]:
            require_meetings()

    def queued():
        with library.connection() as db:
            return db.execute(
                "SELECT 1 FROM overviews WHERE id=? AND lang=? AND status='queued'", (key, language)
            ).fetchone() is not None

    async def events():
        with request.app.state.insights.previews.watch(key, language) as channel:
            yield ": connected\n\n"
            while True:
                changed = channel.changed
                if channel.draft is None and not queued():
                    yield "event: end\ndata: {}\n\n"
                    return
                if channel.draft is not None:
                    payload = json.dumps(channel.draft, ensure_ascii=False, separators=(",", ":"))
                    yield f"event: draft\ndata: {payload}\n\n"
                try:
                    await asyncio.wait_for(changed.wait(), timeout=5)
                except TimeoutError:
                    if not queued():
                        yield "event: end\ndata: {}\n\n"
                        return
                    yield ": waiting\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/api/sessions/{key}/overview")
async def choose_overview(key: UUID, choice: OverviewChoice, request: Request):
    check_origin(request)
    if choice.language != "auto" and choice.language not in LANGUAGES:
        raise HTTPException(400, "Choose a supported overview language.")
    return overview_response(request, str(key), choice)


@router.put("/api/sessions/{key}/overview")
def save_overview(key: UUID, body: OverviewEdit, request: Request):
    check_origin(request)
    if body.language not in LANGUAGES and body.language != "und":
        raise HTTPException(400, "Choose a supported overview language.")
    key = str(key)
    with request.app.state.library.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        session_status(db, key)
        try:
            edit_overview(db, key, body.language, body.revision, body.summary, body.markdown)
        except RegenerationConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        value = selection(db, key)
    return JSONResponse(value, headers={"Cache-Control": "no-store"})


@router.get("/api/sessions/{key}/chat")
def chat_history(key: UUID, request: Request):
    with request.app.state.library.connection() as db:
        session_status(db, str(key))
        rows = db.execute(
            """SELECT t.*,n.source_digest AS current_source FROM chat_turns t LEFT JOIN notes n ON n.id=t.id
               WHERE t.id=? ORDER BY t.rowid""",
            (str(key),),
        ).fetchall()
    messages = []
    for row in rows:
        answer = row["answer"]
        if row["source_digest"] and row["current_source"] != row["source_digest"]:
            # Historical dialogue stays readable after re-transcription; its old seek targets no longer apply.
            answer = re.sub(r" *⟦\d+(?:\.\d+)?⟧", "", answer)
        messages.append(
            {"id": row["message_id"], "role": "user", "parts": [{"type": "text", "text": row["question"]}]}
        )
        messages.append(
            {
                "id": "answer-" + row["message_id"],
                "role": "assistant",
                "metadata": {"status": row["status"], **({"error": row["error"]} if row["error"] else {})},
                "parts": [{"type": "text", "text": answer}],
            }
        )
    return JSONResponse(messages, headers={"Cache-Control": "no-store"})


def completed_answer(db, signature):
    return db.execute(
        """SELECT answer FROM chat_turns WHERE request_digest=? AND status='complete'
           ORDER BY rowid LIMIT 1""",
        (signature,),
    ).fetchone()


@router.post("/api/sessions/{key}/chat")
async def ask_chat(key: UUID, body: ChatQuestion, request: Request):
    check_origin(request)
    key = str(key)
    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Enter a question.")
    store = request.app.state.library
    insights = request.app.state.insights
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        session = session_status(db, key)
        if session["status"] != "complete" or not session["size"]:
            raise HTTPException(409, "Chat needs a completed transcript with speech.")
        initialize(db, key)
        context = chat_context.snapshot(db, key)
        source = context["source"]
        existing = db.execute(
            "SELECT rowid,* FROM chat_turns WHERE id=? AND message_id=?", (key, body.id)
        ).fetchone()
        if existing and existing["question"] != question:
            raise HTTPException(409, "This message ID already belongs to another question.")

        if db.execute("SELECT 1 FROM chat_turns WHERE id=? AND status='streaming'", (key,)).fetchone():
            raise HTTPException(409, "A reply is already in progress.")
        # Retry an existing turn with precisely the conversation that preceded it.
        before = existing["rowid"] if existing else 2**63 - 1
        recent = db.execute(
            """SELECT question,answer FROM chat_turns WHERE id=? AND rowid<?
                    AND source_digest=? AND status IN ('complete','stopped')
                    ORDER BY rowid DESC LIMIT 5""",
            (key, before, source),
        ).fetchall()
        history = []
        for row in reversed(recent):
            history.extend(
                [
                    {"role": "user", "content": row["question"]},
                    {"role": "assistant", "content": row["answer"]},
                ]
            )
        from insights import STAGE_REVISIONS

        signature = writing_cache.memo_key(
            "chat-answer-v2", [STAGE_REVISIONS["chat"], source, context["fingerprint"], context["personal"], history, question]
        )
        cached = completed_answer(db, signature)
        answer, status = (cached["answer"], "complete") if cached else ("", "streaming")
        db.execute(
            """INSERT INTO chat_turns(id,message_id,question,answer,status,source_digest,request_digest)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(id,message_id) DO UPDATE SET
                   status=excluded.status,answer=excluded.answer,source_digest=excluded.source_digest,
                   request_digest=excluded.request_digest,error=NULL""",
            (key, body.id, question, answer, status, source, signature),
        )
    if cached:
        return StreamingResponse(
            iter([answer]), media_type="text/plain", headers={"Cache-Control": "no-store"}
        )

    def save(answer, status, reason=None):
        with store.connection() as db:
            return db.execute(
                """UPDATE chat_turns SET answer=?,status=?,error=? WHERE id=? AND message_id=?
                   AND request_digest=? AND status='streaming'""",
                (answer, status, reason, key, body.id, signature),
            ).rowcount

    async def stream():
        answer = ""
        status = "stopped"
        reason = None
        last_save = time.monotonic()
        try:
            with writing_cache.scope(store, key, purpose="chat"):
                async with insights.chat_priority():
                    # A peer may have finished the same request while this stream waited for the model.
                    with store.connection() as db:
                        current = db.execute("SELECT source_digest FROM notes WHERE id=?", (key,)).fetchone()
                        if not current or current["source_digest"] != source:
                            return
                        cached = completed_answer(db, signature)
                    if cached:
                        answer = cached["answer"]
                        status = "complete"
                        yield answer
                        return
                    async for token in insights.chat_tokens(key, question, history, context):
                        answer += token
                        if time.monotonic() - last_save > 0.4:
                            if not save(answer, "streaming"):
                                return
                            last_save = time.monotonic()
                        yield token
                    status = "complete" if answer.strip() else "error"
                    if status == "error":
                        reason = "The local writing model returned an empty answer. Retry this message."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = "error"
            if isinstance(exc, RuntimeError) and str(exc).startswith(("Run Setup Jaro", "Local writing model")):
                reason = str(exc)
            elif isinstance(exc, ValueError) and str(exc).startswith("Please shorten the question"):
                reason = str(exc)
            else:
                reason = "The local writing model could not finish this answer. Retry the message."
            logger.warning("Local chat failed (%s)", type(exc).__name__)
        finally:
            save(answer, status, reason)

    return StreamingResponse(
        stream(), media_type="text/plain", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )
