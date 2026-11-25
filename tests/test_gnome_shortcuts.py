import json
from pathlib import Path
import shlex
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gnome_shortcuts import BASE, ENTRY, KEY, SCHEMA, GnomeShortcuts
from portable_capture import CaptureError


class GnomeShortcutsTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="Jaro library '")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        with patch("wayland_dictation_commands.ENGINE_ROOT", Path("/usr/lib/jaro/engine")):
            self.shortcuts = GnomeShortcuts(self.root)
        self.other = BASE + "custom0/"
        self.listed = [self.other]
        self.values = {self.other: {"binding": "<Shift><Alt><Primary>D", "command": "external-command", "name": "Other"}}
        context = patch("gnome_shortcuts.gsettings", side_effect=self.gsettings)
        self.calls = context.start()
        self.addCleanup(context.stop)

    def gsettings(self, action, location, key=None, value=None):
        if location == SCHEMA:
            self.assertEqual(key, KEY)
            if action == "get":
                return str(self.listed) if self.listed else "@as []"
            self.assertEqual(action, "set")
            self.listed = json.loads(value)
            return ""
        path = location.removeprefix(f"{ENTRY}:")
        if action == "reset-recursively":
            self.values.pop(path, None)
            return ""
        self.assertTrue(path.startswith(BASE))
        settings = self.values.setdefault(path, {})
        if action == "get":
            return repr(settings.get(key, ""))
        self.assertEqual(action, "set")
        settings[key] = json.loads(value)
        return ""

    def test_respects_existing_binding_and_preserves_an_edited_accelerator(self):
        self.assertFalse(self.shortcuts.installed())
        self.assertTrue(self.shortcuts.install())
        primary, recovery, cancel = self.shortcuts.entries
        self.assertEqual(self.listed, [self.other, primary, recovery, cancel])
        self.assertEqual(self.values[primary]["binding"], "")
        self.assertEqual(self.values[recovery]["binding"], "<Control><Alt>v")
        self.assertEqual(self.values[cancel]["binding"], "<Control><Alt><Shift>x")
        self.assertTrue(self.values[cancel]["command"].endswith("dictation cancel"))
        self.assertEqual(shlex.split(self.values[primary]["command"]), [
            "env", f"RUMBLE_DATA_DIR={self.root}", "JARO_GNOME_SHORTCUT=1", "/usr/bin/jardctl", "dictation", "toggle",
        ])
        self.values[primary]["binding"] = "F9"
        self.listed.remove(cancel)
        self.assertEqual(self.shortcuts.installed_entries(), {primary, recovery})
        self.assertFalse(self.shortcuts.install())
        self.assertEqual(self.shortcuts.installed_entries(), {primary, recovery, cancel})
        self.assertEqual(self.values[primary]["binding"], "F9")
        elsewhere = GnomeShortcuts(self.root / "second library")
        self.assertFalse(elsewhere.installed())
        elsewhere.remove()
        self.assertTrue(self.shortcuts.installed())
        self.shortcuts.remove()
        self.assertFalse(self.shortcuts.installed())
        self.assertEqual(self.listed, [self.other])
        self.assertEqual(self.values, {
            self.other: {"binding": "<Shift><Alt><Primary>D", "command": "external-command", "name": "Other"},
        })

    def test_refuses_to_rewrite_an_unreadable_list(self):
        self.calls.side_effect = None
        self.calls.return_value = "@as invalid"
        with self.assertRaises(CaptureError):
            self.shortcuts.install()
        self.calls.assert_called_once_with("get", SCHEMA, KEY)

    def test_adding_annotate_preserves_other_keys_and_removes_only_its_own_entry(self):
        self.shortcuts.install()
        old_keys = list(self.listed)
        self.values[self.other]["binding"] = "<Shift><Alt><Primary>S"
        self.values[old_keys[1]]["binding"] = "F9"
        with patch("wayland_dictation_commands.ENGINE_ROOT", Path("/usr/lib/jaro/engine")):
            updated = GnomeShortcuts(self.root, annotation=True)
        self.assertTrue(updated.install())  # The existing custom key owns the default.
        entry = updated.annotation_entry
        self.assertEqual(self.listed, [*old_keys, entry])
        self.assertEqual(self.values[entry], {
            "name": "Jaro · Annotate", "command": updated.prefix + " annotate", "binding": "",
        })
        self.values[entry]["binding"] = "F10"
        self.assertFalse(updated.install())
        self.assertEqual(self.values[entry]["binding"], "F10")
        self.assertEqual(self.values[old_keys[1]]["binding"], "F9")
        self.assertFalse(self.shortcuts.install())  # A desktop without screen capture can remove the obsolete entry.
        self.assertEqual(self.listed, old_keys)
        self.assertNotIn(entry, self.values)
        self.assertEqual(self.values[self.other]["command"], "external-command")
        self.values[self.other]["binding"] = "F11"
        self.values[old_keys[1]]["binding"] = "<Shift><Alt><Primary>S"
        self.assertTrue(updated.install())
        self.assertEqual(self.values[entry]["binding"], "")
        self.assertEqual(self.values[old_keys[1]]["binding"], "<Shift><Alt><Primary>S")
