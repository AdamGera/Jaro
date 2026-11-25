import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import wave

from portable_audio_control import AudioPreferences
from portable_dictation_history import SCHEMA, SECONDS_PER_DAY, PortableDictationHistory
from portable_dictation_sounds import SoundPreferences


@unittest.skipUnless(os.name == "posix", "requires portable dictation history on a POSIX filesystem")
class PortableDictationHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.history = PortableDictationHistory(self.root)

    def record(self, text, *, app="GTK Editor", age=0, audio=None, kind="dictation"):
        key = str(uuid4())
        stored, epoch = self.history.record({
            "id": key, "time": time() - age, "text": text, "app": app,
            "seconds": 2.5, "inferenceMs": 160, "totalMs": 210, "kind": kind,
        }, audio)
        return key, stored, epoch

    def wav(self):
        path = self.root / "original.wav"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
            with wave.open(file, "wb") as audio:
                audio.setparams((1, 2, 16_000, 0, "NONE", ""))
                audio.writeframes(b"\x02\x01" * 1600)
        return path

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
        old_schema = old_schema.replace(",\n    language TEXT NOT NULL DEFAULT ''", "")
        old_schema = old_schema.replace(",\n    windowsShortcutMode TEXT NOT NULL DEFAULT 'hold'", "")
        old_schema = old_schema.replace(",\n    linuxShortcutMode TEXT NOT NULL DEFAULT 'tap'", "")
        for column in ("x11AnnotationShortcut", "windowsAnnotationShortcut", "x11RawShortcut", "windowsRawShortcut"):
            old_schema = old_schema.replace(f",\n    {column} TEXT NOT NULL DEFAULT ''", "")
        old_schema = old_schema.replace(",\n    pasteRestoreToken TEXT NOT NULL DEFAULT ''", "")
        old_schema = old_schema.replace(",\n    vocabularyEntries TEXT NOT NULL DEFAULT '[]'", "")
        old_schema = old_schema.replace(",\n    replacements TEXT NOT NULL DEFAULT ''", "")
        old_schema = old_schema.replace(",\n    outputProfiles TEXT NOT NULL DEFAULT ''", "")
        old_schema = old_schema.replace(",\n    indicatorPosition TEXT NOT NULL DEFAULT 'top' CHECK (indicatorPosition IN ('top','bottom','off'))", "")
        old_schema = old_schema.replace(",\n    soundEnabled INTEGER NOT NULL DEFAULT 1 CHECK (soundEnabled IN (0,1))", "")
        old_schema = old_schema.replace(",\n    soundStyle TEXT NOT NULL DEFAULT 'classic'", "")
        old_schema = old_schema.replace(",\n    soundVolume INTEGER NOT NULL DEFAULT 70 CHECK (soundVolume BETWEEN 0 AND 100)", "")
        old_schema = old_schema.replace(",\n    otherAudio TEXT NOT NULL DEFAULT 'none'", "")
        old_schema = old_schema.replace(",\n    otherAudioPercent INTEGER NOT NULL DEFAULT 20 CHECK (otherAudioPercent BETWEEN 0 AND 100)", "")
        for column in ("shortcutEnabled", "portalShortcutEnabled", "noiseCleanup", "boostMicrophone", "virtualPasteEnabled",
                       "x11AnnotationEnabled", "windowsAnnotationEnabled", "portalRawShortcut"):
            old_schema = old_schema.replace(f",\n    {column} INTEGER NOT NULL DEFAULT 0 CHECK ({column} IN (0,1))", "")
        old_schema = old_schema.replace(",\n    kind TEXT NOT NULL DEFAULT 'dictation' CHECK (kind IN ('dictation','raw','printed'))", "")
        key = str(uuid4())
        with self.history.connection(write=True, create=True) as existing, existing:
            existing.executescript(old_schema)
            existing.execute("INSERT INTO dictations VALUES (?,?,?,?,?,?,?)",
                             (key, time(), "Retain this transcription.", "GTK Editor", 2.5, 160, 210))
        self.assertEqual(self.history.page(legacy=True)["entries"][0]["id"], key)
        self.history.initialize()
        self.assertFalse(self.history.commands_enabled())
        self.assertEqual(self.history.language(), "")
        self.assertEqual(self.history.windows_shortcut_mode(), "hold")
        self.assertEqual(self.history.linux_shortcut_mode(), "tap")
        self.assertFalse(self.history.shortcut_enabled())
        self.assertFalse(self.history.shortcut_enabled(portal=True))
        self.assertFalse(self.history.portal_raw_shortcut())
        self.assertEqual(self.history.annotation_shortcut(), ("", False))
        self.assertEqual(self.history.desktop_shortcut(raw=True), "")
        self.assertEqual(self.history.desktop_shortcut(windows=True, raw=True), "")
        self.assertEqual(self.history.consume_paste_grant(), ("", False))
        self.assertEqual(self.history.output_preferences(), ([], "", ""))
        self.assertEqual(self.history.indicator_position(), "top")
        self.assertEqual(self.history.sound_preferences(), SoundPreferences())
        self.assertEqual(self.history.audio_preferences(), AudioPreferences())
        self.assertEqual(self.history.voice_input(), {"noiseCleanup": False, "boostMicrophone": False})
        self.history.configure(commands=True, language="fr", windows_shortcut_mode="holdDouble", linux_shortcut_mode="doubleTap",
                               indicator_position="bottom")
        self.history.save_shortcut_enabled(True)
        self.history.save_annotation_shortcut("Alt+Shift+F9", True)
        self.history.save_annotation_shortcut("Ctrl+Shift+F9", True, windows=True)
        self.history.save_desktop_shortcut("F9", raw=True)
        self.history.save_desktop_shortcut("Ctrl+F8", windows=True, raw=True)
        self.history.save_portal_raw_shortcut(True)
        self.history.save_sound_preferences(SoundPreferences(False, "signature", 34))
        self.history.save_audio_preferences(AudioPreferences("lower", 35))
        self.history.configure(noise_cleanup=True)
        self.history.initialize()
        self.assertTrue(self.history.commands_enabled())
        self.assertEqual(self.history.language(), "fr")
        self.assertEqual(self.history.indicator_position(), "bottom")
        self.assertEqual(self.history.sound_preferences(), SoundPreferences(False, "signature", 34))
        self.assertEqual(self.history.audio_preferences(), AudioPreferences("lower", 35))
        self.assertEqual(self.history.voice_input(), {"noiseCleanup": True, "boostMicrophone": False})
        self.assertEqual(self.history.windows_shortcut_mode(), "holdDouble")
        self.assertEqual(self.history.linux_shortcut_mode(), "doubleTap")
        self.assertTrue(self.history.shortcut_enabled())
        self.assertEqual(self.history.annotation_shortcut(), ("Alt+Shift+F9", True))
        self.assertEqual(self.history.annotation_shortcut(windows=True), ("Ctrl+Shift+F9", True))
        self.assertEqual(self.history.desktop_shortcut(raw=True), "F9")
        self.assertEqual(self.history.desktop_shortcut(windows=True, raw=True), "Ctrl+F8")
        self.assertTrue(self.history.portal_raw_shortcut())
        self.assertFalse(self.history.shortcut_enabled(portal=True))
        entry = self.history.page(legacy=True)["entries"][0]
        self.assertEqual((entry["id"], entry["kind"]), (key, "dictation"))

    def test_budget_trims_oldest_by_utf8_bytes(self):
        self.history.initialize()
        original = self.wav()
        with patch("portable_dictation_history.HISTORY_TEXT_BUDGET", 7):
            old, _, _ = self.record("first", audio=original)
            self.assertTrue((self.history.audio_dir / f"{old}.wav").exists())
            recent, _, _ = self.record("ééé")
        self.assertEqual([entry["id"] for entry in self.history.page(legacy=True)["entries"]], [recent])
        self.assertFalse((self.history.audio_dir / f"{old}.wav").exists())

    def test_printed_history_migration_preserves_raw_entries_and_audio(self):
        with self.history.connection(write=True, create=True) as db, db:
            db.executescript(SCHEMA.replace("'dictation','raw','printed'", "'dictation','raw'"))
        normal, _, _ = self.record("Regular text.")
        raw, _, _ = self.record("The words as heard.", audio=self.wav(), kind="raw")
        self.history.initialize()
        self.history.initialize()
        printed, stored, _ = self.record("Only sent to stdout.", kind="printed")
        self.assertTrue(stored)
        entries = {entry["id"]: entry for entry in self.history.page(legacy=True, include_audio=True)["entries"]}
        self.assertEqual({key: entry["kind"] for key, entry in entries.items()},
                         {normal: "dictation", raw: "raw", printed: "printed"})
        self.assertIn("audioExpiresAt", entries[raw])

    def test_original_audio_expires_at_24_hours_with_transcript_retained(self):
        initial = time()
        self.history.initialize()
        original = self.wav()
        before = original.read_bytes()
        with patch("portable_dictation_history.time", return_value=initial):
            legacy_id, _, _ = self.record("Legacy transcript")
            key, _, _ = self.record("Transcript with audio", audio=original)
            self.assertFalse(original.exists())
            page = self.history.page(legacy=True, include_audio=True)["entries"]
            rows = {item["id"]: item for item in page}
            self.assertNotIn("audioExpiresAt", rows[legacy_id])
            deadline = rows[key]["time"] + SECONDS_PER_DAY
            self.assertEqual(rows[key]["audioExpiresAt"], deadline)
            self.assertEqual(self.history.expire(), deadline)
            clip = self.history.audio_dir / f"{key}.wav"
            self.assertEqual(clip.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.history.audio_dir.stat().st_mode & 0o777, 0o700)
            with self.history.open_audio(key) as (stream, size):
                self.assertEqual((stream.read(), size), (before, len(before)))
            revision = self.history.status()[0]["revision"]
        with patch("portable_dictation_history.time", return_value=deadline):
            with self.assertRaises(FileNotFoundError), self.history.open_audio(key):
                pass
            self.assertTrue(clip.exists())
            self.assertIsNone(self.history.expire())
            self.assertFalse(clip.exists())
            self.assertGreater(self.history.status()[0]["revision"], revision)
            row, = self.history.page(query="with audio", legacy=True, include_audio=True)["entries"]
            self.assertNotIn("audioExpiresAt", row)
            self.assertEqual(row["text"], "Transcript with audio")

    def test_delete_clear_and_launch_remove_associated_and_orphaned_wavs(self):
        self.history.initialize()
        key, _, _ = self.record("Delete me", audio=self.wav())
        self.record("Keep me", audio=self.wav())
        self.assertEqual(len(list(self.history.audio_dir.iterdir())), 2)
        self.history.remove([key])
        self.assertFalse((self.history.audio_dir / f"{key}.wav").exists())
        orphan = self.history.audio_dir / f"{uuid4()}.wav"
        orphan.write_bytes(b"orphan")
        reopen = PortableDictationHistory(self.root)
        reopen.initialize()
        self.assertFalse(orphan.exists())
        self.assertEqual(reopen.remove(), 1)
        self.assertFalse(list(reopen.audio_dir.iterdir()))

    def test_history_off_and_audio_write_failure_leave_transcript_flow_available(self):
        self.history.initialize()
        original = self.wav()
        self.history.configure(retention=0)
        self.assertFalse(self.record("Unsaved", audio=original)[1])
        self.assertTrue(original.exists())
        self.assertFalse(self.history.audio_dir.exists())
        self.history.configure(retention=7)
        with patch("portable_dictation_history.os.replace", side_effect=PermissionError), self.assertLogs():
            key, saved, _ = self.record("Text still saved", audio=original)
        self.assertTrue(saved)
        self.assertTrue(original.exists())
        row, = self.history.page(legacy=True, include_audio=True)["entries"]
        self.assertEqual(row["id"], key)
        self.assertNotIn("audioExpiresAt", row)
