"""Locations shared by source runs and the packaged engine."""
import os
from pathlib import Path
import sys

ENGINE_ROOT = Path(__file__).resolve().parent
# The signed bundle includes engine files but omits the checkout manifest.
WORKSPACE_ROOT = ENGINE_ROOT.parent if (ENGINE_ROOT.parent / "pyproject.toml").is_file() else ENGINE_ROOT


def library_path():
    if configured := os.environ.get("RUMBLE_DATA_DIR"):
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support/Rumble").resolve()
    if os.name == "nt":
        default = Path.home() / "AppData/Local"
        base = Path(os.environ.get("LOCALAPPDATA", default))
        return ((base if base.is_absolute() else default) / "Jaro").resolve()
    default = Path.home() / ".local/share"
    base = Path(os.environ.get("XDG_DATA_HOME", default))
    return ((base if base.is_absolute() else default) / "jaro").resolve()
