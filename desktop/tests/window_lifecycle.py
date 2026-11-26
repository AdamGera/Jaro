"""Exercise Jard's startup and activation handlers in an isolated AppKit bundle."""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
from uuid import uuid4


SETUP = Path(__file__).resolve().parents[1] / "setup.html"
INTERFACE_LANGUAGES = SETUP.with_name("interface-languages.json")
SETTINGS_PAGE = b"""<!doctype html><script>
window.settingsEvents = [];
window.loadCount = FIXTURE_LOAD_COUNT;
const lastRoute = localStorage.getItem('fixture-last-session');
if (lastRoute && !location.hash) history.replaceState(null, '', lastRoute);
for (const event of ['jard-interface-language', 'jard-dock-icon']) {
    window.addEventListener('native-sdk:' + event, ({detail}) => {
        window.settingsEvents.push(event === 'jard-interface-language'
            ? {language: detail.selected, resolved: detail.resolved} : {dock: detail.enabled});
    });
}
</script><title>Application settings fixture</title>"""
HELPER = r"""#!/usr/bin/env python3
import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    response = {"id": request["id"], "ok": True, "result": {}}
    if request["command"] == "jard.prepare":
        outcome = os.environ["JARD_WINDOW_FIXTURE_PREPARE"]
        if outcome == "ready":
            print(json.dumps({"navigate": "app", "workspaceKey": "a" * 64}), flush=True)
            response["result"] = {"ready": True}
        elif outcome == "missing":
            response["result"] = {"ready": False}
        else:
            response.update(ok=False, error={"message": "Fixture engine failed to start."})
    elif request["command"] == "jard.latestMeeting" and os.environ["JARD_WINDOW_FIXTURE_SCENARIO"] == "normal":
        response["result"] = {"session": "3196de0c-cd84-4211-9367-98b5d859c20a"}
    elif request["command"] == "jard.latestMeetingTranscript":
        response["result"] = {"text": "**00:07 · Them**  \nShip the release."}
    print(json.dumps({"response": response}), flush=True)
"""


def wait_for(probe, message):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if result := probe():
            return result
        time.sleep(.03)
    raise AssertionError(message)


def read(path):
    return json.loads(path.read_text()) if path.exists() else None


