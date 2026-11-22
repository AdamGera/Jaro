"""Exercise the packaged CLI, AppKit host, and isolated macOS preferences."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import select
import signal
import socket
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def process_ids(executable):
    for line in subprocess.check_output(["/bin/ps", "-axo", "pid=,comm="], text=True).splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[1] == str(executable):
            yield int(fields[0])


def wait_for(probe, message, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := probe():
            return result
        time.sleep(.15)
    raise AssertionError(message)


def wait_for_exit(pid, timeout=30):
    with contextlib.closing(select.kqueue()) as queue:
        event = select.kevent(pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)
        try:
            queue.control([event], 0, 0)
        except ProcessLookupError:
            return
        assert queue.control(None, 1, timeout), f"Fixture process {pid} did not exit."


def run(package, models, menu_host=None, *, clone_models=False):
    identifier = "local.jardaudio.cli-fixture." + uuid4().hex
    with tempfile.TemporaryDirectory(prefix="jard-lifecycle-") as temporary:
        folder = Path(temporary).resolve()
        app = folder / "Jard lifecycle fixture.app"
        subprocess.run(["/bin/cp", "-cR", package, app], check=True)
        manifest = app / "Contents/Info.plist"
        info = plistlib.loads(manifest.read_bytes())
        info.update(CFBundleIdentifier=identifier, CFBundleDisplayName="Jard lifecycle fixture")
        info["JardUpdates"]["check_on_start"] = False
        executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
        if menu_host:
            # Exercise the final refresh against an unavailable feed without reaching the production channel.
            info["JardUpdates"]["feed_url"] = "https://127.0.0.1:9/native-update.json"
            info["JardUpdates"].pop("rollout_url", None)
            executable.unlink()
            subprocess.run(["/bin/cp", "-p", menu_host, executable], check=True)
        manifest.unlink()
        manifest.write_bytes(plistlib.dumps(info))
        def sign(bundle):
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "-r",
                            f'=designated => identifier "{identifier}"', bundle], check=True)

        sign(app)
        helpers = app / "Contents/Helpers"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        root = folder / "library"
        base = f"http://127.0.0.1:{port}"
        library_id = hashlib.sha256(os.fsencode(root)).hexdigest()
        env = {**os.environ, "RUMBLE_PORT": str(port), "RUMBLE_DATA_DIR": str(root), "RUMBLE_LOG_DIR": str(folder / "logs")}

        def cli(*args, data=None, succeeds=True, extra_env=None):
            result = subprocess.run([helpers / "jardctl", "--json", *args], input=data, env={**env, **(extra_env or {})},
                                    text=True, capture_output=True, timeout=52)
            assert (result.returncode == 0) == succeeds, (args, result.stdout, result.stderr)
            return json.loads(result.stdout if succeeds else result.stderr)

        stage = folder / ".jard-update-fixture"

        def installed_info():
            return plistlib.loads(manifest.read_bytes())

        def stage_update(version):
            installed = installed_info()
            stage.mkdir(mode=0o700)
            candidate = stage / app.name
            subprocess.run(["/bin/cp", "-cR", app, candidate], check=True)
            candidate_manifest = candidate / "Contents/Info.plist"
            candidate_manifest.unlink()
            candidate_manifest.write_bytes(plistlib.dumps({**installed, "CFBundleVersion": version}))
            sign(candidate)
            subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", candidate], check=True)
            subprocess.run(["/usr/bin/defaults", "write", identifier, "JardPendingUpdate", "-dict",
                            "installed", str(app), "runningVersion", installed["CFBundleVersion"], "stage", str(stage),
                            "candidate", candidate.name, "version", version, "notes", "Fixture update"], check=True)

        def engine_status():
            with urlopen(base + "/api/status", timeout=2) as response:
                status = json.load(response)
            assert status["libraryId"] == library_id, status
            return status["status"]

        def wait_engine():
            def ready():
                with contextlib.suppress(URLError):
                    return engine_status() == "ready"
            wait_for(ready, "The fixture engine did not become ready.")

        def wait_staged_update():
            wait_for(lambda: cli("update-channel")["staged"], "The signed stage did not restore in the background.")

        def invoke_fixture(action, title):
            receipt = root / ".update-menu-invoked"
            receipt.unlink(missing_ok=True)
            (root / ".update-menu-fixture").write_text(action)
            wait_for(receipt.is_file, f"The native fixture did not perform {action}.")
            assert receipt.read_text() == title

        def change_lease(url, method):
            with urlopen(Request(url, method=method, headers={"Origin": base}), timeout=2) as response:
                assert response.status == 204

        if clone_models:
            model_manifest = (app / "Contents/Resources/engine/desktop/build-tools/models.json").read_bytes()
            model_root = root / "models" / hashlib.sha256(model_manifest).hexdigest()[:16]
            for item in json.loads(model_manifest):
                name = item["file"]
                output = model_root / name
                output.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["/bin/cp", "-c", models / name, output], check=True)
        subprocess.run([helpers / "jard-engine", "models", "--from", models], env=env,
                       check=True, timeout=40, stdout=subprocess.DEVNULL)
        preference_lock = Path.home() / "Library" / "Preferences" / f".{identifier}.jard-settings.lock"
        try:
            subprocess.run(["/usr/bin/defaults", "write", identifier, "JardCLIFixtureSentinel", "-string", "unchanged"], check=True)
            offline = {"dictation.mode": "hold", "dictation.retention": 1, "dictation.languages": ["en"],
                       "dictation.audioWhileDictating": "lower", "dictation.lowerAudioPercent": 35,
                       "capture.enabled": False, "capture.hotkey": "f9", "capture.show_overlays_in_recordings": True}
            assert cli("settings", "apply", "-", data=json.dumps(offline)) == offline
            assert not (root / ".native-control").exists(), "Offline settings launched a desktop process."
            conflict = {"capture.enabled": True, "capture.hotkey": "option+shift+v", "dictation.mode": "tap"}
            assert "already in use" in cli("settings", "apply", "-", data=json.dumps(conflict), succeeds=False)["error"]
            assert "Invalid" in cli("settings", "apply", "-", data='{"dictation.mode":"tap","dictation.retention":5}',
                                    succeeds=False)["error"]
            assert "supported" in cli("settings", "apply", "-", data='{"dictation.languages":["invalid"]}',
                                      succeeds=False)["error"]
            assert "Invalid" in cli("settings", "apply", "-", data='{"dictation.lowerAudioPercent":20.5}',
                                    succeeds=False)["error"]
            with preference_lock.open("rb+") as competing_writer:
                fcntl.flock(competing_writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = cli("settings", "apply", "-", data='{"dictation.mode":"tap"}', succeeds=False)
                assert "Close Jard" in locked["error"], locked
            preferences = plistlib.loads(subprocess.check_output(["/usr/bin/defaults", "export", identifier, "-"]))
            assert preferences["JardCLIFixtureSentinel"] == "unchanged"
            assert preferences["JardSystemDictation"]["mode"] == "hold"
            assert preferences["JardSystemDictation"]["previousRetention"] == 1
            assert preferences["JardSystemDictation"]["annotationEnabled"] is False
            assert preferences["JardSystemDictation"]["annotateLabel"] == "F9"
            original = {**preferences["JardSystemDictation"], "soundVolume": ["invalid"],
                        "futurePreference": {"enabled": True}}
            preferences["JardSystemDictation"] = original
            subprocess.run(["/usr/bin/defaults", "import", identifier, "-"], input=plistlib.dumps(preferences), check=True)
            assert cli("settings", "apply", "-", data='{"dictation.fillerWords":true}') == {"dictation.fillerWords": True}
            repaired = plistlib.loads(subprocess.check_output(["/usr/bin/defaults", "export", identifier, "-"]))
            assert repaired["JardSystemDictation"]["soundVolume"] == 70
            assert repaired["JardSystemDictation"]["fillerWords"] is True
            assert repaired["JardSystemDictation"]["futurePreference"] == {"enabled": True}
            assert repaired["JardCLIFixtureSentinel"] == "unchanged"
            recoveries = [plistlib.loads(data) for name, data in repaired.items()
                          if name.startswith("JardSystemDictationRecovery-")]
            assert recoveries == [original]
            preferences = {**repaired, "JardSystemDictation": ["unreadable root"]}
            subprocess.run(["/usr/bin/defaults", "import", identifier, "-"], input=plistlib.dumps(preferences), check=True)
            assert "existing value was preserved" in cli("settings", "apply", "-", data='{"dictation.sound":true}',
                                                         succeeds=False)["error"]
            blocked = plistlib.loads(subprocess.check_output(["/usr/bin/defaults", "export", identifier, "-"]))
            assert blocked == preferences
            subprocess.run(["/usr/bin/defaults", "import", identifier, "-"], input=plistlib.dumps(repaired), check=True)
            print("Offline writes repaired known fields, kept the original and future keys, and refused an unreadable root.", flush=True)

            started = cli("start", "--hidden")
            assert started["status"] == "running" and started["bundle"] == str(app), started
            assert not started["windowVisible"] and not started["dockIconVisible"], started
            still_running = cli("start", "--hidden")
            assert still_running["changed"] is False and still_running["pid"] == started["pid"], still_running
            wait_engine()
            assert cli("status")["desktop"]["lastFailure"] == ""
            capture_preferences = {"capture.enabled": False, "capture.hotkey": "f9", "capture.show_overlays_in_recordings": True}
            assert cli("settings", "get", "capture.") == capture_preferences
            refused = cli("annotate", "start", "--source", "display", succeeds=False)
            assert "turned off" in refused["error"], refused
            assert "already in use" in cli("settings", "apply", "-", data=json.dumps(conflict), succeeds=False)["error"]
            assert cli("settings", "get", "dictation.mode") == {"dictation.mode": "hold"}
            assert cli("settings", "get", "capture.") == capture_preferences
            for invalid in ("k", "option++k", "option+option+k", "fn+k", "shift+keycode-53"):
                assert "Invalid Annotate shortcut" in cli("settings", "set", "capture.hotkey", invalid, succeeds=False)["error"]
            assert cli("settings", "set", "capture.hotkey", "ctrl + shift + k") == {"capture.hotkey": "ctrl + shift + k"}
            assert cli("settings", "get", "capture.hotkey") == {"capture.hotkey": "control+shift+k"}
            assert cli("settings", "set", "capture.enabled", "on") == {"capture.enabled": True}
            assert cli("settings", "set", "capture.show_overlays_in_recordings", "off") == {"capture.show_overlays_in_recordings": False}
            assert "There is no active take" in cli("dictation", "stop", "--print", succeeds=False)["error"]
            annotation = cli("annotate", "status")
            assert annotation["phase"] == "idle" and annotation["capture"] is None and annotation["last"] is None
            assert cli("annotate", "stop") == annotation == cli("capture", "cancel")
            if os.environ.get("JARD_TEST_NO_WIRED_PHONE") == "1":
                refusal = cli("annotate", "start", "--source", "phone", succeeds=False)
                assert "Connect an unlocked iPhone or iPad by USB" in refusal["error"], refusal
                annotation = cli("annotate", "status")
                assert annotation["busy"] is False and annotation["last"]["source"] == "phone", annotation
                assert annotation["last"]["phase"] == "error", annotation
            assert cli("settings", "get", "dictation.mode") == {"dictation.mode": "hold"}
            assert cli("settings", "set", "dictation.vocabulary", "Jaro") == {"dictation.vocabulary": "Jaro"}
            added = cli("vocabulary", "add", "Mei Lin", "--sounds-like", "May Lean,May Linn")
            assert added["changed"] and added["entries"][-1] == {"term": "Mei Lin", "soundsLike": ["May Lean", "May Linn"]}
            assert not cli("vocabulary", "add", "MEI LIN", "--sounds-like", "MAY LEAN")["changed"]
            assert cli("vocabulary", "list", "--json") == {"entries": added["entries"]}
            assert "eight alternate spellings" in cli("vocabulary", "add", "Test", "--sounds-like",
                                                        ",".join(["extra"] * 9), succeeds=False)["error"]
            mismatch = cli("settings", "apply", "-", data='{"dictation.mode":"tap"}', succeeds=False,
                           extra_env={"RUMBLE_DATA_DIR": str(folder / "different-library")})
            assert "Close Jard" in mismatch["error"], mismatch
            supported = cli("settings", "list")
            assert supported["capture.enabled"] == {"type": "boolean", "readOnly": False}
            assert supported["capture.hotkey"] == {"type": "string", "readOnly": False}
            assert supported["capture.show_overlays_in_recordings"] == {"type": "boolean", "readOnly": False}
            assert supported["dictation.shortcutLabel"]["readOnly"] is True
            assert supported["dictation.mode"]["choices"] == ["hold", "holdDouble", "tap", "doubleTap"]
            assert cli("settings", "get", "dictation.lowerAudioPercent") == {"dictation.lowerAudioPercent": 35}
            assert cli("settings", "set", "dictation.audioWhileDictating", "pause") == {"dictation.audioWhileDictating": "pause"}
            assert cli("settings", "get", "dictation.sou")["dictation.sound"] is True
            assert cli("settings", "set", "dictation.sound", "off") == {"dictation.sound": False}
            desired = {"dictation.mode": "tap", "dictation.languages": ["en", "he"], "dictation.rewriteInstructions": "Preserve names."}
            assert cli("settings", "apply", "-", data=json.dumps(desired)) == desired
            assert "Invalid" in cli("settings", "apply", "-", data=json.dumps({
                "dictation.mode": "hold", "dictation.retention": 5,
            }), succeeds=False)["error"]
            assert cli("settings", "get", "dictation.mode") == {"dictation.mode": "tap"}
            assert "read-only" in cli("settings", "set", "dictation.shortcutLabel", "Other", succeeds=False)["error"]
            assert "supported" in cli("settings", "apply", "-", data='{"dictation.languages":["invalid"]}', succeeds=False)["error"]
            assert "Repeated" in cli("settings", "apply", "-", data='{"dictation.mode":"hold","dictation.mode":"tap"}', succeeds=False)["error"]
            assert cli("launch-at-login") == {"enabled": False}
            restarted = cli("restart", "--force")
            assert restarted["status"] == "running" and restarted["pid"] != started["pid"], restarted
            assert restarted["previousPid"] == started["pid"] and restarted["bundle"] == str(app), restarted
            assert cli("vocabulary", "list")["entries"] == added["entries"]
            assert cli("vocabulary", "remove", "mei lin") == {"changed": True, "entries": [{"term": "Jaro", "soundsLike": []}]}
            assert not cli("vocabulary", "remove", "mei lin")["changed"]
            saved = cli("settings", "get", "dictation.")
            assert {key: saved[key] for key in desired} == desired and saved["dictation.sound"] is False
            assert saved["dictation.audioWhileDictating"] == "pause" and saved["dictation.lowerAudioPercent"] == 35
            assert cli("settings", "get", "capture.") == {"capture.enabled": True, "capture.hotkey": "control+shift+k",
                                                        "capture.show_overlays_in_recordings": False}
            if menu_host:
                wait_engine()
                invoke_fixture("setup-reopen", "Reopen Jard")
                wait_for_exit(restarted["pid"])
                new = wait_for(lambda: next(iter(process_ids(executable)), None), "Setup did not relaunch the AppKit host.")
                wait_engine()
                resumed = wait_for(lambda: cli("status").get("desktop"), "The automatically reopened host did not answer.")
                assert resumed["pid"] == new and resumed["bundle"] == str(app), resumed
                invoke_fixture("setup-status", "access")
                invoke_fixture("setup-dismiss", "later")
                print("Reopen Jard resumed setup through the native WebView with the same app path and library.", flush=True)
            assert cli("quit")["status"] == "stopped"

            next_version = "99.0.0"
            stage_update(next_version)
            staged = cli("start")
            wait_staged_update()
            wait_engine()
            if menu_host:
                lease = base + f"/api/clients/{uuid4()}"
                change_lease(lease, "POST")
                invoke_fixture("quit", f"Quit {info['CFBundleDisplayName']}")
                wait_for_exit(staged["pid"])
                assert engine_status() == "ready", "Quit interrupted the engine with another connected client."
                assert stage.is_dir() and installed_info()["CFBundleVersion"] == info["CFBundleVersion"]
                change_lease(lease, "DELETE")
                staged = cli("start")
                wait_staged_update()
                invoke_fixture("quit", f"Quit {info['CFBundleDisplayName']}")
                wait_for_exit(staged["pid"])
            else:
                stopped = cli("quit", "--force")
                assert stopped["status"] == "stopped" and stopped["updateApplied"] is True, stopped
            assert "before using desktop controls" in cli("settings", "list", succeeds=False)["error"]
            assert installed_info()["CFBundleVersion"] == next_version
            updated = cli("start")
            assert updated["changed"] and updated["status"] == "running" and updated["version"] == next_version, updated
            wait_for(lambda: not stage.exists(), "Launch did not remove the previous update backup.")
            assert cli("quit")["status"] == "stopped"
            if menu_host:
                print("AppKit Quit retained the stage and engine while another client was connected, then applied it on an idle quit.", flush=True)
                stage_update("99.0.1")
                old = cli("start")
                wait_staged_update()
                wait_engine()
                browser = base + f"/api/clients/{uuid4()}/stream"
                with urlopen(browser, timeout=5) as stream:
                    assert stream.readline() == b": Jaro connected\n"
                    invoke_fixture("restart-guard", "Close other Jard windows or browser tabs before restarting.")
                    assert old["pid"] in process_ids(executable) and stage.is_dir()
                # Drop the connection without a browser DELETE and retry from the app menu.
                invoke_fixture("restart", "Restart to Update")
                wait_for_exit(old["pid"])
                new = wait_for(lambda: next((pid for pid in process_ids(executable) if pid != old["pid"]), None),
                               "Restart to Update did not relaunch the AppKit host.")
                wait_engine()
                updated = cli("start")
                assert not updated["changed"] and updated["pid"] == new and updated["bundle"] == str(app), updated
                assert updated["version"] == "99.0.1", updated
                wait_for(lambda: not stage.exists(), "Relaunch did not remove the previous update backup.")
                assert cli("quit")["status"] == "stopped"
                print("Restart to Update protected the connected browser, then replaced and reopened the same app and library after disconnect.", flush=True)
                stage_update("99.0.2")
                superseded = cli("start")
                wait_staged_update()
                wait_engine()
                invoke_fixture("restart-then-quit", "Restart to Update")
                wait_for_exit(superseded["pid"])
                assert installed_info()["CFBundleVersion"] == "99.0.2"
                time.sleep(1)
                assert not list(process_ids(executable)), "Quit relaunched the pending update."
                print("AppKit Quit won over a pending final check, applied the verified update, and stayed closed.", flush=True)
                assert cli("start")["version"] == "99.0.2"
                wait_for(lambda: not stage.exists(), "Launch did not remove the previous update backup.")
                assert cli("quit")["status"] == "stopped"
                stage_update("99.0.3")
                system_quit = cli("start")
                wait_staged_update()
                wait_engine()
                os.kill(system_quit["pid"], signal.SIGTERM)
                wait_for_exit(system_quit["pid"])
                assert installed_info()["CFBundleVersion"] == "99.0.3"
                time.sleep(1)
                assert not list(process_ids(executable)), "System termination relaunched the staged update."
                print("The AppKit termination request installed the verified update on Quit without relaunching.", flush=True)
                assert cli("start")["version"] == "99.0.3"
                wait_for(lambda: not stage.exists(), "Launch did not remove the previous update backup.")
                assert cli("quit")["status"] == "stopped"
                for action, version in (("sidebar-restart", "99.0.4"), ("settings-restart", "99.0.5")):
                    stage_update(version)
                    previous = cli("start")
                    wait_staged_update()
                    wait_engine()
                    invoke_fixture(action, "Restart to Update")
                    wait_for_exit(previous["pid"])
                    new = wait_for(lambda: next(iter(process_ids(executable)), None),
                                   "The workspace control did not relaunch the same app.")
                    wait_engine()
                    current = cli("start")
                    assert not current["changed"] and current["pid"] == new and current["bundle"] == str(app), current
                    assert current["version"] == version, current
                    wait_for(lambda: not stage.exists(), "Launch did not remove the previous update backup.")
                    assert cli("quit")["status"] == "stopped"
                print("Both real workspace buttons installed verified updates and reopened the same app and library.", flush=True)
            print("Offline settings stayed isolated, locked writers were rejected, and desktop updates preserved the app path.")
        finally:
            # Terminate only the fixture bundle even when an assertion interrupts it.
            for pid in process_ids(executable):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
                try:
                    wait_for_exit(pid, timeout=5)
                except AssertionError:
                    if pid in process_ids(executable):
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)
                        wait_for_exit(pid, timeout=5)
            with contextlib.suppress(URLError):
                with urlopen(base + "/api/status", timeout=2) as response:
                    ours = json.load(response)["libraryId"] == library_id
                if ours:
                    with urlopen(Request(base + "/api/quit", data=b"", headers={"Origin": base, "X-Jard-Action": "quit"}), timeout=2):
                        pass
            for _ in range(20):
                with socket.socket() as listener:
                    if listener.connect_ex(("127.0.0.1", port)) != 0:
                        break
                time.sleep(.1)
            subprocess.run(["/usr/bin/defaults", "delete", identifier], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            preference_lock.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("models", type=Path)
    parser.add_argument("--menu-host", type=Path, help="QA host built with JARD_TEST_SOURCE=desktop/tests/update_menu.m")
    parser.add_argument("--clone-models", action="store_true", help="Seed from APFS clones before the installer verifies the files.")
    args = parser.parse_args()
    run(args.package.resolve(), args.models.resolve(), args.menu_host.resolve() if args.menu_host else None,
        clone_models=args.clone_models)
