from contextlib import contextmanager
import hashlib
import importlib.util
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import jardctl
import model_installation
from engine_paths import WORKSPACE_ROOT


class ModelInstallationTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.installer = self.work / "model_setup.py"
        shutil.copyfile(WORKSPACE_ROOT / "desktop/build-tools/model_setup.py", self.installer)
        self.source = self.work / "source"
        self.source.mkdir()
        manifest = []
        for name, data, repo in (
            ("LICENSE", b"fixture license", "speech"),
            ("ctc/weights", b"fixture alignment", "speech"),
            ("writing/weights", b"fixture writing model", "writing"),
        ):
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            manifest.append({"file": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                             "url": f"https://example.invalid/{repo}/resolve/pinned/{name}"})
        manifest_data = json.dumps(manifest).encode()
        (self.work / "models.json").write_bytes(manifest_data)
        library = self.work / "library"
        self.install_dir = library / "models" / hashlib.sha256(manifest_data).hexdigest()[:16]
        self.enterContext(patch.dict(os.environ, {"RUMBLE_DATA_DIR": str(library)}))
        self.enterContext(patch.object(model_installation, "MODEL_DIR", self.install_dir))
        self.enterContext(patch.object(model_installation, "model_command",
            side_effect=lambda action, *, all_models=False: [sys.executable, str(self.installer), action, "--speech-only"]))

    def run_full(self, action, *arguments):
        result = subprocess.check_output([sys.executable, str(self.installer), action, *arguments], text=True, timeout=10)
        return json.loads(result.splitlines()[-1])

    def test_speech_install_reuses_full_store_without_claiming_full_readiness(self):
        self.assertFalse(model_installation.model_status()["ready"])
        self.assertFalse(self.install_dir.exists())
        result = model_installation.install_model(self.source)
        self.assertTrue(result["ready"])
        self.assertTrue((self.install_dir / ".speech-complete").is_file())
        self.assertTrue((self.install_dir / "ctc/weights").is_file())
        self.assertFalse((self.install_dir / "writing/weights").exists())
        self.assertFalse(self.run_full("model-status")["ready"])
        speech_file = self.install_dir / "ctc/weights"
        before = speech_file.stat().st_mtime_ns
        self.assertTrue(self.run_full("models", "--from", str(self.source))["ready"])
        self.assertEqual(speech_file.stat().st_mtime_ns, before)
        self.assertTrue(model_installation.model_status()["ready"])

    def test_completed_partials_are_promoted_without_redownloading(self):
        for name in ("LICENSE", "ctc/weights"):
            destination = self.install_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.source / name, destination.with_suffix(".partial"))
        inode = (self.install_dir / "ctc/weights.partial").stat().st_ino
        self.assertTrue(model_installation.install_model()["ready"])
        self.assertEqual((self.install_dir / "ctc/weights").stat().st_ino, inode)

    def test_wrong_hash_does_not_publish_ready(self):
        (self.source / "ctc/weights").write_bytes(b"x" * len(b"fixture alignment"))
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            model_installation.install_model(self.source)
        self.assertFalse(model_installation.model_status()["ready"])
        self.assertFalse((self.install_dir / ".speech-complete").exists())

    def test_portable_model_install_publishes_and_repairs_the_writing_runtime(self):
        spec = importlib.util.spec_from_file_location("model_setup_fixture", self.installer)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        archive = self.source / installer.LLAMA_ARCHIVE["file"]
        archive.parent.mkdir()
        with tarfile.open(archive, "w:gz") as bundle:
            for name in (*installer.LLAMA_REQUIRED, "other-cli"):
                item = tarfile.TarInfo(f"llama-{installer.LLAMA_BUILD}/{name}")
                item.mode = 0o700
                item.size = 1
                bundle.addfile(item, BytesIO(b"a"))
        installer.LLAMA_ARCHIVE = {**installer.LLAMA_ARCHIVE, "size": archive.stat().st_size,
                                   "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
        runtime = self.install_dir / "qwen" / f"llama-{installer.LLAMA_BUILD}"
        with (patch.dict(os.environ, {"JARD_MODEL_DIR": str(self.install_dir)}),
              patch("sys.platform", "linux"), patch.object(installer, "emit")):
            self.assertFalse(installer.status(portable=True)["ready"])
            with patch.object(installer, "install_runtime", side_effect=RuntimeError("interrupted extraction")):
                with self.assertRaisesRegex(RuntimeError, "interrupted extraction"):
                    installer.install(self.source, portable=True)
            self.assertFalse((self.install_dir / "revision").exists())
            self.assertFalse(installer.status(portable=True)["ready"])
            installer.install(self.source, portable=True)
            self.assertTrue(installer.status(portable=True)["ready"])
            self.assertFalse((runtime / "other-cli").exists())
            (runtime / "libggml.so.1").unlink()
            self.assertFalse(installer.status(portable=True)["ready"])
            with patch.object(installer, "fetch") as fetch:
                installer.install(None, portable=True)
                fetch.assert_not_called()
            self.assertTrue(installer.status(portable=True)["ready"])

    def test_windows_runtime_uses_its_own_receipt_and_extracts_only_worker_files(self):
        spec = importlib.util.spec_from_file_location("model_setup_fixture", self.installer)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with patch("sys.platform", "win32"):
            root = self.install_dir
            archive = root / installer.runtime_archive()["file"]
            archive.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive, "w") as bundle:
                for name in (*installer.WINDOWS_LLAMA_REQUIRED, "ggml-cpu-haswell.dll", "llama-connect.exe"):
                    bundle.writestr(name, b"fixture")
            installer.install_runtime(root)
            runtime = installer.runtime_directory(root)
            self.assertTrue(runtime.name.endswith("-win-x64"))
            self.assertTrue(installer.runtime_ready(runtime))
            self.assertTrue((runtime / "ggml-cpu-haswell.dll").is_file())
            self.assertFalse((runtime / "llama-connect.exe").exists())
            (runtime / ".verified").write_text(installer.LLAMA_ARCHIVE["sha256"])
            self.assertFalse(installer.runtime_ready(runtime))

    def test_terminal_must_consent_and_machine_output_never_prompts(self):
        for allow, is_terminal, answer, install in (
            (True, True, "\n", True),
            (True, True, "YES\n", True),
            (True, True, "n\n", False),
            (True, True, "", False),
            (False, True, "yes\n", False),
            (True, False, "yes\n", False),
        ):
            with self.subTest(allow=allow, is_terminal=is_terminal, answer=answer):
                reader, writer = Mock(), StringIO()
                reader.isatty.return_value = is_terminal
                reader.readline.return_value = answer
                writer.isatty = lambda: True
                with patch("sys.stdin", reader), patch("sys.stderr", writer), patch.object(model_installation, "install_model") as download:
                    if install:
                        model_installation.ensure_model(allow_prompt=allow)
                        download.assert_called_once_with(progress=True)
                    else:
                        with self.assertRaises(model_installation.ModelUnavailable):
                            model_installation.ensure_model(allow_prompt=allow)
                        download.assert_not_called()
                self.assertEqual("Download Rumble2 Flash" in writer.getvalue(), allow and is_terminal)

    def test_transcription_uses_one_consented_install_and_sends_the_original_piece(self):
        first = b"\xaa\x00\x01\x00"
        second = b"\xee\x00\x02\x00"
        opened = []

        @contextmanager
        def embedded():
            opened.append("started")
            try:
                yield {"fake": "service"}
            finally:
                opened.append("closed")

        args = SimpleNamespace(file=self.work / "audio.wav", language=["en"], json=False)
        reader, stderr, stdout = Mock(), StringIO(), StringIO()
        reader.isatty.return_value = True
        reader.readline.return_value = "yes\n"
        stderr.isatty = lambda: True
        real_install = model_installation.install_model
        with patch("transcription_pieces.audio_pieces", lambda _: (value for value in [first, second])), \
                patch("jardctl.running_engine", return_value=None), patch("sdk_embedded.owned_transcriber", embedded), \
                patch.object(model_installation, "install_model", side_effect=lambda **_:
                             real_install(self.source)) as install, \
                patch("sdk_embedded.sdk_transcription", side_effect=["First.", "Second."]) as send, \
                patch("sys.stdin", reader), patch("sys.stderr", stderr), patch("sys.stdout", stdout):
            jardctl.transcribe_file(self.work, args)
        self.assertEqual(opened, ["started", "closed"])
        install.assert_called_once_with(progress=True)
        self.assertEqual([item.args[1][44:] for item in send.call_args_list], [first, second])
        self.assertEqual(stdout.getvalue(), "First. Second.\n")