class SettingsPage(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/_desktop/connect":
            self.send_response(302)
            self.send_header("Location", "/#")
            self.send_header("Set-Cookie", "fixture_workspace=ready; HttpOnly; Path=/; SameSite=Strict")
            self.end_headers()
            return
        if self.path != "/":
            self.send_error(404)
            return
        if "fixture_workspace=ready" not in self.headers.get("Cookie", "").split("; "):
            self.send_error(401)
            return
        loads = getattr(self.server, "workspace_loads", 0) + 1
        self.server.workspace_loads = loads
        page = SETTINGS_PAGE.replace(b"FIXTURE_LOAD_COUNT", str(loads).encode())
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


def offline_preferences(target, changes, *, succeeds):
    process = subprocess.run([target, "--settings-apply-offline"], input=json.dumps(changes),
                             text=True, capture_output=True, timeout=10)
    result = json.loads(process.stdout)
    if succeeds:
        assert not process.returncode and result == changes, result
    else:
        assert process.returncode and result.get("error"), result
    return result


def verify_application_settings(root, invoke, target):
    def settings(action, **payload):
        (root / "settings.json").write_text(json.dumps({"action": "settings-" + action, **payload}))
        return invoke("application-settings")

    wait_for(lambda: invoke("settings-events")["events"] is not None, "The settings event listener did not load.")
    initial = settings("list")
    catalog = initial["result"]
    assert {"system", "fr", "zh-Hant"} <= set(catalog["ui_language"]["choices"])
    assert catalog["show_dock_icon"]["type"] == "boolean"
    assert initial["language"] == "fr" and initial["frenchMenu"] and initial["dock"]["enabled"], initial
    assert settings("get", key="dictation.languages")["result"] == {"dictation.languages": ["es"]}
    assert "Close Jard" in offline_preferences(target, {"ui_language": "ja"}, succeeds=False)["error"]

    rejected = settings("apply", changes={"ui_language": "ja", "show_dock_icon": 0, "dictation.languages": ["de"]})
    assert rejected["error"] and rejected["language"] == "fr" and rejected["dock"]["enabled"], rejected
    assert settings("get", key="dictation.languages")["result"] == {"dictation.languages": ["es"]}

    applied = settings("apply", changes={"ui_language": "ZH-hant", "show_dock_icon": False, "dictation.languages": ["de"]})
    assert applied["error"] is None and applied["language"] == "zh-Hant" and not applied["dock"]["enabled"], applied
    wait_for(lambda: {"language": "zh-Hant", "resolved": "zh-Hant"} in invoke("settings-events")["events"],
             "The workspace did not receive the selected language.")
    wait_for(lambda: {"dock": False} in invoke("settings-events")["events"],
             "The workspace did not receive the Dock preference.")

    system = settings("apply", changes={"ui_language": None})["resolved"]
    wait_for(lambda: {"language": "system", "resolved": system} in invoke("settings-events")["events"],
             "The workspace did not receive the System selection.")
    same_language = settings("apply", changes={"ui_language": system})
    assert same_language["language"] == same_language["resolved"] == system, same_language
    wait_for(lambda: {"language": system, "resolved": system} in invoke("settings-events")["events"],
             "The workspace missed a selection change with the same resolved language.")
    assert settings("get", key="ui_lang")["result"] == {"ui_language": system}
    assert settings("get", key="show_dock_icon")["result"] == {"show_dock_icon": False}
    assert settings("get", key="dictation.languages")["result"] == {"dictation.languages": ["de"]}
    hidden = invoke("close")
    assert not hidden["visible"] and not hidden["dockVisible"] and hidden["quitAvailable"], hidden


def run_case(executable, scenario, prepare):
    with tempfile.TemporaryDirectory(prefix="jard-window-") as folder, ExitStack() as resources_to_close:
        root = Path(folder)
        identifier = "local.jardaudio.window-fixture." + uuid4().hex
        contents = root / "Jard Window Fixture.app/Contents"
        resources = contents / "Resources"
        resources.mkdir(parents=True)
        shutil.copyfile(INTERFACE_LANGUAGES, resources / INTERFACE_LANGUAGES.name)
        target = contents / "MacOS/jard-window-fixture"
        target.parent.mkdir()
        shutil.copy2(executable, target)
        info = {
            "CFBundleIdentifier": identifier, "CFBundleExecutable": target.name,
            "CFBundleDisplayName": "Jard Window Fixture", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1", "NSMicrophoneUsageDescription": "Fixture microphone status only.",
        }
        if scenario in {"dock-updates", "managed-updates", "launch-update", "focus-update", "update-restart", "quit-capture"}:
            info["JardUpdates"] = {"feed_url": "https://127.0.0.1:9/update.json",
                                   "check_on_start": scenario in {"managed-updates", "launch-update"},
                                   "public_key": "dxMjVxu9nD8oMW/Cx+DNTJ2sYv8JS8z95lJkz63hBMM="}
        if scenario == "focus-update":
            info["JardUpdates"].update(channel="dev", development={
                "feed_url": "https://127.0.0.1:9/development.json",
                "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            })
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        helper = root / "prepare-helper"
        helper.write_text(HELPER)
        helper.chmod(0o700)
        if scenario in {"application-settings", "web-recovery"}:
            server = resources_to_close.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), SettingsPage))
            Thread(target=server.serve_forever, daemon=True).start()
            resources_to_close.callback(server.shutdown)
            port = server.server_port
        else:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        env = {
            **os.environ, "JARD_WINDOW_FIXTURE_ROOT": str(root), "JARD_WINDOW_FIXTURE_SCENARIO": scenario,
            "JARD_WINDOW_FIXTURE_PREPARE": prepare, "JARD_TEST_HELPER": str(helper), "JARD_TEST_SETUP": str(SETUP),
            "RUMBLE_DATA_DIR": str(root / "library"), "RUMBLE_PORT": str(port),
        }
        subprocess.run(["codesign", "--force", "--sign", "-", str(contents.parent)], check=True, capture_output=True)
        subprocess.run(["defaults", "write", identifier, "JardSystemDictation", "-dict",
                        "enabled", "-bool", "NO" if scenario in {"dock-setting", "dock-unavailable"} else "YES",
                        "onboarding", "welcome" if scenario == "login-setup" else "done"],
                       check=True)
        if scenario in {"dock-login", "dock-unavailable", "dock-updates"} or scenario.startswith("dock-cold-"):
            subprocess.run(["defaults", "write", identifier, "JardShowDockIcon", "-bool", "NO"], check=True)
        if scenario == "managed-updates":
            subprocess.run(["defaults", "write", identifier, "DisableUpdates", "-bool", "YES"], check=True)
        if scenario == "locale-preselected":
            subprocess.run(["defaults", "write", identifier, "JardInterfaceLanguage", "-string", "fr"], check=True)
        if scenario == "application-settings":
            offline_preferences(target, {"ui_language": "fr", "show_dock_icon": True,
                                        "dictation.languages": ["es"]}, succeeds=True)
            offline_preferences(target, {"ui_language": "invalid", "show_dock_icon": False,
                                        "dictation.languages": ["de"]}, succeeds=False)
        launch_args = [target, "--hidden"] if scenario.startswith("hidden-") else [target]
        try:
            start = time.monotonic()
            with (root / "host.log").open("wb") as log, subprocess.Popen(launch_args, env=env, stderr=log) as process:
                def invoke(command):
                    reply = root / "reply.json"
                    reply.unlink(missing_ok=True)
                    staging = root / "next-command"
                    staging.write_text(command)
                    staging.replace(root / "command")
                    return wait_for(lambda: read(reply), f"{scenario}: AppKit did not acknowledge {command}.")

                try:
                    initial = wait_for(lambda: read(root / "launched.json"), f"{scenario}: AppKit did not launch.")
                    assert initial == {"visibleBeforeLaunch": False,
                                       "visible": scenario in {"normal", "annotation-managed", "managed-updates", "login-setup", "dock-setting",
                                                               "dock-unavailable", "hidden-unavailable", "locale-preselected",
                                                               "launch-update", "focus-update", "update-restart", "quit-capture", "application-settings", "web-recovery"}}, initial
                    if prepare == "ready":
                        wait_for(lambda: invoke("status")["ready"], "The hidden workspace did not finish preparing.")
                    elif prepare == "missing":
                        title = "Configurer sur ce Mac" if scenario == "locale-preselected" else "Set up on this Mac"
                        wait_for(lambda: invoke("setup-title")["title"] == title, "Model setup did not open.")
                        assert invoke("status")["visible"] == (scenario != "hidden-missing")
                    elif prepare == "error":
                        wait_for(lambda: invoke("setup-title")["title"] == "Couldn’t open the workspace", "Engine error did not open.")
                        assert invoke("status")["visible"]

                    if scenario == "web-recovery":
                        wait_for(lambda: invoke("web-recovery-state").get("loads") == 1, "Fixture workspace did not load.")
                        assert invoke("web-recovery-prepare")["ok"]
                        assert invoke("web-recovery-kill")["ok"]
                        recovered = wait_for(lambda: (state if (state := invoke("web-recovery-state")).get("loads") == 2 else None),
                                             "The workspace did not reload after WebKit exited.")
                        assert recovered["route"] == "#fixture-notes" and recovered["recording"], recovered
                        assert recovered["helperRunning"] and not recovered["sheet"], recovered
                        assert invoke("web-recovery-kill")["ok"]
                        second = wait_for(lambda: (state if (state := invoke("web-recovery-state"))["sheet"] else None),
                                          "The second WebKit exit did not offer recovery.")
                        assert second["recording"] and second["helperRunning"] and second.get("loads", 0) == 0, second
                        assert invoke("web-recovery-retry")["ok"]
                        retried = wait_for(lambda: (state if (state := invoke("web-recovery-state")).get("loads") == 3 else None),
                                           "Manual retry did not reload the workspace.")
                        assert retried["route"] == "#fixture-notes" and retried["recording"] and retried["helperRunning"], retried
                        invoke("web-recovery-finish")
                    elif scenario == "normal":
                        assert invoke("input-device-events") == {"events": 1, "registered": True}
                        vocabulary = invoke("vocabulary-menu")
                        assert vocabulary == {
                            "initiallyHidden": True, "emptyCount": 2, "recent": [f"Name {i}" for i in range(32, 27, -1)],
                            "full": True, "opened": "vocabulary", "added": "add-term", "addEnabled": True, "hidden": True,
                        }, vocabulary
                        paste_menu = invoke("paste-menu")
                        assert paste_menu == {"title": "Paste Last Dictation", "shortcut": "v",
                            "shown": True, "enabled": False, "dockVisible": True}, paste_menu
                        policy = invoke("annotation-policy")
                        assert policy["shortcutRegistered"] and not policy["state"]["managed"], policy
                        assert policy["permissionFailures"] == dict.fromkeys(
                            ("allow", "repair", "restart"), "Finish the current recording or update first."), policy
                        toggle = invoke("annotation-toggle")
                        assert toggle["off"] is False and toggle["status"] is False and toggle["on"] is True, toggle
                        assert toggle["start"] == "Annotate is turned off in Settings.", toggle
                        assert toggle["toggleDisabled"] == toggle["start"], toggle
                        assert toggle["togglePicker"] is True and toggle["toggleRecording"] is True, toggle
                        shared = invoke("annotation-shared-meeting")
                        assert shared == {"pickerBlocked": True, "sharedReady": True}, shared
                        concurrent = invoke("annotation-start-over-shared-microphone")
                        assert concurrent == dict.fromkeys(("sharedMeetingAllowed", "otherMeetingBlocked", "sharedDictationAllowed",
                                                           "otherDictationBlocked", "pickerPreserved"), True), concurrent
                        live_meeting = invoke("menu-live-meeting")
                        assert live_meeting == {"idleFailure": "There is no live meeting transcript.", "pane": "live-meeting",
                                                "recording": True, "visible": True}, live_meeting
                        copy = invoke("menu-copy-latest")
                        assert copy == {"available": True, "title": "Copy Last Meeting Transcript"}, copy
                        result = wait_for(lambda: read(root / "latest-meeting-copy.json"), "The menu did not copy the saved transcript.")
                        assert result == {"text": "**00:07 · Them**  \nShip the release.", "error": "", "copied": True}, result
                        assert invoke("menu-copy-label-fr") == {"title": "Copier la transcription de la dernière réunion"}
                    elif scenario == "annotation-managed":
                        policy = invoke("annotation-policy")
                        message = "Screen annotation is managed by your organization."
                        assert policy["state"]["managed"] and policy["state"]["phase"] == "idle", policy
                        assert policy["state"]["message"] == message and not policy["state"]["busy"], policy
                        assert not policy["shortcutRegistered"] and not policy["canStart"], policy
                        assert policy["startFailures"] == dict.fromkeys(("display", "window", "area", "phone"), message), policy
                        assert policy["permissionFailures"] == dict.fromkeys(("status", "allow", "repair", "restart"), message), policy
                    elif scenario == "managed-updates":
                        policy = invoke("update-policy")
                        message = "Updates managed by your organization"
                        assert policy["update"] == {"phase": "unavailable", "updatesDisabled": True,
                                                    "manualCheck": False, "currentVersion": "1"}, policy
                        assert not policy["updaterCreated"] and policy["visibleActions"] == 0, policy
                        assert policy["statusMenuShown"] and not policy["statusMenuEnabled"], policy
                        assert policy["statusMenuTitle"] == message, policy
                        assert policy["failures"] == dict.fromkeys(
                            ("check", "channel", "confirmSwitch", "restart", "download", "releaseNotes"), message), policy
                    elif scenario == "login":
                        assert not invoke("status")["visible"]
                        assert invoke("dock")["visible"]
                        assert not invoke("close")["visible"]
                        assert invoke("activate")["visible"]
                        assert invoke("overlay")["overlayFocused"]
                        assert not invoke("close")["visible"]
                        assert not invoke("activate")["visible"]
                        invoke("dismiss-overlay")
                        assert invoke("activate")["visible"]
                        invoke("minimize")
                        wait_for(lambda: invoke("status")["minimized"], "AppKit did not finish minimizing the window.")
                        assert invoke("activate")["minimized"]
                    elif scenario == "login-idle":
                        # When the system sends no startup activation, the later switch must still work.
                        time.sleep(1.05)
                        assert not invoke("status")["visible"]
                        assert invoke("activate")["visible"]
                    elif scenario == "login-input":
                        assert invoke("status")["visible"]
                    elif scenario == "dock-setting":
                        assert invoke("status")["dockSetting"]["enabled"]
                        changed = invoke("dock-off")
                        assert changed["dockVisible"] and changed["quitAvailable"]
                        assert changed["dockSetting"] == {"enabled": False, "menuBarAvailable": True}
                        assert invoke("record-menu") == {"shown": True, "enabled": True}
                        assert invoke("preview-active")["soundPreview"]
                        invoke("minimize")
                        minimized = wait_for(lambda: (state if (state := invoke("status"))["minimized"] else None),
                                             "The Dock preference did not preserve minimization.")
                        assert minimized["dockVisible"] and not minimized["soundPreview"]
                        already_hidden = invoke("cli-hide")
                        assert already_hidden["result"] == {"windowVisible": False, "changed": False}, already_hidden
                        assert already_hidden["minimized"] and not already_hidden["visible"], already_hidden
                        invoke("cli-show")
                        wait_for(lambda: invoke("status")["visible"], "The CLI did not reopen the minimized workspace.")
                        assert invoke("preview-active")["soundPreview"]
                        hidden = invoke("cli-hide")
                        assert hidden["result"] == {"windowVisible": False, "changed": True}, hidden
                        assert not hidden["visible"] and not hidden["minimized"] and not hidden["dockVisible"], hidden
                        assert not invoke("status")["soundPreview"]
                        assert invoke("cli-hide")["result"] == {"windowVisible": False, "changed": False}
                        shown = invoke("cli-show")
                        assert shown["visible"] and shown["dockVisible"] and not shown["error"], shown
                        assert shown["result"] == {"windowVisible": True, "requestedPane": "history"}, shown
                        assert shown["navigation"]["pane"] == "history" and shown["navigation"]["id"], shown
                        assert invoke("cli-show")["navigation"]["id"] != shown["navigation"]["id"]
                        assert not invoke("cli-hide")["visible"]
                        replayed = invoke("cli-replay")
                        assert replayed == {"busyError": "Finish the current recording before replaying dictation setup.",
                                            "updateError": "Jaro is preparing to close.", "blockedStep": "done",
                                            "blockedWindowVisible": False, "step": "welcome", "unchanged": True,
                                            "visible": True, "error": ""}, replayed
                        assert invoke("menu-open")["visible"]
                        assert invoke("preview-active")["soundPreview"]
                        closed = invoke("close")
                        assert not closed["visible"] and not closed["dockVisible"] and not closed["soundPreview"], closed
                        lost = invoke("menu-off")
                        assert lost["dockVisible"] and not lost["dockSetting"]["menuBarAvailable"], lost
                        assert not invoke("menu-on")["dockVisible"]
                        reopened = invoke("menu-open")
                        assert reopened["visible"] and reopened["dockVisible"]
                        reset = invoke("dock-on")
                        assert reset["dockVisible"] and reset["dockSetting"] == {"enabled": True, "menuBarAvailable": False}
                    elif scenario == "dock-login":
                        assert not invoke("status")["dockVisible"]
                        opened = invoke("menu-open")
                        assert opened["visible"] and opened["dockVisible"]
                    elif scenario == "login-menu-unavailable":
                        fallback = invoke("status")
                        assert fallback["visible"] and fallback["dockVisible"] and not fallback["dockSetting"]["menuBarAvailable"], fallback
                    elif scenario in {"dock-cold-ready", "hidden-ready", "hidden-missing"}:
                        quiet = invoke("status")
                        assert not quiet["visible"] and not quiet["dockVisible"] and quiet["quitAvailable"], quiet
                        assert quiet["dockSetting"]["menuBarAvailable"], quiet
                        assert not invoke("activate")["visible"]
                        opened = invoke("dock" if scenario == "hidden-ready" else "menu-open")
                        assert opened["visible"] and opened["dockVisible"], opened
                    elif scenario == "dock-cold-input":
                        assert invoke("status")["visible"]
                    elif scenario == "hidden-unavailable":
                        fallback = invoke("status")
                        assert fallback["visible"] and fallback["dockVisible"] and not fallback["dockSetting"]["menuBarAvailable"], fallback
                    elif scenario == "launch-update":
                        assert invoke("update-status")["update"]["phase"] == "idle"
                        wait_for(lambda: invoke("update-status")["update"]["phase"] == "error",
                                 "The launch update check did not run.")
                        assert time.monotonic() - start >= 10
                        assert not invoke("update-status")["update"]["manualCheck"]
                    elif scenario == "focus-update":
                        assert invoke("focus-update") == {"disabled": "idle", "other": "idle", "workspace": "checking"}
                    elif scenario == "update-restart":
                        assert invoke("restart-transaction") == {"passed": True}
                    elif scenario == "quit-capture":
                        assert invoke("quit-capture") == {"passed": True}
                    elif scenario == "application-settings":
                        verify_application_settings(root, invoke, target)
                    elif scenario == "dock-updates":
                        assert not invoke("status")["dockVisible"]
                        assert invoke("meeting-input-status") == {"passed": True}
                        assert invoke("update-status")["statusMenuTitle"] == "Check for Updates…"
                        assert invoke("status-check")["visible"]

                        def finished_check(manual, previous=None):
                            state = invoke("update-status")
                            update = state["update"]
                            if update["phase"] == "error" and update["manualCheck"] == manual:
                                if previous is None or update["openUpdateSettings"] != previous:
                                    return state

                        first = wait_for(lambda: finished_check(True), "The status item check did not finish.")
                        assert first["update"]["manualCheck"] and not first["sheet"], first
                        request = first["update"]["openUpdateSettings"]
                        assert request
                        invoke("background-check")
                        automatic = wait_for(lambda: finished_check(False), "The background check did not finish.")
                        assert not automatic["update"]["manualCheck"] and not automatic["sheet"], automatic
                        assert automatic["update"]["openUpdateSettings"] == request, automatic
                        assert invoke("application-check")["visible"]
                        application = wait_for(lambda: finished_check(True, request), "The application menu check did not finish.")
                        assert application["update"]["manualCheck"] and not application["sheet"], application
                        assert application["update"]["openUpdateSettings"] != request, application
                        held = invoke("update-hold")
                        assert held["update"]["phase"] == "checking" and held["update"]["staged"], held
                        assert held["update"]["restartBlocked"] and held["publishedHold"] and not held["statusMenuEnabled"], held
                        cleared = invoke("update-unhold")
                        assert cleared["update"]["staged"] and not cleared["update"]["restartBlocked"], cleared
                        assert not cleared["publishedHold"] and cleared["statusMenuEnabled"], cleared
                    elif scenario == "dock-unavailable":
                        closed = invoke("close")
                        assert not closed["visible"] and closed["dockVisible"] and not closed["dockSetting"]["menuBarAvailable"], closed
                        assert invoke("dock")["visible"]
                    elif scenario == "locale-preselected":
                        assert invoke("setup-title")["lang"] == "fr"
                        assert invoke("locale-test") == {"frMenu": True, "jaMenu": True,
                            "resolved": ["en", "pl", "zh-Hant", "zh", "zh-Hant", "pt"],
                            "invalidRejected": True, "accepted": True, "selected": "ja"}
                except (AssertionError, TypeError):
                    print((root / "host.log").read_text(), file=sys.stderr)
                    raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
        finally:
            subprocess.run(["defaults", "delete", identifier], capture_output=True)


