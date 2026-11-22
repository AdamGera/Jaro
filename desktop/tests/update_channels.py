"""Exercise the native updater against disposable, signed HTTPS feeds."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import plistlib
import runpy
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

development_release_notes = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "build-tools/package.py")
)["development_release_notes"]


def run(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def wait_for(path):
    deadline = monotonic() + 30
    while not path.exists() and monotonic() < deadline:
        sleep(.1)
    assert path.is_file(), f"Updater did not write {path.name}"


def exercise(root, executable):
    identity = "local.jardaudio.update-channels." + uuid4().hex
    assets = root / "releases"
    assets.mkdir()
    keys = {channel: root / f"{channel}.key" for channel in ("stable", "dev")}
    public_keys = {channel: run("native", "update", "keygen", "--private-key", seed)
                   for channel, seed in keys.items()}
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
        "-addext", "extendedKeyUsage=serverAuth", "-addext", "keyUsage=digitalSignature,keyEncipherment",
        "-keyout", root / "server.key", "-out", root / "server.pem")
    run("openssl", "x509", "-in", root / "server.pem", "-outform", "DER", "-out", root / "server.cer")
    requests = []
    ranges = []
    feed_requests = []
    feed_agents = []
    rollout_requests = []

    class Releases(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            name = self.path.lstrip("/")
            if name.endswith(".json"):
                feed_agents.append((name, self.headers.get("User-Agent")))
            if name in {"rollout-stable.json", "rollout-dev.json"}:
                self.rollout_feed(name)
                return
            if (root / "cache-mode").is_file() and name in {"stable.json", "development.json", "alternate-feed.json"}:
                self.cached_feed(name)
                return
            if name == "development.json":
                name = "dev-" + (root / "development-feed").read_text() + ".json"
            if name == "stable.json":
                name = (root / "stable-feed").read_text() + ".json"
            requests.append(name)
            if name == "stalled-feed.json":
                self.send_response(200)
                self.send_header("Content-Length", "128")
                self.end_headers()
                try:
                    for _ in range(24):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        sleep(1)
                except (OSError, ssl.SSLError):
                    return
                return
            if name == "resume.zip" and (root / "resume-behavior").read_text() == "resume":
                self.send_response(302)
                self.send_header("Location", "/resume-cdn.zip")
                self.end_headers()
                return
            file = assets / ("resume.zip" if name == "resume-cdn.zip" else name)
            if "/" in name or not file.is_file():
                self.send_error(404)
                return
            if file.name in {"resume.zip", "resume-full.zip"}:
                self.resumable_archive(file)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(file.stat().st_size))
            self.end_headers()
            with file.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)

        def rollout_feed(self, name):
            mode = (root / name.removesuffix(".json")).read_text()
            incoming = self.headers.get("If-None-Match")
            rollout_requests.append((name, mode, incoming, self.headers.get("Cache-Control")))
            if mode == "delayed-404":
                sleep(5)
            if mode in {"missing", "delayed-404", "unavailable"}:
                self.send_error(503 if mode == "unavailable" else 404)
                return
            file = assets / mode
            tag = f'"{mode}"'
            if incoming == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.end_headers()
                return
            payload = file.read_bytes()
            self.send_response(200)
            self.send_header("ETag", tag)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def cached_feed(self, name):
            mode = (root / "cache-mode").read_text()
            incoming = self.headers.get("If-None-Match")
            feed_requests.append((mode, name, incoming, self.headers.get("Cache-Control")))
            if mode in {"redirect", "wrong-tag"} and name == "stable.json":
                self.send_response(302)
                self.send_header("Location", "/alternate-feed.json")
                self.end_headers()
                return
            tag = 'W/"old-state"' if mode == "current" else 'W/"shared-state"'
            body = "stable-four.json" if mode == "stable" else "stable-five.json"
            unchanged = incoming == tag and mode in {"stable", "redirect", "current"}
            if mode in {"unsolicited", "wrong-tag"} or unchanged:
                self.send_response(304)
                self.send_header("ETag", '"wrong-state"' if mode == "wrong-tag" else tag.removeprefix("W/"))
                self.end_headers()
                return
            if mode == "malformed":
                tag = "unquoted"
            elif mode == "current":
                body = "stable.json"
            elif mode in {"invalid-signature", "switch-channel"}:
                body = "dev-first.json"
            payload = (assets / body).read_bytes()
            self.send_response(200)
            self.send_header("ETag", tag)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def resumable_archive(self, file):
            behavior = (root / "resume-behavior").read_text()
            value = self.headers.get("Range")
            assert value and value.startswith("bytes="), value
            start, end = map(int, value.removeprefix("bytes=").split("-"))
            size = file.stat().st_size
            assert 0 <= start <= end < size and end - start < 8 * 1024 * 1024
            ranges.append((file.name, behavior, start, end))
            if behavior == "interrupt" and start:
                self.send_error(503)
                return
            if behavior == "full":
                start, end = 0, size - 1
                self.send_response(200)
            else:
                self.send_response(206)
                reported_start = start - 1 if behavior == "invalid" else start
                self.send_header("Content-Range", f"bytes {reported_start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with file.open("rb") as source:
                source.seek(start)
                body = source.read(32768 if behavior == "interrupt" else end - start + 1)
            try:
                self.wfile.write(body)
                self.wfile.flush()
                if behavior == "interrupt":
                    self.connection.shutdown(socket.SHUT_WR)
            except (OSError, ssl.SSLError):
                pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Releases) as server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root / "server.pem", root / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        origin = f"https://localhost:{server.server_port}"
        stable = {"feed_url": origin + "/stable.json", "public_key": public_keys["stable"]}
        dev = {"feed_url": origin + "/development.json", "public_key": public_keys["dev"],
               "release_url": origin + "/notes"}

        def bundle(folder, channel, version, flavor, dev_notes=None):
            app = folder / "Fixture.app"
            binary = app / "Contents/MacOS/fixture"
            binary.parent.mkdir(parents=True)
            shutil.copyfile(executable, binary)
            binary.chmod(0o755)
            if flavor == "resume":
                (app / "Contents/Resources").mkdir()
                (app / "Contents/Resources/fixture.bin").write_bytes(os.urandom(9 * 1024 * 1024))
            if dev_notes is not None:
                notes_file = app / "Contents/Resources/Jaro-development-notes.json"
                notes_file.parent.mkdir(exist_ok=True)
                notes_file.write_bytes(dev_notes)
            updates = {**stable, "channel": channel, "development": dev}
            if flavor == "rollout-installed":
                updates["rollout_url"] = origin + "/rollout-stable.json"
                updates["development"] = {**dev, "rollout_url": origin + "/rollout-dev.json"}
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": identity, "CFBundleVersion": version, "CFBundleShortVersionString": version,
                "CFBundlePackageType": "APPL", "CFBundleExecutable": "fixture", "LSUIElement": True,
                "JardUpdates": updates, "FixtureFlavor": flavor,
                "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
            }))
            run("codesign", "--force", "--sign", "-", "-r", f'=designated => identifier "{identity}"', app)
            return app

        def publish(asset, signer, channel, version="2.0.0", dev_notes=None):
            app = bundle(root / asset, channel, version, asset, dev_notes)
            archive = assets / f"{asset}.zip"
            run("ditto", "-c", "-k", "--norsrc", "--keepParent", app, archive)
            manifest = root / "release-app.json"
            config = stable if signer == "stable" else dev
            manifest.write_text(json.dumps({
                "id": identity, "name": "fixture", "display_name": "Fixture", "version": version,
                "platforms": ["macos"], "updates": {key: config[key] for key in ("feed_url", "public_key")},
            }))
            run("native", "update", "sign", "--manifest", manifest, "--target", "macos-aarch64", "--archive", archive,
                "--url", origin + f"/{archive.name}", "--notes", f"Notes for {asset}",
                "--output", assets / f"{asset}.json", "--private-key", keys[signer])

        publish("stable", "stable", "stable")
        publish("stable-four", "stable", "stable", "4.0.0")
        publish("stable-five", "stable", "stable", "5.0.0")
        publish("stable-six", "stable", "stable", "6.0.0")
        publish("dev-first", "dev", "dev")
        history = root / "prior-development-notes.json"
        history.write_bytes(development_release_notes("1.1.0", "- Older change\n* Shared change", None))
        notes_for_second = development_release_notes("2.0.0", "Current change\nShared change", history)
        publish("dev-second", "dev", "dev", dev_notes=notes_for_second)
        # Repacking the same version replaces its notes while carrying older builds.
        history.write_bytes(notes_for_second)
        assert development_release_notes("2.0.0", "Current change\nShared change", history) == notes_for_second
        history.write_bytes(development_release_notes("2.2.0", "Skipped change\nShared change", history))
        publish("dev-later", "dev", "dev", "2.10.0",
                development_release_notes("2.10.0", "Latest change\nShared change", history))
        publish("dev-invalid-notes", "dev", "dev", "2.1.0", notes_for_second)
        publish("dev-wrong-bundle", "dev", "stable")
        publish("resume", "stable", "stable", "6.0.0")
        publish("resume-full", "stable", "stable", "7.0.0")
        shutil.copyfile(assets / "stable.json", assets / "dev-invalid-key.json")
        (root / "development-feed").write_text("first")
        (root / "stable-feed").write_text("stable")
        installed = bundle(root, "stable", "3.0.0", "installed")
        entry = installed / "Contents/MacOS/fixture"
        resume_bundle = bundle(root / "resume-install", "stable", "3.0.0", "resume-installed")
        resume_bundle = resume_bundle.rename(root / "Resume.app")
        resume_entry = resume_bundle / "Contents/MacOS/fixture"
        cache_bundle = bundle(root / "cache-install", "stable", "3.0.0", "cache-installed").rename(root / "Cache.app")
        cache_entry = cache_bundle / "Contents/MacOS/fixture"
        schedule_bundle = bundle(root / "schedule-install", "dev", "2.0.0", "schedule-installed")
        schedule_entry = schedule_bundle.rename(root / "Schedule.app") / "Contents/MacOS/fixture"
        rollout_bundle = bundle(root / "rollout-install", "stable", "3.0.0", "rollout-installed")
        rollout_entry = rollout_bundle.rename(root / "Rollout.app") / "Contents/MacOS/fixture"
        preserved_bundle = bundle(root / "preserved-install", "stable", "3.0.0", "preserved-stage-installed")
        preserved_app = preserved_bundle.rename(root / "Preserved.app")
        cache = Path.home() / "Library/Caches" / f"{identity}.update-archives"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run([entry], check=True, timeout=90)
            marker = root / "installed-on-quit"
            wait_for(marker)
            assert not (root / "stable-reopened").exists(), "Install on quit relaunched automatically"
            subprocess.run([entry], check=True, timeout=15)
            print(marker.read_text())
            print((root / "stable-reopened").read_text())
            print((root / "late-stage-fenced").read_text())
            print((root / "stage-cleanup").read_text())
            print((root / "receipt-recovery").read_text())
            print((root / "restore-admission").read_text())
            print((root / "install-admission").read_text())
            assert requests.count("stable.zip") == 1, requests
            assert requests.count("stable-four.zip") == 2, requests
            assert requests.count("stable-five.zip") == 2, requests
            assert requests.count("stalled-feed.json") == 1, requests
            assert requests.count("dev-wrong-bundle.zip") == 1, requests
            assert requests.count("dev-second.zip") == 1, requests
            assert requests.count("dev-first.zip") == 2, requests
            assert requests.count("dev-invalid-notes.zip") == 1, requests
            assert requests.count("dev-later.zip") == 1, requests
            assert requests.count("dev-invalid-key.json") == 1, requests
            assert requests.count("dev-missing.json") == 1, requests
            print("Native signatures, stale release confirmation, downgrade, equal-version switch, and restart refresh verified.")
            print("Development notes covered skipped builds, survived stage recovery, and used signed feed notes for a mismatched index.")

            def transfer(name, behavior):
                (root / "stable-feed").write_text(name)
                (root / "resume-behavior").write_text(behavior)
                subprocess.run([resume_entry], check=True, timeout=20)
                return [row[2:] for row in ranges if row[:2] == (name + ".zip", behavior)]

            resumed = transfer("resume", "interrupt")
            assert len(resumed) == 3 and resumed[0][0] == 0 and all(start == 32768 for start, _ in resumed[1:]), resumed
            partial, = cache.glob("stable-*.partial")
            assert partial.stat().st_size == 32768
            invalid = transfer("resume", "invalid")
            assert len(invalid) == 3 and all(start == 32768 for start, _ in invalid), invalid
            assert partial.stat().st_size == 32768 and partial.read_bytes() == (assets / "resume.zip").read_bytes()[:32768]
            before_redirect = requests.count("resume.zip")
            resumed = transfer("resume", "resume")
            assert len(resumed) == 2 and resumed[0][0] == 32768 and resumed[1][0] == resumed[0][1] + 1, resumed
            assert requests.count("resume.zip") == before_redirect + 1 and requests.count("resume-cdn.zip") == 2
            assert not partial.exists()
            transfer("resume-full", "interrupt")
            partial, = cache.glob("stable-*.partial")
            with partial.open("r+b") as stream:
                byte = stream.read(1)
                stream.seek(0)
                stream.write(bytes([byte[0] ^ 0xFF]))
            transfer("resume-full", "tamper")
            assert not partial.exists()
            transfer("resume-full", "interrupt")
            assert transfer("resume-full", "full")[0][0] == 32768
            assert not list(cache.glob("*.partial"))
            print("Interrupted signed downloads resumed across processes and HTTPS redirects, rejected mismatched ranges and altered prefixes, and handled servers that return the complete file.")
            stable_downloads = requests.count("stable-four.zip")
            subprocess.run([cache_entry], check=True, timeout=30)
            tag = 'W/"shared-state"'
            assert [(mode, name, validator) for mode, name, validator, _ in feed_requests] == [
                ("unsolicited", "stable.json", None),
                ("current", "stable.json", None),
                ("current", "stable.json", 'W/"old-state"'),
                ("stable", "stable.json", 'W/"old-state"'),
                ("stable", "stable.json", tag),
                ("stable", "stable.json", None),
                ("redirect", "stable.json", tag),
                ("redirect", "alternate-feed.json", None),
                ("redirect", "stable.json", None),
                ("redirect", "alternate-feed.json", tag),
                ("wrong-tag", "stable.json", None),
                ("wrong-tag", "alternate-feed.json", tag),
                ("malformed", "stable.json", None),
                ("malformed", "stable.json", None),
                ("stable", "stable.json", None),
                ("invalid-signature", "stable.json", tag),
                ("invalid-signature", "stable.json", None),
                ("stable", "stable.json", None),
                ("unsolicited", "stable.json", None),
                ("stable", "stable.json", None),
                ("switch-channel", "development.json", None),
            ], feed_requests
            assert "no-cache" in feed_requests[5][3] and "no-cache" in feed_requests[-3][3], feed_requests
            assert requests.count("stable-four.zip") == stable_downloads + 2
            print("Only verified feeds supplied validators, with fresh restart requests and independent channel and redirect resources.")
            assert ("alternate-feed.json", "jaro/stable/3.0.0") in feed_agents, feed_agents
            (root / "cache-mode").unlink()
            run("defaults", "delete", identity, "JardUpdateChannel")
            since = len(feed_agents)
            subprocess.run([schedule_entry], check=True, timeout=30)
            scheduled_feeds = ["development.json"] * 9 + ["stable.json"] * 2 + ["development.json", "stable.json"]
            assert [name for name, _ in feed_agents[since:]] == scheduled_feeds, feed_agents[since:]
            assert {agent for _, agent in feed_agents[since:]} == {"jaro/dev/2.0.0"}, feed_agents[since:]
            print("Development backoff, focus, manual and restart overrides, Stable cadence, and installed-build User-Agent passed.")
            sign_rollout = root / "sign-rollout"
            desktop = Path(__file__).resolve().parents[1]
            run("xcrun", "swiftc", "-O", desktop / "update/UpdateRollout.swift",
                desktop / "build-tools/sign-update-rollout.swift", "-o", sign_rollout)

            def policy(name, release, percentage, required=False, signer="stable", channel="stable"):
                path = assets / (name + ".json")
                run(sign_rollout, assets / (release + ".json"), path, keys[signer], channel, str(percentage), str(required).lower())
                return path

            policy("zero", "stable-five", 0)
            policy("closed-boundary", "stable-five", 37)
            policy("open-boundary", "stable-five", 38)
            policy("required", "stable-six", 0, True)
            policy("older-required", "stable", 0, True)
            policy("wrong-channel", "stable-five", 100, channel="dev")
            policy("wrong-key", "dev-first", 100, signer="dev")
            policy("dev-zero", "dev-first", 0, signer="dev", channel="dev")
            invalid = json.loads((assets / "required.json").read_text())
            invalid["signature"] = "A" * 86 + "=="
            (assets / "invalid-policy.json").write_text(json.dumps(invalid))
            wrong_seed = subprocess.run([sign_rollout, assets / "stable-five.json", assets / "unsigned.json",
                                         keys["dev"], "stable", "50", "false"], capture_output=True, text=True, timeout=5)
            assert wrong_seed.returncode and "same key" in wrong_seed.stderr and not (assets / "unsigned.json").exists()

            (root / "rollout-stable").write_text("zero.json")
            (root / "rollout-dev").write_text("dev-zero.json")
            since = len(requests)
            subprocess.run([rollout_entry], check=True, timeout=45)
            assert len(rollout_requests) == 1 and not requests[since:], (rollout_requests, requests[since:])
            subprocess.run([rollout_entry], check=True, timeout=45)
            assert requests[since:] == ["stable-four.json", "stable-four.zip", "stalled-feed.json",
                                        "stable-five.zip", "stable-six.zip", "dev-first.zip"], requests[since:]
            assert rollout_requests[0][:2] == ("rollout-stable.json", "zero.json"), rollout_requests
            assert ("rollout-stable.json", "closed-boundary.json", '"closed-boundary.json"', None) in rollout_requests
            assert any(mode == "closed-boundary.json" and validator is None and "no-cache" in (cache or "")
                       for _, mode, validator, cache in rollout_requests)
            assert all(agent == "jaro/stable/3.0.0" for name, agent in feed_agents if name.startswith("rollout-")), feed_agents
            print("Signed rollout kept its private bucket across launches, enforced the percentage, required flag and channel authority, and preserved the legacy fallback and staged release.")
            run("defaults", "delete", identity, "JardUpdateChannel")
            (root / "stable-feed").write_text("stable-four")
            (root / "development-feed").write_text("first")
            since = len(requests)
            subprocess.run([preserved_app / "Contents/MacOS/fixture"], check=True, timeout=25)
            result = plistlib.loads((preserved_app / "Contents/Info.plist").read_bytes())
            assert result["CFBundleVersion"] == "4.0.0" and result["JardUpdates"]["channel"] == "stable", result
            assert requests[since:] == ["stable-four.json", "stable-four.zip", "dev-first.json"], requests[since:]
            print("Quit installed the verified Stable update at the same path while a Development offer awaited confirmation.")
        finally:
            server.shutdown()
            thread.join()
            subprocess.run(["defaults", "delete", identity], capture_output=True)
            if cache.is_dir() and not cache.is_symlink():
                shutil.rmtree(cache)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="jard-update-channels-") as work:
        exercise(Path(work).resolve(), Path(sys.argv[1]).resolve())
