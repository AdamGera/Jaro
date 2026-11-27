"""Parity tests comparing the native Rust `jaro-cli` binary against Python `jardctl.py`."""
import base64
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from uuid import uuid4
import zlib

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / "engine"
JARDCTL_PY = ENGINE_DIR / "jardctl.py"


def find_rust_cli() -> Path | None:
    for candidate in (
        REPO_ROOT / "desktop/cli/target/release/jaro-cli",
        REPO_ROOT / "desktop/cli/target/debug/jaro-cli",
    ):
        if candidate.is_file():
            return candidate
    return None


def make_png_bytes() -> bytes:
    # Valid minimal 1x1 PNG
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def seed_library(root: Path):
    from session_store import SessionStore
    import personal_notes

    store = SessionStore(root)

    # 1. Completed meeting with speaker labels, timestamps, personal notes, and meeting brief
    audio1 = root / "tmp1.wav"
    audio1.write_bytes(b"RIFF" + b"\x00" * 40)
    mid, _ = store.create(
        audio1,
        "Q3 Architecture Review",
        "audio/wav",
        {
            "recording": True,
            "diarization": True,
            "meeting": True,
            "sourceSpeakerTurns": True,
            "meetingProvider": "Zoom",
            "meetingStartedAt": "2026-10-20T14:00:00+00:00",
            "meetingEndedAt": "2026-10-20T14:05:00+00:00",
            "meetingCalendar": {"title": "Q3 Architecture Review", "attendees": ["Alice", "Bob"]},
        },
        300.0,
    )
    chunk1 = {
        "text": "Let's review the Kubernetes deployment timeline. Soundfolio search feels much faster now.",
        "language": "English",
        "segments": [
            {"speaker": 1, "start": 1.2, "end": 5.5, "text": "Let's review the Kubernetes deployment timeline."},
            {"speaker": 2, "start": 10.0, "end": 14.4, "text": "Soundfolio search feels much faster now."},
        ],
        "words": [],
        "activity": [],
    }
    store.append(mid, chunk1, 300.0)
    store.finish(mid)
    with store.connection() as db:
        doc = {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Action Items"}]},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Ship Rust CLI"}]}],
                        }
                    ],
                },
            ],
        }
        db.execute("INSERT INTO personal_notes VALUES(?,?,1)", (mid, personal_notes.encode(doc)))
        db.execute(
            "INSERT OR REPLACE INTO meeting_briefs(id,status,title,summary) VALUES(?,?,?,?)",
            (mid, "ready", "Architecture & Rust CLI", "Agreed to ship the Rust CLI for instant MCP startup."),
        )

    # 2. Regular non-meeting session
    audio2 = root / "tmp2.wav"
    audio2.write_bytes(b"RIFF" + b"\x00" * 40)
    sid, _ = store.create(
        audio2,
        "Voice Memo",
        "audio/wav",
        {"recording": True, "diarization": False, "meeting": False},
        12.0,
    )
    store.append(
        sid,
        {
            "text": "Remember to buy espresso beans after standup.",
            "language": "English",
            "segments": [{"speaker": None, "start": 0.5, "end": 3.0, "text": "Remember to buy espresso beans after standup."}],
            "words": [],
            "activity": [],
        },
        12.0,
    )
    store.finish(sid)

    # 3. Screen annotation session with manifest & contact sheet
    ann_id = str(uuid4())
    stage_dir = root / "tmp-ann"
    stage_dir.mkdir()
    ann_video = stage_dir / f"{ann_id}.mp4"
    ann_video.write_bytes(b"fake-mp4")
    manifest = {
        "version": 1,
        "source": "display",
        "coordinateSpace": "normalized-bottom-left",
        "frames": [1.5, 4.2],
        "events": [
            {"time": 1.0, "stroke": 1, "from": [0.1, 0.2], "to": [0.3, 0.4], "liftedAt": 2.5},
        ],
    }
    ann_video.with_suffix(".annotation.json").write_text(json.dumps(manifest), encoding="utf-8")
    ann_video.with_suffix(".annotation.png").write_bytes(make_png_bytes())
    aid, _ = store.create(
        ann_video,
        "Bug Walkthrough",
        "video/mp4",
        {"recording": True, "diarization": False, "meeting": False},
        6.0,
        key=ann_id,
        annotation={"version": 1, "source": "display", "strokeCount": 1, "frames": [1.5, 4.2]},
    )
    store.append(
        aid,
        {
            "text": "The alignment button overlaps on narrow viewports.",
            "language": "English",
            "segments": [{"speaker": None, "start": 0.2, "end": 4.0, "text": "The alignment button overlaps on narrow viewports."}],
            "words": [],
            "activity": [],
        },
        6.0,
    )
    store.finish(aid)

    return mid, sid, aid


class RustCliParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rust_bin = find_rust_cli()
        if cls.rust_bin is None:
            raise unittest.SkipTest("Build desktop/cli before running Rust CLI parity tests.")

    def _env(self, home: Path, data_dir: Path, log_dir: Path, bundle_id: str = "test.jaro.parity") -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "RUMBLE_DATA_DIR": str(data_dir),
            "RUMBLE_LOG_DIR": str(log_dir),
            "RUMBLE_PORT": "19876",
            "JARD_DESKTOP_BUNDLE_ID": bundle_id,
            "PYTHONPATH": f"{ENGINE_DIR}:{REPO_ROOT}",
            "TZ": "UTC",
        }

    def run_both(self, args: list[str], env: dict[str, str], stdin_text: str | None = None):
        py = subprocess.run(
            [sys.executable, str(JARDCTL_PY), *args],
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=20,
        )
        rs = subprocess.run(
            [str(self.rust_bin), *args],
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return py, rs

    def test_empty_library_offline_never_creates_database(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            missing_lib = base / "nonexistent-library"
            env = self._env(base / "home", missing_lib, base / "logs")
            for args in (
                ["meetings", "list", "--json"],
                ["sessions", "list", "--json"],
                ["annotate", "list", "--json"],
                ["status", "--json"],
            ):
                py, rs = self.run_both(args, env)
                self.assertEqual(rs.returncode, py.returncode, args)
                self.assertEqual(json.loads(rs.stdout), json.loads(py.stdout), args)
                self.assertFalse(missing_lib.exists(), f"CLI created missing library for {args}")

    def test_meetings_sessions_annotations_and_exports_match_python(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()
            data_dir = base / "lib"
            log_dir = base / "logs"
            mid, sid, aid = seed_library(data_dir)
            env = self._env(home, data_dir, log_dir)

            # JSON commands: exact JSON object parity
            for args in (
                ["meetings", "list", "--json"],
                ["meetings", "list", "--query", "architecture", "--json"],
                ["meetings", "show", "latest", "--json"],
                ["meetings", "show", mid[:8], "--from", "5", "--to", "20", "--json"],
                ["meetings", "search", "kubernetis", "--json"],
                ["meetings", "search", "kubernetis", "--no-fuzzy", "--json"],
                ["meetings", "search", "\"Rust CLI\"", "--json"],
                ["sessions", "list", "--json"],
                ["sessions", "show", sid, "--json"],
                ["sessions", "search", "espresso", "--json"],
                ["annotate", "list", "--json"],
                ["annotate", "show", "latest", "--json"],
                ["annotate", "context", aid[:8], "--json"],
                ["annotate", "inspect", aid, "--strokes", "--json"],
                ["annotate", "inspect", aid, "--json"],
            ):
                py, rs = self.run_both(args, env)
                self.assertEqual(rs.returncode, py.returncode, f"rc mismatch for {args}: rs={rs.stderr} py={py.stderr}")
                self.assertEqual(json.loads(rs.stdout), json.loads(py.stdout), f"json mismatch for {args}")

            # Text / Markdown / SRT / VTT commands: exact stdout parity
            for args in (
                ["meetings", "list"],
                ["meetings", "show", "latest", "--md"],
                ["meetings", "show", "latest", "--notes"],
                ["meetings", "show", "latest", "--srt"],
                ["meetings", "show", "latest", "--vtt"],
                ["meetings", "search", "kubernetes"],
                ["sessions", "list"],
                ["annotate", "context", "latest"],
                ["annotate", "inspect", "latest", "--strokes"],
            ):
                py, rs = self.run_both(args, env)
                self.assertEqual(rs.returncode, py.returncode, f"rc mismatch for {args}: rs={rs.stderr}")
                self.assertEqual(rs.stdout, py.stdout, f"text mismatch for {args}")

            # Error parity
            for args in (
                ["meetings", "show", "deadbeef"],
                ["meetings", "show", "deadbeef", "--json"],
                ["meetings", "show", "latest", "--from", "9999"],
                ["meetings", "show", "latest", "--md", "--json"],
            ):
                py, rs = self.run_both(args, env)
                self.assertEqual(rs.returncode, py.returncode, f"err rc mismatch for {args}")
                if "--json" in args and py.stderr.strip().startswith("{"):
                    self.assertEqual(json.loads(rs.stderr), json.loads(py.stderr), f"err json mismatch for {args}")

    def test_history_and_logs_match_python(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            prefs_dir = home / "Library/Preferences"
            prefs_dir.mkdir(parents=True)
            bundle_id = "test.jaro.parity"
            plist_path = prefs_dir / f"{bundle_id}.plist"
            now = time.time()
            payload = {
                "JardSystemDictation": {
                    "retention": 7,
                    "previousRetention": 7,
                    "vocabulary": "Soundfolio\n",
                    "vocabularySoundsLike": {"Soundfolio": ["sound folio"]},
                },
                "JardSystemDictationHistory": [
                    {
                        "id": "d1",
                        "time": now - 120,
                        "text": "Testing sound folio search engine.",
                        "raw": "testing sound folio search engine",
                        "app": "Terminal",
                        "mode": "Clean",
                        "kind": "dictation",
                        "seconds": 2.4,
                    },
                    {
                        "id": "d2",
                        "time": now - 60,
                        "text": "Quick note in Notes app.",
                        "app": "Notes",
                        "seconds": 1.2,
                    },
                ],
            }
            with plist_path.open("wb") as f:
                plistlib.dump(payload, f)
            os.chmod(plist_path, 0o600)

            log_dir = base / "logs"
            log_dir.mkdir(mode=0o700)
            ev_log = log_dir / "live.ndjson"
            ev_log.write_text("line1\nline2\nline3\n", encoding="utf-8")
            os.chmod(ev_log, 0o600)

            env = self._env(home, base / "lib", log_dir, bundle_id=bundle_id)
            for args in (
                ["history"],
                ["history", "--json", "--limit", "10", "--raw"],
                ["history", "--json", "--app", "terminal"],
                ["history", "--json", "--grep", "notes"],
                ["history", "--json", "--search", "soundfolio"],
                ["history", "--text", "--raw", "--oldest-first"],
            ):
                py, rs = self.run_both(args, env)
                self.assertEqual(rs.returncode, py.returncode, f"rc mismatch for {args}: rs={rs.stderr} py={py.stderr}")
                if "--text" in args:
                    self.assertEqual(rs.stdout, py.stdout, f"text mismatch for {args}")
                else:
                    self.assertEqual(json.loads(rs.stdout), json.loads(py.stdout), f"json mismatch for {args}")

            # Logs parity
            py, rs = self.run_both(["logs", "-n", "2"], env)
            self.assertEqual(rs.returncode, 0)
            self.assertEqual(rs.stdout, py.stdout)
            self.assertEqual(rs.stdout, "line2\nline3\n")

            # Reject world-readable plist (privacy guard)
            os.chmod(plist_path, 0o644)
            py, rs = self.run_both(["history", "--json"], env)
            self.assertEqual(rs.returncode, 1)
            self.assertEqual(json.loads(rs.stderr), json.loads(py.stderr))

    def test_mcp_stdio_server_matches_python_1_to_1(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            home.mkdir()
            data_dir = base / "lib"
            log_dir = base / "logs"
            mid, sid, aid = seed_library(data_dir)
            env = self._env(home, data_dir, log_dir)

            requests = [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),  # notification: no response
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": None}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/templates/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "list_meetings", "arguments": {}}}),
                json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {"kind": "all"}}}),
                json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_transcripts", "arguments": {"query": "kubernetis"}}}),
                json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "get_transcript", "arguments": {"meeting_id": "latest"}}}),
                json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_meeting_notes", "arguments": {"meeting_id": mid}}}),
                json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "list_annotations", "arguments": {}}}),
                json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "get_annotation", "arguments": {"annotation_id": aid, "include_strokes": True}}}),
                json.dumps({"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "list_dictations", "arguments": {}}}),
                json.dumps({"jsonrpc": "2.0", "id": 13, "method": "resources/read", "params": {"uri": f"jard-meeting://{mid}"}}),
                json.dumps({"jsonrpc": "2.0", "id": 14, "method": "resources/read", "params": {"uri": f"jard-session://{sid}"}}),
                json.dumps({"jsonrpc": "2.0", "id": 15, "method": "resources/read", "params": {"uri": f"jard-annotation://{aid}"}}),
                json.dumps({"jsonrpc": "2.0", "id": 16, "method": "resources/read", "params": {"uri": f"jard-annotation://{aid}/contact-sheet"}}),
                # Error cases
                '{"jsonrpc":"2.0","id":17,"method":"tools/call","params":{"name":"get_transcript","arguments":{"meeting_id":"latest","start":1e309}}}',
                '{"jsonrpc":"2.0","id":18,"method":"unknown/method"}',
                json.dumps({"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}}),
                json.dumps({"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {"name": "get_transcript", "arguments": {"meeting_id": "latest", "start": 0, "from": 0}}}),
                json.dumps({"jsonrpc": "2.0", "id": 21, "method": "resources/read", "params": {"uri": "jard-meeting://deadbeef"}}),
            ]
            stdin_payload = "\n".join(requests) + "\n"
            py, rs = self.run_both(["mcp"], env, stdin_text=stdin_payload)
            self.assertEqual(py.returncode, 0, py.stderr)
            self.assertEqual(rs.returncode, 0, rs.stderr)

            py_lines = [json.loads(line) for line in py.stdout.splitlines() if line.strip()]
            rs_lines = [json.loads(line) for line in rs.stdout.splitlines() if line.strip()]
            self.assertEqual(len(rs_lines), len(py_lines))
            for idx, (p, r) in enumerate(zip(py_lines, rs_lines)):
                # For tools/call, compare parsed JSON payload inside content[0].text so key order doesn't matter
                if "result" in p and isinstance(p["result"].get("content"), list) and not p["result"].get("isError"):
                    self.assertEqual(r.get("id"), p.get("id"), f"resp #{idx}")
                    self.assertEqual(len(r["result"]["content"]), len(p["result"]["content"]), f"resp #{idx}")
                    self.assertEqual(
                        json.loads(r["result"]["content"][0]["text"]),
                        json.loads(p["result"]["content"][0]["text"]),
                        f"tool payload mismatch at resp #{idx} (id={p.get('id')})",
                    )
                    if len(p["result"]["content"]) > 1:
                        self.assertEqual(r["result"]["content"][1:], p["result"]["content"][1:], f"image mismatch at resp #{idx}")
                else:
                    self.assertEqual(r, p, f"MCP mismatch at resp #{idx} (id={p.get('id')})")

    def test_python_fallback_works_for_unhandled_commands_and_override(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = self._env(base / "home", base / "lib", base / "logs")
            # Subcommand --help delegates seamlessly to Python argparse
            res = subprocess.run(
                [str(self.rust_bin), "meetings", "show", "--help"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("--srt", res.stdout)

            # JARO_CLI_PYTHON=1 forces Python even for native commands
            env_py = {**env, "JARO_CLI_PYTHON": "1"}
            res2 = subprocess.run(
                [str(self.rust_bin), "meetings", "list", "--json"],
                env=env_py,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(res2.returncode, 0, res2.stderr)
            self.assertEqual(json.loads(res2.stdout), [])

    def test_packaged_app_bundle_fallback_and_info_plist_bundle_id(self):
        import shutil

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            prefs = home / "Library/Preferences"
            prefs.mkdir(parents=True)
            custom_bundle_id = "local.jaro.customtest"
            plist_path = prefs / f"{custom_bundle_id}.plist"
            with plist_path.open("wb") as f:
                plistlib.dump(
                    {
                        "JardSystemDictation": {"retention": 7, "previousRetention": 7},
                        "JardSystemDictationHistory": [
                            {"id": "b1", "time": time.time() - 10, "text": "Bundled bundle ID read works."}
                        ],
                    },
                    f,
                )
            os.chmod(plist_path, 0o600)

            app = base / "Jaro.app/Contents"
            helpers = app / "Helpers"
            py_bin = app / "Resources/PythonRuntime/bin"
            helper_dir = app / "Resources/engine/desktop/build-tools"
            helpers.mkdir(parents=True)
            py_bin.mkdir(parents=True)
            helper_dir.mkdir(parents=True)

            with (app / "Info.plist").open("wb") as f:
                plistlib.dump({"CFBundleIdentifier": custom_bundle_id}, f)

            bundled_cli = helpers / "jardctl"
            shutil.copy2(self.rust_bin, bundled_cli)
            os.symlink(sys.executable, py_bin / "python3.13")
            # Stub helper.py that echoes the argv it received so we verify exact helper.c invocation parity.
            (helper_dir / "helper.py").write_text(
                "import json, sys; print(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
            )

            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                "RUMBLE_DATA_DIR": str(base / "lib"),
                "RUMBLE_LOG_DIR": str(base / "logs"),
                "RUMBLE_PORT": "19876",
            }
            # 1. Native `history` reads CFBundleIdentifier from Contents/Info.plist without JARD_DESKTOP_BUNDLE_ID
            hist = subprocess.run(
                [str(bundled_cli), "history", "--json", "--limit", "5"],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(hist.returncode, 0, hist.stderr)
            entries = json.loads(hist.stdout)["entries"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["final_text"], "Bundled bundle ID read works.")

            # 2. Unhandled command delegates to Resources/engine/desktop/build-tools/helper.py with `control <args>`
            fb = subprocess.run(
                [str(bundled_cli), "model", "status", "--json"],
                cwd=base,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(fb.returncode, 0, fb.stderr)
            self.assertEqual(json.loads(fb.stdout), ["control", "model", "status", "--json"])


if __name__ == "__main__":
    unittest.main()
