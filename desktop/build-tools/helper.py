"""Resolve the signed bundle's private runtimes before starting the selected helper."""
import os
from pathlib import Path
import plistlib
import sys

from model_setup import directory

tools = Path(__file__).resolve().parent
root = tools.parent.parent
helpers = root.parent.parent / "Helpers"
os.environ.update({
    "JARD_ENGINE_ROOT": str(root),
    "JARD_MODEL_DIR": str(directory()),
    "JARD_EMBEDDED_HELPER": str(helpers / "jard-engine"),
    "JARD_LLAMA_SERVER": str(helpers / "llama/llama-server"),
    "PATH": f"{helpers}:/usr/bin:/bin:/usr/sbin:/sbin",
    "PYTHONDONTWRITEBYTECODE": "1",
})
action = sys.argv[1:]
if action[0] == "ui":
    script = root / "desktop/mac_bridge.py"
    args = action[1:]
elif action[0] == "control":
    with (root.parent.parent / "Info.plist").open("rb") as manifest:
        os.environ["JARD_DESKTOP_BUNDLE_ID"] = plistlib.load(manifest)["CFBundleIdentifier"]
    os.environ["JARD_DESKTOP_BUNDLE_PATH"] = str(root.parents[2])
    script = root / "jardctl.py"
    args = action[1:]
elif action[1:] == ["start"]:
    script = root / "server.py"
    args = ["--background", "--no-open"]
elif action[1:3] == ["service", "--embedded"]:
    script = root / "server.py"
    args = action[2:]
elif action[1:2] in (["models"], ["model-status"]):
    script = tools / "model_setup.py"
    args = action[1:]
else:
    sys.exit("Usage: jard-engine start | service --embedded | model-status | models [--from MODEL_FOLDER]")
if action[0] != "control":
    os.chdir(root)
os.execv(sys.executable, [sys.executable, "-EsBu", str(script), *args])
