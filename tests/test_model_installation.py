from contextlib import ExitStack, contextmanager, nullcontext
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

    def test_cli_repair_replaces_same_size_damage_and_preserves_healthy_files(self):
        self.assertTrue(model_installation.install_model(self.source)["ready"])
        weights = self.install_dir / "ctc/weights"
        original = weights.read_bytes()
        weights.write_bytes(b"x" * len(original))
        weights.with_suffix(".partial").write_bytes(original)
        license_file = self.install_dir / "LICENSE"
        before = license_file.stat()
        self.assertTrue(model_installation.model_status()["ready"])

        output = StringIO()
        with patch("sys.argv", ["jardctl", "model", "install", "--repair", "--json"]), patch("sys.stdout", output):
            jardctl.main()

        self.assertTrue(json.loads(output.getvalue())["ready"])
        self.assertEqual(weights.read_bytes(), original)
        self.assertFalse(weights.with_suffix(".partial").exists())
        self.assertEqual(license_file.stat().st_ino, before.st_ino)
        self.assertEqual(license_file.stat().st_mtime_ns, before.st_mtime_ns)

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
                library = runtime / "libggml.so.1"
                inode = library.stat().st_ino
                installer.install(None, portable=True, repair=True)
                self.assertEqual(library.stat().st_ino, inode)
                library.write_bytes(b"z")
                self.assertTrue(installer.status(portable=True)["ready"])
                installer.install(None, portable=True, repair=True)
                self.assertEqual(library.read_bytes(), b"a")
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
            variant = runtime / "ggml-cpu-haswell.dll"
            variant.write_bytes(b"damaged")
            self.assertTrue(installer.runtime_ready(runtime))
            installer.install_runtime(root)
            self.assertEqual(variant.read_bytes(), b"fixture")
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

    def test_portable_cli_waits_for_running_engine_without_preparing_a_second_model(self):
        record = {"port": 12345}
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), ExitStack() as scope, \
                    patch("jardctl.sys.platform", platform), patch("jardctl.ensure_model") as prepare, \
                    patch("sdk_discovery.read_sdk_record", return_value=record) as discover, \
                    patch("sdk_embedded.sdk_service_status", side_effect=["loading", "ready"]), \
                    patch("jardctl.time.sleep"):
                self.assertIs(jardctl.transcription_service(self.work, scope, allow_prompt=False), record)
                self.assertEqual([call.args for call in discover.call_args_list], [(self.work,), (self.work,)])
                prepare.assert_not_called()

    def test_portable_cli_checks_again_after_preparing_offline_model(self):
        record = {"port": 12345}
        for status in (None, "ready"):
            with self.subTest(status=status), ExitStack() as scope, \
                    patch("jardctl.sys.platform", "linux"), patch("jardctl.ensure_model") as prepare, \
                    patch("sdk_discovery.read_sdk_record", return_value=record), \
                    patch("sdk_embedded.sdk_service_status", side_effect=[None, status]), \
                    patch("sdk_embedded.owned_transcriber", return_value=nullcontext("embedded")) as start:
                result = jardctl.transcription_service(self.work, scope, allow_prompt=False)
                prepare.assert_called_once_with(allow_prompt=False)
                if status is None:
                    self.assertEqual(result, "embedded")
                    start.assert_called_once_with()
                else:
                    self.assertIs(result, record)
                    start.assert_not_called()

    def test_portable_cli_rejects_live_service_errors_without_starting_another_engine(self):
        for error, status in ((RuntimeError("bad discovery"), None), (None, "error")):
            with self.subTest(error=error, status=status), ExitStack() as scope, \
                    patch("jardctl.sys.platform", "win32"), patch("jardctl.ensure_model") as prepare, \
                    patch("sdk_discovery.read_sdk_record", return_value={"port": 12345}, side_effect=error), \
                    patch("sdk_embedded.sdk_service_status", return_value=status), \
                    patch("sdk_embedded.owned_transcriber") as start:
                with self.assertRaisesRegex(RuntimeError, "bad discovery|speech model is unavailable"):
                    jardctl.transcription_service(self.work, scope, allow_prompt=False)
                prepare.assert_not_called()
                start.assert_not_called()
