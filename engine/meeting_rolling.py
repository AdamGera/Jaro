"""Provisional meeting transcript storage and reconciliation by capture time."""
import json
import math
import os

RATE = 48_000
MAX_LINE = 1024 * 1024
MIXED_SPEAKERS = "mixed"
SOURCE_LABELS = {"1": "You", "2": "Them"}


def rolling_path(source):
    return source.with_suffix(".rolling.ndjson")


def append_rolling(source, start, end, result):
    line = json.dumps([start, end, result], ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(line) > MAX_LINE:
        raise ValueError("The provisional transcript window is too large.")
    descriptor = os.open(rolling_path(source), os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def read_rolling(source, frames):
    """Read complete records only. A process can die during its final write."""
    last = 0
    try:
        stream = os.fdopen(os.open(rolling_path(source), os.O_RDONLY | os.O_NOFOLLOW), "rb")
    except OSError:
        return
    with stream:
        while line := stream.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                break
            try:
                start, end, result = json.loads(line)
                if type(start) is not int or type(end) is not int or not last <= start < end or start >= frames:
                    continue
                if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                        or not all(isinstance(result.get(key), list) for key in ("segments", "words", "activity"))):
                    continue
                if not all(valid_record(row, text=True) for row in (*result["segments"], *result["words"])):
                    continue
                if not all(valid_record(row, text=False) for row in result["activity"]):
                    continue
            except (ValueError, TypeError):
                continue
            last = end
            end = min(end, frames)
            if result.get("segments") and end < last:
                limit = end / RATE
                result = next(trim_chunks([result], lambda _, offset: offset is not None and offset < limit), None)
                if result is None:
                    continue
            yield start, end, result


def intervals(spans):
    combined = []
    for start, end in sorted(spans):
        if combined and start <= combined[-1][1]:
            combined[-1] = (combined[-1][0], max(end, combined[-1][1]))
        else:
            combined.append((start, end))
    return combined


def covers(spans, time):
    return any(start <= time < end for start, end in spans)


def midpoint(turn):
    start, end = turn.get("start"), turn.get("end")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and math.isfinite(start) and math.isfinite(end):
        return (start + end) / 2
    return None


def valid_record(row, *, text):
    if not isinstance(row, dict) or not {"start", "end", "speaker"} <= row.keys():
        return False
    speaker = row["speaker"]
    if speaker is not None and (type(speaker) is not int or not 1 <= speaker <= 8):
        return False
    if text:
        return isinstance(row.get("text"), str) and all(
            row[key] is None or type(row[key]) in (int, float) and math.isfinite(row[key]) for key in ("start", "end"))
    return speaker is not None and midpoint(row) is not None


def trim_chunks(chunks, keep):
    """Keep the original turn when possible and preserve separators when cutting its words."""
    for chunk in chunks:
        kept_turns, kept_words, activity = [], [], []
        for turn in chunk["segments"]:
            speaker, start, end = (turn[key] for key in ("speaker", "start", "end"))
            center = midpoint(turn)
            if center is None:
                if keep(speaker, None):
                    kept_turns.append(turn)
                continue
            # Aligned words belong to the turn of the same source that contains their midpoint.
            aligned = [(word, point) for word in chunk["words"] if word.get("speaker") == speaker
                       and (point := midpoint(word)) is not None and start <= point <= end]
            if not aligned:
                if keep(speaker, center):
                    kept_turns.append(turn)
                continue
            chosen = [keep(speaker, point) for _, point in aligned]
            if all(chosen):
                kept_turns.append(turn)
                kept_words.extend(word for word, _ in aligned)
            elif any(chosen):
                group = []
                for (word, _), use in zip(aligned, chosen):
                    if use:
                        group.append(word)
                    elif group:
                        kept_turns.append(word_turn(turn, group))
                        kept_words.extend(group)
                        group = []
                if group:
                    kept_turns.append(word_turn(turn, group))
                    kept_words.extend(group)
        for row in chunk.get("activity", []):
            for turn in kept_turns:
                if row["speaker"] == turn["speaker"] and turn["start"] is not None and turn["end"] is not None:
                    start, end = max(row["start"], turn["start"]), min(row["end"], turn["end"])
                    if start < end:
                        activity.append({**row, "start": start, "end": end})
        if kept_turns:
            if kept_turns == chunk["segments"]:
                yield chunk
                continue
            yield {**chunk, "text": " ".join(turn["text"] for turn in kept_turns),
                   "segments": kept_turns, "words": kept_words, "activity": activity}


def word_turn(turn, group):
    return {**turn, "start": group[0]["start"], "end": group[-1]["end"],
            "text": "".join(word["text"] + word.get("separator", " ") for word in group).strip()}


def mixed_speaker_timeline(final, rolling, processed):
    if not final:
        return rolling
    # The live and final diarization passes can assign different IDs to a voice.
    # Only final IDs are shown once the final pass has written a prefix.
    tail = trim_chunks(rolling, lambda _, point: point is not None and point >= processed)
    return [*final, *[{**chunk, "activity": [],
                      **{key: [{**row, "speaker": None} for row in chunk[key]] for key in ("segments", "words")}}
                     for chunk in tail]]


def timeline_chunks(final, rolling, *, mixed=False):
    if not rolling:
        return final
    covering = {speaker: intervals(span for chunk in final for span in chunk.get("meetingCoverage", {}).get(str(speaker), []))
                for speaker in (1, 2)}
    if mixed:
        # A damaged source track cannot be reconstructed from mixed speech.
        # Keep already attributed turns and use mixed recognition in their gaps.
        attributed = intervals((turn["start"], turn["end"]) for chunk in rolling for turn in chunk["segments"]
                               if midpoint(turn) is not None)
        # Speaker identities inferred from the mix are independent of the source labels.
        final = [{**chunk, **{key: [{**item, "speaker": None} for item in chunk.get(key, [])]
                             for key in ("segments", "words")}, "activity": []} for chunk in final]
        return [*rolling, *trim_chunks(final, lambda _, point: point is None or not covers(attributed, point))]
    return [*final, *trim_chunks(rolling, lambda speaker, point: point is None or not covers(covering.get(speaker, ()), point))]
