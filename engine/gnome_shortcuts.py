"""Install this library's command shortcuts in the current GNOME session."""
from ast import literal_eval
import json
import logging
import os
import re
import shutil
import subprocess

from engine_lifecycle import library_id
from portable_capture import CaptureError
from wayland_dictation_commands import command_prefix


SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
KEY = "custom-keybindings"
ENTRY = SCHEMA + ".custom-keybinding"
KEYBOARD_SCHEMA = "org.gnome.desktop.peripherals.keyboard"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
BINDINGS = (
    ("dictation", "Jaro · Start or finish dictation", "toggle", "<Control><Alt><Shift>d"),
    ("paste-last", "Jaro · Paste last dictation", "paste-last", "<Control><Alt>v"),
    ("cancel", "Jaro · Cancel dictation", "cancel", "<Control><Alt><Shift>x"),
)
logger = logging.getLogger(__name__)


def supported():
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    return "gnome" in desktops and shutil.which("gsettings") is not None


def gsettings(*args):
    try:
        result = subprocess.run(["gsettings", *args], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", timeout=2, check=True)
        if result.stderr.strip():
            raise ValueError(result.stderr.strip())
        return result.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError) as failure:
        logger.warning("Could not access GNOME custom shortcuts.", exc_info=True)
        raise CaptureError("Could not change GNOME shortcuts. Open GNOME Settings → Keyboard → Custom Shortcuts.") from failure


def variant(text):
    try:
        return literal_eval(text)
    except (SyntaxError, ValueError) as failure:
        raise CaptureError("Could not read GNOME custom shortcuts.") from failure


def paths():
    value = variant(gsettings("get", SCHEMA, KEY).removeprefix("@as ").strip())
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise CaptureError("Could not read GNOME custom shortcuts.")
    return value


def accelerator(text):
    modifiers = re.findall(r"<([^<>]+)>", text.lower())
    aliases = {"primary": "control", "ctrl": "control", "mod1": "alt"}
    return frozenset(aliases.get(key, key) for key in modifiers), re.sub(r"<[^<>]+>", "", text.lower())


def repeat_window():
    try:
        delay, interval = (int(gsettings("get", KEYBOARD_SCHEMA, key).rsplit(maxsplit=1)[-1])
                           for key in ("delay", "repeat-interval"))
        return max(1.0, max(delay, interval) / 1000 + .35)
    except (CaptureError, ValueError, IndexError):
        return 1.0


class GnomeShortcuts:
    def __init__(self, root):
        self.prefix = command_prefix(root, gnome=True)
        self.entries = {
            f"{BASE}jaro-{library_id(root)}-{identifier}/": (label, action, default)
            for identifier, label, action, default in BINDINGS
        }

    def installed(self):
        return bool(self.installed_entries())

    def installed_entries(self):
        return self.entries.keys() & paths()

    def install(self):
        listed = paths()
        taken = set()
        for path in listed:
            if path not in self.entries and re.fullmatch(r"/(?:[a-zA-Z0-9_-]+/)+", path):
                value = variant(gsettings("get", f"{ENTRY}:{path}", "binding"))
                if isinstance(value, str):
                    taken.add(accelerator(value))
        missing_key = False
        for path, (label, action, default) in self.entries.items():
            location = f"{ENTRY}:{path}"
            gsettings("set", location, "name", json.dumps(label, ensure_ascii=False))
            command = f"{self.prefix} {action}"
            gsettings("set", location, "command", json.dumps(command, ensure_ascii=False))
            if path not in listed:
                binding = "" if accelerator(default) in taken else default
                gsettings("set", location, "binding", json.dumps(binding))
            else:
                binding = variant(gsettings("get", location, "binding"))
            missing_key |= not binding
        # Read again to retain bindings added elsewhere while the new entries were staged.
        latest = paths()
        missing = [path for path in self.entries if path not in latest]
        if missing:
            gsettings("set", SCHEMA, KEY, json.dumps(latest + missing, ensure_ascii=False))
        if not self.entries.keys() <= set(paths()):
            raise CaptureError("GNOME did not save the custom shortcuts.")
        return missing_key

    def remove(self):
        listed = paths()
        kept = [path for path in listed if path not in self.entries]
        if listed != kept:
            gsettings("set", SCHEMA, KEY, json.dumps(kept, ensure_ascii=False))
            if self.installed():
                raise CaptureError("GNOME did not remove the custom shortcuts.")
        for path in self.entries:
            gsettings("reset-recursively", f"{ENTRY}:{path}")
