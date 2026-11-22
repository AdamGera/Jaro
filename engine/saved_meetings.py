"""Read saved meetings and render exports from the session library."""
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

from meeting_rolling import MIXED_SPEAKERS, RATE, SOURCE_LABELS, read_rolling
from meeting_search import rank_passages, words
from native_recordings import list_drafts
from personal_notes import document_text
from session_store import MissingSession


def parse_offset(value):
    if value is None:
        return None
    try:
        text = str(value).strip().lower()
        if not text:
            return None
        if ":" in text:
            parts = [part.strip() for part in text.split(":")]
            if len(parts) not in (2, 3) or any(part.startswith(("+", "-")) for part in parts):
                raise ValueError
            numbers = [float(part) for part in parts]
            if any(not math.isfinite(n) for n in numbers):
                raise ValueError
            if any(not n.is_integer() for n in numbers[:-1]) or any(n >= 60 for n in numbers[1:]):
                raise ValueError
            seconds = sum(n * 60**index for index, n in enumerate(reversed(numbers)))
        else:
            scale = {"s": 1, "m": 60, "h": 3600}.get(text[-1])
            amount = text[:-1].strip() if scale else text
            if amount.startswith(("+", "-")):
                raise ValueError
            seconds = float(amount) * (scale or 1)
        milliseconds = seconds * 1000
        if not 0 <= milliseconds < 2**64:
            raise ValueError
        return math.floor(milliseconds + .5)
    except (TypeError, OverflowError, ValueError):
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS for transcript offsets.") from None


