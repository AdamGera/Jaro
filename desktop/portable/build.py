"""Build Jaro’s Windows or Linux shell with the pinned Native SDK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
SDK_VERSION = "0.11.1"
LAYER_SHELL_VERSION = "1.0.4"
LAYER_SHELL_SHA256 = "7fe327dc3740e4b6f5edfd855e23f84b1ac1ec6854b731047b95df7feb46498b"


def linux_layer_shell_flags(output, work):
    environment = os.environ.copy()
    runpath = []
    if platform.freedesktop_os_release().get("ID") == "ubuntu":
        # Ubuntu 24.04 ships GTK4, but no GTK4 layer-shell library.
        archive = work / "layer-shell.tar.gz"
        url = f"https://deb.debian.org/debian/pool/main/g/gtk4-layer-shell/gtk4-layer-shell_{LAYER_SHELL_VERSION}.orig.tar.gz"
        subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--output", archive, url], check=True)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != LAYER_SHELL_SHA256:
                raise ValueError("GTK4 layer-shell source checksum mismatch.")
        with tarfile.open(archive) as source_tar:
            source_tar.extractall(work, filter="data")
        source = work / f"gtk4-layer-shell-{LAYER_SHELL_VERSION}"
        build_dir = work / "layer-shell-build"
        prefix = work / "layer-shell"
        subprocess.run(["meson", "setup", build_dir, source, "--prefix", prefix, "--libdir", "lib",
                        "--buildtype", "release", "-Dintrospection=false", "-Dvapi=false"], check=True)
        subprocess.run(["meson", "install", "-C", build_dir], check=True)
        library = output / "lib"
        library.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prefix / "lib/libgtk4-layer-shell.so.0", library / "libgtk4-layer-shell.so.0")
        shutil.copyfile(source / "LICENSE", library / "gtk4-layer-shell-LICENSE")
        pkg_paths = [str(prefix / "lib/pkgconfig"), environment.get("PKG_CONFIG_PATH", "")]
        environment["PKG_CONFIG_PATH"] = os.pathsep.join(filter(None, pkg_paths))
        runpath = ["-Wl,-rpath,$ORIGIN/../lib"]
    flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "gtk4-layer-shell-0", "gtk4"],
                                    text=True, env=environment).split()
    return [*flags, *runpath]


def build(target, output):
    if target == "linux" and (sys.platform != "linux" or platform.machine() != "x86_64"):
        raise RuntimeError("Build the Linux shell on an x86-64 Linux system with GTK4 and WebKitGTK 6.0 development libraries.")
    if target == "linux" and not shutil.which("cc"):
        raise RuntimeError("Install a system C compiler for the Linux meeting controls.")
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
            subprocess.run([*flags, HERE / "windows_capture.c", HERE / "windows_browser_pages.c",
                            "-municode", "-lole32", "-loleaut32", "-luser32", "-o", output / "bin/jaro-capture.exe"], check=True)
            subprocess.run([*flags, HERE / "windows_screen.c", HERE / "windows_pen.c", "-ld3d11", "-ldxgi", "-ldwmapi", "-lgdi32",
                            "-luser32", "-lole32", "-o", output / "bin/jaro-screen.exe"], check=True)
            subprocess.run([*flags, HERE / "windows_hotkeys.c", HERE / "windows_clipboard.c", HERE / "windows_paste_picker.c",
                            "-luser32", "-lgdi32", "-lole32", "-ladvapi32", "-o", output / "bin/jaro-dictation.exe"], check=True)
            subprocess.run([*flags, HERE / "windows_indicator.c", "-municode", "-Wl,--subsystem,windows",
                            "-luser32", "-lgdi32", "-lshcore", "-o", output / "bin/jaro-indicator.exe"], check=True)
            subprocess.run([*flags, HERE / "windows_call_surface.c", "-municode", "-Wl,--subsystem,windows",
                            "-luser32", "-lgdi32", "-lshcore", "-o", output / "bin/jaro-call-surface.exe"], check=True)
            subprocess.run([*flags, HERE / "windows_tray.c", "-municode", "-Wl,--subsystem,windows", "-luser32",
                            "-lshell32", "-o", output / "bin/jaro-tray.exe"], check=True)
            subprocess.run([*flags, HERE / "save_dialog.c", "-municode", "-Wl,--subsystem,windows", "-lole32",
                            "-luuid", "-lshell32", "-o", output / "bin/jaro-save-video.exe"], check=True)
            shutil.copyfile(HERE / "windows/Jaro.ico", output / "bin/jaro-tray.ico")
            subprocess.run([*flags, HERE / "windows_audio_control.c", HERE / "windows_media_control.c",
                            "-lole32", "-lapi-ms-win-core-winrt-l1-1-0", "-lapi-ms-win-core-winrt-string-l1-1-0",
                            "-o", output / "bin/jaro-audio-control.exe"], check=True)
        else:
            flags = linux_layer_shell_flags(output, Path(work))
            subprocess.run([zig, "cc", "-O2", "-Wall", "-Wextra", "-Werror", HERE / "linux/pen.c",
                            *flags, "-lm", "-o", output / "bin/jaro-pen"], check=True)
            x11 = subprocess.check_output(["pkg-config", "--libs", "x11", "xext"], text=True).split()
            subprocess.run([zig, "cc", "-O2", "-Wall", "-Wextra", "-Werror", HERE / "linux/indicator.c",
                            *flags, *x11, "-lm", "-o", output / "bin/jaro-indicator"], check=True)
            # Use the distro linker for the GTK notification helper's ELF layout.
            subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", HERE / "linux/call_surface.c",
                            *flags, *x11, "-o", output / "bin/jaro-call-surface"], check=True)
            subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", HERE / "save_dialog.c",
                            *flags, "-o", output / "bin/jaro-save-video"], check=True)

            wayland = subprocess.check_output(["pkg-config", "--cflags", "--libs", "wayland-client", "xkbcommon"],
                                              text=True).split()
            helpers = (
                ("wayland_clipboard.c", "jaro-wayland-clipboard", ("ext-data-control-v1", "wlr-data-control-unstable-v1")),
                ("focused_window.c", "jaro-focused-window", ("wlr-foreign-toplevel-management-unstable-v1",)),
            )
            for implementation, executable, definitions in helpers:
                sources = []
                for name in definitions:
                    protocol = HERE / "linux/protocols" / f"{name}.xml"
                    header, source = Path(work) / f"{name}.h", Path(work) / f"{name}.c"
                    subprocess.run(["wayland-scanner", "client-header", protocol, header], check=True)
                    subprocess.run(["wayland-scanner", "private-code", protocol, source], check=True)
                    sources.append(source)
                subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", work,
                                HERE / "linux" / implementation, *sources, *wayland,
                                "-o", output / "bin" / executable], check=True)


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