if __name__ == "__main__":
    for launch, outcome in [("normal", "ready"), ("annotation-managed", "ready"), ("managed-updates", "ready"), ("login-setup", "ready"), ("login", "ready"),
                            ("login-idle", "ready"), ("login-input", "ready"), ("login-menu-unavailable", "ready"),
                            ("login-model", "missing"), ("login-error", "error"), ("dock-setting", "ready"),
                            ("dock-login", "ready"), ("dock-updates", "ready"),
                            ("launch-update", "ready"), ("focus-update", "ready"), ("update-restart", "ready"), ("quit-capture", "ready"),
                            ("application-settings", "ready"), ("web-recovery", "ready"),
                            ("dock-unavailable", "ready"), ("locale-preselected", "missing"),
                            ("dock-cold-ready", "ready"), ("dock-cold-input", "ready"), ("dock-cold-missing", "missing"),
                            ("hidden-ready", "ready"), ("hidden-missing", "missing"), ("hidden-unavailable", "ready")]:
        run_case(Path(sys.argv[1]).resolve(), launch, outcome)
    print("AppKit verified quiet login, setup recovery, Dock and menu bar reopen, overlay focus, minimization, and the missing-item fallback.")
    print("AppKit verified menu-only launch for a ready Dock-hidden app, explicit hidden launch before setup, and both startup fallbacks.")
    print("AppKit verified persisted interface language, System tag resolution, inline setup, and live native menu labels.")
    print("AppKit checked updates from both menus, opened a hidden workspace, and separated the next background result.")
    print("AppKit verified the launch delay, the automatic update preference, and Development checks on workspace focus.")
    print("AppKit verified a native capture hold on a staged update during a background check.")
    print("AppKit verified a recording can defer restart and a failed restart waits for the engine to reopen.")
    print("AppKit verified managed update actions and the managed Annotate capture and shortcut restrictions.")
