"""Explicit annotation handoff to existing local coding-agent conversations."""
import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from uuid import UUID

import personal_notes
import session_images
from saved_meetings import timestamp
from saved_annotations import annotation_context, annotation_detail, inline_contact_sheet
from session_store import SessionStore

AGENT_NAMES = {"codex": "Codex", "claude": "Claude Code", "t3": "T3 Code"}
CONTACT_SHEET = "Screenshots from the annotation are in this contact sheet"
BACKGROUND_JOBS = {}
T3_THREAD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:-]{0,99}")
LINE_LIMIT = 16 * 1024 * 1024


def executable(agent):
    home = Path.home()
    name = {"codex": "codex", "claude": "claude", "t3": "t3"}[agent]
    candidates = [Path(path) / name for path in ("/opt/homebrew/bin", "/usr/local/bin", home / ".local/bin")]
    if agent == "codex":
        for app in (Path("/Applications/ChatGPT.app"), home / "Applications/ChatGPT.app"):
            candidates.append(app / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
        candidates.append(home / ".codex/bin/codex")
    elif agent == "claude":
        candidates.append(home / ".claude/local/claude")
    else:
        candidates.insert(0, Path(os.environ.get("T3CODE_HOME") or home / ".t3") / "bin/t3")
    for manager in (home / ".nvm/versions/node", home / ".local/share/mise/installs/node"):
        bins = [path for path in manager.glob(f"*/bin/{name}") if path.is_file()]
        candidates.extend(sorted(bins, key=lambda path: tuple(int(n) for n in re.findall(r"\d+", path.parents[1].name)), reverse=True))
    installed = shutil.which(name)
    if installed:
        candidates.insert(0, Path(installed))
    for path in candidates:
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return path
    raise ValueError(f"Install the {AGENT_NAMES[agent]} command-line tool to find its local conversations.")


def row(identifier, title, directory=""):
    return {"id": identifier, "title": " ".join(str(title or "Untitled conversation").split())[:180],
            "directory": directory if isinstance(directory, str) else ""}


async def launch(program, *arguments, cwd=None):
    # Finder launches Jaro without a login shell. Keep the resolved tool's bin
    # directory on PATH for package-manager shims that invoke Node.
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(program.parent), "/opt/homebrew/bin", "/usr/local/bin", os.defpath,
                                           environment.get("PATH", "")))
    return await asyncio.create_subprocess_exec(
        program, *arguments, cwd=cwd, env=environment, limit=LINE_LIMIT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
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
    base = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3").expanduser().resolve()
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


def session_payload(root, identifier, agent, local_url):
    library = SessionStore(root, read_only=True)
    session = library.get(identifier, include_turns=True)
    if session["status"] not in ("complete", "interrupted") or session.get("finalizationPending"):
        raise ValueError("Wait for the session to finish before sharing its context.")
    lines = ["Use this saved Jaro session as context for my next question.", "", f"# {session['name']}", "",
             f"Recorded: {session['created']}"]
    if provider := session.get("meetingProvider"):
        lines.append(f"Meeting app: {provider}")
    brief = session.get("meetingBrief") or {}
    overview = (session.get("overview") or {}).get("version") or {}
    summary = "\n\n".join(filter(None, (overview.get("summary"), overview.get("markdown"))))
    summary = re.sub(r"⟦(\d+(?:\.\d+)?)⟧", lambda match: f"({timestamp(float(match[1]))})", summary)
    for label, detail in (("Quick brief", brief.get("summary")), ("Overview", summary),
                          ("My Notes", personal_notes.document_text(session["personalNotes"]["document"]))):
        if detail:
            lines.extend(["", f"## {label}", "", detail])
    transcript = session["transcript"]
    names = transcript.get("speakerNames") or {}
    segments = session.get("meetingTurns") or transcript.get("segments") or []
    lines.extend(["", "## Transcript", ""])
    for segment in segments:
        name = segment.get("speaker")
        label = names.get(str(name)) or (f"Speaker {name}" if name is not None else "")
        start = timestamp(segment["start"]) if segment.get("start") is not None else ""
        prefix = " · ".join(filter(None, (start, label)))
        if segment.get("text"):
            lines.append((f"**{prefix}**  \n" if prefix else "") + segment["text"])
    if not segments:
        lines.append(transcript.get("text") or "No speech was transcribed.")
    if interruption := transcript.get("interruption"):
        lines.extend(["", interruption])

    images = []
    annotation = session.get("annotation") or {}
    if annotation:
        detail = annotation_detail(library, identifier)
        visual_context = annotation_context({**detail, "transcript": ""})["markdown"].partition("## Narration")[0].strip()
        lines.extend(["", "## Screen annotation", "", visual_context.removeprefix("# Screen annotation").strip()])
    with library.connection() as db:
        attachments = session_images.list_images(db, identifier)
        references = [item["url"] for item in attachments]
        if annotation.get("contactSheetUrl"):
            references.insert(0, annotation["contactSheetUrl"])
            if agent != "t3":
                encoded = inline_contact_sheet(library, identifier)
                if not encoded:
                    raise ValueError("The saved annotation images could not be attached.")
                images.append(("image/png", encoded))
        if agent != "t3":
            for attachment in attachments:
                image = session_images.get(db, identifier, attachment["id"])
                if image:
                    images.append(("image/jpeg", base64.b64encode(image).decode("ascii")))
    if references:
        if agent == "t3":
            lines.extend(["", "## Images", "", "Read these local images while Jaro is running.",
                          *(f"- {local_url}{path}" for path in references)])
        else:
            lines.extend(["", f"{len(images)} image files are attached in gallery order."])
    text = "\n".join(lines) + "\n"
    return text, images, bool(references), bool(annotation)


def check_size(agent, text, images):
    if len(text.encode()) > (120_000 if agent == "t3" else 256 * 1024):
        raise ValueError("The full session exceeds this agent’s message limit.")
    if sum(len(data) for _, data in images) > 13 * 1024 * 1024:
        raise ValueError("The images exceed this agent’s attachment limit.")
    if agent == "claude" and any(len(data) > 5 * 1024 * 1024 for _, data in images):
        raise ValueError("An image exceeds Claude Code’s attachment limit.")


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
