"""Build Jaro’s Windows or Linux shell with the pinned Native SDK."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SDK_VERSION = "0.11.1"


def build(target, output):
    if target == "linux" and (sys.platform != "linux" or platform.machine() != "x86_64"):
        raise RuntimeError("Build the Linux shell on an x86-64 Linux system with GTK4 and WebKitGTK 6.0 development libraries.")
    native = shutil.which("native")
    if not native or not subprocess.check_output([native, "--version"], text=True).startswith(f"native {SDK_VERSION} "):
        raise RuntimeError(f"Install @native-sdk/cli@{SDK_VERSION}.")
    command = Path(native).resolve()
    installed = command.parent / "node_modules/@native-sdk/cli" if os.name == "nt" else command.parent.parent
    sdk = Path(os.environ.get("NATIVE_SDK_PATH") or installed).resolve()
    metadata = json.loads((sdk / "package.json").read_bytes())
    if (metadata.get("name"), metadata.get("version")) != ("@native-sdk/cli", SDK_VERSION):
        raise RuntimeError(f"NATIVE_SDK_PATH must point to @native-sdk/cli@{SDK_VERSION}.")
    zig = shutil.which("zig")
    if not zig or subprocess.check_output([zig, "version"], text=True).strip() != "0.16.0":
        raise RuntimeError("Put Zig 0.16.0 on PATH.")

    # Use the SDK’s generated runner and platform link rules. The workspace
    # is loaded from the owned engine, so there is no separate frontend build.
    with tempfile.TemporaryDirectory(prefix="jaro-native-") as work:
        project = Path(work) / "jaro"
        subprocess.run([native, "init", project, "--frontend", "vite", "--framework", sdk], check=True)
        shutil.copyfile(HERE / "app.json", project / "app.json")
        shutil.copyfile(HERE / "main.zig", project / "src/main.zig")
        shutil.copyfile(HERE.parent / "assets/icon.png", project / "assets/icon.png")
        # Resolve GTK and WebKit against the target Linux distribution.
        target_args = ["-Dtarget=x86_64-windows-gnu"] if target == "windows" else []
        subprocess.run([
            zig, "build", *target_args, "-Doptimize=ReleaseSmall",
            "-Dweb-layer=include", "-Djs-bridge=false", "-Dwebview-inspect=false",
            "--prefix", output,
        ], cwd=project, check=True)
        if target == "windows":
            flags = [zig, "cc", "-target", "x86_64-windows-gnu", "-O2", "-Wall", "-Wextra", "-Werror"]
            subprocess.run([*flags, HERE / "windows_capture.c", "-lole32", "-o", output / "bin/jaro-capture.exe"], check=True)
            subprocess.run([*flags, HERE / "windows_hotkeys.c", HERE / "windows_clipboard.c",
                            "-luser32", "-lgdi32", "-lole32", "-o", output / "bin/jaro-dictation.exe"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("windows", "linux"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    output = args.out or HERE.parent / "out" / "portable" / args.target
    try:
        build(args.target, output.resolve())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
