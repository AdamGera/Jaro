"""Read-only stdio tools for Jard's meetings and screen annotations."""
import json
import sqlite3
import sys

from saved_annotations import annotation_context, annotation_detail, inline_contact_sheet
from meeting_exports import clock_24
from saved_meetings import get_meeting, meeting_detail, meeting_search_report, meetings, notes, render_export
from session_store import MissingSession

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TEXT = {"type": "string"}
LIMIT = {"type": "integer", "minimum": 1}
MEETING = {"meeting_id": {**TEXT, "description": "Meeting UUID, unique prefix, or latest."}}
OFFSET = {"type": ["number", "string"], "description": "Seconds, MM:SS, HH:MM:SS, or an s, m, or h suffix."}
BOOLEAN = {"type": "boolean"}
ANNOTATION = {"annotation_id": {**TEXT, "description": "Annotation UUID, unique prefix, or latest."}}
ANNOTATION_TOOLS = ("list_annotations", "get_annotation")
ANNOTATION_URI = "jard-annotation://"
DATE_FILTERS = {
    "since": {**TEXT, "description": "Include meetings overlapping this date or time onward. Accepts today, yesterday, or a period such as 7d."},
    "until": {**TEXT, "description": "Include meetings starting before this date or time. A date includes that entire day."},
    "include_in_progress": {**BOOLEAN, "description": "Include recordings and meetings still transcribing. Defaults to true."},
}


def tool(name, description, properties, required=()):
    return {"name": name, "description": description, "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "inputSchema": {"type": "object", "properties": properties, "required": list(required),
                            "additionalProperties": False}}


TOOLS = {
    spec["name"]: spec for spec in (
        tool("list_meetings", "Find Jard meetings by title or provider.", {
            "query": TEXT, **DATE_FILTERS, "limit": {**LIMIT, "maximum": 200}}),
        tool("search_transcripts", "Find ranked transcript and note passages with surrounding speech, timestamps, and per-meeting counts.", {
            "query": {**TEXT, "description": "Wrap exact phrases in double quotes."}, **MEETING, **DATE_FILTERS,
            "fuzzy": BOOLEAN, "limit": {**LIMIT, "maximum": 50}}, ("query",)),
        tool("get_transcript", "Read live or saved turns overlapping the requested time range.", {
            **MEETING, "start": OFFSET, "end": OFFSET, "from": OFFSET, "to": OFFSET, "speaker": TEXT,
            "cursor": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000}}, ("meeting_id",)),
        tool("get_meeting_notes", "Read saved overview and personal notes.", MEETING, ("meeting_id",)),
        tool("list_annotations", "List saved screen annotations, newest first.", {"limit": {**LIMIT, "maximum": 200}}),
        tool("get_annotation", "Read saved annotation context with narration, frame times, and an optional inline contact sheet.", {
            **ANNOTATION, "cursor": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 60000},
            "include_contact_sheet": {**BOOLEAN, "description": "Include the image. Defaults to true on the first page."},
        }, ("annotation_id",)),
    )
}


def call(library, name, args, live=None):
    schema = TOOLS[name]["inputSchema"]
    if not isinstance(args, dict) or set(args) - schema["properties"].keys() or set(schema["required"]) - args.keys():
        raise ValueError("Invalid tool arguments.")
    for key, value in args.items():
        spec = schema["properties"][key]
        kind = spec["type"]
        valid = ((kind == "string" and isinstance(value, str)) or
                 (kind == "boolean" and type(value) is bool) or
                 (kind == "integer" and type(value) is int and spec["minimum"] <= value <= spec.get("maximum", value)) or
                 (isinstance(kind, list) and type(value) in (str, int, float)))
        if not valid:
            raise ValueError(f"Invalid value for {key}.")
    if name == "list_annotations":
        return {"annotations": library.annotations(limit=args.get("limit", 20))}
    if name == "get_annotation":
        detail = annotation_detail(library, args["annotation_id"])
        return {**annotation_context(detail, cursor=args.get("cursor", 0), max_chars=args.get("max_chars", 15000)),
                "status": detail["status"], "frames": detail["frames"],
                "contact_sheet_uri": ANNOTATION_URI + detail["id"] + "/contact-sheet" if detail["contactSheetPath"] else None}
    meeting_id = args.get("meeting_id")
    filters = {"since": args.get("since"), "until": args.get("until"), "live": live,
               "include_in_progress": args.get("include_in_progress", True)}
    if name == "list_meetings":
        return {"meetings": meetings(library, args.get("query", ""), args.get("limit", 20), **filters)}
    if name == "search_transcripts":
        return meeting_search_report(library, args["query"], args.get("limit", 10), identifier=meeting_id,
                                     fuzzy=args.get("fuzzy", True), **filters)
    if name == "get_meeting_notes":
        record = get_meeting(library, meeting_id, turns=False, live=live)
        title = (record.get("meetingBrief") or {}).get("title")
        return {"id": record["id"], "title": record["name"], **({"summaryTitle": title} if title else {}), **notes(record)}
    if {"from", "start"} <= args.keys() or {"to", "end"} <= args.keys():
        raise ValueError("Use one name for each transcript offset.")
    detail = meeting_detail(get_meeting(library, meeting_id, live=live), args.get("start", args.get("from")),
                            args.get("end", args.get("to")), speaker=args.get("speaker"))
    detail.pop("notes")
    turns = detail["turns"]
    cursor = args.get("cursor", 0)
    if cursor > len(turns):
        raise ValueError("The cursor is beyond the transcript.")
    length, end = 0, cursor
    while end < len(turns):
        size = len(turns[end]["text"])
        if end > cursor and length + size > args.get("max_chars", 15000):
            break
        length += size
        end += 1
    return {**detail, "turns": turns[cursor:end], "next_cursor": end if end < len(turns) else None}


