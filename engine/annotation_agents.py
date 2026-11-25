"""Explicit annotation handoff to existing local coding-agent conversations."""
import asyncio
import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from saved_annotations import annotation_context, annotation_detail, inline_contact_sheet
from session_agent_context import check_size
from session_store import SessionStore

AGENT_NAMES = {"codex": "Codex", "claude": "Claude Code", "t3": "T3 Code"}
CONTACT_SHEET = "Screenshots from the annotation are in this contact sheet"
BACKGROUND_JOBS = {}
T3_THREAD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:-]{0,99}")
LINE_LIMIT = 16 * 1024 * 1024
NPM_PACKAGES = {"codex": "@openai/codex", "claude": "@anthropic-ai/claude-code", "t3": "t3"}


def t3_home():
    return Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3").expanduser().resolve()


def windows_agent_command(agent):
    home = Path.home()
    local_apps = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
    app_data = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
    bins = [Path(item.strip('"')) for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    bins.extend((home / ".local/bin", local_apps / "Microsoft/WinGet/Links", app_data / "npm"))
    if agent == "t3":
        if custom_bin := os.environ.get("T3CODE_INSTALL_BIN_DIR"):
            bins.append(Path(custom_bin))
        bins.append(t3_home() / "bin")
    bins = list(dict.fromkeys(path for path in bins if path.is_absolute()))
    if direct := next((folder / f"{agent}.exe" for folder in bins if (folder / f"{agent}.exe").is_file()), None):
        return (direct,)
    if agent == "t3":
        versions = t3_home() / "runtime/versions"
        for folder in bins:
            with contextlib.suppress(OSError, ValueError):
                # Follow the launcher selected by the official installer without invoking a command shell.
                shim = (folder / "t3.cmd").read_text(encoding="utf-8-sig")
                if entry := re.fullmatch(r'@echo off\n"([^"\n]+)" %\*\s*', shim):
                    target = Path(entry[1]).resolve(strict=True)
                    parts = target.relative_to(versions).parts
                    if len(parts) == 2 and parts[1].lower() == "t3.exe" and target.is_file():
                        return (target,)
    node_bins = bins + [Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "nodejs"]
    for folder in bins:
        if not (folder / f"{agent}.cmd").is_file():
            continue
        with contextlib.suppress(OSError, ValueError, AttributeError, TypeError, RuntimeError):
            package = (folder / "node_modules" / NPM_PACKAGES[agent]).resolve()
            manifest = json.loads((package / "package.json").read_bytes())
            if manifest.get("name") != NPM_PACKAGES[agent]:
                continue
            entries = manifest.get("bin")
            script = (package / (entries.get(agent) if isinstance(entries, dict) else entries)).resolve(strict=True)
            if not script.is_relative_to(package) or not script.is_file():
                continue
            if script.suffix.lower() == ".exe":
                return (script,)
            if script.suffix.lower() in (".js", ".mjs", ".cjs"):
                node = next((path / "node.exe" for path in [folder, *node_bins] if (path / "node.exe").is_file()), None)
                if node:
                    return (node, script)
    return None


def executable(agent):
    if sys.platform == "win32":
        if program := windows_agent_command(agent):
            return program
        raise ValueError(f"Install the {AGENT_NAMES[agent]} command-line tool to find its local conversations.")
    home = Path.home()
    name = agent
    directories = ["/usr/local/bin", home / ".local/bin"]
    if sys.platform == "darwin":
        directories.insert(0, "/opt/homebrew/bin")
    candidates = [Path(path) / name for path in directories]
    if agent == "codex":
        if sys.platform == "darwin":
            for app in (Path("/Applications/ChatGPT.app"), home / "Applications/ChatGPT.app"):
                candidates.append(app / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
        candidates.append(home / ".codex/bin/codex")
    elif agent == "claude":
        candidates.append(home / ".claude/local/claude")
    else:
        candidates.insert(0, t3_home() / "bin/t3")
    for manager in (home / ".nvm/versions/node", home / ".local/share/mise/installs/node"):
        bins = [path for path in manager.glob(f"*/bin/{name}") if path.is_file()]
        candidates.extend(sorted(bins, key=lambda path: tuple(int(n) for n in re.findall(r"\d+", path.parents[1].name)), reverse=True))
    installed = shutil.which(name)
    if installed:
        candidates.insert(0, Path(installed))
    for path in candidates:
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return (path,)
    raise ValueError(f"Install the {AGENT_NAMES[agent]} command-line tool to find its local conversations.")


def row(identifier, title, directory=""):
    return {"id": identifier, "title": " ".join(str(title or "Untitled conversation").split())[:180],
            "directory": directory if isinstance(directory, str) else ""}


async def launch(program, *arguments, cwd=None):
    # Desktop launchers may omit the login shell's paths. Keep the resolved tool's bin on PATH.
    environment = os.environ.copy()
    defaults = ("/opt/homebrew/bin", "/usr/local/bin", os.defpath) if sys.platform == "darwin" else (
        ("/usr/local/bin", os.defpath) if os.name != "nt" else ())
    environment["PATH"] = os.pathsep.join((str(program[0].parent), *defaults, environment.get("PATH", "")))
    return await asyncio.create_subprocess_exec(
        *program, *arguments, cwd=cwd, env=environment, limit=LINE_LIMIT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


async def stop_process(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.communicate()


async def send_json(process, payload):
    process.stdin.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
    await process.stdin.drain()


async def read_json(process):
    while raw := await process.stdout.readline():
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


async def codex_rpc(process, request_id, method, params):
    await send_json(process, {"id": request_id, "method": method, "params": params})
    while (event := await read_json(process)) is not None:
        if event.get("id") == request_id and "method" not in event:
            if isinstance(event.get("result"), dict):
                return event["result"]
            raise ValueError(f"Codex declined {method}. Check that the chat is available and idle.")
    raise ValueError("Codex closed the connection before responding.")


async def codex_server(program):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    control = home / "app-server-control/app-server-control.sock"
    args = ("proxy", "--sock", str(control)) if control.is_socket() else ("--stdio",)
    process = await launch(program, "app-server", *args)
    try:
        await codex_rpc(process, 1, "initialize", {"clientInfo": {"name": "jaro", "title": "Jaro", "version": "1"}})
        await send_json(process, {"method": "initialized"})
        return process
    except BaseException:
        await stop_process(process)
        raise


async def codex_destinations(program, query):
    process = await codex_server(program)
    try:
        params = {"archived": False, "limit": 60}
        if query:
            params["searchTerm"] = query
        data = (await codex_rpc(process, 2, "thread/list", params))["data"]
        return [row(thread["id"], thread.get("name") or thread.get("preview"), thread.get("cwd"))
                for thread in data if thread.get("canAcceptDirectInput") is not False and not thread.get("parentThreadId")]
    finally:
        await stop_process(process)


def claude_destinations(query, target=None):
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    recent = []
    for path in projects.glob(f"*/{target or '*'}.jsonl"):
        try:
            recent.append((path.stat().st_mtime, path))
        except OSError:
            continue
    sessions = {}
    for _, path in sorted(recent, reverse=True)[:500]:
        try:
            identifier = str(UUID(path.stem))
            with path.open("rb") as source:
                events = source.read(128 * 1024).splitlines()
        except (OSError, ValueError):
            continue
        title, cwd = "", ""
        for line in events:
            try:
                event = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            if not isinstance(event, dict) or event.get("sessionId") != identifier or event.get("isSidechain"):
                continue
            cwd = cwd or event.get("cwd") or ""
            if event.get("type") != "user":
                continue
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = next((part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text"), None)
            if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                title = content
                break
        if cwd and identifier not in sessions:
            session = row(identifier, title, cwd)
            if matches(session, query):
                sessions[identifier] = session
        if len(sessions) == 60:
            break
    return list(sessions.values())


def t3_destinations(query, target=None):
    base = t3_home()
    db = base / "userdata/statev2.sqlite"
    if not db.is_file():
        return []
    try:
        with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
            for table in ("orchestration_v2_projection_threads", "projection_threads"):
                try:
                    rows = connection.execute(f"""
                        SELECT t.thread_id, t.title, coalesce(p.workspace_root, '')
                        FROM {table} AS t
                        LEFT JOIN projection_projects AS p USING (project_id)
                        WHERE t.deleted_at IS NULL AND t.archived_at IS NULL
                            AND (? IS NULL OR t.thread_id = ?)
                            AND (instr(lower(t.title), lower(?)) OR instr(lower(t.thread_id), lower(?))
                                 OR instr(lower(coalesce(p.workspace_root,'')), lower(?)))
                        ORDER BY t.updated_at DESC LIMIT 60
                    """, (target, target, query, query, query)).fetchall()
                except sqlite3.OperationalError:
                    continue
                return [row(*values) for values in rows if T3_THREAD.fullmatch(values[0])]
    except sqlite3.Error as error:
        raise ValueError("The T3 Code conversation list is unavailable.") from error
    return []


def matches(session, query):
    return any(query.casefold() in field.casefold() for field in session.values())


async def destinations(agent, query):
    if agent not in AGENT_NAMES:
        raise ValueError("Choose a coding agent.")
    query = query.strip() if isinstance(query, str) else ""
    if len(query) > 300:
        raise ValueError("Enter a shorter search.")
    program = executable(agent)
    try:
        async with asyncio.timeout(12):
            if agent == "codex":
                sessions = await codex_destinations(program, query)
            else:
                read = claude_destinations if agent == "claude" else t3_destinations
                sessions = await asyncio.to_thread(read, query)
    except (OSError, TimeoutError, KeyError) as error:
        raise ValueError(f"Could not list {AGENT_NAMES[agent]} conversations.") from error
    return {"sessions": sessions}


def annotation_payload(root, identifier, agent):
    library = SessionStore(root, read_only=True)
    detail = annotation_detail(library, identifier)
    if detail["status"] == "processing":
        raise ValueError("Wait for the annotation narration to finish before sending.")
    prompt = annotation_context(detail)["markdown"]
    if len(prompt.encode()) > 256 * 1024:
        raise ValueError("This narration is too long to send from the picker. Use Copy for Agent.")
    path = detail["contactSheetPath"]
    image = inline_contact_sheet(library, identifier) if path and agent != "t3" else None
    if path and agent != "t3" and not image:
        raise ValueError("Jaro could not attach the contact sheet. Use Copy for Agent to share the capture.")
    if image and agent == "claude" and len(image) > 5 * 1024 * 1024:
        raise ValueError("The contact sheet exceeds Claude Code’s image limit. Use Copy for Agent.")
    if path and agent == "t3":
        prompt += f"\n{CONTACT_SHEET}. Read the local image file: {json.dumps(path, ensure_ascii=False)}\n"
    return prompt, image, bool(path)


async def finish_in_background(process, finished):
    try:
        async with asyncio.timeout(3600):
            while (event := await read_json(process)) is not None:
                if finished(event):
                    break
    except (TimeoutError, OSError, ValueError):
        pass
    finally:
        await stop_process(process)


def keep_running(agent, target, process, finished):
    key = agent, target
    task = asyncio.create_task(finish_in_background(process, finished))
    BACKGROUND_JOBS[key] = task
    task.add_done_callback(lambda _: BACKGROUND_JOBS.pop(key, None))


async def close():
    tasks = list(BACKGROUND_JOBS.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def send_codex(program, target, text, images):
    process = await codex_server(program)
    accepted = False
    try:
        # Keep the saved chat's engine configuration. Refuse an active turn
        # when the running server reports one, and let the server arbitrate.
        resumed = await codex_rpc(process, 2, "thread/resume", {"threadId": target, "excludeTurns": True})
        thread = resumed.get("thread") or {}
        if thread.get("id") != target or thread.get("canAcceptDirectInput") is False:
            raise ValueError("Choose an available Codex conversation.")
        if (thread.get("status") or {}).get("type") == "active":
            raise ValueError("This Codex conversation is busy. Wait for its current turn to finish.")
        inputs = [{"type": "text", "text": text, "text_elements": []}]
        inputs.extend({"type": "image", "url": f"data:{mime};base64,{data}"} for mime, data in images)
        try:
            turn = (await codex_rpc(process, 3, "turn/start", {"threadId": target, "input": inputs}))["turn"]["id"]
        except (TimeoutError, OSError, KeyError) as error:
            raise ValueError("Codex delivery is unconfirmed. Check the conversation before sending again.") from error
        accepted = True
        keep_running("codex", target, process, lambda event: event.get("method") == "turn/completed"
                     and (event.get("params") or {}).get("turn", {}).get("id") == turn)
    finally:
        if not accepted:
            await stop_process(process)


async def send_claude(program, target, text, images):
    sessions = await asyncio.to_thread(claude_destinations, "", target)
    if not sessions or not Path(sessions[0]["directory"]).is_dir():
        raise ValueError("This Claude Code session or its project is unavailable.")
    process = await launch(program, "--print", "--resume", target, "--input-format", "stream-json",
                           "--output-format", "stream-json", "--verbose", "--replay-user-messages",
                           cwd=sessions[0]["directory"])
    accepted = False
    try:
        blocks = [{"type": "text", "text": text}]
        blocks.extend({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
                      for mime, data in images)
        await send_json(process, {"type": "user", "message": {"role": "user", "content": blocks}})
        process.stdin.close()
        while (event := await read_json(process)) is not None:
            if event.get("type") == "result":
                if event.get("is_error"):
                    raise ValueError("Claude Code did not accept the annotation. Check the CLI sign-in and session.")
                return
            kind = event.get("type")
            acknowledged = kind == "assistant" or (kind == "user" and event.get("isReplay") is True)
            if acknowledged and event.get("session_id") == target:
                accepted = True
                keep_running("claude", target, process, lambda item: item.get("type") == "result")
                return
        raise ValueError("Claude Code did not confirm the send. Check the conversation and your CLI sign-in.")
    finally:
        if not accepted:
            await stop_process(process)


async def send_t3(program, target, text):
    if not await asyncio.to_thread(t3_destinations, "", target):
        raise ValueError("This T3 Code conversation is unavailable. Refresh the list.")
    process = await launch(program, "thread", "send", "--json", target, "-")
    try:
        output, _ = await process.communicate(text.encode())
        if process.returncode:
            raise ValueError("Open T3 Code and verify the conversation is available before sending.")
        acknowledgement = json.loads(output)
        if (not isinstance(acknowledgement, dict) or acknowledgement.get("threadId") != target
                or acknowledgement.get("delivery") not in ("started", "queued")):
            raise ValueError("T3 Code delivery is unconfirmed. Check the conversation before sending again.")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("T3 Code did not confirm the send. Check the conversation before sending again.") from error
    finally:
        await stop_process(process)


async def deliver(agent, target, text, images):
    if agent not in AGENT_NAMES:
        raise ValueError("Choose a coding agent.")
    if not isinstance(target, str) or not T3_THREAD.fullmatch(target):
        raise ValueError("Choose a valid conversation.")
    if agent != "t3":
        target = str(UUID(target))
    if (agent, target) in BACKGROUND_JOBS:
        raise ValueError(f"This {AGENT_NAMES[agent]} conversation is still handling a send from Jaro.")
    program = executable(agent)
    check_size(agent, text, images)
    try:
        async with asyncio.timeout(22):
            if agent == "codex":
                await send_codex(program, target, text, images)
            elif agent == "claude":
                await send_claude(program, target, text, images)
            else:
                await send_t3(program, target, text)
    except (OSError, TimeoutError) as error:
        raise ValueError(f"{AGENT_NAMES[agent]} delivery is unconfirmed. Check the conversation before sending again.") from error


async def send_annotation(root, agent, identifier, target):
    identifier = str(UUID(identifier))
    text, image, has_sheet = await asyncio.to_thread(annotation_payload, root, identifier, agent)
    await deliver(agent, target, text, [("image/png", image)] if image else [])
    return {"contactSheet": "reference" if has_sheet and agent == "t3" else "attached" if has_sheet else "none"}


async def open_agent(agent, target):
    bundle = {"codex": "com.openai.codex", "claude": "com.anthropic.claudefordesktop",
              "opencode": "ai.opencode.desktop", "t3": "com.t3tools.t3code"}[agent]
    arguments = ["/usr/bin/open", "-b", bundle]
    if agent == "codex":
        arguments.append("codex://threads/" + str(UUID(target)))
    try:
        process = await asyncio.create_subprocess_exec(*arguments, stdout=asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(process.wait(), 3) == 0
    except (OSError, TimeoutError):
        return False
