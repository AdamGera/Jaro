import asyncio
import errno
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import annotation_retention
import annotation_storage
import dictation_settings
import meeting_retention
import output_transform_operations
import screen_draft
from session_store import SessionStore


class StorageAndTransformsFixesTests(unittest.TestCase):
    def test_session_store_get_uses_shared_read_without_write_lock(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = SessionStore(root)

            source = root / "sample.wav"
            source.write_bytes(b"RIFFdata")
            key, _ = store.create(
                source, "sample.wav", "audio/wav",
                {"recording": True, "languageHints": ["en"], "diarization": False},
                1.0,
            )
            store.append(key, {"text": "hello", "language": "English", "segments": [], "words": []}, 1.0)
            store.finish(key)
            # First get initializes default notes cleanly
            initial = store.get(key)
            self.assertEqual(initial["transcript"]["text"], "hello")

            # Hold a RESERVED write lock on another connection (same as BEGIN IMMEDIATE);
            # previously SessionStore.get() also issued BEGIN IMMEDIATE and deadlocked/blocked here.
            # With deferred BEGIN on reads, an initialized session reads cleanly without waiting!
            blocker = sqlite3.connect(store.path, timeout=0.05)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                loaded = store.get(key)
                self.assertEqual(loaded["transcript"]["text"], "hello")
                readonly = SessionStore(root, read_only=True)
                self.assertEqual(readonly.get(key)["transcript"]["text"], "hello")
            finally:
                blocker.rollback()
                blocker.close()

    def test_duplicate_and_write_archive_fall_back_on_exfat_link_and_fchmod_errors(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            src = root / "clip.mov"
            src.write_bytes(b"video-bytes-12345")

            for code in (errno.EPERM, errno.ENOTSUP, errno.ENOSYS, errno.EINVAL):
                dst = root / f"copy-{code}.mov"
                with (
                    patch("annotation_storage.os.link", side_effect=OSError(code, "no hardlinks")),
                    patch("annotation_storage.os.fchmod", side_effect=OSError(errno.EPERM, "no chmod")),
                ):
                    annotation_storage.duplicate(src, dst)
                self.assertEqual(dst.read_bytes(), b"video-bytes-12345")

            media_dir = root / "media"
            media_dir.mkdir()
            (media_dir / "source.wav").write_bytes(b"audio-bytes-67890")
            archive = root / "archive"
            with (
                patch("annotation_storage.os.link", side_effect=OSError(errno.EPERM, "exfat")),
                patch("annotation_storage.os.fchmod", side_effect=OSError(errno.EPERM, "exfat")),
            ):
                meeting_retention.write_archive(
                    archive, {"Summary.md": "hello\n"}, media_dir, media_folder="Audio",
                )
            self.assertEqual((archive / "Audio" / "source.wav").read_bytes(), b"audio-bytes-67890")

    def test_terminate_handles_missing_stdin_and_pipe_transports(self):
        async def run():
            pipe2 = MagicMock()
            transport = MagicMock()
            transport.get_pipe_transport.side_effect = lambda fd: None if fd == 1 else pipe2

            async def wait():
                return 0

            proc1 = SimpleNamespace(pid=999999, returncode=0, stdin=None, _transport=transport, wait=wait)
            with patch("output_transform_operations._signal_group"):
                await output_transform_operations._terminate(proc1)
            pipe2.close.assert_called_once()

            proc2 = SimpleNamespace(
                pid=999999, returncode=0,
                stdin=SimpleNamespace(is_closing=lambda: False, transport=None),
                _transport=None, wait=wait,
            )
            with patch("output_transform_operations._signal_group"):
                await output_transform_operations._terminate(proc2)

        asyncio.run(run())

    def test_screen_draft_caches_ffmpeg_encoder_probe_and_recovers_after_error(self):
        with patch("screen_draft.subprocess.check_output", return_value=" V..... libx264 \n") as probe:
            self.assertEqual(screen_draft.windows_h264_encoder(False), "libx264")
            self.assertEqual(screen_draft.windows_h264_encoder(True), "libx264")
            self.assertEqual(probe.call_count, 1)

        with patch("screen_draft.subprocess.check_output", side_effect=OSError("missing")) as failing:
            self.assertEqual(screen_draft.windows_h264_encoder(False), "h264_mf")
            self.assertIsNone(screen_draft.windows_h264_encoder(True))
            self.assertEqual(failing.call_count, 2)

    def test_dictation_settings_accepts_full_settings_documents(self):
        valid = {f"key.{i}": i for i in range(35)}
        self.assertTrue(dictation_settings.validate_changes(valid))
        too_many = {f"key.{i}": i for i in range(65)}
        with self.assertRaises(ValueError):
            dictation_settings.validate_changes(too_many)


if __name__ == "__main__":
    unittest.main()
