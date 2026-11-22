import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
from engine_paths import ENGINE_ROOT


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.directory = self.root / "logs"
        self.environment = {"RUMBLE_LOG_DIR": str(self.directory), "RUMBLE_DATA_DIR": str(self.root / "absent")}
        self.enterContext(patch.dict(os.environ, self.environment))

    def test_writing_creates_private_files_and_compacts_complete_records(self):
        with patch.multiple(diagnostics, LOG_LIMIT=210, LOG_KEEP=140):
            for _ in range(8):
                diagnostics.write_event("dictation", "inference_complete", inference_ms=12)
        output = io.BytesIO()
        diagnostics.tail_log(diagnostics.EVENT_LOG, 100, False, output)
        events = list(map(json.loads, output.getvalue().splitlines()))
        self.assertTrue(1 < len(events) < 8)
        self.assertTrue(all(row["inferenceMs"] == 12 for row in events))
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / diagnostics.EVENT_LOG).stat().st_mode & 0o777, 0o600)

    def test_maintenance_compacts_both_logs_and_keeps_existing_appenders(self):
        self.directory.mkdir()
        events = self.directory / diagnostics.EVENT_LOG
        process = self.directory / diagnostics.PROCESS_LOG
        start = b"large record without a line break" * 128 + b"\n"
        retained = "مرحبا\nآخر سطر\n".encode()
        events.write_bytes(start + retained)
        process.write_bytes(start + retained)
        inode = process.stat().st_ino

        def intervals():
            self.assertEqual(events.read_bytes(), retained)
            self.assertEqual(process.read_bytes(), retained)
            self.assertEqual(process.stat().st_ino, inode)
            writer.write(start + b"message from the running engine\n")
            writer.flush()
            yield
            self.assertEqual(events.read_bytes(), retained)
            self.assertEqual(process.read_bytes(), b"message from the running engine\n")
            writer.write(b"still writing\n")
            writer.flush()
            raise KeyboardInterrupt

        with process.open("ab") as writer:
            calls = intervals()

            def advance(seconds):
                self.assertEqual(seconds, 3600)
                next(calls)

            with patch.multiple(diagnostics, LOG_LIMIT=128, LOG_KEEP=60), patch.object(
                diagnostics.time, "sleep", side_effect=advance
            ):
                with self.assertRaises(KeyboardInterrupt):
                    diagnostics.maintain_logs()
        output = io.BytesIO()
        diagnostics.tail_log(diagnostics.PROCESS_LOG, 2, False, output)
        self.assertEqual(output.getvalue(), b"message from the running engine\nstill writing\n")
        self.assertEqual(process.stat().st_mode & 0o777, 0o600)

    def test_maintenance_skips_missing_files_and_unsafe_paths(self):
        def stop(_):
            raise KeyboardInterrupt

        with patch.object(diagnostics.time, "sleep", side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                diagnostics.maintain_logs()
            self.assertFalse(self.directory.exists())
            self.directory.mkdir()
            outside = self.root / "other.log"
            original = b"unrelated text\n" * 64
            outside.write_bytes(original)
            mode = outside.stat().st_mode & 0o777
            events = self.directory / diagnostics.EVENT_LOG
            process = self.directory / diagnostics.PROCESS_LOG
            events.symlink_to(outside)
            process.hardlink_to(outside)
            with patch.multiple(diagnostics, LOG_LIMIT=128, LOG_KEEP=60):
                with self.assertRaises(KeyboardInterrupt):
                    diagnostics.maintain_logs()
            self.assertEqual(outside.read_bytes(), original)
            self.assertEqual(outside.stat().st_mode & 0o777, mode)

    def test_tail_reads_recent_complete_lines_and_preserves_utf8(self):
        self.directory.mkdir()
        log = self.directory / diagnostics.PROCESS_LOG
        log.write_bytes(b"x" * 32 + "\nold\n".encode() + "مرحبا\nlatest\n".encode())
        output = io.BytesIO()
        with patch.object(diagnostics, "TAIL_BYTES", 21):
            diagnostics.tail_log(diagnostics.PROCESS_LOG, 2, False, output)
        self.assertEqual(output.getvalue().decode(), "مرحبا\nlatest\n")

    def test_follow_handles_partial_utf8_rename_and_truncation(self):
        self.directory.mkdir()
        log = self.directory / diagnostics.PROCESS_LOG
        completed = "مرحبا\n".encode()
        log.write_bytes(b"existing\n" + completed[:1])
        output = io.BytesIO()

        def apply_updates():
            self.assertEqual(output.getvalue(), b"")
            with log.open("ab") as file:
                file.write(completed[1:])
            yield
            self.assertEqual(output.getvalue(), completed)
            log.rename(log.with_suffix(".1"))
            yield
            log.write_bytes(b"new file contains a longer line\n")
            yield
            log.write_bytes(b"x\n")
            yield
            self.assertEqual(output.getvalue(), completed + b"new file contains a longer line\nx\n")
            raise KeyboardInterrupt

        updates = apply_updates()
        with patch.object(diagnostics.time, "sleep", side_effect=lambda _: next(updates)):
            with self.assertRaises(KeyboardInterrupt):
                diagnostics.tail_log(diagnostics.PROCESS_LOG, 0, True, output)

    def test_follow_skips_the_rest_of_an_oversized_record(self):
        self.directory.mkdir()
        log = self.directory / diagnostics.PROCESS_LOG
        log.write_bytes(b"x" * 64)
        output = io.BytesIO()

        def apply_updates():
            with log.open("ab") as file:
                file.write(b"discarded fragment\nnext line\n")
            yield
            self.assertEqual(output.getvalue(), b"next line\n")
            raise KeyboardInterrupt

        updates = apply_updates()
        with patch.object(diagnostics, "TAIL_BYTES", 32), patch.object(diagnostics.time, "sleep", side_effect=lambda _: next(updates)):
            with self.assertRaises(KeyboardInterrupt):
                diagnostics.tail_log(diagnostics.PROCESS_LOG, 1, True, output)

    def test_log_file_must_be_a_regular_nonsymlink_file(self):
        self.directory.mkdir()
        log = self.directory / diagnostics.EVENT_LOG
        target = self.root / "target"
        target.write_text("Unrelated data.")
        log.symlink_to(target)
        with self.assertRaises(OSError):
            diagnostics.tail_log(diagnostics.EVENT_LOG, 1, False, io.BytesIO())
        diagnostics.write_event("desktop", "connected")
        self.assertEqual(target.read_text(), "Unrelated data.")
        log.unlink()
        os.mkfifo(log)
        with self.assertRaises(OSError):
            diagnostics.tail_log(diagnostics.EVENT_LOG, 1, False, io.BytesIO())

    def test_cli_reads_offline_and_stops_follow_on_interrupt(self):
        self.directory.mkdir()
        log = self.directory / diagnostics.PROCESS_LOG
        log.write_text("first\nlast\n")
        command = [sys.executable, str(ENGINE_ROOT / "jardctl.py"), "logs", "--process", "-n", "1"]
        result = subprocess.check_output(command, text=True, timeout=10)
        self.assertEqual(result, "last\n")
        self.assertFalse((self.root / "absent").exists())
        with subprocess.Popen([*command, "--follow"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            try:
                self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                self.assertEqual(process.stdout.readline(), b"last\n")
                process.send_signal(signal.SIGINT)
                out, error = process.communicate(timeout=5)
                self.assertEqual((process.returncode, out, error), (130, b"", b""))
            finally:
                if process.poll() is None:
                    process.kill()
