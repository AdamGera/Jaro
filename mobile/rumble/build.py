#!/usr/bin/env python3
"""Build the pinned CPU runtime for an opt-in Jaro mobile build."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CRISP_REV = "0e591b2409e47824a242baae80255d150e9aa0a9"
GGML_REV = "97098170cff27586706c503245a0a5008635f654"
GGML_SHA256 = "09917c595cf86993b57816efa054c4db332b0196467fb66a25fbf6bf7917d788"
ANDROID_ABI = "arm64-v8a"


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url, sha256, path):
    if path.is_file() and checksum(path) == sha256:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        temporary = None
        try:
            request = Request(url, headers={"User-Agent": "Jaro mobile native build"})
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as destination:
                temporary = Path(destination.name)
                with urlopen(request, timeout=30) as response:
                    shutil.copyfileobj(response, destination)
            if checksum(temporary) != sha256:
                raise ValueError(f"SHA-256 mismatch for {url}")
            temporary.replace(path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def select_ggml(archive_path, output):
    selected = {"CMakeLists.txt", "LICENSE", "cmake", "include", "src"}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2 or parts[1] not in selected:
                continue
            if parts[0] != f"ggml-{GGML_REV}" or ".." in parts or not (member.isfile() or member.isdir()):
                raise ValueError(f"Unexpected ggml archive entry {member.name}")
            path = output.joinpath(*parts[1:])
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

    # The standalone runtime installs a process-wide terminate handler, its
    # backtrace can fork a debugger, and its backend registry links dlopen/dlclose
    # even when dynamic backends are disabled. All are inappropriate in a mobile library.
    source = output / "src" / "ggml.c"
    code = source.read_text()
    start = code.index("\n#if defined(__linux__) || \\\n")
    end = code.index("\nstatic ggml_abort_callback_t", start)
    source.write_text(code[:start] + "\nvoid ggml_print_backtrace(void) {}\n" + code[end:])
    (output / "src" / "ggml.cpp").write_text('#include "ggml-impl.h"\n')
    (output / "src" / "ggml-backend-dl.h").write_text(
        "#pragma once\n"
        "#include <filesystem>\n"
        "#include <memory>\n"
        "namespace fs = std::filesystem;\n"
        "using dl_handle = void;\n"
        "struct dl_handle_deleter { void operator()(void *) const {} };\n"
        "using dl_handle_ptr = std::unique_ptr<dl_handle, dl_handle_deleter>;\n"
        "dl_handle * dl_load_library(const fs::path & path);\n"
        "void * dl_get_sym(dl_handle * handle, const char * name);\n"
        "const char * dl_error();\n"
    )
    (output / "src" / "ggml-backend-dl.cpp").write_text(
        '#include "ggml-backend-dl.h"\n'
        "dl_handle * dl_load_library(const fs::path &) { return nullptr; }\n"
        "void * dl_get_sym(dl_handle *, const char *) { return nullptr; }\n"
        'const char * dl_error() { return ""; }\n'
    )


def prepare_sources(cache, output):
    manifest = json.loads((ROOT / "source-checksums.json").read_text())
    crisp = cache / f"CrispASR-{CRISP_REV}"

    def fetch_crisp(entry):
        name, sha256 = entry
        fetch(f"https://raw.githubusercontent.com/CrispStrobe/CrispASR/{CRISP_REV}/{name}", sha256, crisp / name)
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(crisp / name, destination)

    ggml_archive = cache / f"ggml-{GGML_REV}.tar.gz"
    with ThreadPoolExecutor(max_workers=8) as executor:
        ggml = executor.submit(fetch, f"https://codeload.github.com/CrispStrobe/ggml/tar.gz/{GGML_REV}", GGML_SHA256, ggml_archive)
        list(executor.map(fetch_crisp, manifest.items()))
        ggml.result()
    select_ggml(ggml_archive, output / "ggml")


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)


def build(args, source, output, *options):
    run(args.cmake, "-G", "Ninja", "-S", ROOT, "-B", output, "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_MAKE_PROGRAM={args.ninja}", f"-DJARO_RUMBLE_SOURCE={source}", *options)
    run(args.cmake, "--build", output, "--target", "jaro_rumble", "--parallel", args.jobs)


def build_ios(args, source, work):
    archives = []
    sdks = (("iphoneos", "arm64"), ("iphonesimulator", args.simulator_arch))
    for sdk, arch in sdks:
        directory = work / sdk
        build(args, source, directory, "-DCMAKE_SYSTEM_NAME=iOS", "-DCMAKE_OSX_DEPLOYMENT_TARGET=18.0",
              f"-DCMAKE_OSX_SYSROOT={sdk}", f"-DCMAKE_OSX_ARCHITECTURES={arch}")
        library = work / "combined" / sdk / "libjaro_rumble.a"
        library.parent.mkdir(parents=True)
        ggml = directory / "ggml" / "src"
        run("xcrun", "libtool", "-static", "-o", library, directory / "libjaro_rumble.a",
            *(ggml / name for name in ("libggml.a", "libggml-base.a", "libggml-cpu.a")))
        archives.extend(("-library", library, "-headers", ROOT / "include"))
    result = work / "JaroRumble.xcframework"
    run("xcodebuild", "-create-xcframework", *archives, "-output", result)
    destination = args.output / "ios" / result.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = work / "previous.xcframework"
    if destination.exists():
        destination.replace(previous)
    try:
        result.replace(destination)
    except OSError:
        if previous.exists():
            previous.replace(destination)
        raise
    return destination


def build_android(args, source, work):
    toolchain = args.ndk / "build" / "cmake" / "android.toolchain.cmake"
    if not toolchain.is_file():
        raise ValueError(f"Android NDK CMake toolchain not found at {toolchain}")
    directory = work / "android"
    build(args, source, directory, f"-DCMAKE_TOOLCHAIN_FILE={toolchain}", f"-DANDROID_ABI={ANDROID_ABI}",
          "-DANDROID_PLATFORM=android-26", "-DANDROID_STL=c++_static", "-DANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES=ON")
    tools = next(path.parent for path in (args.ndk / "toolchains" / "llvm" / "prebuilt").glob("*/bin/llvm-readelf"))
    library = directory / "libjaro_rumble.so"
    result = work / library.name
    run(tools / "llvm-strip", "--strip-unneeded", "-o", result, library)
    program_headers = subprocess.check_output([tools / "llvm-readelf", "-lW", result], text=True)
    loads = [int(alignment, 16) for alignment in re.findall(r"^\s*LOAD\s+.*\s+(0x[0-9a-f]+)$", program_headers, re.M)]
    relro = re.search(r"^\s*GNU_RELRO\s+\S+\s+(\S+)\s+\S+\s+\S+\s+(\S+)", program_headers, re.M)
    if not loads or min(loads) < 0x4000 or not relro or sum(int(field, 16) for field in relro.groups()) % 0x4000:
        raise ValueError("The Android runtime does not support 16 KiB memory pages")
    destination = args.output / "android" / ANDROID_ABI / result.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.replace(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("ios", "android"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="Directory for runtime artifacts")
    parser.add_argument("--cache", type=Path, help="Cache for pinned upstream source files and archive")
    parser.add_argument("--cmake", type=Path, default=shutil.which("cmake"))
    parser.add_argument("--ninja", type=Path, default=shutil.which("ninja"))
    parser.add_argument("--ndk", type=Path, default=os.environ.get("ANDROID_NDK_HOME"))
    parser.add_argument("--simulator-arch", choices=("arm64", "x86_64"), default="arm64")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 4))
    args = parser.parse_args()
    if args.cmake is None:
        parser.error("Provide --cmake or install CMake 3.22 or newer")
    args.ninja = args.ninja or args.cmake.with_name("ninja")
    if not args.ninja.is_file():
        parser.error("Provide --ninja or install Ninja")
    if args.target == "android" and args.ndk is None:
        parser.error("Provide --ndk or set ANDROID_NDK_HOME")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    cache = args.cache or args.output / ".source-cache"
    with tempfile.TemporaryDirectory(prefix="build-", dir=args.output) as directory:
        work = Path(directory)
        source = work / "source"
        prepare_sources(cache, source)
        destination = (build_ios if args.target == "ios" else build_android)(args, source, work)
    print(destination)


if __name__ == "__main__":
    main()
