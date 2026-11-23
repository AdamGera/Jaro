"""Install one verified offline model set and report machine-readable progress to the app."""

import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import zipfile

MANIFEST = Path(__file__).with_name("models.json")
FILES = json.loads(MANIFEST.read_bytes())
SET_ID = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:16]
TOTAL = sum(item["size"] for item in FILES)
SPEECH_URL = FILES[0]["url"].rsplit("/", 1)[0] + "/"
SPEECH_FILES = [item for item in FILES if item["url"].startswith(SPEECH_URL)]
SPEECH_TOTAL = sum(item["size"] for item in SPEECH_FILES)
SPEECH_REVISION = FILES[0]["url"].split("/resolve/")[1].split("/")[0]
LLAMA_BUILD = "b12521"
LLAMA_NAME = f"llama-{LLAMA_BUILD}-bin-ubuntu-x64.tar.gz"
LLAMA_ARCHIVE = {
    "file": f"qwen/{LLAMA_NAME}",
    "size": 23_852_655,
    "sha256": "ab8343cb39e17a18f183ce0a35543a6c3dcc15fb287f55909dd182cb975e345e",
    "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/{LLAMA_NAME}",
}
LLAMA_REQUIRED = ("llama-server", "libllama-server-impl.so", "libllama-common.so.1",
                  "libllama.so.1", "libmtmd.so.1", "libggml.so.1", "libggml-base.so.1", "libggml-cpu-x64.so", "LICENSE")
WINDOWS_LLAMA_NAME = f"llama-{LLAMA_BUILD}-bin-win-cpu-x64.zip"
WINDOWS_LLAMA_ARCHIVE = {
    "file": f"qwen/{WINDOWS_LLAMA_NAME}",
    "size": 25_555_594,
    "sha256": "bd4e093bce292452248c318d4b176fbfebe1ca43a7f3779ebb3846fe7753653e",
    "url": f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}/{WINDOWS_LLAMA_NAME}",
}
WINDOWS_LLAMA_REQUIRED = ("llama-server.exe", "llama-server-impl.dll", "llama-common.dll", "llama.dll", "mtmd.dll",
                          "ggml.dll", "ggml-base.dll", "ggml-cpu-x64.dll", "libomp.dll", "LICENSE-LLVM-OpenMP")


def directory():
    library = Path(os.environ.get("RUMBLE_DATA_DIR") or "~/Library/Application Support/Rumble").expanduser().resolve()
    return library / "models" / SET_ID


def model_directory(speech_only, portable=False):
    alternate = os.environ.get("JARD_MODEL_DIR") if speech_only or portable else None
    return Path(alternate).expanduser().resolve() if alternate else directory()


def runtime_files_present(runtime):
    required = WINDOWS_LLAMA_REQUIRED if sys.platform == "win32" else LLAMA_REQUIRED
    return (all((runtime / name).is_file() for name in required)
            and (sys.platform == "win32" or os.access(runtime / required[0], os.X_OK)))


def runtime_archive():
    return WINDOWS_LLAMA_ARCHIVE if sys.platform == "win32" else LLAMA_ARCHIVE


def runtime_directory(root):
    name = f"llama-{LLAMA_BUILD}" + ("-win-x64" if sys.platform == "win32" else "")
    return root / "qwen" / name


def runtime_ready(runtime):
    try:
        return (
            (runtime / ".verified").read_text().strip() == runtime_archive()["sha256"]
            and runtime_files_present(runtime)
        )
    except (OSError, UnicodeError):
        return False


def install_runtime(root):
    target = runtime_directory(root)
    if runtime_ready(target):
        return
    asset = runtime_archive()
    path = root / asset["file"]
    with TemporaryDirectory(prefix=".llama-install-", dir=target.parent) as folder:
        stage = Path(folder)
        extracted = stage / target.name
        if sys.platform == "win32":
            with zipfile.ZipFile(path) as archive:
                needed = [entry for entry in archive.namelist() if entry in WINDOWS_LLAMA_REQUIRED
                          or (entry == Path(entry).name and entry.startswith("ggml-cpu-") and entry.endswith(".dll"))]
                archive.extractall(extracted, members=needed)
        else:
            with tarfile.open(path, "r:gz") as archive:
                def required(member, destination):
                    filename = Path(member.name)
                    if filename.parent != Path(target.name):
                        return None
                    if filename.name in ("llama-server", "LICENSE") or filename.name.startswith(
                        ("libggml", "libllama.", "libllama-server-impl.", "libllama-common.", "libmtmd.")
                    ):
                        return tarfile.data_filter(member, destination)
                    return None

                archive.extractall(stage, filter=required)
        if not runtime_files_present(extracted):
            raise ValueError("The verified writing runtime archive is incomplete.")
        (extracted / ".verified").write_text(asset["sha256"] + "\n")
        if target.exists() or target.is_symlink():
            target.replace(stage / "previous")
        extracted.replace(target)