def date_bound(value, now, *, through=False):
    if value is None:
        return None
    value = value.strip().lower()
    try:
        if value == "now":
            return now
        if value in ("today", "yesterday"):
            local = now.astimezone().date() - timedelta(days=value == "yesterday") + timedelta(days=through)
            return datetime.combine(local, time.min).astimezone(timezone.utc)
        if re.fullmatch(r"[0-9]{10,13}", value):
            return datetime.fromtimestamp(int(value) / (1000 if len(value) > 10 else 1), timezone.utc)
        relative = re.fullmatch(r"([0-9]+)\s*([mhdw])(?: ago)?", value)
        if relative:
            amount, unit = relative.groups()
            return now - timedelta(minutes=int(amount) * {"m": 1, "h": 60, "d": 1440, "w": 10080}[unit])
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return datetime.combine(date.fromisoformat(value) + timedelta(days=through), time.min).astimezone(timezone.utc)
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[t ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?\s*(?:z|[+-][0-9]{2}:?[0-9]{2})?", value):
            return datetime.fromisoformat(value.replace("z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    raise ValueError("Use a date, ISO date and time, today, yesterday, or a period such as 7d.")


def meeting_span(row):
    saved = datetime.fromisoformat(row["created"])
    duration = timedelta(seconds=row["duration"] or row.get("processed") or 0)
    start = datetime.fromisoformat(row["meetingStartedAt"]) if row.get("meetingStartedAt") else saved - duration
    end = datetime.fromisoformat(row["meetingEndedAt"]) if row.get("meetingEndedAt") else start + duration
    return start, end


def interrupted_meetings(library):
    for draft in list_drafts(library.root / "staging"):
        if not draft["meeting"] or library.exists(draft["id"]):
            continue
        duration = (draft["bytes"] - 44) // 2 / RATE
        end = datetime.fromtimestamp(draft["modified"], timezone.utc)
        start = end - timedelta(seconds=duration)
        if isinstance(draft.get("started"), str):
            try:
                stored = datetime.fromisoformat(draft["started"])
                if stored.utcoffset() == timedelta(0) and 2000 <= stored.year < 9999:
                    start, end = stored, stored + timedelta(seconds=duration)
            except (ValueError, OverflowError):
                pass
        provider = draft["provider"] or "Meeting"
        calendar = draft["calendar"] if isinstance(draft["calendar"], dict) else None
        title = draft.get("provisionalTitle")
        name = draft.get("meetingTitle") or (calendar or {}).get("title") or title or f"{provider} meeting"
        yield {"id": draft["id"], "name": name,
               "meetingProvider": provider, "meeting": True, "meetingCalendar": calendar, "created": start.isoformat(),
               **({"meetingMicrophoneWarning": "digitalSilence"} if draft.get("meetingMicrophoneWarning") == "digitalSilence" else {}),
               **({"provisionalTitle": title} if title else {}),
               "meetingStartedAt": start.isoformat(), "meetingEndedAt": end.isoformat(), "duration": duration,
               "processed": 0, "status": "interrupted"}


def meetings(library, query="", limit=20, *, since=None, until=None, live=None, include_in_progress=True):
    now = datetime.now(timezone.utc)
    first, last = date_bound(since, now), date_bound(until, now, through=True)
    if first is not None and last is not None and first >= last:
        raise ValueError("The until time must be after the since time.")
    rows = library.list("", meetings_only=True)
    saved = {row["id"] for row in rows}
    rows.extend(draft for draft in interrupted_meetings(library) if draft["id"] not in saved)
    if live and not any(row["id"] == live["id"] and row["status"] != "processing" for row in rows):
        rows = [{key: value for key, value in live.items() if key != "meetingTurns"},
                *(row for row in rows if row["id"] != live["id"])]
    terms = set(words(query))
    matches = []
    for row in rows:
        if not include_in_progress and row["status"] in ("recording", "processing"):
            continue
        start, end = meeting_span(row)
        if (first is not None and end < first) or (last is not None and start >= last):
            continue
        searchable = " ".join(filter(None, (row["name"], row.get("summaryTitle"), row.get("provisionalTitle"), row.get("meetingProvider"))))
        if terms <= set(words(searchable)):
            matches.append((start, row["id"], row))
    matches.sort(reverse=True)
    return [row for _, _, row in matches[:limit]]


def get_meeting(library, identifier, *, turns=True, live=None, staged=True):
    if identifier == "latest":
        newest = meetings(library, limit=1, live=live)
        if not newest:
            raise MissingSession(identifier)
        identifier = newest[0]["id"]
    try:
        key = str(UUID(identifier))
    except ValueError:
        if not re.fullmatch(r"[0-9a-fA-F-]{6,36}", identifier):
            raise MissingSession(identifier) from None
        matches = [row["id"] for row in meetings(library, limit=None, live=live) if row["id"].startswith(identifier.lower())]
        if len(matches) > 1:
            raise ValueError("That meeting ID prefix matches more than one meeting.") from None
        if not matches:
            raise MissingSession(identifier) from None
        key = matches[0]
    try:
        session = library.get(key, include_turns=turns)
    except MissingSession:
        session = None
    if live and key == live["id"] and session is None:
        segments = live.get("meetingTurns", [])
        labels = {} if live.get("meetingSpeakerMode") == MIXED_SPEAKERS else SOURCE_LABELS
        return {**live, "transcript": {"durationSeconds": live["duration"], "segments": segments,
                                      "speakerNames": labels, "text": "", "transcriptSource": "rolling"}}
    if session is None:
        if not staged:
            raise MissingSession(key)
        draft = next((draft for draft in interrupted_meetings(library) if draft["id"] == key), None)
        if draft is None:
            # An importer may have committed and cleared staging while the read was in progress.
            session = library.get(key, include_turns=turns)
        else:
            source = library.root / "staging" / key
            mixed = False
            segments = []
            if turns:
                for _, _, result in read_rolling(source, round(draft["duration"] * RATE)):
                    mixed |= result.get("meetingSpeakerMode") == MIXED_SPEAKERS
                    segments.extend(result["segments"])
            try:
                session = library.get(key, include_turns=turns)
            except MissingSession:
                return {**draft, "staged": True,
                        "transcript": {"durationSeconds": draft["duration"], "segments": segments,
                                       "speakerNames": {} if mixed else SOURCE_LABELS, "text": "",
                                       "transcriptSource": "rolling" if segments else "draft"}}
    if not session["meeting"]:
        raise MissingSession(key)
    return session


def notes(session):
    overview = (session.get("overview") or {}).get("version") or {}
    brief = session.get("meetingBrief") or {}
    return {
        **({"summary": brief["summary"]} if brief.get("summary") else {}),
        "overview": "\n\n".join(value for value in (overview.get("summary"), overview.get("markdown")) if value),
        "personalNotes": document_text(session.get("personalNotes", {}).get("document") or {"type": "doc", "content": []}),
    }


def meeting_detail(session, start=None, end=None, *, speaker=None):
    start, end = parse_offset(start), parse_offset(end)
    if end is not None and end <= (start or 0):
        raise ValueError("The end time must be after the start time.")
    transcript = session["transcript"]
    duration = transcript["durationSeconds"]
    labels = transcript.get("speakerNames", {})
    turns = []
    available = session.get("meetingTurns") or transcript["segments"]
    if start:
        extent = max(duration or 0, max((turn["end"] or turn["start"] or 0 for turn in available), default=0))
        recording = session["status"] == "recording"
        if recording and session.get("meetingStartedAt"):
            began = datetime.fromisoformat(session["meetingStartedAt"]).astimezone(timezone.utc)
            extent = max(extent, (datetime.now(timezone.utc) - began).total_seconds())
        elapsed = round(extent * 1000)
        if start >= elapsed:
            progress = " so far" if recording else ""
            raise ValueError(f"The start time {range_clock(start)} is at or beyond the meeting's end at {range_clock(elapsed)}{progress}.")
    if not available and transcript["text"] and start is None and end is None and speaker is None:
        available = [{"speaker": None, "start": None, "end": None, "text": transcript["text"]}]
    for turn in sorted(available, key=lambda turn: turn["start"] or 0):
        first, last = turn["start"], turn["end"]
        if start is not None and round((last if last is not None else first or 0) * 1000) <= start:
            continue
        if end is not None and (first is None or round(first * 1000) >= end):
            continue
        name = labels.get(str(turn["speaker"])) or (f"Speaker {turn['speaker']}" if turn["speaker"] else None)
        if speaker and (not name or name.casefold() != speaker.casefold()):
            continue
        turns.append({"start": first, "end": last, "speaker": name, "text": turn["text"]})
    return {
        "id": session["id"], "title": session["name"], "created": session["created"],
        "provider": session.get("meetingProvider"), "status": session["status"],
        "duration": duration, "transcriptSource": transcript.get("transcriptSource") or ("final" if session["status"] == "complete"
                                                  else "rolling" if session["status"] in ("recording", "processing") else "draft"),
        "startedAt": session.get("meetingStartedAt"), "endedAt": session.get("meetingEndedAt"),
        "audioRemovedAt": session.get("audioRemovedAt"), "calendar": session.get("meetingCalendar"),
        "finalizationPending": session.get("finalizationPending", False),
        **({"range": {"from_ms": start or 0, "to_ms": end}} if start is not None or end is not None else {}),
        **({"microphoneWarning": "digitalSilence"} if session.get("meetingMicrophoneWarning") == "digitalSilence" else {}),
        **({"staged": True} if session.get("staged") else {}),
        **({"summaryTitle": session["meetingBrief"]["title"]} if (session.get("meetingBrief") or {}).get("title") else {}),
        **({"provisionalTitle": session["provisionalTitle"]} if session.get("provisionalTitle") else {}),
        "notes": notes(session), "turns": turns,
    }


def transcript_passages(turns, *, gap=2.5, word_limit=120):
    previous, count = None, 0
    for turn in turns:
        length = len(turn["text"].split())
        if (previous and previous["end"] is not None and turn["start"] is not None and turn["end"] is not None
                and previous["speaker"] == turn["speaker"] and turn["start"] - previous["end"] <= gap
                and (word_limit is None or count + length <= word_limit)):
            previous["end"] = max(previous["end"], turn["end"])
            previous["text"] += " " + turn["text"]
            count += length
            continue
        if previous:
            yield previous
        previous, count = dict(turn), length
    if previous:
        yield previous


def speech_excerpt(turn, *, before):
    text = " ".join(turn["text"].split())
    if len(text) > 120:
        if before:
            tail = text[-119:]
            text = "…" + (tail.partition(" ")[2] or tail)
        else:
            head = text[:119]
            text = (head.rpartition(" ")[0] or head) + "…"
    return {**turn, "text": text}


def meeting_search_report(library, query, limit=10, *, identifier=None, since=None, until=None, live=None,
                          include_in_progress=True, fuzzy=True):
    from saved_dictations import search_vocabulary

    vocabulary = search_vocabulary() if fuzzy else ()
    key = get_meeting(library, identifier, turns=False, live=live)["id"] if identifier else None
    rows = meetings(library, limit=None, since=since, until=until, live=live, include_in_progress=include_in_progress)
    passages, details, speech = [], {}, {}
    for row in rows:
        if key and row["id"] != key:
            continue
        try:
            detail = meeting_detail(get_meeting(library, row["id"], live=live))
        except MissingSession:
            continue
        route = None if detail.get("staged") else f"/?session={detail['id']}&tab=transcript"
        fields = {"id": detail["id"], "title": detail["title"], "status": detail["status"]}
        source = detail["transcriptSource"]
        details[detail["id"]] = {**fields, "transcriptSource": source}
        turns = list(transcript_passages(detail["turns"]))
        start = datetime.fromisoformat(detail["startedAt"] or detail["created"])
        speech[detail["id"]] = (start.astimezone(timezone.utc), turns)
        for index, turn in enumerate(turns):
            stamp = turn["start"]
            passages.append({**fields, **turn, "source": "transcript",
                             "transcriptSource": source, "_turn": index,
                             "route": route + f"&at={stamp}" if route and stamp is not None else route})
        for source, note in detail["notes"].items():
            tab = "notes" if source == "personalNotes" else "overview"
            for paragraph in re.split(r"\n\s*\n", note):
                if paragraph.strip():
                    passages.append({**fields, "source": source, "text": paragraph, "start": None, "end": None,
                                     "speaker": None, "route": f"/?session={detail['id']}&tab={tab}"})
    hits, counts = rank_passages(passages, query, limit, fuzzy, vocabulary=vocabulary)
    for hit in hits:
        index = hit.pop("_turn", None)
        if index is None:
            continue
        start, turns = speech[hit["id"]]
        if hit["start"] is not None:
            hit["absoluteTime"] = (start + timedelta(seconds=hit["start"])).astimezone().isoformat(timespec="milliseconds")
        if index:
            hit["before"] = speech_excerpt(turns[index - 1], before=True)
        if index + 1 < len(turns):
            hit["after"] = speech_excerpt(turns[index + 1], before=False)
    return {
        "passages": hits, "totalMatches": sum(counts.values()), "searchedMeetings": len(details),
        "matchingMeetings": [{**details[key], "matchCount": count} for key, count in counts.items()],
    }


def search_meetings(library, query, limit=10, **scope):
    return meeting_search_report(library, query, limit, **scope)["passages"]


def timestamp(seconds, *, subtitle=False, srt=False):
    hours, rest = divmod(round(seconds * 1000), 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, ms = divmod(rest, 1000)
    if subtitle:
        return f"{hours:02}:{minutes:02}:{seconds:02}{',' if srt else '.'}{ms:03}"
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def markdown_turns(turns):
    for turn in turns:
        lead = " · ".join(filter(None, (timestamp(turn["start"]) if turn["start"] is not None else None, turn["speaker"])))
        yield (f"**{lead}**  \n" if lead else "") + turn["text"]


def saved_meeting_clipboard(library, key):
    session = get_meeting(library, key, staged=False)
    if session["status"] not in ("complete", "interrupted") or session["finalizationPending"]:
        raise ValueError("Wait for this meeting to finish transcribing before copying.")
    turns = ({**turn, "text": turn["text"].strip(), "speaker": turn["speaker"] or "Speaker"}
             for turn in meeting_detail(session)["turns"] if turn["text"].strip())
    text = "\n\n".join(markdown_turns(transcript_passages(turns, gap=3, word_limit=None)))
    if not text:
        raise ValueError("This meeting has no transcript to copy.")
    return text


def latest_transcript(library, live=None):
    rows = meetings(library, limit=None, live=live)
    if live:
        rows.sort(key=lambda row: row["id"] != live["id"])
    for row in rows:
        try:
            detail = meeting_detail(get_meeting(library, row["id"], live=live, staged=False))
        except MissingSession:
            continue
        turns = [turn for turn in detail["turns"] if turn["text"].strip()]
        if turns:
            return {"session": detail["id"], "text": "\n\n".join(markdown_turns(turns))}
    raise ValueError("No meeting transcript is available to copy.")


def meeting_date(detail, use_24_hour):
    started = datetime.fromisoformat(detail.get("startedAt") or detail["created"]).astimezone()
    if use_24_hour:
        clock = f"{started.hour:02}:{started.minute:02}"
    else:
        period = "AM" if started.hour < 12 else "PM"
        clock = f"{started.hour % 12 or 12}:{started.minute:02} {period}"
    return f"- **Date:** {started.date().isoformat()}, {clock}"


def range_clock(milliseconds):
    seconds, fraction = divmod(milliseconds, 1000)
    return timestamp(seconds) + (f".{fraction:03}" if fraction else "")


def render_export(detail, kind, *, use_24_hour=False, include_date=True, companion=False):
    title = " ".join(detail["title"].split())
    attendees = (detail.get("calendar") or {}).get("attendees", [])
    attendance = ["**Attendees** · " + ", ".join(attendees)] if attendees else []
    summary = detail.get("notes", {}).get("summary")
    summary_title = detail.get("summaryTitle")
    summary_parts = ["## Summary", *([f"**{summary_title}**"] if summary_title and summary_title != title else []),
                     summary, "_Written on this Mac by Jard’s local Qwen model._"]
    summary_section = ["\n\n".join(summary_parts)] if summary else []
    date_line = [meeting_date(detail, use_24_hour)] if include_date and kind in ("md", "notes") else []
    notes_sections = []
    if kind == "notes" or companion:
        for key, label in (("overview", "Overview"), ("personalNotes", "My Notes")):
            if body := detail["notes"][key]:
                notes_sections.append(f"## {label}\n\n{body}")
    if kind == "notes":
        return "\n\n".join([f"# {title}", *date_line, *attendance, *summary_section, *notes_sections]) + "\n"
    if kind == "md":
        rows = [f"# {title}", *date_line, *attendance]
        if companion:
            rows.append(f"- **Status:** {detail['status'].capitalize()} · {detail['transcriptSource']} transcript")
        if bounds := detail.get("range"):
            to = "end" if bounds["to_ms"] is None else range_clock(bounds["to_ms"])
            rows.append(f"- **Range:** {range_clock(bounds['from_ms'])}–{to}")
        rows.extend([*summary_section, *notes_sections])
        if summary_section or companion:
            rows.append("## Transcript")
        rows.extend(markdown_turns(detail["turns"]))
        return "\n\n".join(rows) + "\n"
    cues = []
    for turn in detail["turns"]:
        if turn["start"] is None or turn["end"] is None or turn["end"] <= turn["start"]:
            continue
        first = timestamp(turn["start"], subtitle=True, srt=kind == "srt")
        last = timestamp(turn["end"], subtitle=True, srt=kind == "srt")
        if first == last:
            continue
        body = " ".join(turn["text"].replace("-->", "->").split())
        if not body:
            continue
        if turn["speaker"]:
            speaker = " ".join(turn["speaker"].replace("-->", "->").split())
            if speaker:
                body = f"{speaker}: {body}"
        if kind == "vtt":
            body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        prefix = f"{len(cues) + 1}\n" if kind == "srt" else ""
        cues.append(f"{prefix}{first} --> {last}\n{body}")
    return ("WEBVTT\n\n" if kind == "vtt" else "") + ("\n\n".join(cues) + "\n" if cues else "")


def export_name(session, kind):
    stem = Path(session["audioName"]).stem
    return f"{stem} - Notes.md" if kind == "notes" else f"{stem}.{kind}"
