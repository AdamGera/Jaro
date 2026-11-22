"""Private draft WAV files produced by the Mac capture helper."""
import json
import os
import struct
import tempfile
from uuid import UUID
from contextlib import suppress


def recording_marker(path):
    return path.with_suffix(".recording")


def meeting_title_path(path):
    return path.with_suffix(".meeting-title")


def read_meeting_title(path):
    from meeting_brief import clean_title

    try:
        descriptor = os.open(meeting_title_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            value = stream.read(481)
        return clean_title(value.decode("utf8")) if len(value) <= 480 else None
    except (OSError, ValueError):
        return None


def write_meeting_title(path, title):
    from meeting_brief import clean_title

    title = clean_title(title)
    target = meeting_title_path(path)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".meeting-title-", delete=False) as stream:
        try:
            stream.write(title.encode("utf8"))
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(stream.name, target)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(stream.name)
    return title


def source_tracks(path):
    return {source: path.with_suffix(f".{source}.wav") for source in ("mic", "system")}


def normalize_meeting_title(title):
    name = " ".join(title.split()) if isinstance(title, str) else ""
    if not name or len(name) > 200 or any(not char.isprintable() and char not in "\u200c\u200d" for char in name):
        raise ValueError("Enter a meeting title of 1 to 200 printable characters.")
    return name


def write_recording_metadata(claim, metadata):
    claim.seek(0)
    claim.write(json.dumps(metadata, ensure_ascii=False).encode())
    claim.truncate()
    claim.flush()
    os.fsync(claim.fileno())


def recording_metadata(claim):
    data = claim.read(32768).decode("utf8", errors="replace")
    if data.startswith("{"):
        with suppress(ValueError):
            value = json.loads(data)
            if isinstance(value, dict):
                if "meetingTitle" in value:
                    try:
                        value["meetingTitle"] = normalize_meeting_title(value["meetingTitle"])
                    except ValueError:
                        value.pop("meetingTitle")
                return value
    else:
        with suppress(ValueError):
            return {"notesDraft": str(UUID(data[:36]))}
    return {}


def is_meeting_stage(path, metadata=None):
    if metadata is None:
        metadata = {}
        with suppress(FileNotFoundError):
            with recording_marker(path).open("rb") as claim:
                metadata = recording_metadata(claim)
    provider = metadata.get("meetingProvider")
    return (isinstance(provider, str) and bool(provider)
            or any(track.is_file() for track in source_tracks(path).values()))


def list_drafts(folder):
    import fcntl

    drafts = []
    for marker in folder.glob("*.recording"):
        try:
            key = str(UUID(marker.stem))
            path = folder / key
            if key != marker.stem or path.is_symlink():
                continue
            with marker.open("rb") as claim:
                fcntl.flock(claim, fcntl.LOCK_SH | fcntl.LOCK_NB)
                info = path.stat()
                meta = recording_metadata(claim)
        except (ValueError, FileNotFoundError, BlockingIOError):
            continue
        if info.st_size > 44:
            provider = meta.get("meetingProvider")
            meeting = is_meeting_stage(path, meta)
            drafts.append(dict(id=key, path=str(path), bytes=info.st_size, modified=info.st_mtime, meeting=meeting,
                               provider=provider if isinstance(provider, str) else None,
                               started=meta.get("meetingStartedAt"), calendar=meta.get("meetingCalendar"),
                               meetingTitle=meta.get("meetingTitle"),
                               meetingMicrophoneWarning=meta.get("meetingMicrophoneWarning"),
                               provisionalTitle=read_meeting_title(path) if meeting else None))
    return sorted(drafts, key=lambda row: row["modified"], reverse=True)


def capture_active(folder):
    import fcntl

    for pattern in ("*.recording", "*.dictation"):
        for marker in folder.glob(pattern):
            try:
                with marker.open("rb") as claim:
                    fcntl.flock(claim, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except FileNotFoundError:
                continue
    return False


def read_wav_header(stream):
    header = stream.read(44)
    if (len(header) != 44 or header[:4] != b"RIFF" or header[8:16] != b"WAVEfmt "
            or struct.unpack_from("<IHHIIHH", header, 16) != (16, 1, 1, 48_000, 96_000, 2, 16)
            or header[36:40] != b"data"):
        raise ValueError("This draft needs a 48 kHz mono PCM recording made by Jaro.")


def repair_wav(path):
    from audio_file import MAX_AUDIO_SECONDS

    with path.open("r+b") as stream:
        read_wav_header(stream)
        size = stream.seek(0, os.SEEK_END)
        # A short final write can leave half a sample. Preserve every whole sample.
        pcm_size = (size - 44) // 2 * 2
        if not 0 < pcm_size <= (MAX_AUDIO_SECONDS + 1) * 96_000:
            raise ValueError("The draft is empty or exceeds the recording limit.")
        stream.truncate(pcm_size + 44)
        stream.seek(4)
        stream.write(struct.pack("<I", pcm_size + 36))
        stream.seek(40)
        stream.write(struct.pack("<I", pcm_size))
        stream.flush()
        os.fsync(stream.fileno())
        return pcm_size // 2


def recover_wav(path):
    if not recording_marker(path).is_file():
        raise ValueError("Only a Jard microphone or Mac audio recording can be recovered.")
    return repair_wav(path)


def recover_tracks(source, expected_frames):
    tracks = source_tracks(source)
    try:
        for path in tracks.values():
            if path.is_symlink() or not path.is_file() or repair_wav(path) != expected_frames:
                return {}
    except (OSError, ValueError):
        # The mixed take remains recoverable if a storage failure truncated a source track.
        return {}
    return tracks
