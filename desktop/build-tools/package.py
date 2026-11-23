#!/usr/bin/env python3
"""Build (or repackage) Jaro using only pinned inputs and macOS build tools."""
import argparse
from base64 import b64decode
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import ssl
import subprocess
import tempfile
from urllib.parse import urlsplit

TOOLS = Path(__file__).resolve().parent
DESKTOP = TOOLS.parent
REPO = DESKTOP.parent
ENGINE = REPO / "engine"
CACHE = DESKTOP / ".build"
OUT = DESKTOP / "out"
PUBLISHER = Path.home() / ".config/jard/soundfolio-local/macos-publisher"
MACH = {bytes.fromhex(x) for x in ("cffaedfe", "feedfacf", "cafebabe", "bebafeca", "cafebabf")}
DEV_NOTES_NAME = "Jaro-development-notes.json"
DEV_NOTES_LIMIT = 128 * 1024
DEV_NOTES_BUILDS = 40
DEV_NOTES_CHANGES = 40
ROLLOUT_FEED_NAME = "native-update-v2.json"


def run(*args, cwd=REPO):
    subprocess.run([str(x) for x in args], cwd=cwd, check=True)


def is_mach(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(4) in MACH


def prepare_sdk():
    pinned = "0.11.1"
    node = subprocess.check_output(["node", "-p", "process.versions.node"], text=True)
    if int(node.split(".")[0]) < 24:
        raise SystemExit("Build with Node 24+ on PATH.")
    native = shutil.which("native")
    if not native or not subprocess.check_output([native, "--version"], text=True).startswith(f"native {pinned} "):
        raise SystemExit(f"Install @native-sdk/cli@{pinned} on the build machine.")
    installed = Path(native).resolve().parent.parent
    sdk = Path(os.environ.get("NATIVE_SDK_PATH", installed)).resolve()
    metadata = json.loads((sdk / "package.json").read_bytes())
    if metadata.get("name") != "@native-sdk/cli" or metadata.get("version") != pinned:
        raise SystemExit(f"NATIVE_SDK_PATH must point to @native-sdk/cli@{pinned}.")
    os.environ["NATIVE_SDK_PATH"] = str(sdk)


def prepare_transform_runtime():
    cargo = shutil.which("cargo")
    if cargo is None:
        raise SystemExit("Install Rust 1.97 and Cargo to build the transformation runtime.")
    manifest = DESKTOP / "transforms/Cargo.toml"
    target = "aarch64-apple-darwin"
    build = CACHE / "transform-target"
    env = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": "26.0", "CARGO_TARGET_DIR": str(build)}
    subprocess.run([cargo, "build", "--release", "--locked", "--manifest-path", manifest, "--target", target],
                   check=True, cwd=REPO, env=env)
    metadata = subprocess.check_output([cargo, "metadata", "--format-version", "1", "--locked",
                                        "--manifest-path", manifest, "--filter-platform", target], cwd=REPO, env=env)
    crates = [package for package in json.loads(metadata)["packages"] if package["source"]]
    return build / target / "release/jaro-transforms", crates


def copy_transform_licenses(crates, directory):
    directory.mkdir()
    for package in crates:
        root = Path(package["manifest_path"]).parent
        paths = {path for path in root.iterdir() if path.is_file() and
                 path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT"))}
        if package["license_file"]:
            paths.add(root / package["license_file"])
        if package["name"] == "rquickjs-sys":
            paths.add(root / "quickjs/LICENSE")
        package_name = f'{package["name"]}-{package["version"]}'
        license_type = package["license"] or "(see license file)"
        (directory / f"{package_name}.txt").write_text(
            f"{package_name}  {license_type}\n" +
            "".join(f"\n{item.relative_to(root)}\n{item.read_text(errors='replace')}\n" for item in sorted(paths)),
            encoding="utf-8",
        )


def copy_engine(resources):
    target = resources / "engine"
    target.mkdir()
    # Only ship engine source and catalogs, even if a checkout holds private files.
    for name in (
        "audio_file.py", "chat_context.py", "diarization.py", "diagnostics.py", "engine_lifecycle.py", "engine_paths.py",
        "insights.py", "local_qwen.py", "meeting_transcript.py", "model_installation.py", "native_recordings.py", "note_stream.py", "overview_exchanges.py", "place_hints.py",
        "personal_notes.py", "rumble_model.py", "annotation_assets.py", "annotation_deliveries.py", "annotation_retention.py", "annotation_routing.py",
        "annotation_storage.py",
        "annotation_rollback.py", "saved_annotations.py", "dictation_output.py", "dictation_settings.py", "output_transform_catalog.py", "output_transform_network.py", "output_transform_policy.py", "output_transform_operations.py", "output_transform_runtime.py", "output_transform_workspace.py", "jardctl.py", "server.py", "session_images.py", "session_notes.py", "session_store.py",
        "meeting_mcp.py", "meeting_brief.py", "meeting_exports.py", "meeting_retention.py", "meeting_rolling.py", "meeting_search.py", "saved_dictations.py", "saved_meetings.py",
        "linux_capture.py", "managed_preferences.py", "portable_capture.py", "portable_workspace.py", "sdk_discovery.py", "sdk_embedded.py", "speaker_alignment.py", "transcription_pieces.py", "title_words.py", "writing_api.py", "writing_cache.py",
        "overview_autonyms.json", "overview_languages.json", "place_names.json", "windows_job.py",
        "dictation_input.py", "stupase_runner.py", "portable_dictation_sounds.py", "portable_audio_control.py",
    ):
        shutil.copy2(ENGINE / name, target)
    shutil.copytree(REPO / "dist", target / "dist")
    shutil.copytree(ENGINE / "stupase_model", target / "stupase_model", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(DESKTOP / "transforms" / "starter", target / "transform-starter")
    shutil.copytree(REPO / "licenses", resources / "licenses")
    shutil.copytree(ENGINE / "stupase_model/licenses", resources / "licenses/stupase")
    shutil.copy2(ENGINE / "stupase_model/NOTICE.md", resources / "licenses/stupase/NOTICE.md")
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(REPO / name, resources / "licenses" / name)
    for notice in ("LICENSE", "NOTICE"):
        shutil.copy2(REPO / notice, resources / notice)
    for name in ("COPYING.LGPLv2.1", "LICENSE.md"):
        shutil.copy2(CACHE / "ffmpeg-7.1.1" / name, resources / "licenses" / f"FFmpeg-{name}")
    shutil.copy2(CACHE / "llama-b12521/LICENSE", resources / "licenses/llama.cpp-LICENSE")
    shutil.copy2(TOOLS / "MODEL-NOTICES.txt", resources / "licenses")
    # Ship the FFmpeg source and exact configuration to reproduce the linked codecs.
    shutil.copy2(CACHE / "ffmpeg-7.1.1.tar.xz", resources / "licenses")
    shutil.copy2(TOOLS / "build-codecs.sh", resources / "licenses")
    shutil.copy2(Path(os.environ["NATIVE_SDK_PATH"]) / "LICENSE", resources / "licenses/Native-SDK-LICENSE")
    (target / "desktop/build-tools").mkdir(parents=True)
    for name in ("mac_bridge.py", "sdk_capture.py", "annotation_automation.py", "annotation_agents.py"):
        shutil.copy2(DESKTOP / name, target / "desktop" / name)
    for name in ("helper.py", "model_setup.py", "models.json"):
        shutil.copy2(TOOLS / name, target / "desktop/build-tools")


def update_url(address):
    url = urlsplit(address) if isinstance(address, str) and address else None
    return url and url.scheme == "https" and url.hostname and url.username is None and url.password is None


def update_configuration(channel, development, stable_rollout_url=None):
    stable = json.loads((DESKTOP / "app.json").read_bytes())["updates"]
    updates = {**stable, "channel": channel}
    suffix = "/releases/latest/download/native-update.json"
    if stable_rollout_url is None and urlsplit(stable["feed_url"]).hostname == "github.com" and stable["feed_url"].endswith(suffix):
        repository = stable["feed_url"].removesuffix(suffix)
        stable_rollout_url = f"{repository}/releases/download/stable-rollout/{ROLLOUT_FEED_NAME}"
    if stable_rollout_url is not None:
        if not update_url(stable_rollout_url) or stable_rollout_url == stable["feed_url"]:
            raise ValueError("Stable rollout needs a separate HTTPS feed URL.")
        updates["rollout_url"] = stable_rollout_url
    if development is None:
        if channel == "dev":
            raise ValueError("A Development build needs --dev-updates with its signed feed configuration.")
        return updates
    dev = json.loads(development.expanduser().read_bytes())
    if not isinstance(dev, dict):
        raise ValueError("Provide a JSON object with feed_url, public_key, and optional release_url and rollout_url.")
    for key in ("feed_url", "release_url", "rollout_url"):
        if key != "feed_url" and key not in dev:
            continue
        if not update_url(dev.get(key)):
            raise ValueError(f"Development {key} must be an HTTPS URL.")
    if "rollout_url" in dev and dev["rollout_url"] in (dev["feed_url"], stable["feed_url"], stable_rollout_url):
        raise ValueError("Development rollout needs a separate HTTPS feed URL.")
    try:
        key = b64decode(dev.get("public_key", ""), validate=True)
    except (TypeError, ValueError) as error:
        raise ValueError("Development public_key must be Base64 for a 32-byte Ed25519 public key.") from error
    if len(key) != 32 or key == b64decode(stable["public_key"]) or dev["feed_url"] in (stable["feed_url"], stable_rollout_url):
        raise ValueError("Development needs its own feed URL and a distinct 32-byte Ed25519 public key.")
    updates["development"] = {field: dev[field] for field in ("feed_url", "public_key", "release_url", "rollout_url") if field in dev}
    return updates


def sign_rollout_feed(feed, output, key, channel, percentage, required):
    executable = output.parent / "sign-update-rollout"
    run("xcrun", "swiftc", "-O", DESKTOP / "update/UpdateRollout.swift",
        TOOLS / "sign-update-rollout.swift", "-o", executable)
    run(executable, feed, output, key, channel, percentage, str(required).lower())


def development_version(version):
    if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}", version):
        raise ValueError("Development release notes need a version in major.minor.patch form.")
    return tuple(int(part) for part in version.split("."))


def development_release_notes(version, notes, previous):
    current = development_version(version)
    changes = []
    for line in notes.splitlines():
        title = "".join(character for character in line if ord(character) >= 32 and ord(character) != 127)
        title = title.strip().removeprefix("- ").removeprefix("* ").removeprefix("• ").strip()[:160]
        if title and title not in changes and len(changes) < DEV_NOTES_CHANGES:
            changes.append(title)
    builds = [{"version": version, "changes": changes}]
    if previous is not None:
        with previous.expanduser().open("rb") as stream:
            data = stream.read(DEV_NOTES_LIMIT + 1)
        if len(data) > DEV_NOTES_LIMIT:
            raise ValueError("The previous Development notes file is too large.")
        index = json.loads(data)
        if (not isinstance(index, dict) or type(index.get("schema")) is not int or index["schema"] != 1
                or not isinstance(index.get("builds"), list) or not 1 <= len(index["builds"]) <= DEV_NOTES_BUILDS):
            raise ValueError("The previous Development notes file has an unsupported format.")
        last = current
        for index_position, build in enumerate(index["builds"]):
            if not isinstance(build, dict):
                raise ValueError("The previous Development notes contain an invalid build.")
            build_version = development_version(build.get("version"))
            build_changes = build.get("changes")
            if (not isinstance(build_changes, list) or len(build_changes) > DEV_NOTES_CHANGES or
                    any(not isinstance(item, str) or not item or len(item) > 160 or
                        any(ord(char) < 32 or ord(char) == 127 for char in item) for item in build_changes)):
                raise ValueError("The previous Development notes contain an invalid change.")
            if index_position == 0 and build_version == current:
                continue  # A rebuild replaces this revision's notes.
            if build_version >= last:
                raise ValueError("Previous Development releases must appear newest first and precede this build.")
            last = build_version
            builds.append({"version": build["version"], "changes": build_changes})
    del builds[DEV_NOTES_BUILDS:]
    while True:
        data = json.dumps({"schema": 1, "builds": builds}, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) <= DEV_NOTES_LIMIT:
            return data
        builds.pop()


def assemble(app, updates, transforms):
    contents = app / "Contents"
    info_file = contents / "Info.plist"
    with info_file.open("rb") as data:
        info = plistlib.load(data)
    info.update({
        "JardUpdates": updates,
        "JardOfflineSettingsProtocol": 1,
        "LSMinimumSystemVersion": "26.0",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHumanReadableCopyright": "Copyright © 2026 Jard (Joint AI Research & Dynamics)",
        "NSMicrophoneUsageDescription": "Record your voice for dictation or when you choose to record a session.",
        "NSCameraUsageDescription": "Mirror and record the screen of the iPhone or iPad you connect by USB for an annotation.",
        "NSScreenCaptureUsageDescription": "Record the display you select for an annotation or capture Mac audio for a call.",
        "NSAudioCaptureUsageDescription": "Include the sound from calls and other apps in the recording.",
        "NSCalendarsFullAccessUsageDescription": "Show upcoming meetings and let you join or record them from reminders. Jard only reads calendar events.",
        "NSAppleEventsUsageDescription": "Pause and resume media or read the active browser tab for website profiles when you allow access in Dictation settings.",
    })
    with info_file.open("wb") as data:
        plistlib.dump(info, data)
    resources = contents / "Resources"
    resources.mkdir(exist_ok=True)
    copy_engine(resources)
    transform_executable, transform_crates = transforms
    copy_transform_licenses(transform_crates, resources / "licenses/Transformation Runtime")
    sdk = resources / "sdk" / "typescript"
    sdk.mkdir(parents=True)
    for name in ("index.mjs", "index.d.mts", "README.md"):
        shutil.copy2(REPO / "sdk" / "typescript" / name, sdk / name)
    shutil.copy2(DESKTOP / "setup.html", resources / "setup.html")
    shutil.copy2(DESKTOP / "interface-languages.json", resources / "interface-languages.json")
    shutil.copytree(DESKTOP / "assets/dictation-sounds", resources / "dictation-sounds")
    runtime = contents / "Resources/PythonRuntime"
    shutil.copytree(CACHE / "python", runtime, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    helpers = contents / "Helpers"
    helpers.mkdir(exist_ok=True)
    shutil.copy2(transform_executable, helpers / "jaro-transforms")
    run("xcrun", "clang", "-O2", "-arch", "arm64", "-mmacosx-version-min=26", TOOLS / "helper.c", "-o", helpers / "jard-engine")
    shutil.copy2(helpers / "jard-engine", helpers / "jard-ui")
    shutil.copy2(helpers / "jard-engine", helpers / "jardctl")
    swift = ["xcrun", "swiftc", "-O", "-parse-as-library", "-swift-version", "5", "-target", "arm64-apple-macos26.0"]
    capture = DESKTOP / "capture"
    run(*swift, *(capture / n for n in ("CLI.swift", "Capture.swift", "Recording.swift", "WarmMicrophone.swift", "MeetingSystemAudio.swift")), "-o", helpers / "jard-capture")
    run(*swift, capture / "JardDialogs.swift", "-o", helpers / "jard-dialog")
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2(CACHE / "ffmpeg-7.1.1" / name, helpers)
    llama = helpers / "llama"
    llama.mkdir()
    for path in (CACHE / "llama-b12521").iterdir():
        if path.name == "llama-server" or path.suffix == ".dylib":
            shutil.copy2(path, llama / path.name, follow_symlinks=False)


def sign(app, identity):
    flags = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        flags += ["--options", "runtime", "--timestamp"]
    with (app / "Contents/Info.plist").open("rb") as plist:
        main_executable = app / "Contents/MacOS" / plistlib.load(plist)["CFBundleExecutable"]
    for path in (app / "Contents").rglob("*"):
        if path != main_executable and is_mach(path):
            args = ["--entitlements", TOOLS / "capture.entitlements"] if path.name == "jard-capture" else []
            run(*flags, *args, path)
    run(*flags, "--entitlements", TOOLS / "host.entitlements", app)
    run("codesign", "--verify", "--deep", "--strict", app)


def sign_with_publisher(app, directory, *, release=False):
    certificate = directory / "certificate.pem"
    key = directory / "private-key.pem"
    if key.resolve().is_relative_to(REPO) or not key.is_file() or key.stat().st_mode & 0o077:
        raise SystemExit("Keep private-key.pem outside the checkout with owner-only permissions.")
    if not certificate.is_file():
        raise SystemExit("The signing identity is missing certificate.pem.")
    der = ssl.PEM_cert_to_DER_cert(certificate.read_text())
    if release or directory == PUBLISHER.resolve():
        pinned = ssl.PEM_cert_to_DER_cert((TOOLS / "jard-publisher.crt").read_text())
        if der != pinned:
            raise SystemExit("This build must match the pinned publisher certificate.")
    signer = shutil.which("rcodesign") or Path.home() / ".local/bin/rcodesign"
    if not Path(signer).is_file() or subprocess.check_output([signer, "--version"], text=True).strip() != "apple-codesign 0.29.0":
        raise SystemExit("Publisher signing requires rcodesign 0.29.0 on PATH.")

    entitlements = TOOLS / "capture.entitlements"
    run(signer, "sign", "--pem-file", key, "--pem-file", certificate, "--timestamp-url", "none",
        "--entitlements-xml-file", TOOLS / "host.entitlements",
        "--entitlements-xml-file", f"Contents/Helpers/jard-capture:{entitlements}", app)
    run("codesign", "--verify", "--deep", "--strict", app)
    # Check the actual signer of both processes involved in recording access.
    # Apple's requirement language represents a certificate by its SHA-1 fingerprint.
    fingerprint = hashlib.sha1(der).hexdigest()
    with (app / "Contents/Info.plist").open("rb") as plist:
        bundle_id = plistlib.load(plist)["CFBundleIdentifier"]
    for path, identifier in ((app, bundle_id), (app / "Contents/Helpers/jard-capture", "jard-capture")):
        requirement = f'=identifier "{identifier}" and certificate leaf = H"{fingerprint}"'
        run("codesign", "--verify", "--strict", "-R", requirement, path)


def notarize(item, profile):
    if item.suffix == ".app":
        upload = item.with_suffix(".zip")
        run("ditto", "-c", "-k", "--keepParent", item, upload)
    else:
        upload = item
    run("xcrun", "notarytool", "submit", upload, "--keychain-profile", profile, "--wait")
    run("xcrun", "stapler", "staple", item)
    run("xcrun", "stapler", "validate", item)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-shell", action="store_true", help="Use the previously compiled desktop shell")
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument("--publisher-identity", type=Path, metavar="DIRECTORY",
                         help="Sign using a persistent publisher certificate.pem and private-key.pem")
    signing.add_argument("--identity", help="Keychain signing identity, or - for an explicitly ad-hoc development build")
    parser.add_argument("--notary-profile", help="Keychain notarytool profile for publishing")
    parser.add_argument("--dmg", action="store_true")
    parser.add_argument("--update-key", type=Path, help="Sign an installable update ZIP and feed with an external Ed25519 seed")
    parser.add_argument("--channel", choices=("stable", "dev"), default="stable", help="Release channel stamped into the signed app")
    parser.add_argument("--dev-updates", type=Path, help="JSON with the Development feed URL and distinct public key")
    parser.add_argument("--stable-rollout-url", help="Override the Stable rollout feed URL")
    parser.add_argument("--rollout-percent", type=int, help="Offer the signed rollout to this percentage of installs (0–100)")
    parser.add_argument("--rollout-required", action="store_true", help="Offer the signed release to all eligible installs")
    parser.add_argument("--previous-dev-notes", type=Path, help="Rolling notes JSON from the last Development release")
    parser.add_argument("--notes", default="", help="Release notes shown in the native update prompt")
    opts = parser.parse_args()
    dev_notes = None
    try:
        updates = update_configuration(opts.channel, opts.dev_updates, opts.stable_rollout_url)
        if opts.channel == "dev":
            version = json.loads((DESKTOP / "app.json").read_bytes())["version"]
            dev_notes = development_release_notes(version, opts.notes, opts.previous_dev_notes)
        elif opts.previous_dev_notes:
            raise ValueError("--previous-dev-notes applies to the Development channel.")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    release_settings = updates["development"] if opts.channel == "dev" else updates
    rollout_percent = 100 if opts.rollout_percent is None else opts.rollout_percent
    if not 0 <= rollout_percent <= 100:
        parser.error("--rollout-percent must be between 0 and 100.")
    if (opts.rollout_percent is not None or opts.rollout_required) and (not opts.update_key or not release_settings.get("rollout_url")):
        parser.error("Rollout controls require --update-key and a rollout feed URL for this channel.")
    if opts.identity is None and opts.publisher_identity is None and (PUBLISHER.exists() or opts.update_key):
        opts.publisher_identity = PUBLISHER
    if opts.update_key and opts.identity == "-":
        parser.error("Release updates need the persistent publisher certificate or a signing identity.")
    opts.identity = opts.identity or "-"
    if opts.update_key:
        opts.update_key = opts.update_key.expanduser().resolve()
        if opts.update_key.is_relative_to(REPO) or not opts.update_key.is_file():
            parser.error("Use a private update signing key stored outside the checkout.")
    if opts.notary_profile and opts.identity == "-":
        parser.error("Notarization needs a Developer ID Application identity.")
    if opts.publisher_identity and not opts.publisher_identity.expanduser().is_dir():
        parser.error("The publisher identity directory is missing. Restore it from the maintainer backup.")
    CACHE.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    prepare_sdk()
    run(TOOLS / "prepare-runtime.sh")
    run("bun", "install", "--frozen-lockfile")
    run("bun", "run", "build")
    transforms = prepare_transform_runtime()
    if not opts.reuse_shell:
        run(TOOLS / "build-shell.sh")
    # Build entirely in staging so a compile or signing failure preserves the previous app.
    with tempfile.TemporaryDirectory(dir=OUT, prefix=".package-") as stage:
        stage = Path(stage)
        app = stage / "Jaro.app"
        run("native", "package", "--target", "macos", "--signing", "none", "--binary", DESKTOP / "zig-out/bin/jard-soundfolio-local", "--output", app, cwd=DESKTOP)
        assemble(app, updates, transforms)
        if dev_notes is not None:
            (app / "Contents/Resources" / DEV_NOTES_NAME).write_bytes(dev_notes)
            (stage / DEV_NOTES_NAME).write_bytes(dev_notes)
        if opts.publisher_identity:
            sign_with_publisher(app, opts.publisher_identity.expanduser().resolve(), release=bool(opts.update_key))
        else:
            sign(app, opts.identity)
        if opts.notary_profile:
            notarize(app, opts.notary_profile)
        if opts.update_key:
            with (app / "Contents/Info.plist").open("rb") as stream:
                version = plistlib.load(stream)["CFBundleVersion"]
            archive = stage / "Jaro-arm64.zip"
            # The installer expects exactly one top-level .app, including its symlinks and executable modes.
            run("ditto", "-c", "-k", "--norsrc", "--keepParent", app, archive)
            tag = f"dev-v{version}" if opts.channel == "dev" else f"v{version}"
            url = f"https://github.com/AdamGera/Jaro/releases/download/{tag}/{archive.name}"
            feed = stage / "native-update.json"
            release_manifest = DESKTOP / "app.json"
            if opts.channel == "dev":
                metadata = json.loads(release_manifest.read_bytes())
                metadata["updates"] = {key: updates["development"][key] for key in ("feed_url", "public_key")}
                release_manifest = stage / "release-manifest.json"
                release_manifest.write_text(json.dumps(metadata))
            run("native", "update", "sign", "--manifest", release_manifest,
                "--target", "macos-aarch64", "--archive", archive, "--url", url,
                "--notes", opts.notes, "--private-key", opts.update_key, "--output", feed)
            if release_settings.get("rollout_url"):
                sign_rollout_feed(feed, stage / ROLLOUT_FEED_NAME, opts.update_key, opts.channel,
                                  rollout_percent, opts.rollout_required)
        if opts.dmg or opts.notary_profile:
            image_root = stage / "image"
            image_root.mkdir()
            image_app = image_root / app.name
            app.rename(image_app)
            app = image_app
            (image_root / "Applications").symlink_to("/Applications")
            image = stage / "Jaro-arm64.dmg"
            run("hdiutil", "create", "-srcfolder", image_root, "-volname", "Jaro", "-format", "UDZO", image)
            if opts.identity != "-":
                run("codesign", "--sign", opts.identity, "--timestamp", image)
            if opts.notary_profile:
                notarize(image, opts.notary_profile)
            image.replace(OUT / image.name)
        output = OUT / app.name
        if output.exists():
            output.rename(stage / "previous.app")
        app.rename(output)
        if opts.update_key:
            archive.replace(OUT / archive.name)
            feed.replace(OUT / feed.name)
            rollout = stage / ROLLOUT_FEED_NAME
            if rollout.exists():
                rollout.replace(OUT / rollout.name)
            else:
                (OUT / ROLLOUT_FEED_NAME).unlink(missing_ok=True)
        if dev_notes is not None:
            (stage / DEV_NOTES_NAME).replace(OUT / DEV_NOTES_NAME)
        print(output)


if __name__ == "__main__":
    main()
