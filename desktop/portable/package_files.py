"""Stage the workspace shared by the Linux and Windows packages."""

import json
from pathlib import Path
import shutil

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
    (target / "portable-startup-v1").touch()
    files = [
        Path("pyproject.toml"),
        Path("desktop/portable/open_workspace.py"),
        Path("desktop/portable/desktop_instance.py"),
        Path("desktop/build-tools/model_setup.py"),
        Path("desktop/build-tools/models.json"),
        Path("sdk/typescript/index.mjs"),
        Path("sdk/typescript/index.d.mts"),
        Path("sdk/typescript/README.md"),
        Path("skills/jaro-annotate/SKILL.md"),
    ]
    for path in files:
        if (ROOT / path).is_symlink():
            raise ValueError(f"Cannot package a linked source file: {path}")
        install(ROOT / path, target / path)
    engine_root = ROOT / "engine"
    for item in sorted(engine_root.iterdir()):
        if item.name == "__pycache__" or item.is_dir():
            continue
        if item.suffix not in (".py", ".json"):
            continue
        if item.is_symlink():
            raise ValueError(f"Cannot package a linked source file: {item.relative_to(ROOT)}")
        install(item, target / item.relative_to(ROOT))
    shutil.copytree(engine_root / "stupase_model", target / "engine" / "stupase_model",
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    cues = Path("desktop/assets/dictation-sounds")
    for clip in (ROOT / cues).rglob("*.wav"):
        install(clip, target / cues / clip.relative_to(ROOT / cues))
