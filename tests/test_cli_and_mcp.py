from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from annotation_assets import staged_annotation_paths
from engine_paths import ENGINE_ROOT
import jardctl
import meeting_mcp
from personal_notes import NoteEdit, save as save_note
from portable_dictation_history import PortableDictationHistory
import saved_dictations
from session_store import SessionStore


class CliAndMcpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.library_root = self.root / "library"
        self.cli = [sys.executable, str(ENGINE_ROOT / "jardctl.py")]
        self.env = {
            **os.environ,
            "RUMBLE_DATA_DIR": str(self.library_root),
            "RUMBLE_PORT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_cli(self, *args, stdin=None, check=True):
        result = subprocess.run(
            [*self.cli, *args],
            input=stdin,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def run_mcp(self, *requests):
        payload = "".join(
            (item if isinstance(item, str) else json.dumps({"jsonrpc": "2.0", **item})) + "\n"
            for item in requests
        )
        output = self.run_cli("mcp", stdin=payload).stdout
        return [json.loads(line) for line in output.splitlines()]

    def seed_library(self):
        store = SessionStore(self.library_root)
        # 1. Regular non-meeting session
        audio1 = self.root / "voice-memo.wav"
        audio1.write_bytes(b"memo audio")
        session_id, _ = store.create(
            audio1, "Architecture memo", "audio/wav",
            {"recording": True, "diarization": True, "meeting": False}, 8,
        )
        store.append(session_id, {
            "text": "Keep the CLI simple and fast.",
            "language": "English",
            "words": [],
            "segments": [{"speaker": 1, "start": 1.0, "end": 4.0, "text": "Keep the CLI simple and fast."}],
        }, 4)
        store.finish(session_id)
        with store.connection() as db:
            save_note(db, session_id, NoteEdit(revision=0, document={
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Ship sessions in MCP."}]}],
            }))

        # 2. Meeting session
        audio2 = self.root / "sync.wav"
        audio2.write_bytes(b"sync audio")
        meeting_id, _ = store.create(
            audio2, "Weekly sync", "audio/wav",
            {"recording": True, "diarization": True, "meeting": True, "meetingProvider": "Zoom"}, 10,
        )
        store.append(meeting_id, {
            "text": "Review the quarterly roadmap.",
            "language": "English",
            "words": [],
            "segments": [{"speaker": 2, "start": 2.0, "end": 5.0, "text": "Review the quarterly roadmap."}],
        }, 5)
        store.finish(meeting_id)

        # 3. Screen annotation session
        movie = self.root / "demo.mp4"
        movie.write_bytes(b"demo movie")
        artifacts = staged_annotation_paths(movie)
        artifacts["manifest"].write_text(json.dumps({
            "version": 1,
            "coordinateSpace": "normalized-bottom-left",
            "events": [
                {"stroke": 1, "time": 1.2, "liftedAt": 2.4, "from": [0.1, 0.2], "to": [0.4, 0.5]},
            ],
        }))
        artifacts["contact-sheet"].write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
            b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03\x02\x00\xee\x8c\x2f\x5b\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        annotation_id, _ = store.create(
            movie, "Button layout bug.mp4", "video/mp4",
            {"recording": False, "diarization": False, "languageHints": ["en"]}, 6,
            annotation={"version": 1, "source": "display", "frames": [1.0, 3.0], "strokeCount": 1},
        )
        store.append(annotation_id, {
            "text": "The submit button clips on narrow screens.",
            "language": "English",
            "words": [],
            "segments": [{"speaker": None, "start": 0.5, "end": 3.5, "text": "The submit button clips on narrow screens."}],
        }, 3.5)
        store.finish(annotation_id)
        return session_id, meeting_id, annotation_id

    def test_empty_library_works_offline_without_creating_database(self):
        self.assertFalse(self.library_root.exists())
        self.assertEqual(json.loads(self.run_cli("meetings", "list", "--json").stdout), [])
        self.assertEqual(json.loads(self.run_cli("sessions", "list", "--json").stdout), [])
        self.assertEqual(json.loads(self.run_cli("annotate", "list", "--json").stdout), {"version": 1, "annotations": []})
        self.assertEqual(json.loads(self.run_cli("meetings", "search", "anything", "--json").stdout), [])
        self.assertEqual(json.loads(self.run_cli("sessions", "search", "anything", "--json").stdout), [])

        init, tools, m_list, s_list, s_search, missing = self.run_mcp(
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"id": 2, "method": "tools/list", "params": None},
            {"id": 3, "method": "tools/call", "params": {"name": "list_meetings", "arguments": None}},
            {"id": 4, "method": "tools/call", "params": {"name": "list_sessions", "arguments": None}},
            {"id": 5, "method": "tools/call", "params": {"name": "search_transcripts", "arguments": {"query": "roadmap"}}},
            {"id": 6, "method": "tools/call", "params": {"name": "get_transcript", "arguments": {"meeting_id": "latest"}}},
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "jaro")
        self.assertIn("instructions", init["result"])
        self.assertTrue(all(
            t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"]
            for t in tools["result"]["tools"]
        ))
        self.assertEqual(json.loads(m_list["result"]["content"][0]["text"]), {"meetings": []})
        self.assertEqual(json.loads(s_list["result"]["content"][0]["text"]), {"sessions": []})
        self.assertEqual(json.loads(s_search["result"]["content"][0]["text"])["totalMatches"], 0)
        self.assertTrue(missing["result"]["isError"])
        self.assertEqual(missing["result"]["content"][0]["text"], "Meeting not found.")
        self.assertFalse(self.library_root.exists())

    def test_mcp_protocol_handles_blank_lines_nulls_nan_and_broken_pipe(self):
        responses = self.run_mcp(
            "",
            "   ",
            {"id": 1, "method": "ping", "params": None},
            '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_transcript","arguments":{"meeting_id":"latest","start":NaN}}}',
            {"id": 3, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {"kind": "invalid"}}},
            {"id": 4, "method": "tools/call", "params": {"name": "get_transcript", "arguments": {"meeting_id": "latest", "session_id": "latest"}}},
            '{"jsonrpc":"2.0","id":1e309,"method":"ping"}',
            {"id": 6, "method": "ping"},
        )
        self.assertEqual(len(responses), 6)
        self.assertEqual(responses[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(responses[1]["error"]["code"], -32700)
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])
        self.assertEqual(responses[4]["error"]["code"], -32700)
        self.assertEqual(responses[5], {"jsonrpc": "2.0", "id": 6, "result": {}})

        reader = SessionStore(self.library_root, read_only=True)
        with patch.object(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n')), \
                patch("builtins.print", side_effect=BrokenPipeError):
            meeting_mcp.serve(reader)

    def test_sessions_and_meetings_in_cli_and_mcp(self):
        session_id, meeting_id, annotation_id = self.seed_library()

        # CLI: meetings list sees only meetings; sessions list sees all recordings
        listed_meetings = json.loads(self.run_cli("meetings", "list", "--json").stdout)
        listed_sessions = json.loads(self.run_cli("sessions", "list", "--json").stdout)
        self.assertEqual([row["id"] for row in listed_meetings], [meeting_id])
        self.assertEqual({row["id"] for row in listed_sessions}, {session_id, meeting_id, annotation_id})

        shown_session = json.loads(self.run_cli("sessions", "show", session_id[:8], "--json").stdout)
        self.assertEqual(shown_session["id"], session_id)
        self.assertEqual(shown_session["turns"][0]["text"], "Keep the CLI simple and fast.")
        self.assertEqual(shown_session["notes"]["personalNotes"], "Ship sessions in MCP.")
        self.assertIn("Keep the CLI simple and fast.", self.run_cli("sessions", "show", session_id, "--md").stdout)

        search_hits = json.loads(self.run_cli("sessions", "search", "simple", "--json").stdout)
        self.assertEqual([hit["id"] for hit in search_hits], [session_id])

        # MCP: list_sessions, search_transcripts, get_transcript, get_meeting_notes, resources, strokes
        (all_sessions, only_regular, meeting_search, all_search,
         regular_transcript, latest_meeting_transcript, regular_notes,
         templates, session_resource, annotation_with_strokes) = self.run_mcp(
            {"id": 1, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {}}},
            {"id": 2, "method": "tools/call", "params": {"name": "list_sessions", "arguments": {"kind": "session"}}},
            {"id": 3, "method": "tools/call", "params": {"name": "search_transcripts", "arguments": {"query": "simple"}}},
            {"id": 4, "method": "tools/call", "params": {"name": "search_transcripts", "arguments": {"query": "simple", "include_sessions": True}}},
            {"id": 5, "method": "tools/call", "params": {"name": "get_transcript", "arguments": {"session_id": session_id}}},
            {"id": 6, "method": "tools/call", "params": {"name": "get_transcript", "arguments": {"meeting_id": "latest"}}},
            {"id": 7, "method": "tools/call", "params": {"name": "get_meeting_notes", "arguments": {"session_id": session_id}}},
            {"id": 8, "method": "resources/templates/list"},
            {"id": 9, "method": "resources/read", "params": {"uri": f"jard-session://{session_id}"}},
            {"id": 10, "method": "tools/call", "params": {"name": "get_annotation", "arguments": {"annotation_id": annotation_id, "include_strokes": True}}},
        )
        self.assertEqual(
            {row["id"]: row["kind"] for row in json.loads(all_sessions["result"]["content"][0]["text"])["sessions"]},
            {session_id: "session", meeting_id: "meeting", annotation_id: "annotation"},
        )
        self.assertEqual(
            [row["id"] for row in json.loads(only_regular["result"]["content"][0]["text"])["sessions"]],
            [session_id],
        )
        self.assertEqual(json.loads(meeting_search["result"]["content"][0]["text"])["passages"], [])
        self.assertEqual(json.loads(all_search["result"]["content"][0]["text"])["passages"][0]["id"], session_id)
        self.assertEqual(json.loads(regular_transcript["result"]["content"][0]["text"])["turns"][0]["text"], "Keep the CLI simple and fast.")
        self.assertEqual(json.loads(latest_meeting_transcript["result"]["content"][0]["text"])["id"], meeting_id)
        self.assertEqual(json.loads(regular_notes["result"]["content"][0]["text"])["personalNotes"], "Ship sessions in MCP.")
        self.assertIn("jard-session://{session_id}", {t["uriTemplate"] for t in templates["result"]["resourceTemplates"]})
        self.assertIn("Keep the CLI simple and fast.", session_resource["result"]["contents"][0]["text"])
        annotation_data = json.loads(annotation_with_strokes["result"]["content"][0]["text"])
        self.assertEqual(annotation_data["strokes"], [{"stroke": 1, "start": 1.2, "end": 2.4, "segments": 1}])

    def test_mcp_list_dictations_and_portable_vocabulary_search(self):
        self.library_root.mkdir(parents=True)
        history = PortableDictationHistory(self.library_root)
        history.initialize()
        now = datetime.now(timezone.utc).timestamp()
        history.record({
            "id": "d1", "time": now - 60, "text": "Spoke with May Lean about the schedule.",
            "app": "Notes", "seconds": 3.0, "inferenceMs": 40, "totalMs": 80, "kind": "dictation",
        })
        history.record({
            "id": "d2", "time": now - 30, "text": "Check the deployment status.",
            "app": "Terminal", "seconds": 2.0, "inferenceMs": 30, "totalMs": 60, "kind": "dictation",
        })

        # Portable vocabulary add/merge/list/remove
        added = jardctl.portable_vocabulary(self.library_root, "add", term="MeiLin", sounds_like=["May Lean", "May Linn"])
        self.assertTrue(added["changed"])
        self.assertEqual(added["entries"], [{"term": "MeiLin", "soundsLike": ["May Lean", "May Linn"]}])
        same = jardctl.portable_vocabulary(self.library_root, "add", term="meilin", sounds_like=["may lean"])
        self.assertFalse(same["changed"])

        # Portable history search uses vocabulary aliases
        found = history.page(search="MeiLin", legacy=False)
        self.assertEqual([entry["id"] for entry in found["entries"]], ["d1"])

        # Cross-platform shared_vocabulary and search_vocabulary read portable DB on non-macOS
        with patch.object(saved_dictations.sys, "platform", "linux"), \
                patch.object(saved_dictations, "library_path", return_value=self.library_root):
            self.assertEqual(saved_dictations.shared_vocabulary(), ("MeiLin",))
            self.assertEqual(saved_dictations.search_vocabulary(), (("MeiLin", ("May Lean", "May Linn")),))

        # MCP list_dictations uses the history reader
        reader = SessionStore(self.library_root, read_only=True)
        response = meeting_mcp.reply(
            reader,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "list_dictations", "arguments": {"app": "Terminal", "limit": 5}}},
            history_reader=lambda **filters: history.page(**filters),
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual([entry["id"] for entry in payload["entries"]], ["d2"])

        removed = jardctl.portable_vocabulary(self.library_root, "remove", term="MEILIN")
        self.assertTrue(removed["changed"])
        self.assertEqual(removed["entries"], [])

    def test_trailing_json_flag_and_cross_platform_status_and_guards(self):
        # Trailing --json works on status (previously failed!)
        status = json.loads(self.run_cli("status", "--json").stdout)
        self.assertEqual(status["engine"], "stopped")
        self.assertEqual(status["model"], "Reson8/Rumble-2-Flash")

        # Portable status checks SDK service instead of macOS port 8765
        output = io.StringIO()
        with patch.object(jardctl.sys, "platform", "linux"), \
                patch.object(jardctl, "portable_engine_status", return_value="ready") as check, \
                patch("sys.argv", ["jardctl", "status", "--json"]), \
                patch("sys.stdout", output):
            jardctl.main()
        check.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["engine"], "ready")

        # macOS-only commands exit cleanly with a clear error on Linux instead of crashing
        errors = io.StringIO()
        with patch.object(jardctl.sys, "platform", "linux"), \
                patch("sys.argv", ["jardctl", "start"]), \
                patch("sys.stderr", errors):
            with self.assertRaises(SystemExit) as failure:
                jardctl.main()
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("macOS", errors.getvalue())

    def test_portable_agent_setup_endpoint(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        import server

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(portable_workspace=False)))
        with self.assertRaises(HTTPException) as missing:
            server.portable_agent_setup(request)
        self.assertEqual(missing.exception.status_code, 404)

        request.app.state.portable_workspace = True
        with patch("portable_login.installed_launcher", return_value=Path("/usr/bin/jaro")):
            response = server.portable_agent_setup(request)
        self.assertEqual(json.loads(response.body), {"cli": "/usr/bin/jardctl"})
        with patch("portable_login.installed_launcher", return_value=None):
            fallback = server.portable_agent_setup(request)
        self.assertEqual(json.loads(fallback.body), {"cli": "jardctl"})
