from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, skipIf
from unittest.mock import patch
import os
import struct
import subprocess
import sys

from profile_applications import profile_aliases, selected_application, shortcut_target


class ProfileApplicationTests(TestCase):
    @skipIf(os.name == "nt", "uses POSIX executable permissions and symlinks")
    def test_unlisted_applications_use_the_executable_seen_by_dictation(self):
        with TemporaryDirectory() as folder:
            app = Path(folder) / "Custom Editor.EXE"
            app.touch(mode=0o700)
            self.assertEqual(selected_application("windows", app)["id"], "Custom Editor")
            linked = app.with_name("Launcher")
            linked.symlink_to(app)
            self.assertEqual(selected_application("x11", linked)["id"], app.name)
            with self.assertRaises(ValueError):
                selected_application("windows", linked)
            app.chmod(0o600)
            with self.assertRaises(ValueError):
                selected_application("wayland", app)

    @skipIf(sys.platform != "linux", "uses the Linux desktop application registry")
    def test_launcher_outside_the_registry_is_selectable(self):
        python = Path("/usr/bin/python3")
        if not python.is_file() or subprocess.run(
                [python, "-IBc", "from gi.repository import Gio"], capture_output=True).returncode:
            self.skipTest("requires the distribution's GIO bindings")
        with TemporaryDirectory() as folder:
            launcher = Path(folder) / "org.jaro.Fixture.desktop"
            launcher.write_text("[Desktop Entry]\nType=Application\nName=External Editor\n"
                                "Exec=/usr/bin/true\nStartupWMClass=ExternalEditor\n")
            self.assertEqual(selected_application("x11", launcher), {"id": "ExternalEditor", "name": "External Editor"})
            self.assertEqual(selected_application("wayland", launcher)["id"], "org.jaro.Fixture")
            launcher.write_text("[Desktop Entry]\nType=Link\nURL=https://example.org\n")
            with self.assertRaises(ValueError):
                selected_application("x11", launcher)

    def test_aliases_respect_desktop_identity_and_ambiguous_window_classes(self):
        entries = [
            ("org.first.Editor", "Editor", ["TextEditor", "shared", "second"]),
            ("second", "Other Editor", ["shared", "another"]),
        ]
        profiles = [SimpleNamespace(target="app:org.first.Editor")]
        with patch("profile_applications.linux_entries", return_value=entries):
            self.assertEqual(profile_aliases("x11", profiles), {"texteditor": "org.first.Editor"})

    def test_no_app_rules_means_no_desktop_scan(self):
        with patch("profile_applications.linux_entries") as scan:
            for platform in ("x11", "wayland", "windows"):
                self.assertEqual(profile_aliases(platform, [SimpleNamespace(target="site:example.test")]), {})
            scan.assert_not_called()

    def test_shell_link_unicode_target_is_bounded_by_linkinfo(self):
        target = r"C:\Apps\Éditeur.exe"
        header = bytearray(76)
        struct.pack_into("<I", header, 0, 76)
        struct.pack_into("<I", header, 20, 3)
        info = bytearray(36) + target.encode("utf-16-le") + b"\0\0"
        struct.pack_into("<III", info, 0, len(info), 36, 1)
        struct.pack_into("<I", info, 28, 36)
        prefix = header + b"\x02\x00\x00\x00"
        self.assertEqual(shortcut_target(prefix + info), target)
        self.assertEqual(shortcut_target(prefix + info[:-1]), "")
        struct.pack_into("<I", info, 0, 36)
        self.assertEqual(shortcut_target(prefix + info), "")
