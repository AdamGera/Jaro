"""Launch an atomic update fixture through the same macOS relaunch path as Jard."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


for mode in ("restart", "failed launch", "quit"):
    with tempfile.TemporaryDirectory(prefix="jard-relaunch-") as directory:
        folder = Path(directory).resolve()
        identity = "local.jardaudio.update-fixture." + uuid4().hex
        stage = folder / ".jard-update-fixture"
        stage.mkdir(mode=0o700)
        library = folder / "Library"
        library.mkdir()
        sentinel = library / "session.txt"
        sentinel.write_text("preserve this session")
        env = {**os.environ, "RUMBLE_DATA_DIR": str(library)}
        if mode == "quit":
            (folder / "quit-only").touch()

        def make_bundle(parent, version, *, fail=False):
            app = parent / "Fixture.app"
            exe = app / "Contents/MacOS/fixture"
            exe.parent.mkdir(parents=True)
            shutil.copyfile(sys.argv[1], exe)
            exe.chmod(0o755)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": identity, "CFBundleVersion": version, "CFBundlePackageType": "APPL",
                "CFBundleExecutable": "fixture", "LSUIElement": True, "FixtureFailsAtLaunch": fail,
                "JardUpdateStartupConfirmation": 2,
            }))
            subprocess.run(["codesign", "--force", "--sign", "-", "-r",
                            f'=designated => identifier "{identity}"', app],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
            return exe

        try:
            old = make_bundle(folder, "1.1.0")
            make_bundle(stage, "1.2.0", fail=mode == "failed launch")
            subprocess.run([old] + (["quit"] if mode == "quit" else []), check=True, timeout=10, env=env)
            marker = folder / "launched"
            if mode == "quit":
                time.sleep(.4)
                assert not marker.exists(), "Update on quit relaunched the app."
                subprocess.run([old], check=True, timeout=10, env=env)
            deadline = time.monotonic() + 20
            while (not marker.is_file() or stage.exists()) and time.monotonic() < deadline:
                time.sleep(.1)
            rolled_back = mode == "failed launch"
            assert marker.is_file(), f"{mode}: the app did not reopen"
            expected = "old version restored" if rolled_back else "new version launched; backup cleaned"
            assert marker.read_text() == expected, (mode, marker.read_text())
            assert not stage.exists(), f"{mode}: the retired stage still exists"
            installed = plistlib.loads((folder / "Fixture.app/Contents/Info.plist").read_bytes())
            assert installed["CFBundleVersion"] == ("1.1.0" if rolled_back else "1.2.0")
            assert sentinel.read_text() == "preserve this session"
            print(f"{mode}: reopened the expected version at the same path and preserved the library.")
        finally:
            subprocess.run(["defaults", "delete", identity], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
