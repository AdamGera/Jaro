"""Stage the workspace shared by the Linux and Windows packages."""

import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def install(source, destination, mode=0o644):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)

def release_metadata(target, kind, version, revision=0):
    (target / "portable-release.json").write_text(json.dumps({
        "kind": kind, "version": version, "revision": revision,
    }), encoding="ascii")


def stage_workspace(target, dist):
    if not (dist / "index.html").is_file():
        raise ValueError("Build the web workspace with bun run build.")
    shutil.copytree(dist, target / "dist")
    engine_files = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "engine"], cwd=ROOT
    ).decode().strip("\0").split("\0")
    files = [
        Path("pyproject.toml"),
        Path("desktop/portable/open_workspace.py"),
        Path("desktop/portable/desktop_instance.py"),
        Path("desktop/build-tools/model_setup.py"),
        Path("desktop/build-tools/models.json"),
        *(Path(name) for name in engine_files if Path(name).suffix in (".py", ".json")),
    ]
    for path in files:
        if (ROOT / path).is_symlink():
            raise ValueError(f"Cannot package a linked source file: {path}")
        install(ROOT / path, target / path)
    for name in ("dictation_input.py", "stupase_runner.py", "portable_update.py", "linux_direct.py", "portable_dictation_sounds.py", "desktop_pen.py", "gnome_shortcuts.py", "linux_dictation.py", "linux_pen.py", "linux_screencast.py", "windows_screencast.py",
                 "screen_draft.py", "screencast_ink.py", "portable_audio_control.py", "linux_dictation_audio.py", "recording_power.py",
                 "portable_login.py", "linux_meeting_audio.py", "portable_calls.py", "portable_call_surface.py", "wayland_field.py",
                 "portable_call_pages.py", "portable_tray.py", "linux_tray.py", "desktop_clipboard.py"):
        install(ROOT / "engine" / name, target / "engine" / name)
    shutil.copytree(ROOT / "engine" / "stupase_model", target / "engine" / "stupase_model",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    cues = Path("desktop/assets/dictation-sounds")
    for clip in (ROOT / cues).rglob("*.wav"):
        install(clip, target / cues / clip.relative_to(ROOT / cues))
