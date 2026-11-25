"""Manage the installed desktop's per-user launch at login registration."""

from configparser import ConfigParser, Error
from contextlib import suppress
import os
from pathlib import Path
import stat
import sys
import tempfile
from threading import Lock


LINUX_LAUNCHER = Path("/usr/bin/jaro")
LINUX_ROOT = Path("/usr/lib/jaro")
ENTRY_NAME = "local.jardaudio.desktop.desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVAL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Jaro"


def autostart_entry():
    config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config.is_absolute():
        config = Path.home() / ".config"
    return config / "autostart" / ENTRY_NAME


class LinuxLogin:
    def __init__(self, entry, launcher=LINUX_LAUNCHER):
        self.entry = entry
        if launcher == LINUX_LAUNCHER:
            self.command = f"{launcher} --background"
            self.icon = "local.jardaudio.desktop"
            self.try_exec = str(launcher)
        else:
            from linux_direct import desktop_argument, desktop_value

            self.command = f"{desktop_argument(launcher)} --background"
            self.icon = desktop_value(launcher.parent / "current/share/jaro.png")
            self.try_exec = desktop_value(launcher)

    def set_state(self, enabled):
        current = self.read()
        if current is None:
            return {"state": "unknown", "message": "A different Jaro startup entry is managed by your desktop."}
        registered = bool(current)
        active = registered and current.get("Hidden", "false").lower() != "true" and (
            current.get("X-GNOME-Autostart-enabled", "true").lower() != "false"
        )
        if enabled is False and registered:
            self.entry.unlink(missing_ok=True)
            active = False
        elif enabled is True and not active:
            self.write()
            active = True
        return {"state": "enabled" if active else "disabled"}

    def read(self):
        parser = ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            fd = os.open(self.entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        except OSError:
            return None
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            content = file.read(8193)
        if len(content) > 8192:
            return None
        try:
            parser.read_string(content.decode("utf-8"))
            item = parser["Desktop Entry"]
        except (Error, KeyError, UnicodeError):
            return None
        if item.get("X-Jaro-Managed") == "true" and item.get("Exec") == self.command:
            return item
        return None

    def write(self):
        self.entry.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".jaro-", dir=self.entry.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write("[Desktop Entry]\nType=Application\nName=Jaro\n"
                           f"Icon={self.icon}\nExec={self.command}\nTryExec={self.try_exec}\n"
                           "Terminal=false\nX-Jaro-Managed=true\n")
            os.replace(temporary, self.entry)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


class WindowsLogin:
    def __init__(self, launcher):
        self.launcher = launcher
        self.command = f'"{launcher}" --background'

    def owns(self, command):
        if command.casefold() == self.command.casefold():
            return True
        path, separator, arguments = command[1:].partition('"')
        if not command.startswith('"') or not separator or arguments.casefold() != " --background":
            return False
        executable = Path(path)
        if executable.is_absolute():
            with suppress(OSError, ValueError):
                return executable.resolve(strict=True) == self.launcher.resolve(strict=True)
        return False

    def set_state(self, enabled):
        import winreg

        access = winreg.KEY_QUERY_VALUE | (winreg.KEY_SET_VALUE if enabled is not None else 0)
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)
        except FileNotFoundError:
            if enabled is not True:
                return {"state": "disabled"}
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)
        with key as run:
            try:
                current = winreg.QueryValueEx(run, VALUE_NAME)
            except FileNotFoundError:
                current = None
            if current is not None and (current[1] != winreg.REG_SZ or not self.owns(current[0])):
                return {"state": "unknown", "message": "A different Jaro startup entry is managed by Windows."}
            if enabled is True and current is None:
                if len(self.command.encode("utf-16-le")) // 2 > 260:
                    raise OSError("The Jaro launcher path is too long for Windows startup.")
                winreg.SetValueEx(run, VALUE_NAME, 0, winreg.REG_SZ, self.command)
            elif enabled is False and current is not None:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(run, VALUE_NAME)
        if enabled is False or (enabled is None and current is None):
            return {"state": "disabled"}

        # Windows can disable an application while leaving its Run value intact.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVAL_KEY, 0, winreg.KEY_QUERY_VALUE) as approvals:
                approval, kind = winreg.QueryValueEx(approvals, VALUE_NAME)
        except FileNotFoundError:
            return {"state": "enabled"}
        if kind == winreg.REG_BINARY and len(approval) == 12:
            flag = int.from_bytes(approval[:4], "little")
            if flag in (2, 6):
                return {"state": "enabled"}
            if flag in (3, 7):
                return {"state": "approval"}
        return {"state": "unknown", "message": "Check Jaro in Windows Settings under Apps → Startup."}


class PortableLogin:
    def __init__(self, registration):
        self.registration = registration
        self.lock = Lock()

    def set_state(self, enabled=None):
        with self.lock:
            return self.registration.set_state(enabled)

    def open_settings(self):
        with self.lock:
            status = self.registration.set_state(None)
            if status["state"] == "approval":
                os.startfile("ms-settings:startupapps")
            return status


def installed_launcher(root):
    if os.name == "nt":
        from portable_update import windows_launcher

        with suppress(OSError):
            launcher = windows_launcher()
            versions = launcher.parent.parent / "versions"
            if launcher.is_file() and root.parent == versions.resolve(strict=True):
                return launcher
    elif sys.platform == "linux":
        if root == LINUX_ROOT and os.access(LINUX_LAUNCHER, os.X_OK):
            return LINUX_LAUNCHER
        from linux_direct import managed_root

        if direct := managed_root(root):
            return direct / "jaro"
    return None


def load_login(root):
    launcher = installed_launcher(root)
    if not launcher or not (root / "portable-release.json").is_file():
        return None
    registration = WindowsLogin(launcher) if os.name == "nt" else LinuxLogin(autostart_entry(), launcher)
    return PortableLogin(registration)
