"""Private draft WAV files and recording markers."""
import json
import os
import stat
import struct
import tempfile
import wave
from uuid import UUID
from contextlib import suppress


PORTABLE_RECORDING_RATE = 16_000


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


def open_portable_recording_marker(marker, *, create=False):
    if marker.is_symlink():
        raise OSError("Invalid recording marker.")
    writable = create or os.name == "nt"
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    claim = os.fdopen(os.open(marker, flags, 0o600), "r+b" if writable else "rb")
    try:
        if not stat.S_ISREG(os.fstat(claim.fileno()).st_mode):
            raise OSError("Invalid recording marker.")
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(claim.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(claim, (fcntl.LOCK_EX if create else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BaseException:
        claim.close()
        if create:
            with suppress(OSError):
                marker.unlink()
        raise
    return claim


def portable_draft_frames(path):
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (PORTABLE_RECORDING_RATE, 1, 2):
                return 0
            return min(audio.getnframes(), max(0, path.stat().st_size - 44) // 2)
    except (EOFError, OSError, wave.Error):
        return 0


def portable_meeting_drafts(folder):
    for marker in folder.glob("*.recording"):
        try:
            key = str(UUID(marker.stem))
            if key != marker.stem:
                continue
            path = folder / f"{key}.wav"
            # The recorder creates audio only after it owns the marker.
            if not path.is_file():
                continue
            with open_portable_recording_marker(marker) as claim:
                metadata = recording_metadata(claim)
                provider = metadata.get("meetingProvider")
                if not isinstance(provider, str) or not provider or not (frames := portable_draft_frames(path)):
                    continue
                modified = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        yield dict(id=key, path=str(path), duration=frames / PORTABLE_RECORDING_RATE, modified=modified,
                   meeting=True, provider=provider, started=metadata.get("meetingStartedAt"), calendar=None,
                   meetingTitle=metadata.get("meetingTitle"))


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
    # Windows portable takes live in microphone-drafts and do not use the Mac helper's locks.
    if os.name == "nt":
        return []
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
