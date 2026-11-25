"""Local application choices for portable dictation profiles."""
import json
import os
from pathlib import Path, PureWindowsPath
import struct
import subprocess
import sys


def linux_entries():
    # Use the distribution's GIO installation, as the screen recorder does.
    result = subprocess.run(["/usr/bin/python3", "-IB", __file__], capture_output=True, timeout=4, check=True)
    return json.loads(result.stdout)


def desktop_entries():
    from gi.repository import Gio

    entries = []
    for app in Gio.AppInfo.get_all():
        if not isinstance(app, Gio.DesktopAppInfo) or not app.get_id() or not app.should_show():
            continue
        ident = app.get_id().removesuffix(".desktop")
        if ident == "local.jardaudio.desktop" or not ident:
            continue
        aliases = [app.get_startup_wm_class() or ""]
        executable = Path(app.get_executable() or "").name
        if executable not in {"env", "flatpak", "snap", "sh", "bash", "wine", "java", "electron"} and not executable.startswith("python"):
            aliases.append(executable)
        entries.append((ident, app.get_display_name() or ident, aliases))
    return entries


def launcher_metadata(path, platform):
    from gi.repository import Gio

    app = Gio.DesktopAppInfo.new_from_filename(str(path))
    if not app or app.get_string("Type") != "Application" or app.get_is_hidden():
        raise ValueError("Choose an application launcher.")
    desktop_id = app.get_id().removesuffix(".desktop")
    resolved = path.resolve()
    installed = next((item for item in Gio.AppInfo.get_all()
                      if isinstance(item, Gio.DesktopAppInfo) and item.get_filename()
                      and Path(item.get_filename()).resolve() == resolved), None)
    if installed:
        ident = installed.get_id().removesuffix(".desktop")
    elif platform == "wayland":
        ident = desktop_id
    else:
        ident = app.get_startup_wm_class() or Path(app.get_executable() or "").name
        if not ident or ident in {"env", "sh", "bash", "flatpak", "snap"}:
            ident = desktop_id
    return {"id": ident, "name": app.get_display_name() or ident}


def selected_application(platform, path):
    if not path.is_file():
        raise ValueError("Choose an application file.")
    if platform == "windows":
        if path.suffix.lower() != ".exe":
            raise ValueError("Choose a Windows .exe application.")
        ident = path.stem
    elif path.suffix == ".desktop":
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("This application launcher is too large.")
        result = subprocess.run(["/usr/bin/python3", "-IB", __file__, platform, str(path)],
                                capture_output=True, timeout=4)
        if result.returncode:
            raise ValueError("Choose a valid desktop application launcher.")
        return json.loads(result.stdout)
    elif os.access(path, os.X_OK):
        ident = path.resolve().name
    else:
        raise ValueError("Choose a .desktop launcher or an executable application.")
    return {"id": ident, "name": ident}


def shortcut_target(data):
    """Read the local path from a Shell Link's LinkInfo. Never resolve or run it."""
    if len(data) < 76 or data[:4] != b"L\0\0\0":
        return ""
    flags, = struct.unpack_from("<I", data, 20)
    if not flags & 2:
        return ""
    try:
        start = 76
        if flags & 1:
            start += 2 + struct.unpack_from("<H", data, start)[0]
        size, header, kind = struct.unpack_from("<III", data, start)
        if not 28 <= header <= size <= len(data) - start or not kind & 1:
            return ""
        info = data[start:start + size]
        unicode_at = struct.unpack_from("<I", info, 28)[0] if header >= 36 else 0
        if unicode_at:
            if not header <= unicode_at < size:
                return ""
            end = next((i for i in range(unicode_at, size - 1, 2) if info[i:i + 2] == b"\0\0"), None)
            return info[unicode_at:end].decode("utf-16-le") if end is not None else ""
        at, = struct.unpack_from("<I", info, 16)
        end = info.find(b"\0", at)
        return info[at:end].decode("mbcs" if os.name == "nt" else "cp1252") if header <= at < end else ""
    except (struct.error, UnicodeError):
        return ""


def windows_entries():
    import winreg
    from windows_dictation import windowed_applications

    apps = {}

    def add(path, label=""):
        executable = PureWindowsPath(path)
        stem = executable.stem
        if (executable.suffix.lower() == ".exe" and stem and not stem.lower().startswith("unins")
                and stem.casefold() not in {"jaro", "jaro-capture"}):
            apps.setdefault(stem.casefold(), (stem, label or stem, []))

    for variable in ("APPDATA", "ProgramData"):
        if not (directory := os.environ.get(variable)):
            continue
        programs = Path(directory) / "Microsoft/Windows/Start Menu/Programs"
        for root, folders, files in os.walk(programs):
            if len(Path(root).relative_to(programs).parts) >= 4:
                folders.clear()
            for filename in files:
                path = Path(root) / filename
                if path.suffix.lower() != ".lnk":
                    continue
                try:
                    with path.open("rb") as handle:
                        target = shortcut_target(handle.read(256 * 1024))
                    if target and Path(target).is_file():
                        add(target, path.stem)
                except OSError:
                    continue
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths", 0, winreg.KEY_READ | view) as keys:
                    for i in range(winreg.QueryInfoKey(keys)[0]):
                        name = winreg.EnumKey(keys, i)
                        try:
                            path = winreg.QueryValue(keys, name)
                        except OSError:
                            path = ""
                        add(os.path.expandvars(path.strip('"')) or name)
            except OSError:
                continue
    for path in windowed_applications():
        add(path)
    return list(apps.values())


def application_entries(platform):
    if platform == "windows":
        return windows_entries()
    return linux_entries()


def profile_aliases(platform, profiles):
    """Build the class-to-desktop-ID map at settings load/save, outside recording."""
    targets = {p.target[4:].casefold() for p in profiles if p.target.lower().startswith("app:")}
    if platform not in ("wayland", "x11") or not targets:
        return {}
    try:
        entries = linux_entries()
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    aliases = {}
    ids = {ident.casefold() for ident, _, _ in entries}
    for ident, _, names in entries:
        for name in names:
            key = name.casefold()
            if key and key not in ids:
                previous = aliases.setdefault(key, ident)
                if previous != ident:
                    aliases[key] = ""
    return {key: ident for key, ident in aliases.items() if ident.casefold() in targets}


if __name__ == "__main__":
    entries = launcher_metadata(Path(sys.argv[2]), sys.argv[1]) if len(sys.argv) == 3 else desktop_entries()
    print(json.dumps(entries))
