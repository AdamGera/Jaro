"""Disposable local agent transports for the signed bridge handoff contract."""
import json
import sqlite3
from contextlib import closing
from uuid import uuid4


def install_agent_fixtures(root, env):
    targets = {"codex": str(uuid4()), "claude": str(uuid4()), "t3": "thread_local_" + str(uuid4())}
    env.update({
        "CODEX_HOME": str(root / "codex-state"),
        "CLAUDE_CONFIG_DIR": str(root / "claude-state"),
        "T3CODE_HOME": str(root / "t3-state"),
        "FIXTURE_AGENT_ROOT": str(root),
        "FIXTURE_AGENT_TARGETS": json.dumps(targets),
    })
    projects = root / "claude-state/projects/disposable"
    projects.mkdir(parents=True)
    (projects / f"{targets['claude']}.jsonl").write_text(json.dumps({
        "sessionId": targets["claude"], "cwd": str(root), "type": "user",
        "message": {"content": "Disposable agent chat"},
    }) + "\n")
    t3 = root / "t3-state/userdata"
    t3.mkdir(parents=True)
    with closing(sqlite3.connect(t3 / "statev2.sqlite")) as db:
        db.execute("CREATE TABLE projection_projects (project_id TEXT, workspace_root TEXT)")
        db.execute("INSERT INTO projection_projects VALUES (?, ?)", ("project", str(root)))
        db.execute("""CREATE TABLE orchestration_v2_projection_threads (
            thread_id TEXT, title TEXT, project_id TEXT, deleted_at TEXT, archived_at TEXT, updated_at TEXT
        )""")
        db.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, 'project', NULL, NULL, '2026-01-01')",
                   (targets["t3"], "Disposable agent chat"))
        db.commit()
    for name in ("codex", "claude", "t3"):
        executable = root / name
        executable.write_text("#!/usr/bin/env python3\n" + TOOL)
        executable.chmod(0o700)
    return targets


TOOL = r"""
import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
root = Path(os.environ["FIXTURE_AGENT_ROOT"])
targets = json.loads(os.environ["FIXTURE_AGENT_TARGETS"])
target = targets[command]

def reply(x):
    print(json.dumps(x), flush=True)

def sent(message):
    (root / (command + "-annotation.json")).write_text(json.dumps(message))

if command == "codex":
    assert sys.argv[1:3] == ["app-server", "--stdio"]
    for line in sys.stdin:
        request = json.loads(line)
        method = request["method"]
        if method == "initialized":
            continue
        result = {}
        params = request["params"]
        if method == "thread/list":
            assert params["archived"] is False
            result["data"] = [{"id": target, "name": "Disposable agent chat", "cwd": str(root)}]
        if method == "thread/resume":
            if params["threadId"] != target:
                reply({"id": request["id"], "error": {"message": "Unknown thread"}})
                continue
            result["thread"] = {"id": target, "status": {"type": "idle"}}
        if method == "turn/start":
            assert params["threadId"] == target
            sent(params["input"])
            result["turn"] = {"id": "turn-fixture"}
        reply({"id": request["id"], "result": result})
        if method == "turn/start":
            reply({"method": "turn/completed", "params": {"turn": {"id": "turn-fixture", "status": "completed"}}})
elif command == "claude":
    assert sys.argv[sys.argv.index("--resume") + 1] == target
    assert Path.cwd() == root.resolve()
    request = json.loads(sys.stdin.readline())
    assert request["type"] == "user"
    sent(request["message"]["content"])
    reply({"type": "result", "session_id": target, "is_error": False})
elif command == "t3":
    assert sys.argv[1:] == ["thread", "send", "--json", target, "-"]
    sent(sys.stdin.read())
    reply({"threadId": target, "runId": "run-fixture", "delivery": "queued"})
"""