def annotation_resource(library, uri):
    identifier, slash, kind = uri.removeprefix(ANNOTATION_URI).partition("/")
    detail = annotation_detail(library, identifier)
    if not slash:
        return {"uri": uri, "mimeType": "text/markdown", "text": annotation_context(detail)["markdown"]}
    if kind == "contact-sheet" and (image := inline_contact_sheet(library, detail["id"])):
        return {"uri": uri, "mimeType": "image/png", "blob": image}
    raise MissingSession(identifier)


def reply(library, incoming, live_snapshot=lambda **_: None):
    if not isinstance(incoming, dict) or incoming.get("jsonrpc") != "2.0" or not isinstance(incoming.get("method"), str):
        return {"id": None, "error": {"code": -32600, "message": "Invalid request."}}
    if "id" not in incoming:
        return None
    identifier, method = incoming["id"], incoming["method"]
    params = incoming.get("params", {})
    if not isinstance(params, dict):
        return {"id": identifier, "error": {"code": -32602, "message": "Invalid parameters."}}
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in VERSIONS else VERSIONS[0], "capabilities": {"tools": {}, "resources": {}},
                  "serverInfo": {"name": "jard-meetings", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list(TOOLS.values())}
    elif method == "resources/list":
        result = {"resources": []}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": [{"uriTemplate": "jard-meeting://{meeting_id}", "name": "Saved meeting",
                                         "mimeType": "text/markdown"},
                                        {"uriTemplate": ANNOTATION_URI + "{annotation_id}", "name": "Saved annotation context",
                                         "mimeType": "text/markdown"},
                                        {"uriTemplate": ANNOTATION_URI + "{annotation_id}/contact-sheet", "name": "Annotation contact sheet",
                                         "mimeType": "image/png"}]}
    elif method == "resources/read":
        uri = params.get("uri")
        if isinstance(uri, str) and uri.startswith(ANNOTATION_URI):
            try:
                resource = annotation_resource(library, uri)
            except (ValueError, MissingSession, sqlite3.Error):
                return {"id": identifier, "error": {"code": -32002, "message": "Annotation resource not found."}}
            return {"id": identifier, "result": {"contents": [resource]}}
        try:
            if not isinstance(uri, str) or not uri.startswith("jard-meeting://"):
                raise ValueError
            detail = meeting_detail(get_meeting(library, uri.removeprefix("jard-meeting://"), live=live_snapshot()))
            with library.connection() as db:
                text = render_export(detail, "md", use_24_hour=clock_24(db), companion=True)
            result = {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}
        except (ValueError, MissingSession, sqlite3.Error):
            return {"id": identifier, "error": {"code": -32002, "message": "Meeting resource not found."}}
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS:
            return {"id": identifier, "error": {"code": -32602, "message": "Unknown tool."}}
        try:
            args = params.get("arguments", {})
            annotation = name in ANNOTATION_TOOLS
            live = (live_snapshot(turns=name in ("get_transcript", "search_transcripts"))
                    if not annotation and isinstance(args, dict) and args.get("include_in_progress", True) else None)
            data = call(library, name, args, live)
            content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
            if name == "get_annotation" and args.get("include_contact_sheet", not args.get("cursor")):
                if image := inline_contact_sheet(library, data["id"]):
                    content.append({"type": "image", "mimeType": "image/png", "data": image})
            result = {"content": content}
        except (MissingSession, sqlite3.Error, ValueError) as error:
            subject = "annotation" if name in ANNOTATION_TOOLS else "meeting"
            message = f"{subject.capitalize()} not found." if isinstance(error, MissingSession) else (
                f"The saved {subject} library is unavailable." if isinstance(error, sqlite3.Error) else str(error))
            result = {"isError": True, "content": [{"type": "text", "text": message}]}
    else:
        return {"id": identifier, "error": {"code": -32601, "message": "Method not found."}}
    return {"id": identifier, "result": result}


def serve(library, live_snapshot=lambda **_: None):
    for line in sys.stdin:
        try:
            result = reply(library, json.loads(line), live_snapshot)
        except json.JSONDecodeError:
            result = {"id": None, "error": {"code": -32700, "message": "Invalid JSON."}}
        if result is not None:
            print(json.dumps({"jsonrpc": "2.0", **result}, ensure_ascii=False), flush=True)
