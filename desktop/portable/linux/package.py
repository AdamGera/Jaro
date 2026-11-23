"""Build the Jaro desktop package for Debian 13 amd64."""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from package_files import ROOT, install, release_metadata, stage_workspace

APP_ID = "local.jardaudio.desktop"
PACKAGE_ROOT = "/usr/lib/jaro"
SYSTEM_PYTHON = Path("/usr/bin/python3.13")


def stage_runtime(target, wheelhouse):
    venv = target / "venv"
    binary = venv / "bin/python"
    binary.parent.mkdir(parents=True)
    binary.symlink_to(SYSTEM_PYTHON)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\n")
    packages = venv / "lib/python3.13/site-packages"
    source = ["--no-index", "--find-links", wheelhouse] if wheelhouse else [
        "--index-url", "https://pypi.org/simple", "--find-links", "https://download.pytorch.org/whl/cpu/torch/",
    ]
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", *source,
                    "--only-binary=:all:", "--require-hashes", "--no-deps", "--no-compile",
                    "--target", packages, "-r", HERE / "requirements.txt"], check=True)
    # The package only exposes the Jaro commands, never the wheel entry points.
    wheel_scripts = packages / "bin"
    if wheel_scripts.is_dir():
        shutil.rmtree(wheel_scripts)
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-m", "pip", "--python", binary, "check"], check=True, env=environment)
    subprocess.run([binary, "-IsB", "-c",
                    "import torch, fastapi, dbus_fast, Xlib; assert torch.version.cuda is None"],
                   check=True, env=environment)


def stage_host(target, host):
    with host.open("rb") as binary:
        header = binary.read(20)
        if not header.startswith(b"\x7fELF\x02\x01") or header[18:20] != b"\x3e\x00":
            raise ValueError("Build the x86-64 Linux Native SDK host before packaging.")
    install(host, target / "bin/jaro", 0o755)
    install(host.with_name("jaro-pen"), target / "bin/jaro-pen", 0o755)
    install(host.with_name("jaro-indicator"), target / "bin/jaro-indicator", 0o755)
    install(host.with_name("jaro-call-surface"), target / "bin/jaro-call-surface", 0o755)


def build(host, dist, output, revision, wheelhouse, sdk):
    release = platform.freedesktop_os_release() if sys.platform == "linux" else {}
    if (sys.platform, platform.machine(), sys.version_info[:2], release.get("ID"), release.get("VERSION_ID")) != (
        "linux", "x86_64", (3, 13), "debian", "13",
    ):
        raise ValueError("Build this package on Debian 13 amd64 using Python 3.13 with pip.")
    version = json.loads((HERE.parent / "app.json").read_bytes())["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("The portable app version must use major.minor.patch.")
    sdk_metadata = json.loads((sdk / "package.json").read_bytes())
    if (sdk_metadata.get("name"), sdk_metadata.get("version")) != ("@native-sdk/cli", "0.11.1"):
        raise ValueError("Supply the pinned Native SDK 0.11.1 package.")
    version = f"{version}-{revision}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jaro-package-", dir=output) as work:
        stage = Path(work) / "package"
        target = stage / PACKAGE_ROOT.removeprefix("/")
        stage_host(target, host)
        stage_workspace(target, dist)
        release_metadata(target, "debian", version.rsplit("-", 1)[0], revision)
        stage_runtime(target, wheelhouse)
        install(HERE / "launch", target / "bin/launch", 0o755)
        (target / "in-use").touch()
        commands = stage / "usr/bin"
        commands.mkdir(parents=True)
        for name in ("jaro", "jardctl"):
            (commands / name).symlink_to(f"{PACKAGE_ROOT}/bin/launch")
        application = stage / f"usr/share/applications/{APP_ID}.desktop"
        install(HERE / f"{APP_ID}.desktop", application)
        subprocess.run(["desktop-file-validate", application], check=True)
        install(ROOT / "desktop/assets/icon.png", stage / f"usr/share/icons/hicolor/512x512/apps/{APP_ID}.png")
        for name in ("LICENSE", "NOTICE"):
            install(ROOT / name, stage / "usr/share/doc/jaro" / name)
        shutil.copytree(ROOT / "licenses", stage / "usr/share/doc/jaro/licenses")
        install(ROOT / "desktop/build-tools/MODEL-NOTICES.txt", stage / "usr/share/doc/jaro/MODEL-NOTICES.txt")
        install(sdk / "LICENSE", stage / "usr/share/doc/jaro/Native-SDK-LICENSE")
        control = stage / "DEBIAN"
        control.mkdir()
        for hook in ("preinst", "prerm", "postinst", "postrm"):
            install(HERE / hook, control / hook, 0o755)
        installed_kib = subprocess.check_output(["du", "-sk", stage], text=True).split()[0]
        (control / "control").write_text(
            f"""Package: jaro
Version: {version}
Architecture: amd64
Maintainer: Jard <support@jard.ai>
Section: sound
Priority: optional
Installed-Size: {installed_kib}
Depends: python3.13, ffmpeg, pulseaudio-utils, libgtk-4-1, libwebkitgtk-6.0-4, libglib2.0-bin,
 gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad,
 libgcc-s1, libstdc++6, libgomp1, libgtk4-layer-shell0, libx11-6, libxext6, ca-certificates, curl, util-linux
Recommends: xdg-desktop-portal, at-spi2-core, gstreamer1.0-tools, gstreamer1.0-pipewire, xclip, wl-clipboard
Homepage: https://www.jard.ai/
Description: Local audio recording, transcription, and dictation
 Jaro records audio and transcribes it on this computer with local models.
"""
        )
        name = f"jaro_{version}_amd64.deb"
        artifact = Path(work) / name
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zzstd", "-z9", "--build", stage, artifact], check=True)
        result = output / name
        artifact.replace(result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="Built Linux Native SDK executable")
    parser.add_argument("--sdk", type=Path, required=True, help="The pinned @native-sdk/cli package directory")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist", help="Built React workspace")
    parser.add_argument("--out", type=Path, default=ROOT / "desktop/out/portable/linux")
    parser.add_argument("--revision", type=int, default=1, help="Debian package revision")
    parser.add_argument("--wheelhouse", type=Path, help="Use a local wheelhouse without downloading wheels")
    args = parser.parse_args()
    if args.revision < 1:
        parser.error("The package revision must be positive.")
    os.umask(0o022)
    try:
        print(build(args.host, args.dist, args.out, args.revision, args.wheelhouse, args.sdk))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
