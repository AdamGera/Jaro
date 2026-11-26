import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from annotation_agent_skill import SKILL, guide, manage
from engine_paths import ENGINE_ROOT


class AnnotationAgentSkillTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.claude = self.root / "Custom Claude"
        self.claude.mkdir()
        self.enterContext(patch("annotation_agent_skill.Path.home", return_value=self.root))
        self.enterContext(patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "XDG_CONFIG_HOME": "", "PI_CODING_AGENT_DIR": ""}))

    def row(self, agent):
        return next(row for row in manage("status")["agents"] if row["agent"] == agent)

    def test_cli_print_install_and_explicit_update_work_offline(self):
        library = self.root / "unused-library"
        env = {**os.environ, "RUMBLE_DATA_DIR": str(library), "RUMBLE_PORT": "1", "PYTHONUTF8": "1"}

        def cli(*options, check=True):
            return subprocess.run([sys.executable, str(ENGINE_ROOT / "jardctl.py"), "annotate", "skill", *options],
                                  env=env, capture_output=True, timeout=15, check=check)

        self.assertEqual(cli().stdout.replace(b"\r\n", b"\n"), guide())
        self.assertEqual(self.row("claude")["state"], "missing")
        cli("--install", "claude")
        path = self.claude / SKILL
        self.assertEqual(path.read_bytes(), guide())
        self.assertEqual(self.row("claude")["state"], "installed")
        original = path.stat().st_mtime_ns
        cli("--install", "claude")
        self.assertEqual(path.stat().st_mtime_ns, original)
        path.write_bytes(b"User edits")
        refused = cli("--install", "claude", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(b"A different guide already exists", refused.stderr)
        self.assertEqual(path.read_bytes(), b"User edits")
        cli("--update", "claude")
        self.assertEqual(path.read_bytes(), guide())
        self.assertFalse(library.exists())

    def test_codex_and_pi_reuse_the_shared_guide_and_skip_an_unconfigured_agent(self):
        (self.root / ".pi").mkdir()
        codex_root = self.root / ".codex"
        with patch("annotation_agent_skill.config_home", side_effect=lambda _name, default: default):
            with self.assertRaisesRegex(ValueError, "Set up this agent"):
                manage("install", "codex")
            self.assertFalse((self.root / ".agents").exists())
            codex_root.mkdir()
            manage("install", "codex")
            self.assertEqual(self.row("codex")["path"], str(self.root / ".agents" / SKILL))
            self.assertEqual(self.row("pi")["state"], "installed")
            manage("install", "pi")
            self.assertFalse((self.root / ".pi" / "agent").exists())

    @unittest.skipUnless(sys.platform != "win32", "Creating symlinks on Windows can require privileges.")
    def test_never_writes_a_linked_guide_or_guide_directory(self):
        external = self.root / "External"
        external.mkdir()
        path = self.claude / SKILL
        path.parent.parent.mkdir()
        (external / "SKILL.md").write_bytes(b"Different guide")
        path.parent.symlink_to(external, target_is_directory=True)
        self.assertEqual(self.row("claude")["state"], "managed")
        with self.assertRaisesRegex(ValueError, "managed elsewhere"):
            manage("update", "claude")
        path.parent.unlink()
        path.parent.mkdir()
        path.symlink_to(external / "SKILL.md")
        with self.assertRaisesRegex(ValueError, "managed elsewhere"):
            manage("update", "claude")
        self.assertTrue(path.is_symlink())
        self.assertEqual((external / "SKILL.md").read_bytes(), b"Different guide")
