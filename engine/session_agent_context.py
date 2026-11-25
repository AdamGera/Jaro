"""Build the saved session and gallery payload shared by desktop handoffs."""
import base64
import json
import re

import personal_notes
import session_images
from saved_annotations import annotation_context, annotation_detail, inline_contact_sheet
from saved_meetings import timestamp
from session_store import SessionStore


def session_payload(root, identifier, agent):
    library = SessionStore(root, read_only=True)
    session = library.get(identifier, include_turns=True)
    if session["status"] not in ("complete", "interrupted") or session.get("finalizationPending"):
        raise ValueError("Wait for the session to finish before sharing its context.")
    lines = ["Use this saved Jaro session as context for my next question.", "", f"# {session['name']}", "",
             f"Recorded: {session['created']}"]
    if provider := session.get("meetingProvider"):
        lines.append(f"Meeting app: {provider}")
    brief = session.get("meetingBrief") or {}
    overview = (session.get("overview") or {}).get("version") or {}
    summary = "\n\n".join(filter(None, (overview.get("summary"), overview.get("markdown"))))
    summary = re.sub(r"⟦(\d+(?:\.\d+)?)⟧", lambda match: f"({timestamp(float(match[1]))})", summary)
    for label, detail in (("Quick brief", brief.get("summary")), ("Overview", summary),
                          ("My Notes", personal_notes.document_text(session["personalNotes"]["document"]))):
        if detail:
            lines.extend(["", f"## {label}", "", detail])
    transcript = session["transcript"]
    names = transcript.get("speakerNames") or {}
    segments = session.get("meetingTurns") or transcript.get("segments") or []
    lines.extend(["", "## Transcript", ""])
    for segment in segments:
        name = segment.get("speaker")
        label = names.get(str(name)) or (f"Speaker {name}" if name is not None else "")
        start = timestamp(segment["start"]) if segment.get("start") is not None else ""
        prefix = " · ".join(filter(None, (start, label)))
        if segment.get("text"):
            lines.append((f"**{prefix}**  \n" if prefix else "") + segment["text"])
    if not segments:
        lines.append(transcript.get("text") or "No speech was transcribed.")
    if interruption := transcript.get("interruption"):
        lines.extend(["", interruption])

    images = []
    references = []
    annotation = session.get("annotation") or {}
    if annotation:
        detail = annotation_detail(library, identifier)
        visual_context = annotation_context({**detail, "transcript": ""})["markdown"].partition("## Narration")[0].strip()
        lines.extend(["", "## Screen annotation", "", visual_context.removeprefix("# Screen annotation").strip()])
        if annotation.get("contactSheetUrl"):
            if agent == "t3":
                if not detail["contactSheetPath"]:
                    raise ValueError("The saved annotation images are unavailable.")
                references.append(detail["contactSheetPath"])
            else:
                encoded = inline_contact_sheet(library, identifier)
                if not encoded:
                    raise ValueError("The saved annotation images could not be attached.")
                images.append(("image/png", encoded))
    # Serialize creation of local image files with image and session deletion.
    with library.connection(write_lock=agent == "t3") as db:
        attachments = session_images.list_images(db, identifier)
        folder = library.media_folder(db, identifier, session) if agent == "t3" and attachments else None
        for attachment in attachments:
            image = session_images.get(db, identifier, attachment["id"])
            if image:
                if folder is not None:
                    try:
                        path = session_images.export_for_agent(folder, attachment["id"], image)
                    except OSError as error:
                        raise ValueError("Jaro could not prepare the local image files.") from error
                    references.append(str(path))
                else:
                    images.append(("image/jpeg", base64.b64encode(image).decode("ascii")))
    if references:
        lines.extend(["", "## Images", "", "Read these local image files in gallery order.",
                      *(f"- {json.dumps(path, ensure_ascii=False)}" for path in references)])
    if images:
        lines.extend(["", f"{len(images)} image files are attached in gallery order."])
    text = "\n".join(lines) + "\n"
    return text, images, bool(images or references), bool(annotation)


def check_size(agent, text, images):
    if len(text.encode()) > (120_000 if agent == "t3" else 256 * 1024):
        raise ValueError("The full session exceeds this agent’s message limit.")
    if sum(len(data) for _, data in images) > 13 * 1024 * 1024:
        raise ValueError("The images exceed this agent’s attachment limit.")
    if agent == "claude" and any(len(data) > 5 * 1024 * 1024 for _, data in images):
        raise ValueError("An image exceeds Claude Code’s attachment limit.")


def opencode_body(text, images):
    check_size("opencode", text, images)
    files = [
        {"name": f"jaro-image-{index}.{'png' if mime == 'image/png' else 'jpg'}",
         "uri": f"data:{mime};base64,{data}"}
        for index, (mime, data) in enumerate(images, 1)
    ]
    return {"text": text, "files": files, "delivery": "queue"}
