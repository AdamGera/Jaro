import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from portable_dictation_history import SCHEMA, PortableDictationHistory


@unittest.skipUnless(os.name == "posix", "requires portable dictation history on a POSIX filesystem")
class PortableDictationHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.history = PortableDictationHistory(self.root)

    def record(self, text, *, app="GTK Editor", age=0):
        key = str(uuid4())
        stored, epoch = self.history.record({
            "id": key, "time": time() - age, "text": text, "app": app,
            "seconds": 2.5, "inferenceMs": 160, "totalMs": 210,
        })
        return key, stored, epoch

    def test_private_history_and_paste_preference_survive_reopening(self):
        self.assertFalse(self.history.path.exists())
        self.assertEqual(self.history.page(legacy=True)["entries"], [])
        self.assertFalse(self.history.path.exists())
        self.history.initialize()
        key, stored, _ = self.record("The café is closed.")
        self.assertTrue(stored)
        self.history.configure(terminal=True)
        reopened = PortableDictationHistory(self.root)
        row = reopened.page(query="CAFÉ", include_timing=True, legacy=True)["entries"][0]
        self.assertEqual((row["id"], row["seconds"], row["inferenceMs"], row["app"]), (key, 2.5, 160, "GTK Editor"))
        self.assertTrue(reopened.status()[0]["terminal"])
        ranked = reopened.page(search="café", legacy=False)["entries"]
        self.assertEqual([entry["id"] for entry in ranked], [key])
        self.assertEqual(reopened.path.stat().st_mode & 0o777, 0o600)
        reopened.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private file"):
            reopened.page(legacy=True)

    def test_off_keeps_previous_retention_and_clear_invalidates_recent_results(self):
        self.history.initialize()
        oldest, _, _ = self.record("expired", age=86_400 * 2)
        recent, _, epoch = self.record("still retained")
        self.history.configure(retention=1)
        self.assertFalse(self.history.status(oldest)[1])
        self.history.configure(retention=0)
        _, stored, _ = self.record("never retained")
        self.assertFalse(stored)
        self.assertEqual([entry["id"] for entry in self.history.page(legacy=True)["entries"]], [recent])
        self.assertEqual(self.history.remove([recent]), 1)
        self.assertFalse(self.history.status(recent)[1])
        self.assertEqual(self.history.remove(), 0)
        settings, _ = self.history.status()
        self.assertEqual(settings["previousRetention"], 1)
        self.assertGreater(settings["clearEpoch"], epoch)

    def test_command_preference_migrates_an_existing_history(self):
        old_schema = SCHEMA.replace(",\n    desktopCommands INTEGER NOT NULL DEFAULT 0 CHECK (desktopCommands IN (0,1))", "")
        with self.history.connection(write=True, create=True) as existing:
            existing.executescript(old_schema)
        key, _, _ = self.record("Retain this transcription.")
        self.assertEqual(self.history.page(legacy=True)["entries"][0]["id"], key)
        self.history.initialize()
        self.assertFalse(self.history.commands_enabled())
        self.history.configure(commands=True)
        self.history.initialize()
        self.assertTrue(self.history.commands_enabled())
        self.assertEqual(self.history.page(legacy=True)["entries"][0]["id"], key)

    def test_budget_trims_oldest_by_utf8_bytes(self):
        self.history.initialize()
        with patch("portable_dictation_history.HISTORY_TEXT_BUDGET", 7):
            self.record("first")
            recent, _, _ = self.record("ééé")
        self.assertEqual([entry["id"] for entry in self.history.page(legacy=True)["entries"]], [recent])
