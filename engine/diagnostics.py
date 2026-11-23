"""Local diagnostic files and an offline tail reader."""
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import time

EVENT_LOG = "live.ndjson"
PROCESS_LOG = "server.log"
TAIL_BYTES = 4 * 1024 * 1024
LOG_LIMIT = 10 * 1024 * 1024
LOG_KEEP = LOG_LIMIT // 2
LOG_TRIM_INTERVAL = 3600


def log_directory():
    return Path(os.environ.get("RUMBLE_LOG_DIR") or Path.home() / "Library/Logs/Jaro")


def open_log(name, *, mode="rb", create=False):
    path = log_directory() / name
    if create:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if mode != "rb":
        directory = path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid():
            raise OSError("Jaro needs a log directory owned by the current user.")
    flags = {"rb": os.O_RDONLY, "r+b": os.O_RDWR, "a+b": os.O_RDWR | os.O_APPEND}[mode]
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o022:
            raise OSError("Jaro logs must be regular files owned by the current user.")
        if mode != "rb":
            if details.st_nlink != 1:
                raise OSError("Jaro logs must not be hard-linked.")
            if details.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise


def write_event(kind, phase, *, inference_ms=None):
    fields = {"inferenceMs": inference_ms} if type(inference_ms) is int and inference_ms >= 0 else {}
    _append_event(kind, phase, fields)


def write_dictation_timing(values):
    names = ("captureFinalizeMs", "submissionWaitMs", "speechRequestMs", "inferenceMs",
             "bridgeRoundTripMs", "outputWaitMs", "clipboardSnapshotMs", "deliveryMs", "totalMs")
    if not isinstance(values, dict):
        return
    fields = {}
    for name in names:
        value = values.get(name)
        if type(value) is int and 0 <= value <= 86_400_000:
            fields[name] = value
    if fields:
        _append_event("dictation", "timing", fields)


def write_transform_event(phase, **fields):
    _append_event("transformation", phase, fields)


def _append_event(kind, phase, fields):
    import fcntl

    event = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, "phase": phase, **fields}
    line = json.dumps(event, separators=(",", ":")).encode() + b"\n"
    with suppress(OSError), open_log(EVENT_LOG, mode="r+b", create=True) as log:
        fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
        compact_log(log)
        log.seek(0, os.SEEK_END)
        log.write(line)


def compact_log(log):
    """Keep recent whole lines without replacing the engine's open log file."""
    size = os.fstat(log.fileno()).st_size
    if size < LOG_LIMIT:
        return
    log.seek(size - LOG_KEEP - 1)
    tail = log.read(LOG_KEEP + 1)
    boundary = tail.find(b"\n")
    recent = tail[boundary + 1:] if boundary >= 0 else b""
    log.seek(0)
    log.write(recent)
    log.flush()
    log.truncate(len(recent))


def maintain_logs():
    import fcntl

    while True:
        for name in (EVENT_LOG, PROCESS_LOG):
            with suppress(OSError), open_log(name, mode="r+b") as log:
                fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
                compact_log(log)
        time.sleep(LOG_TRIM_INTERVAL)


def initial_tail(log, lines, follow):
    size = os.fstat(log.fileno()).st_size
    start = max(0, size - TAIL_BYTES)
    log.seek(max(0, start - 1))
    if start and log.read(1) != b"\n" and not log.readline(TAIL_BYTES).endswith(b"\n"):
        return b"", True
    data = log.read(max(0, size - log.tell()))
    if follow and data and not data.endswith(b"\n"):
        ending = data.rfind(b"\n") + 1
        log.seek(ending - len(data), os.SEEK_CUR)
        data = data[:ending]
    if not lines:
        return b"", False
    start_of_line = len(data) - int(data.endswith(b"\n"))
    for _ in range(lines):
        start_of_line = data.rfind(b"\n", 0, start_of_line)
        if start_of_line < 0:
            break
    return data[start_of_line + 1:], False


def tail_log(name, lines, follow, output):
    with open_log(name) as log:
        details = os.fstat(log.fileno())
        identity = (details.st_dev, details.st_ino)
        data, skipping = initial_tail(log, lines, follow)
        position = log.tell()
    output.write(data)
    output.flush()
    while follow:
        time.sleep(.25)
        try:
            log = open_log(name)
        except FileNotFoundError:
            continue
        with log:
            details = os.fstat(log.fileno())
            current = (details.st_dev, details.st_ino)
            if current != identity or details.st_size < position:
                identity, position, skipping = current, 0, False
            log.seek(position)
            while line := log.readline(TAIL_BYTES):
                complete = line.endswith(b"\n")
                if not complete and not skipping and len(line) < TAIL_BYTES:
                    break
                position = log.tell()
                if complete and not skipping:
                    output.write(line)
                skipping = not complete
        output.flush()
