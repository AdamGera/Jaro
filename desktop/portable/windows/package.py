"""Stage Jaro's self-contained Windows x64 installer payload."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from package_files import ROOT, install, release_metadata, stage_workspace

CPYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
FFMPEG_SHA256 = "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1"
HOST_FILES = ("jaro.exe", "jaro-capture.exe", "jaro-screen.exe", "jaro-dictation.exe", "jaro-indicator.exe",
              "jaro-audio-control.exe", "jaro-call-surface.exe", "jaro-tray.exe", "WebView2Loader.dll")
CODEC_FILES = ("ffmpeg.exe", "ffprobe.exe")
INK_FONT_ARCHIVE = "liberation-fonts-ttf-2.1.5"


def require_sha256(path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"SHA-256 verification failed for {path.name}.")


def install_x64(source, destination):
    with source.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or not header.startswith(b"MZ"):
            raise ValueError(f"Expected a Windows release binary: {source.name}")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        if stream.read(6) != b"PE\0\0\x64\x86":
            raise ValueError(f"Expected an x64 Windows release binary: {source.name}")
    install(source, destination)


def stage_runtime(target, archive, wheelhouse):
    require_sha256(archive, CPYTHON_SHA256)
    runtime = target / "python"
    with ZipFile(archive) as package:
        package.extractall(runtime)
    (runtime / "python313._pth").write_text("python313.zip\n.\nLib\\site-packages\n..\\engine\n", encoding="ascii")
    packages = runtime / "Lib/site-packages"
    source = ["--no-index", "--find-links", wheelhouse] if wheelhouse else [
        "--index-url", "https://pypi.org/simple", "--find-links", "https://download.pytorch.org/whl/cpu/torch/",
    ]
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", *source,
                    "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
                    "--only-binary=:all:", "--require-hashes", "--no-deps", "--no-compile", "--target", packages,
                    "-r", HERE / "requirements.txt"], check=True)
    for wheel_scripts in (packages / "Scripts", packages / "bin"):
        if wheel_scripts.is_dir():
            shutil.rmtree(wheel_scripts)


def stage_launchers(target, zig):
    if subprocess.check_output([zig, "version"], text=True).strip() != "0.16.0":
        raise ValueError("Build the Windows launchers with Zig 0.16.0.")
    target.mkdir()
    args = [zig, "cc", "-target", "x86_64-windows-gnu", "-O2", "-Wall", "-Wextra",
            "-Werror", "-municode", HERE / "launch.c", "-lshell32"]
    subprocess.run([*args, "-Wl,--subsystem,windows", "-lole32", "-luser32", "-o", target / "Jaro.exe"], check=True)
    subprocess.run([*args, "-DJARO_CONSOLE", "-o", target / "jardctl.exe"], check=True)
    for debug in target.glob("*.pdb"):
        debug.unlink()


def stage(args, target):
    version = args.test_version or json.loads((HERE.parent / "app.json").read_bytes())["version"]
    if not re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", version):
        raise ValueError("The app version must use major.minor.patch.")
    sdk_metadata = json.loads((args.sdk / "package.json").read_bytes())
    if (sdk_metadata.get("name"), sdk_metadata.get("version")) != ("@native-sdk/cli", "0.11.1"):
        raise ValueError("Supply the pinned Native SDK 0.11.1 package.")
    codec_source = args.codecs.parent / "ffmpeg-7.1.1.tar.xz"
    require_sha256(codec_source, FFMPEG_SHA256)
    payload = target / "payload"
    stage_workspace(payload, args.dist)
    release_metadata(payload, "windows", version)
    for folder, names in ((args.host, HOST_FILES), (args.codecs, CODEC_FILES)):
        for name in names:
            install_x64(folder / name, payload / "bin" / name)
    install(HERE / "Jaro.ico", payload / "bin" / "jaro-tray.ico")
    stage_runtime(payload, args.python_archive, args.wheelhouse)
    stage_launchers(target / "launchers", args.zig)
    licenses = payload / "licenses"
    shutil.copytree(ROOT / "licenses", licenses)
    shutil.copytree(ROOT / "engine/stupase_model/licenses", licenses / "stupase")
    install(ROOT / "engine/stupase_model/NOTICE.md", licenses / "stupase/NOTICE.md")
    for name in ("LICENSE", "NOTICE"):
        install(ROOT / name, licenses / name)
    install(args.sdk / "LICENSE", licenses / "Native-SDK-LICENSE")
    install(args.sdk / "third_party/webview2/LICENSE.txt", licenses / "WebView2-LICENSE.txt")
    tools = ROOT / "desktop/build-tools"
    install(tools / "MODEL-NOTICES.txt", licenses / "MODEL-NOTICES.txt")
    for path in (codec_source, args.codecs / "COPYING.LGPLv2.1", tools / "build-codecs.sh"):
        install(path, licenses / "ffmpeg" / path.name)
    ink_sources = tools / "windows-ink-sources.txt"
    for line in ink_sources.read_text(encoding="ascii").splitlines():
        if not line or line.startswith("#"):
            continue
        digest, archive, _ = line.split()
        source = args.codecs.parent / archive
        require_sha256(source, digest)
        install(source, licenses / "ffmpeg" / archive)
    for path in (ink_sources, tools / "build-windows-ink-deps.sh"):
        install(path, licenses / "ffmpeg" / path.name)
    with tarfile.open(args.codecs.parent / f"{INK_FONT_ARCHIVE}.tar.gz") as fonts:
        for entry, path in (("LiberationSans-Regular.ttf", payload / "bin/ink-fonts/LiberationSans-Regular.ttf"),
                            ("LICENSE", licenses / "Liberation-Sans-LICENSE")):
            with fonts.extractfile(f"{INK_FONT_ARCHIVE}/{entry}") as file:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(file.read())
    for name in ("setup.iss", "Jaro.ico"):
        install(HERE / name, target / name)
    (target / "version.iss").write_text(f'#define AppVersion "{version}"\n', encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="Native SDK Windows build's bin directory")
    parser.add_argument("--sdk", type=Path, required=True, help="The pinned @native-sdk/cli package directory")
    parser.add_argument("--python-archive", type=Path, required=True, help="Official CPython 3.13.17 x64 embeddable ZIP")
    parser.add_argument("--codecs", type=Path, required=True, help="Windows FFmpeg build directory from build-codecs.sh")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist", help="Built React workspace")
    parser.add_argument("--out", type=Path, required=True, help="New directory to hold the staged installer")
    parser.add_argument("--wheelhouse", type=Path, help="Use a local wheelhouse without downloading wheels")
    parser.add_argument("--zig", type=Path, default=Path("zig"), help="Zig 0.16.0 executable")
    parser.add_argument("--test-version", help="Alternate installer version for upgrade tests")
    args = parser.parse_args()
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.exists():
            raise ValueError("Choose a new directory for the staged installer.")
        with tempfile.TemporaryDirectory(prefix=".jaro-installer-", dir=args.out.parent) as work:
            target = Path(work) / "stage"
            target.mkdir()
            stage(args, target)
            target.rename(args.out)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
    print(args.out / "setup.iss")


if __name__ == "__main__":
    main()
