"""Claim one portable window and its engine for a local library."""

from contextlib import contextmanager, suppress
import errno
import os
import stat
import sys
import tempfile
import time


MAX_STARTUP_ID = 512
STARTUP_VARIABLES = ("XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID")


def replace_private(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(value)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def read_private(path, size):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Invalid desktop activation record.")
        return file.read(size + 1)


def startup_id():
    if sys.platform != "linux":
        return b""
    for kind, name in enumerate(STARTUP_VARIABLES):
        if value := os.environ.get(name):
            encoded = value.encode("utf-8", errors="ignore")
            if len(encoded) <= MAX_STARTUP_ID and not any(byte < 32 for byte in encoded):
                return bytes((48 + kind,)) + encoded
    return b""


def startup_environment(activation):
    if activation[:1] == b"0":
        return {STARTUP_VARIABLES[0]: activation[1:].decode("utf-8", errors="replace")}
    if activation[:1] == b"1":
        return {STARTUP_VARIABLES[1]: activation[1:].decode("utf-8", errors="replace")}
    return {}


def allow_foreground(pid_file):
    import ctypes
    from ctypes import wintypes

    try:
        value = read_private(pid_file, 10)
        if not value.isdigit() or not 0 < (pid := int(value)) < 0xFFFFFFFF:
            return False
        allow = ctypes.WinDLL("user32").AllowSetForegroundWindow
        allow.argtypes = [wintypes.DWORD]
        allow.restype = wintypes.BOOL
        return bool(allow(pid))
    except OSError:
        return False


class DesktopInstance:
    def __init__(self, folder, primary):
        self.primary = primary
        self.pending = folder / "activation"
        self.claimed = folder / "activation.received"
        self.pid = folder / "window-pid"

    def activate(self):
        windows = os.name == "nt"
        awaiting_window = windows and not allow_foreground(self.pid)
        value = startup_id()
        replace_private(self.pending, value)
        if awaiting_window:
            # A login-started instance creates its native window only after this request.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if allow_foreground(self.pid):
                    replace_private(self.pending, value)
                    break
                time.sleep(0.04)

    def set_window(self, pid):
        if os.name == "nt":
            replace_private(self.pid, str(pid).encode("ascii"))

    def take_activation(self):
        try:
            os.replace(self.pending, self.claimed)
        except OSError:
            return None
        try:
            value = read_private(self.claimed, MAX_STARTUP_ID + 1)
            if len(value) <= MAX_STARTUP_ID + 1 and not any(byte < 32 for byte in value):
                return value
            return b""
        except OSError:
            return b""
        finally:
            with suppress(OSError):
                self.claimed.unlink(missing_ok=True)


def try_lock(fd):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        from msvcrt import get_osfhandle

        lock = ctypes.WinDLL("kernel32", use_last_error=True).LockFile
        lock.argtypes = [wintypes.HANDLE, *([wintypes.DWORD] * 4)]
        lock.restype = wintypes.BOOL
        if lock(get_osfhandle(fd), 0, 0, 1, 0):
            return True
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            return False
        raise ctypes.WinError(error)

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return False
    return True


@contextmanager
def claim_desktop(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = root / ".desktop-instance"
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if (not stat.S_ISDIR(info.st_mode) or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077))):
        raise OSError("Jaro could not use its private desktop instance directory.")
    lock = os.open(folder / "lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise OSError("Invalid desktop instance lock.")
        instance = DesktopInstance(folder, try_lock(lock))
        if instance.primary:
            with suppress(OSError):
                instance.pid.unlink(missing_ok=True)
        try:
            yield instance
        finally:
            if instance.primary:
                with suppress(OSError):
                    instance.pid.unlink(missing_ok=True)
    finally:
        os.close(lock)