def installed_size_matches(root, item):
    try:
        path = root / item["file"]
        return path.is_file() and path.stat().st_size == item["size"]
    except OSError:
        return False


def status(speech_only=False, portable=False):
    root = model_directory(speech_only, portable)
    files = SPEECH_FILES if speech_only else FILES
    complete = (root / ".complete").is_file()
    if speech_only:
        complete = complete or (root / ".speech-complete").is_file()
        try:
            revision_matches = (root / "revision").read_text().strip() == SPEECH_REVISION
        except (OSError, UnicodeError):
            revision_matches = False
        # Source checkouts prepared before the packaged installer have only a revision file.
        complete = revision_matches and (complete or root != directory())
    ready = complete and all(installed_size_matches(root, item) for item in files)
    if portable:
        ready = ready and runtime_ready(runtime_directory(root))
    total = SPEECH_TOTAL if speech_only else TOTAL + (runtime_archive()["size"] if portable else 0)
    return {"ready": ready, "bytes": total, "directory": str(root)}


def emit(**data):
    print(json.dumps(data), flush=True)


def valid(path, item):
    if not path.exists() or path.stat().st_size != item["size"]:
        return False
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest() == item["sha256"]


def fetch(item, partial, report):
    # curl uses macOS trust settings and can resume multi-gigabyte model assets.
    if partial.exists() and partial.stat().st_size >= item["size"]:
        partial.unlink()
    curl = "/usr/bin/curl" if sys.platform == "darwin" else "curl"
    with subprocess.Popen([curl, "--fail", "--location", "--silent", "--show-error",
                           "--retry", "3", "--retry-all-errors", "--continue-at", "-", "--output",
                           str(partial), item["url"]], stdout=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as child:
        try:
            while child.poll() is None:
                if partial.exists():
                    report(partial.stat().st_size)
                time.sleep(0.25)
            if child.returncode:
                raise RuntimeError(f"Download failed for {item['file']}. Retry to resume.")
        finally:
            if child.poll() is None:
                child.terminate()


@contextmanager
def install_lock(path):
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno != errno.EACCES:
                        raise
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def install(source, speech_only=False, portable=False):
    root = model_directory(speech_only, portable)
    files = SPEECH_FILES if speech_only else FILES
    if portable:
        files = [*files, runtime_archive()]
    total = sum(item["size"] for item in files)
    marker_name = ".speech-complete" if speech_only else ".complete"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with install_lock(root / ".lock"):
        if (speech_only or portable) and source is None and (ready := status(speech_only, portable))["ready"]:
            emit(event="ready", **ready)
            return
        (root / marker_name).unlink(missing_ok=True)
        completed = 0
        for item in files:
            name = item["file"]
            destination = root / name
            destination.parent.mkdir(exist_ok=True, parents=True)
            partial = destination.with_suffix(destination.suffix + ".partial")

            def progress(size):
                emit(event="progress", file=name, completed=completed + size, total=total)

            if not valid(destination, item):
                progress(0)
                if source is not None or not valid(partial, item):
                    if source:
                        with (source / name).open("rb") as original, partial.open("wb") as copy:
                            while chunk := original.read(8 * 1024 * 1024):
                                copy.write(chunk)
                                progress(copy.tell())
                    else:
                        fetch(item, partial, progress)
                    if not valid(partial, item):
                        partial.unlink(missing_ok=True)
                        raise ValueError(f"File verification failed for {name}. Select the pinned model set or download it.")
                partial.replace(destination)
            completed += item["size"]
            emit(event="progress", file=name, completed=completed, total=total)
        if portable:
            install_runtime(root)
        revision = root / "revision.tmp"
        revision.write_text(SPEECH_REVISION + "\n")
        revision.replace(root / "revision")
        marker = root / (marker_name + ".tmp")
        marker.write_text(SET_ID)
        marker.replace(root / marker_name)
    emit(event="ready", **status(speech_only, portable))


def stop_installation(_signum, _frame):
    # Unwind the download and lock handlers, leaving partial files resumable.
    raise SystemExit(1)


if __name__ == "__main__":
    if os.name != "nt":
        signal.signal(signal.SIGTERM, stop_installation)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["model-status", "models", "model-path"])
    parser.add_argument("--from", dest="source", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--speech-only", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--portable", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.portable and (sys.platform not in ("linux", "win32") or platform.machine().lower() not in ("x86_64", "amd64")):
        parser.error("The full local model set is available on x64 Linux and Windows.")
    try:
        if args.action == "model-status":
            emit(**status(args.speech_only, args.portable))
        elif args.action == "model-path":
            print(directory())
        else:
            install(args.source, args.speech_only, args.portable)
    except (OSError, ValueError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as error:
        emit(event="error", message=str(error))
        sys.exit(1)
