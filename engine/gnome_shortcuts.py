"""Read GNOME portal assignments and manage this library's command shortcuts."""
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
ANNOTATE_BINDING = ("annotate", "Jaro · Annotate", "annotate", "<Control><Alt><Shift>s")
logger = logging.getLogger(__name__)

# Read GNOME's typed setting for this app. The portal remains the only code
# that binds the shortcuts or asks for approval.
PORTAL_SHORTCUTS = """
const {Gio} = imports.gi;
const schema = Gio.SettingsSchemaSource.get_default()?.lookup(
    "org.gnome.settings-daemon.global-shortcuts.application", true);
if (!schema?.has_key("shortcuts")) {
    print("null");
} else {
    const path = "/org/gnome/settings-daemon/global-shortcuts/" + ARGV[0] + "/";
    const settings = new Gio.Settings({settings_schema: schema, path});
    const entries = settings.get_value("shortcuts").deepUnpack();
    print(JSON.stringify(entries.filter(([, properties]) => {
        const accelerators = properties.shortcuts?.deepUnpack();
        return Array.isArray(accelerators) && accelerators.some(value => typeof value === "string" && value.trim());
    }).map(([id]) => id)));
}
"""


def portal_shortcut_ids(application):
    """Return enabled IDs, or None when GNOME's setting cannot be read."""
    gjs = shutil.which("gjs")
    if gjs is None:
        return None
    try:
        result = subprocess.run([gjs, "-c", PORTAL_SHORTCUTS, application], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", timeout=2, check=True)
        ids = json.loads(result.stdout)
        if isinstance(ids, list) and all(isinstance(key, str) for key in ids):
            return set(ids)
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.debug("Could not read saved GNOME portal assignments.", exc_info=True)
    return None


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
    def __init__(self, root, *, annotation=False):
        self.prefix = command_prefix(root, gnome=True)
        prefix = f"{BASE}jaro-{library_id(root)}-"
        self.annotation_entry = prefix + "annotate/"
        bindings = BINDINGS + (ANNOTATE_BINDING,) if annotation else BINDINGS
        self.entries = {
            f"{prefix}{identifier}/": (label, action, default)
            for identifier, label, action, default in bindings
        }
        self.owned = self.entries.keys() | {self.annotation_entry}

    def installed(self):
        return bool(self.installed_entries())

    def installed_entries(self):
        return self.owned.intersection(paths())

    def install(self):
        listed = paths()
        taken = set()
        for path in listed:
            if path in self.owned and path not in self.entries:
                continue
            if re.fullmatch(r"/(?:[a-zA-Z0-9_-]+/)+", path):
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
        obsolete = self.owned.difference(self.entries).intersection(latest)
        kept = [path for path in latest if path not in obsolete]
        kept.extend(path for path in self.entries if path not in latest)
        if kept != latest:
            gsettings("set", SCHEMA, KEY, json.dumps(kept, ensure_ascii=False))
        if self.installed_entries() != self.entries.keys():
            raise CaptureError("GNOME did not save the custom shortcuts.")
        for path in obsolete:
            gsettings("reset-recursively", f"{ENTRY}:{path}")
        return missing_key

    def remove(self):
        listed = paths()
        kept = [path for path in listed if path not in self.owned]
        if listed != kept:
            gsettings("set", SCHEMA, KEY, json.dumps(kept, ensure_ascii=False))
            if self.installed():
                raise CaptureError("GNOME did not remove the custom shortcuts.")
        for path in self.owned:
            gsettings("reset-recursively", f"{ENTRY}:{path}")
