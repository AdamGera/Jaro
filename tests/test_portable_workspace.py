import asyncio
import base64
from contextlib import nullcontext
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import struct
import sys
from tempfile import TemporaryDirectory
from time import monotonic, time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave
from uuid import uuid4

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException

from portable_workspace import BOOTSTRAP, configure_workspace
from server import begin_screencast, discard_screencast, portable_screenshot, screencast_microphones, screencast_status, screenshot_targets
from server import append_screencast_ink, screencast_pen, screencast_preview, screencast_targets, set_screencast_region, start_model_setup, start_workspace_speech
from linux_capture import LinuxRecording, capture_args, default_computer_audio, default_microphone, microphone_inputs
from portable_dictation import OutputContext
from portable_capture import CaptureError, PortableRecording, SAMPLE_RATE, draft_frames, draft_tracks, private_wav, release_cutoff, split_audio
from native_recordings import source_tracks
from session_store import SessionStore
from windows_capture import parse_microphones, WindowsMeetingRecording, WindowsRecording, WindowsScreenAudio


class PortableWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_onboarding_replay_is_owned_persistent_and_keeps_current_setup(self):
        from engine_lifecycle import ClientLeases
        from portable_onboarding import PortableOnboarding
        from server import change_portable_onboarding, connect_browser, windows_onboarding_command
        from windows_dictation_commands import ONBOARDING_ROUTE

        origin = "http://127.0.0.1:43721"
        route = "/api/portable/onboarding"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.post(route)(change_portable_onboarding)
        app.post(ONBOARDING_ROUTE)(windows_onboarding_command)
        state = app.state
        state.draining = False
        state.portable_recording = state.portable_screencast = None
        state.portable_dictation = SimpleNamespace(busy=False, authorizing=False, enabled=True, shortcut="Left Alt")
        state.windows_dictation_control = {"token": "b" * 64}
        state.leases = ClientLeases()
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            root = Path(directory)
            state.portable_onboarding = tour = PortableOnboarding(root)
            first = tour.id
            settings = vars(state.portable_dictation).copy()
            native = SimpleNamespace(app=app, state=SimpleNamespace(portable_desktop=True), headers=headers)
            stream = (await connect_browser(uuid4(), native)).body_iterator
            async def update():
                async with asyncio.timeout(2):
                    async for event in stream:
                        if event.startswith("event: onboarding\n"):
                            return json.loads(event.splitlines()[1].removeprefix("data: "))["id"]

            self.assertEqual(await update(), first)
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                    async def bootstrap(key):
                        return await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record[key]})

                    await bootstrap("workspaceKey")
                    self.assertEqual((await client.post(route, headers=headers, json={"action": "restart"})).status_code, 403)
                    self.assertEqual((await client.post(ONBOARDING_ROUTE, headers=headers)).status_code, 403)
                    await bootstrap("workspaceDesktopKey")
                    self.assertEqual((await client.post(route, json={"action": "restart"})).status_code, 403)
                    state.portable_recording = object()
                    self.assertEqual((await client.post(route, headers=headers, json={"action": "restart"})).status_code, 409)
                    self.assertEqual(tour.id, first)
                    state.portable_recording = None
                    self.assertEqual((await client.post(route, headers=headers, json={"action": "finish", "id": first})).json(), {"id": None})
                    self.assertIsNone(PortableOnboarding(root).id)
                    self.assertIsNone(await update())
                    with patch.dict(os.environ, {"JARO_DESKTOP_TRAY": str(root)}):
                        restarted = (await client.post(ONBOARDING_ROUTE, headers={**headers,
                            "Authorization": "Bearer " + state.windows_dictation_control["token"]})).raise_for_status().json()["id"]
                    self.assertNotEqual(restarted, first)
                    self.assertTrue((root / "open").is_file())
                    self.assertEqual(await update(), restarted)
                    self.assertEqual((await client.post(route, headers=headers, json={"action": "finish", "id": first})).status_code, 409)
                    self.assertIsNotNone(PortableOnboarding(root).id)
                    self.assertEqual(vars(state.portable_dictation), settings)
            finally:
                await stream.aclose()

    async def test_annotation_agent_guides_require_an_owned_desktop(self):
        from annotation_agent_skill import SKILL, guide
        from server import portable_annotation_agent_skill

        origin = "http://127.0.0.1:43721"
        route = "/api/portable/annotation-agent-skill"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.post(route)(portable_annotation_agent_skill)
        app.state.jobs = set()
        app.state.draining = False
        with TemporaryDirectory() as directory, patch("server.URL", origin), patch("server.sys", SimpleNamespace(platform="win32")):
            root = Path(directory)
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root)}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                    async def bootstrap(key):
                        return await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record[key]})

                    request = {"action": "install", "agent": "claude"}
                    await bootstrap("workspaceKey")
                    self.assertEqual((await client.post(route, headers=headers, json=request)).status_code, 403)
                    self.assertFalse((root / SKILL).exists())
                    await bootstrap("workspaceDesktopKey")
                    self.assertEqual((await client.post(route, json=request)).status_code, 403)
                    app.state.draining = True
                    self.assertEqual((await client.post(route, headers=headers, json=request)).status_code, 409)
                    app.state.draining = False
                    response = await client.post(route, headers=headers, json=request)
                    self.assertEqual(response.raise_for_status().headers["cache-control"], "no-store")
                    self.assertEqual((root / SKILL).read_bytes(), guide())
                    self.assertEqual((await client.post(route, headers=headers, json={**request, "agent": "../other"})).status_code, 422)

    async def test_annotation_storage_requires_owned_workspace_and_keeps_selection_during_capture(self):
        from server import portable_annotation_storage, run_linux_annotation_command, windows_annotation_command
        from windows_dictation_commands import ANNOTATION_COMMAND_ROUTE

        origin = "http://127.0.0.1:43721"
        route = "/api/portable/annotation-storage"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.post(route)(portable_annotation_storage)
        app.post(ANNOTATION_COMMAND_ROUTE)(windows_annotation_command)
        state = app.state
        state.portable_screencast = None
        state.portable_recording_lock = asyncio.Lock()
        state.windows_dictation_control = {"token": "b" * 64}
        state.draining = False
        state.jobs = set()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state.library = SessionStore(root / "library")
            state.screen_draft_dir = root / "screen-drafts"
            first, second = root / "First drive", root / "Second drive"
            first.mkdir()
            second.mkdir()
            with (patch("server.URL", origin), patch("server.sys", SimpleNamespace(platform="win32")),
                  patch("desktop_file_dialog.file_dialog", return_value=Path("jaro-save-video")),
                  patch("desktop_file_dialog.choose_file", new_callable=AsyncMock) as choose,
                  patch("desktop_file_dialog.reveal_folder", new_callable=AsyncMock) as reveal):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                    async def action(name):
                        return await client.post(route, headers=headers, json={"action": name})

                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await action("choose")).status_code, 403)
                    choose.assert_not_awaited()
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                    self.assertEqual((await client.post(route, json={"action": "status"})).status_code, 403)
                    self.assertFalse((await action("status")).json()["custom"])
                    choose.return_value = None
                    self.assertFalse((await action("choose")).json()["custom"])
                    choose.return_value = first
                    selected = (await action("choose")).raise_for_status().json()
                    self.assertTrue(selected["available"])
                    self.assertEqual(selected["folder"], str(first.resolve()))
                    choose.assert_awaited_with(Path("jaro-save-video"), "--choose-annotation-storage-folder", str(state.library.audio))
                    await action("reveal")
                    reveal.assert_awaited_once_with(str(first.resolve()))

                    async def interrupted_picker(*_args):
                        state.portable_screencast = SimpleNamespace(phase="recording", saving=False)
                        return second

                    choose.side_effect = interrupted_picker
                    self.assertEqual((await action("choose")).status_code, 409)
                    self.assertEqual((await action("status")).json(), selected)
                    self.assertEqual((await action("reset")).status_code, 409)
                    state.portable_screencast = None

                    command = {"action": "storage", "storageAction": "set", "folder": str(second.resolve())}
                    self.assertEqual((await client.post(ANNOTATION_COMMAND_ROUTE, headers=headers, json=command)).status_code, 403)
                    auth = {**headers, "Authorization": "Bearer " + "b" * 64}
                    changed = (await client.post(ANNOTATION_COMMAND_ROUTE, headers=auth, json=command)).raise_for_status().json()
                    self.assertEqual(changed["folder"], str(second.resolve()))
                    reset = await run_linux_annotation_command(state, {"action": "storage", "storageAction": "reset"})
                    self.assertFalse(reset["custom"])

    async def test_waiting_for_annotation_storage_does_not_keep_restart_busy(self):
        from server import portable_work_in_progress, resume_recordings

        state = SimpleNamespace(status="ready", draining=False, portable_workspace=True, startup_recovery=True,
                                startup_stranded=set(), jobs=set(), submitting=False, model_setup=None,
                                portable_recording=None, portable_screencast=None, portable_dictation=None,
                                insights=SimpleNamespace(model=SimpleNamespace(audio_users=0)),
                                inference=SimpleNamespace(waiting_dictation=False),
                                library=SimpleNamespace(has_pending_recordings=Mock(side_effect=[True, False])))
        guards = []

        async def claim(*_args):
            guards.append(portable_work_in_progress(state))
            return False

        with patch("server.asyncio.sleep", new_callable=AsyncMock), patch("server.recover_pending_recording", side_effect=claim):
            await resume_recordings(state)
        self.assertEqual(guards, [True, False])
        self.assertFalse(state.startup_recovery)

    async def test_rules_manager_creates_a_source_once_before_recording(self):
        from annotation_deliveries import AnnotationDeliveries
        from server import portable_annotation_rules

        origin = "http://127.0.0.1:43723"
        record = {"port": 43723}
        route = "/api/portable/annotation-rules"
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.add_api_route(route, portable_annotation_rules, methods=["POST"])
        app.state.draining = False
        app.state.jobs = set()
        command = {"action": "add", "kind": "app", "source": "ReviewPad.EXE", "target": "ses_new", "service": "a" * 64}
        headers = {"Origin": origin}
        with TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            app.state.library = SimpleNamespace(root=Path(folder))
            with (patch("server.URL", origin), patch("server.sys", SimpleNamespace(platform="win32")),
                  patch("annotation_routing.verify_rule_destination", new_callable=AsyncMock) as verify):
                verify.return_value = (SimpleNamespace(channel="service.json"), {"id": "ses_new", "title": "Review chat"})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await client.post(route, headers=headers, json=command)).status_code, 403)
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                    malformed = {**command, "kind": "site", "source": "https://example.test/%2E%2E/private"}
                    self.assertEqual((await client.post(route, headers=headers, json=malformed)).status_code, 409)
                    verify.assert_not_awaited()
                    results = await asyncio.gather(
                        client.post(route, headers=headers, json=command),
                        client.post(route, headers=headers, json={**command, "source": "windows.reviewpad"}),
                    )
                    self.assertEqual(sorted(response.status_code for response in results), [200, 409])
                    saved, = store.list_rules()
                    self.assertEqual((saved["source"], saved["target"], saved["automatic"]), ("windows.reviewpad", "ses_new", False))
                    self.assertEqual((await client.post(route, headers=headers, json=command)).status_code, 409)
                    self.assertEqual(store.list_rules(), [saved])
                    terminal = {**command, "kind": "terminal", "source": r"C:\Work\Jaro"}
                    response = await client.post(route, headers=headers, json=terminal)
                    self.assertEqual(response.status_code, 200)
                    folder_rule = next(rule for rule in response.json()["rules"] if rule["kind"] == "terminal")
                    self.assertEqual(folder_rule["source"], r"c:\work\jaro")

    async def test_rules_manager_requires_the_desktop_and_retargets_the_exact_rule(self):
        from annotation_deliveries import AnnotationDeliveries
        from annotation_routing import RouteUnavailable
        from server import portable_annotation_rules

        origin = "http://127.0.0.1:43722"
        record = {"port": 43722}
        route = "/api/portable/annotation-rules"
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.add_api_route(route, portable_annotation_rules, methods=["POST"])
        app.state.draining = False
        app.state.jobs = set()
        with TemporaryDirectory() as folder:
            store = AnnotationDeliveries(folder)
            app.state.library = SimpleNamespace(root=Path(folder))
            old = store.remember_rule({"kind": "site", "key": "example.test", "label": "example.test"},
                                      "Earlier chat", "ses_old", "service.json", automatic=True)
            save = {"action": "retarget", "id": old["id"], "target": "ses_new", "service": "a" * 64}
            headers = {"Origin": origin}
            with (patch("server.URL", origin), patch("server.sys", SimpleNamespace(platform="win32")),
                  patch("annotation_routing.verify_rule_destination", new_callable=AsyncMock) as verify):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await client.post(route, headers=headers, json={"action": "list"})).status_code, 403)
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                    self.assertEqual((await client.post(route, json=save)).status_code, 403)
                    self.assertEqual((await client.post(route, headers=headers, json={**save, "automatic": True})).status_code, 422)
                    verify.assert_not_awaited()
                    verify.side_effect = RouteUnavailable("OpenCode is offline.")
                    self.assertEqual((await client.post(route, headers=headers, json=save)).status_code, 409)
                    self.assertEqual(store.list_rules()[0]["id"], old["id"])
                    verify.side_effect = None
                    verify.return_value = (SimpleNamespace(channel="wsl:service.json"), {"id": "ses_new", "title": "New chat"})
                    changed = await client.post(route, headers=headers, json=save)
                    current, = changed.json()["rules"]
                    self.assertEqual((current["source"], current["target"], current["channel"], current["automatic"]),
                                     ("example.test", "ses_new", "wsl:service.json", False))
                    self.assertNotEqual(current["id"], old["id"])
                    self.assertEqual((await client.post(route, headers=headers, json={"action": "remove", "id": old["id"]})).status_code, 409)
                    removed = await client.post(route, headers=headers, json={"action": "remove", "id": current["id"]})
                    self.assertEqual(removed.json()["rules"], [])
                    self.assertEqual(verify.await_count, 2)

    async def test_windows_annotation_handoff_uses_the_desktop_command_credential(self):
        from server import windows_annotation_command
        from windows_dictation_commands import ANNOTATION_COMMAND_ROUTE

        route = ANNOTATION_COMMAND_ROUTE
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.state.windows_dictation_control = {"token": "b" * 64}
        app.add_api_route(route, windows_annotation_command, methods=["POST"])
        payload = {"action": "destinations", "query": "Review"}
        headers = {"Origin": origin}
        auth = {**headers, "Authorization": "Bearer " + "b" * 64}
        reply = {"service": "a" * 64, "sessions": []}
        with (patch("server.URL", origin),
              patch("server.run_portable_annotation_handoff", new_callable=AsyncMock, return_value=reply) as run):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                self.assertEqual((await client.post(route, headers=headers, json=payload)).status_code, 403)
                self.assertEqual((await client.post(route, headers={**auth, "Origin": "https://elsewhere.test"},
                                                    json=payload)).status_code, 403)
                self.assertEqual((await client.post(route, headers=auth, json={"action": "send-session"})).status_code, 400)
                run.assert_not_awaited()
                self.assertEqual((await client.post(route, headers=auth, json=payload)).json(), reply)
                run.assert_awaited_once()
                self.assertEqual(run.await_args.args[1].query, "Review")

    async def test_session_handoff_requires_the_owned_window_and_shares_saved_context(self):
        import personal_notes
        import session_images
        from server import missing_session, portable_annotation_handoff, remove_session_image, session_image
        from session_notes import publish
        from session_store import MissingSession, SessionStore

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/annotation-handoff"
        record = {"port": 43721}
        app = FastAPI()
        configure_workspace(app, record, origin)
        app.add_api_route(endpoint, portable_annotation_handoff, methods=["POST"])
        image_route = "/api/sessions/{key}/images/{image_id}"
        app.add_api_route(image_route, session_image, methods=["GET"])
        app.add_api_route(image_route, remove_session_image, methods=["DELETE"])
        app.add_exception_handler(MissingSession, missing_session)
        app.state.draining = False
        app.state.jobs = set()
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0002 0003 01 01 1100 ffd9")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.wav"
            source.write_bytes(b"fixture audio")
            library = SessionStore(root)
            app.state.library = library
            key, media = library.create(source, source.name, "audio/wav", {"recording": False, "diarization": False}, 3)
            library.append(key, {"text": "Fixture transcript", "language": "English", "words": [], "segments": []}, 3)
            library.finish(key)
            with library.connection() as db:
                publish(db, key, "en", {"language": "en", "title": "Fixture overview",
                                        "summary": "Overview point ⟦2⟧", "markdown": "", "topics": []})
                note = personal_notes.NoteEdit(revision=0, document={"type": "doc", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "User-authored note"}]},
                ]})
                personal_notes.save(db, key, note)
                image = session_images.add(db, key, jpeg)
            headers = {"Origin": origin}
            service = "a" * 64
            payload = {"action": "send-session", "id": key, "target": "ses_fixture", "service": service}
            with (patch("server.URL", origin), patch("server.sys", SimpleNamespace(platform="linux")),
                  patch("annotation_routing.send_session_prompt", new_callable=AsyncMock) as send,
                  patch("annotation_agents.deliver", new_callable=AsyncMock) as deliver):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                    self.assertEqual((await client.post(endpoint, headers=headers, json=payload)).status_code, 403)
                    self.assertEqual((await client.get(image["url"])).status_code, 403)
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await client.post(endpoint, headers=headers, json=payload)).status_code, 403)
                    await client.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                    self.assertEqual((await client.post(endpoint, json=payload)).status_code, 403)
                    wrong_origin = {"Origin": "https://example.test"}
                    self.assertEqual((await client.post(endpoint, headers=wrong_origin, json=payload)).status_code, 403)
                    send.assert_not_awaited()
                    response = await client.post(endpoint, headers=headers, json=payload)
                    self.assertEqual(response.json(), {"images": "attached"})
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    send.assert_awaited_once()
                    body, target, identity = send.await_args.args
                    self.assertEqual((target, identity, body["delivery"]), ("ses_fixture", service, "queue"))
                    for text in ("Overview point (00:02)", "User-authored note", "Fixture transcript"):
                        self.assertIn(text, body["text"])
                    self.assertEqual(body["files"], [{
                        "name": "jaro-image-1.jpg", "uri": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                    }])
                    for agent in ("codex", "claude", "t3"):
                        destination = "fixture_thread" if agent == "t3" else str(uuid4())
                        request = {"action": "send-session", "id": key, "agent": agent, "target": destination}
                        reply = await client.post(endpoint, headers=headers, json=request)
                        self.assertEqual(reply.raise_for_status().json(),
                                         {"images": "references" if agent == "t3" else "attached"})
                        sent_agent, target, text, files = deliver.await_args.args
                        self.assertEqual((sent_agent, target), (agent, destination))
                        for item in ("Overview point (00:02)", "User-authored note", "Fixture transcript"):
                            self.assertIn(item, text)
                        if agent == "t3":
                            self.assertEqual(files, [])
                            self.assertNotIn(origin, text)
                            local_file = Path(json.loads(text.splitlines()[-1].removeprefix("- ")))
                            self.assertEqual(local_file.parent.parent, media.parent)
                            self.assertEqual(local_file.read_bytes(), jpeg)
                        else:
                            self.assertEqual(files, [("image/jpeg", base64.b64encode(jpeg).decode())])
                    (await client.delete(image["url"], headers=headers)).raise_for_status()
                    self.assertFalse(local_file.exists())
                    self.assertEqual((await client.get(image["url"])).status_code, 404)

    async def test_windows_dictation_cli_has_its_own_library_bound_credential(self):
        from sdk_discovery import sdk_proof
        from server import identify_windows_dictation, run_windows_dictation
        from windows_dictation_commands import ROUTE

        origin = "http://127.0.0.1:43211"
        app = FastAPI()
        configure_workspace(app, {"port": 43211, "token": "a" * 64}, origin)
        record = {"apiVersion": 1, "pid": 20, "port": 43211, "libraryId": "b" * 64, "token": "c" * 64}
        app.state.windows_dictation_control = record
        app.state.portable_recording_dir = Path(".")
        command = AsyncMock(return_value={"status": "starting"})
        app.state.portable_dictation = SimpleNamespace(platform="windows", run_command=command)
        app.add_api_route(ROUTE, identify_windows_dictation, methods=["GET"])
        app.add_api_route(ROUTE, run_windows_dictation, methods=["POST"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            headers = {"Origin": origin, "Authorization": "Bearer " + record["token"]}
            with patch("server.URL", origin):
                request = {"action": "start", "raw": True}
                self.assertEqual((await client.post(ROUTE, json=request)).status_code, 403)
                self.assertEqual((await client.post(ROUTE, headers={**headers, "Origin": "https://example.org"},
                                                    json=request)).status_code, 403)
                self.assertEqual((await client.post(ROUTE, headers={**headers, "Authorization": "Bearer " + "a" * 64},
                                                    json=request)).status_code, 403)
                self.assertEqual((await client.post(ROUTE, headers=headers,
                                                    json={"action": "toggle", "printed": True})).status_code, 422)
                command.assert_not_awaited()
                challenge = "d" * 32
                self.assertEqual((await client.get(ROUTE, params={"challenge": challenge})).json(),
                                 {"proof": sdk_proof(record, challenge)})
                self.assertEqual((await client.post(ROUTE, headers=headers, json=request)).json(), {"status": "starting"})
                command.assert_awaited_once_with("start", raw=True, printed=False)

    async def test_windows_meeting_trash_uses_the_owner_command_credential(self):
        from server import trash_meeting_from_windows_command
        from windows_dictation_commands import MEETING_TRASH_ROUTE

        origin = "http://127.0.0.1:43211"
        app = FastAPI()
        configure_workspace(app, {"port": 43211, "token": "a" * 64}, origin)
        app.state.windows_dictation_control = {"token": "b" * 64}
        app.add_api_route(MEETING_TRASH_ROUTE, trash_meeting_from_windows_command, methods=["POST"])
        key = str(uuid4())
        payload = {"id": key}
        headers = {"Origin": origin, "Authorization": "Bearer " + "b" * 64}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            with patch("server.URL", origin), patch("server.remove_saved_session", new_callable=AsyncMock, return_value=True) as remove:
                self.assertEqual((await client.post(MEETING_TRASH_ROUTE, json=payload)).status_code, 403)
                self.assertEqual((await client.post(MEETING_TRASH_ROUTE, headers={**headers, "Authorization": "Bearer " + "a" * 64},
                                                    json=payload)).status_code, 403)
                self.assertEqual((await client.post(MEETING_TRASH_ROUTE, headers={**headers, "Origin": "https://example.org"},
                                                    json=payload)).status_code, 403)
                self.assertEqual((await client.post(MEETING_TRASH_ROUTE, headers=headers, json={"id": "latest"})).status_code, 422)
                remove.assert_not_called()
                response = await client.post(MEETING_TRASH_ROUTE, headers=headers, json=payload)
                self.assertEqual(response.json(), {"id": key, "trashPending": True})
                remove.assert_awaited_once_with(app.state, key, meeting_only=True)

    @unittest.skipUnless(sys.platform == "linux", "Linux abstract sockets and peer credentials")
    async def test_linux_meeting_trash_uses_the_owned_library_socket_without_dictation(self):
        from wayland_dictation_commands import dispatch_meeting_trash, listen

        with TemporaryDirectory() as folder:
            root = Path(folder)
            key = str(uuid4())
            result = {"id": key, "trashPending": False}
            archive = AsyncMock(return_value=result)
            async with await listen(root, None, trash_meeting=archive):
                self.assertEqual(await asyncio.to_thread(dispatch_meeting_trash, root, key), result)
                archive.assert_awaited_once_with(key)
                with self.assertRaisesRegex(RuntimeError, "Open Jaro for this library"):
                    await asyncio.to_thread(dispatch_meeting_trash, root / "different", key)
                with self.assertRaises(ValueError):
                    await asyncio.to_thread(dispatch_meeting_trash, root, "latest")

    async def test_windows_meeting_commands_require_the_owner_credential(self):
        from server import windows_meeting_command
        from windows_dictation_commands import MEETING_COMMAND_ROUTE

        origin = "http://127.0.0.1:43211"
        app = FastAPI()
        configure_workspace(app, {"port": 43211, "token": "a" * 64}, origin)
        app.state.windows_dictation_control = {"token": "b" * 64}
        app.post(MEETING_COMMAND_ROUTE)(windows_meeting_command)
        headers = {"Origin": origin, "Authorization": "Bearer " + "b" * 64}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            with patch("server.URL", origin), patch("server.run_portable_meeting_command", new_callable=AsyncMock,
                                                   return_value={"phase": "recording"}) as command:
                payload = {"action": "record", "title": "Réunion"}
                for rejected in ({}, {**headers, "Authorization": "Bearer " + "a" * 64},
                                 {**headers, "Origin": "https://example.org"}):
                    self.assertEqual((await client.post(MEETING_COMMAND_ROUTE, headers=rejected, json=payload)).status_code, 403)
                self.assertEqual((await client.post(MEETING_COMMAND_ROUTE, headers=headers, json={"action": "cancel"})).status_code, 422)
                command.assert_not_awaited()
                response = await client.post(MEETING_COMMAND_ROUTE, json=payload, headers=headers)
                self.assertEqual(response.json(), {"phase": "recording"})
                self.assertEqual(command.await_args.args[1].model_dump(), payload)

    async def test_portable_meeting_commands_use_both_sources_and_keep_a_named_draft(self):
        from portable_calls import CallWatcher
        from portable_capture import draft_metadata
        from server import PortableMeetingCommand, begin_portable_recording, finish_portable_capture, run_portable_meeting_command
        from session_store import SessionStore

        with TemporaryDirectory() as directory:
            root = Path(directory)
            library = SessionStore(root / "library")
            spool = root / "drafts"
            spool.mkdir()
            opening = asyncio.Event()
            ready = asyncio.Event()
            ready.set()

            def recorder(key, path, mode):
                self.assertEqual(mode, "both")

                async def start():
                    opening.set()
                    await ready.wait()
                    for output in (path, *source_tracks(path).values()):
                        with private_wav(output) as (wav, _):
                            wav.writeframes(b"\x01\x02" * 1600)

                return SimpleNamespace(key=key, start=start, stop=AsyncMock(), frames=1600, worker=asyncio.Future())

            state = SimpleNamespace(library=library, draining=False, status="ready", portable_recording=None,
                                    portable_recording_dir=spool, portable_recording_lock=asyncio.Lock(),
                                    portable_screencast=None, portable_dictation=None,
                                    portable_meeting_recorder=recorder, diarizer=object(), jobs=set())

            async def start(languages, _call, *, title=""):
                return (await begin_portable_recording(state, "both", True, languages, title=title))["id"]

            async def finish(key):
                return await finish_portable_capture(state, key, (), diarization=True)

            async def transcribe(_state, key, *_args, **_options):
                text = "Design review has one decision."
                library.append(key, {"text": text, "segments": [dict(start=0, end=.1, speaker=1, text=text)], "words": []}, .1)
                library.finish(key)

            async def command(action, **options):
                return await run_portable_meeting_command(state, PortableMeetingCommand(action=action, **options))

            state.portable_calls = CallWatcher(state, AsyncMock(), AsyncMock(), start, finish, lambda: True)
            ordinary = SimpleNamespace(key=uuid4(), meeting=False, stop=AsyncMock())
            state.portable_recording = ordinary
            self.assertEqual(await command("status"), {"phase": "unavailable"})
            self.assertFalse((await command("stop"))["changed"])
            with self.assertRaises(HTTPException) as busy:
                await command("record")
            self.assertEqual(busy.exception.status_code, 409)
            ordinary.stop.assert_not_awaited()
            state.portable_recording = None
            with (patch("recording_power.keep_awake_until"), patch("server.audio_duration", return_value=.1),
                  patch("server.process_saved_audio", side_effect=transcribe),
                  patch("desktop_clipboard.copy_text", new_callable=AsyncMock) as clipboard):
                started = await command("record", title="  Réunion  de conception  ")
                key = started["id"]
                self.assertTrue(started["changed"])
                self.assertEqual(started["title"], "Réunion de conception")
                self.assertEqual(draft_metadata(spool / f"{key}.wav")["meetingTitle"], "Réunion de conception")
                self.assertEqual((await command("status"))["id"], key)
                self.assertEqual(state.portable_calls.status()["active"]["name"], "Réunion de conception")
                self.assertFalse((await command("record", title="Ignore duplicate"))["changed"])
                self.assertEqual((await command("stop"))["id"], key)
                await asyncio.gather(*state.jobs)
                saved = library.get(key)
                self.assertTrue(saved["meeting"])
                self.assertEqual(saved["meetingTitle"], "Réunion de conception")
                self.assertEqual(set(saved["recordingTracks"]), {"mic", "system"})
                self.assertTrue(saved["diarization"])
                self.assertIsNone(state.portable_calls.active)
                self.assertEqual(state.portable_calls.completed, key)
                self.assertEqual(await command("copy-last"), {"id": key, "copied": True})
                self.assertIn("Design review", clipboard.await_args.args[0])

                opening.clear()
                ready.clear()
                recording = asyncio.create_task(command("record"))
                await opening.wait()
                stopping = asyncio.create_task(command("stop"))
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                ready.set()
                started, stopped = await asyncio.gather(recording, stopping)
                self.assertEqual(stopped["id"], started["id"])
                await asyncio.gather(*state.jobs)

    async def test_portable_live_meeting_shares_and_recovers_the_rolling_transcript(self):
        from contextlib import asynccontextmanager
        from meeting_mcp import reply
        from meeting_rolling import rolling_path
        from saved_meetings import meetings, read_portable_live_meeting
        from server import (begin_portable_recording, copy_latest_portable_meeting, recover_pending_recording,
                            portable_live_meeting, portable_recording_preview, stopped_portable_recording)
        from session_store import SessionStore

        @asynccontextmanager
        async def audio_priority():
            yield

        with TemporaryDirectory() as directory:
            library = SessionStore(Path(directory))
            spool = library.root / "microphone-drafts"
            spool.mkdir()
            earlier_audio = library.root / "earlier.wav"
            with private_wav(earlier_audio) as (audio, _):
                audio.writeframes(b"\x01\x02" * SAMPLE_RATE)
            earlier, _ = library.create(earlier_audio, "Earlier review", "audio/wav",
                                        {"recording": True, "meeting": True, "diarization": False}, 1)
            earlier_text = "The previous meeting."
            library.append(earlier, {"text": earlier_text, "words": [],
                                    "segments": [dict(start=0, end=1, speaker=1, text=earlier_text)]}, 1)
            library.finish(earlier)

            def recorder(key, path, mode):
                frames = 4 * SAMPLE_RATE
                worker = asyncio.Future()

                async def start():
                    for target in (path, *source_tracks(path).values()):
                        with private_wav(target) as (audio, _):
                            audio.writeframes(b"\x01\x02" * frames)

                async def stop():
                    worker.set_result(None)

                return SimpleNamespace(key=key, path=path, frames=frames, worker=worker, start=start, stop=stop)

            app = FastAPI()
            app.get("/recording/{key}/preview")(portable_recording_preview)
            state = app.state
            state.library = library
            state.portable_workspace = True
            state.draining = False
            state.status = "ready"
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.portable_recording_dir = spool
            state.portable_recording_lock = asyncio.Lock()
            state.portable_meeting_recorder = recorder
            state.jobs = set()
            state.diarizer = SimpleNamespace(session=Mock(return_value=object()))
            state.insights = SimpleNamespace(model=SimpleNamespace(audio_priority=audio_priority))
            finish_gate = asyncio.Event()
            rolling_result = {"text": "The provisional budget.", "language": "English", "words": [],
                              "segments": [dict(start=0, end=4, speaker=1, text="The provisional budget.")]}

            async def finalize(_state, key, *_args, **_options):
                await finish_gate.wait()
                text = "The final budget."
                library.append(key, {"text": text, "segments": [dict(start=0, end=4, speaker=1, text=text)], "words": []},
                               4, next_frame=192_000)
                library.finish(key)

            with (patch("recording_power.keep_awake_until"), patch("server.speech_vocabulary", new_callable=AsyncMock, return_value=()),
                  patch("server.decode_pcm", new_callable=AsyncMock, return_value=rolling_result) as decode,
                  patch("server.audio_duration", return_value=4), patch("server.process_saved_audio", side_effect=finalize),
                  patch("desktop_clipboard.copy_text", new_callable=AsyncMock) as clipboard):
                started = await begin_portable_recording(state, "both", True, ("en",), title="Budget review")
                key = started["id"]
                identifier = str(key)
                capture = state.portable_recording
                state.portable_dictation = SimpleNamespace(busy=True)
                await asyncio.sleep(.4)
                decode.assert_not_awaited()
                self.assertEqual((await copy_latest_portable_meeting(state))["id"], earlier)
                self.assertIn(earlier_text, clipboard.await_args.args[0])
                state.portable_dictation.busy = False
                async with asyncio.timeout(3):
                    while not capture.preview_processed:
                        await asyncio.sleep(.03)
                live = read_portable_live_meeting(library.root, portable_live_meeting(state))
                self.assertEqual(live["duration"], 4)
                self.assertEqual(live["meetingTurns"], rolling_result["segments"])
                self.assertEqual((await copy_latest_portable_meeting(state))["id"], identifier)
                self.assertIn("provisional budget", clipboard.await_args.args[0])
                request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "get_transcript", "arguments": {"meeting_id": identifier}}}
                response = json.loads(reply(library, request, lambda **_: live)["result"]["content"][0]["text"])
                self.assertEqual(response["transcriptSource"], "rolling")
                self.assertEqual(response["turns"][0]["speaker"], "Speaker 1")
                path = f"/recording/{identifier}/preview"
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as browser:
                    preview = (await browser.get(path)).json()
                    self.assertEqual(preview["text"], rolling_result["text"])
                    self.assertEqual((await browser.get(path, params={"after": preview["through"]})).status_code, 204)
                self.assertEqual([row["id"] for row in meetings(library, include_in_progress=False)], [earlier])
                async with stopped_portable_recording(state, key):
                    self.assertIsNone(portable_live_meeting(state))
                    self.assertEqual([row["id"] for row in meetings(library)], [earlier])
                self.assertTrue(rolling_path(capture.path).is_file())
                rows = meetings(library, "Budget review", include_in_progress=False)
                self.assertEqual([(row["id"], row["status"], row["duration"]) for row in rows], [(identifier, "interrupted", 4)])
                response = json.loads(reply(library, request)["result"]["content"][0]["text"])
                self.assertEqual(response["status"], "interrupted")
                self.assertEqual(response["transcriptSource"], "rolling")
                self.assertEqual(response["turns"], [{"start": 0, "end": 4, "speaker": "Speaker 1", "text": "The provisional budget."}])
                stranded = {identifier}
                state.portable_dictation.busy = True
                self.assertTrue(await recover_pending_recording(state, stranded))
                self.assertEqual(stranded, {identifier})
                self.assertFalse(library.exists(identifier))
                state.portable_dictation.busy = False
                self.assertTrue(await recover_pending_recording(state, stranded))
                self.assertFalse(stranded)
                self.assertEqual(library.get(identifier, include_turns=True)["transcript"]["transcriptSource"], "rolling")
                self.assertFalse(rolling_path(capture.path).exists())
                finish_gate.set()
                await asyncio.gather(*state.jobs)
                self.assertEqual(library.get(identifier, include_turns=True)["transcript"]["text"], "The final budget.")
                self.assertEqual([row["id"] for row in meetings(library, "Budget review")], [identifier])
                decode.assert_awaited_once()

    async def test_failed_portable_startup_import_leaves_meeting_available_on_home(self):
        from portable_capture import claim_meeting_draft
        from server import portable_recording_drafts, recover_pending_recording
        from session_store import SessionStore

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.get("/drafts")(portable_recording_drafts)
            state = app.state
            state.library = SessionStore(Path(directory))
            state.portable_workspace = state.startup_recovery = True
            state.status = "ready"
            state.draining = False
            state.portable_recording = state.portable_screencast = state.portable_dictation = None
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recording_dir = state.library.root / "microphone-drafts"
            state.portable_recording_dir.mkdir()
            meeting, recording = (str(uuid4()) for _ in range(2))
            state.startup_stranded = {meeting}
            source = state.portable_recording_dir / f"{meeting}.wav"
            with claim_meeting_draft(source, {"meetingProvider": "Meeting"}), private_wav(source) as (audio, _):
                audio.writeframes(bytes(SAMPLE_RATE * 2))
            with private_wav(state.portable_recording_dir / f"{recording}.wav") as (audio, _):
                audio.writeframes(bytes(SAMPLE_RATE * 2))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as browser:
                drafts = await browser.get("/drafts")
                self.assertEqual(drafts.headers["X-Jard-Meeting-Recovery"], "pending")
                self.assertEqual([draft["id"] for draft in drafts.json()], [recording])
                state.status = "error"
                drafts = await browser.get("/drafts")
                self.assertNotIn("X-Jard-Meeting-Recovery", drafts.headers)
                self.assertEqual({draft["id"] for draft in drafts.json()}, {meeting, recording})
                state.status = "ready"
                with patch("server.accept_audio", side_effect=HTTPException(503, "Storage temporarily unavailable.")):
                    self.assertTrue(await recover_pending_recording(state, state.startup_stranded))
                self.assertFalse(await recover_pending_recording(state, state.startup_stranded))
                self.assertFalse(state.library.exists(meeting))
                self.assertTrue(source.is_file())
                self.assertEqual({draft["id"] for draft in (await browser.get("/drafts")).json()}, {meeting, recording})

    async def test_clear_history_revokes_an_unsaved_paste_last_and_an_in_flight_write(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory

        class Dictation(PortableDictation):
            enabled = True

        with TemporaryDirectory() as folder:
            root = Path(folder)
            history = PortableDictationHistory(root)
            history.initialize()
            controller = Dictation(SimpleNamespace(), None, history)
            job = SimpleNamespace(captured_at=time(), application="Editor", frames=16000,
                                  started=monotonic(), path=root / "speech.wav", raw=False, printed=None)
            result = {"text": "Unsaved dictation", "inferenceMs": 20}
            with patch.object(history, "record", side_effect=PermissionError), self.assertLogs():
                self.assertEqual(await controller.retain(result, job), "Could not save dictation history.")
            self.assertEqual(controller.snapshot()["text"], result["text"])
            unavailable = root / "unavailable.sqlite3"
            history.path.rename(unavailable)
            try:
                self.assertEqual(controller.snapshot()["text"], "")
            finally:
                unavailable.rename(history.path)
            other_process = PortableDictationHistory(root)
            self.assertEqual(other_process.remove(), 0)
            self.assertEqual(controller.snapshot()["text"], "")
            with patch.object(history, "status", side_effect=OSError):
                self.assertEqual(controller.snapshot()["text"], "")

            record = history.record

            def clear_then_save(*args, **options):
                other_process.remove()
                return record(*args, **options)

            with patch.object(history, "record", side_effect=clear_then_save):
                self.assertEqual(await controller.retain(result, job), "")
            self.assertEqual(controller.snapshot()["text"], "")
            self.assertEqual(history.page(legacy=True)["entries"], [])

            fresh = {"text": "New dictation", "inferenceMs": 30}
            with private_wav(job.path) as (audio, _):
                audio.writeframes(b"\x01\x02" * 16000)
            self.assertEqual(await controller.retain(fresh, job), "")
            self.assertEqual(controller.snapshot()["text"], fresh["text"])
            self.assertEqual(history.page(legacy=True)["entries"][0]["text"], fresh["text"])

            printed = {"text": "Returned to the command", "inferenceMs": 30}
            job.printed = asyncio.get_running_loop().create_future()
            with private_wav(job.path) as (audio, _):
                audio.writeframes(b"\x01\x02" * 16000)
            self.assertEqual(await controller.retain(printed, job), "")
            self.assertEqual(controller.snapshot()["text"], fresh["text"])
            kinds = {entry["text"]: entry["kind"] for entry in history.page(legacy=True)["entries"]}
            self.assertEqual(kinds[printed["text"]], "printed")

    async def test_saving_regular_dual_audio_preserves_tracks_without_creating_a_meeting(self):
        from server import audio_recording_drafts, finish_portable_capture
        from session_store import SessionStore

        with TemporaryDirectory() as directory:
            root = Path(directory)
            library = SessionStore(root / "library")
            staging = root / "drafts"
            staging.mkdir()
            key = uuid4()
            source = staging / f"{key}.wav"
            pcm = b"\x01\x02" * 1600
            for path in (source, *source_tracks(source).values()):
                with private_wav(path) as (wav, _):
                    wav.writeframes(pcm)
            state = SimpleNamespace(library=library, portable_recording_dir=staging, portable_recording=None,
                                    portable_recording_lock=asyncio.Lock(), draining=False, diarizer=object(), jobs=set(),
                                    status="ready")
            self.assertFalse(audio_recording_drafts(state)[1][0]["meeting"])

            async def finish(_state, identifier, *_args, **_options):
                library.finish(identifier)

            with (patch("server.audio_duration", return_value=.1), patch("server.process_saved_audio", side_effect=finish)):
                response = await finish_portable_capture(state, key, ("en",), diarization=True)
                await asyncio.gather(*state.jobs)
            self.assertEqual(response.status_code, 202)
            saved = library.get(str(key))
            self.assertFalse(saved["meeting"])
            self.assertTrue(saved["diarization"])
            self.assertEqual(set(saved["recordingTracks"]), {"mic", "system"})
            self.assertEqual(library.list("", meetings_only=True), [])

    async def test_queued_dictation_records_while_processing_and_delivers_in_order(self):
        from portable_dictation import PASTE_SENT, PortableDictation
        from portable_dictation_history import PortableDictationHistory

        class Dictation(PortableDictation):
            enabled = True

        with TemporaryDirectory() as directory:
            root = Path(directory)
            history = PortableDictationHistory(root)
            history.initialize()
            paths = []

            def recorder(_key, path, _mode, **_options):
                paths.append(path)

                async def start():
                    with private_wav(path) as (wav, _):
                        wav.writeframes(b"\x01\x02" * 1600)

                return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False,
                                       start=start, stop=AsyncMock())

            first_started, release_first = asyncio.Event(), asyncio.Event()

            async def transcribe(_state, path, _languages, **_options):
                take = paths.index(path) + 1
                if take in (1, 3):
                    first_started.set()
                    await release_first.wait()
                return {"text": f"Take {take}.", "inferenceMs": 5}

            state = SimpleNamespace(status="ready", draining=False, library=SimpleNamespace(root=root),
                                    portable_recording=None, portable_recording_lock=asyncio.Lock(),
                                    portable_recorder=recorder)
            dictation = Dictation(state, transcribe, history)
            dictation.deliver = AsyncMock(return_value=PASTE_SENT)
            self.addAsyncCleanup(dictation.cancel_capture, include_pending=True)

            async def toggle():
                self.assertTrue(dictation.schedule())
                await dictation.action

            await toggle()
            await toggle()
            await asyncio.wait_for(first_started.wait(), 2)
            self.assertEqual(dictation.snapshot()["pendingJobs"], 1)
            with patch("portable_dictation.MAX_PENDING", 1):
                self.assertFalse(dictation.schedule())
                self.assertTrue(dictation.snapshot()["queueFull"])
                self.assertEqual(len(paths), 1)
            await toggle()
            self.assertEqual(dictation.phase, "recording")
            release_first.set()
            await asyncio.sleep(.02)
            dictation.deliver.assert_not_awaited()
            self.assertEqual(dictation.phase, "recording")
            await toggle()
            await dictation.output_task
            self.assertEqual([call.args[0] for call in dictation.deliver.await_args_list], ["Take 1.", "Take 2."])
            self.assertEqual([entry["text"] for entry in history.page(legacy=True)["entries"]], ["Take 2.", "Take 1."])
            self.assertEqual(dictation.phase, "idle")
            self.assertFalse(list(root.glob(".dictation-*")))

            first_started.clear()
            release_first.clear()
            await toggle()
            await toggle()
            await asyncio.wait_for(first_started.wait(), 2)
            await toggle()
            await toggle()
            self.assertEqual(dictation.snapshot()["pendingJobs"], 2)
            self.assertTrue(await dictation.cancel_dictation())
            self.assertFalse(paths[3].parent.exists())
            release_first.set()
            await dictation.output_task
            self.assertEqual([call.args[0] for call in dictation.deliver.await_args_list],
                             ["Take 1.", "Take 2.", "Take 3."])

    async def test_lost_dictation_is_retained_without_pasting_into_the_focused_app(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory

        class Dictation(PortableDictation):
            enabled = True

        for mode in ("eof", "stall", "silent"):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                class Recorder(PortableRecording):
                    async def spawn(self):
                        return await asyncio.create_subprocess_exec(
                            sys.executable, "-c",
                            "import sys; sys.stdout.buffer.write(b'\\x11\\x22' * 6400); sys.stdout.flush(); "
                            "sys.stdin.buffer.read()",
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        )

                root = Path(directory)
                history = PortableDictationHistory(root)
                history.initialize()
                state = SimpleNamespace(draining=False, status="ready", library=SimpleNamespace(root=root),
                                        portable_recording=None, portable_recording_lock=asyncio.Lock(),
                                        portable_recorder=Recorder)
                signal = "" if mode == "silent" else "Keep these words"
                transcribe = AsyncMock(return_value={"text": signal, "inferenceMs": 10})
                dictation = Dictation(state, transcribe, history)
                dictation.deliver = AsyncMock()
                dictation.text = "Previous dictation"
                with (patch("portable_capture.DICTATION_STALL", .15),
                      patch("portable_dictation.copy_text", new_callable=AsyncMock) as clipboard):
                    if mode == "stall":
                        clipboard.side_effect = OSError("Clipboard unavailable")
                    await dictation.toggle()
                    self.assertEqual(dictation.phase, "recording")
                    capture = dictation.capture
                    if mode != "stall":
                        capture.process.stdin.close()
                    await capture.worker
                    if dictation.action:
                        await dictation.action
                    await dictation.output_task
                    self.assertTrue(capture.interrupted)
                    self.assertEqual(dictation.phase, "idle")
                    dictation.deliver.assert_not_called()
                    transcribe.assert_awaited_once()
                    rows = history.page(legacy=True, include_audio=True)["entries"]
                    if signal:
                        self.assertEqual(dictation.snapshot()["text"], signal)
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["text"], signal)
                        self.assertIn("audioExpiresAt", rows[0])
                        clipboard.assert_awaited_once_with(signal)
                        self.assertIn("Copy last" if mode == "stall" else "was copied", dictation.feedback)
                    else:
                        self.assertEqual(dictation.snapshot()["text"], "Previous dictation")
                        self.assertFalse(rows)
                        clipboard.assert_not_called()

    @staticmethod
    def tail_audio(voiced, through_ms):
        frame = np.arange(through_ms * SAMPLE_RATE // 1000)
        audio = np.zeros(frame.size, dtype="<i2")
        for start, end in voiced:
            part = (start * SAMPLE_RATE // 1000 <= frame) & (frame < end * SAMPLE_RATE // 1000)
            audio[part] = (200 * np.sin(frame[part] * (2 * np.pi * 250 / SAMPLE_RATE))).astype("<i2")
        return audio.tobytes()

    def test_dictation_tail_keeps_quiet_words_and_ignores_an_isolated_click(self):
        release = SAMPLE_RATE
        for audio, heard, end in (
            ([(940, 1080)], 1199, None),
            ([(940, 1080)], 1200, 1200),
            ([(1180, 1200)], 1200, 1200),
            ([(940, 1280)], 1300, None),
            ([(940, 1280)], 1450, 1440),
            ([(940, 1700)], 1700, 1600),
        ):
            with self.subTest(audio=audio, heard=heard):
                cutoff = release_cutoff(0, self.tail_audio(audio, heard), release)
                self.assertEqual(cutoff, end * SAMPLE_RATE // 1000 if end else None)

    async def test_dictation_release_trims_only_after_recorder_finishes(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "voice.wav"
            capture = PortableRecording(uuid4(), path, "mic")
            captured = self.tail_audio([(940, 1280), (1500, 1650)], 1700)
            capture.pcm_epoch = 9
            capture.frames = len(captured) // 2
            capture.blocks.append((0, captured))
            capture.stop = AsyncMock()
            with private_wav(path) as (wav, _):
                wav.writeframes(captured)
            await capture.finish_dictation(10)
            capture.stop.assert_awaited_once()
            self.assertEqual(capture.frames, 1440 * SAMPLE_RATE // 1000)
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnframes(), capture.frames)
                self.assertEqual(wav.readframes(capture.frames), captured[:capture.frames * 2])

    def test_meeting_monitors_capture_hardware_once_and_use_default_as_fallback(self):
        from linux_meeting_audio import meeting_monitors

        virtual = {"name": "effects", "monitor_source": "effects.monitor", "properties": {}}
        headset = {"name": "headset", "monitor_source": "headset.monitor", "properties": {"device.api": "bluez5"}}
        laptop = {"name": "laptop", "monitor_source": "laptop.monitor", "properties": {"device.api": "alsa"}}
        self.assertEqual(meeting_monitors([virtual, headset, laptop, headset], "effects"),
                         ("headset.monitor", "laptop.monitor"))
        self.assertEqual(meeting_monitors([virtual], "effects"), ("effects.monitor",))
        self.assertEqual(meeting_monitors([virtual], "missing"), ())

    async def test_windows_launch_at_login_respects_startup_approval(self):
        from portable_login import APPROVAL_KEY, RUN_KEY, VALUE_NAME, PortableLogin, WindowsLogin
        from server import change_portable_login, get_portable_login, open_portable_login_settings

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/login-item"
        app.add_api_route(endpoint, get_portable_login, methods=["GET"])
        app.add_api_route(endpoint, change_portable_login, methods=["POST"])
        app.add_api_route(endpoint + "/open-settings", open_portable_login_settings, methods=["POST"])
        configure_workspace(app, record, origin)
        registration = WindowsLogin(Path("C:/Program Files/Jaro/bin/Jaro.exe"))
        app.state.portable_login = PortableLogin(registration)
        values = {RUN_KEY: {}, APPROVAL_KEY: {}}

        def open_key(_root, name, _reserved, access):
            if name == APPROVAL_KEY:
                self.assertEqual(access, 1)
            return nullcontext(values[name])

        def query_value(key, name):
            if name not in key:
                raise FileNotFoundError(name)
            return key[name]

        registry = SimpleNamespace(
            HKEY_CURRENT_USER=1, KEY_QUERY_VALUE=1, KEY_SET_VALUE=2, REG_SZ=1, REG_BINARY=3,
            OpenKey=open_key, QueryValueEx=query_value,
            SetValueEx=lambda key, name, _reserved, kind, data: key.update({name: (data, kind)}),
            DeleteValue=lambda key, name: key.pop(name),
        )
        blocked = (b"\x03" + bytes(3) + b"01234567", registry.REG_BINARY)
        values[APPROVAL_KEY][VALUE_NAME] = blocked
        headers = {"Origin": origin}
        with (patch.dict(sys.modules, winreg=registry), patch("server.URL", origin),
              patch("portable_login.os.startfile", create=True) as startfile):
            self.assertEqual(registration.set_state(None), {"state": "disabled"})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                enabled = await browser.post(endpoint, headers=headers, json={"enabled": True})
                self.assertEqual(enabled.json(), {"state": "approval"})
                self.assertEqual(values[RUN_KEY][VALUE_NAME], (registration.command, registry.REG_SZ))
                denied = await browser.post(endpoint + "/open-settings")
                self.assertEqual(denied.status_code, 403)
                startfile.assert_not_called()
                accepted = await browser.post(endpoint + "/open-settings", headers=headers)
                self.assertEqual(accepted.json(), {"state": "approval"})
                startfile.assert_called_once_with("ms-settings:startupapps")
                self.assertEqual(values[APPROVAL_KEY][VALUE_NAME], blocked)
                startfile.side_effect = OSError("Unable to open Settings")
                with self.assertLogs():
                    failed = await browser.post(endpoint + "/open-settings", headers=headers)
                self.assertEqual(failed.status_code, 503)
                self.assertIn("Apps → Startup", failed.json()["detail"])

                for flag, state in ((2, "enabled"), (6, "enabled"), (7, "approval"), (42, "unknown")):
                    values[APPROVAL_KEY][VALUE_NAME] = (bytes([flag]) + bytes(11), registry.REG_BINARY)
                    status = await browser.get(endpoint)
                    self.assertEqual(status.json()["state"], state)
                values[APPROVAL_KEY].clear()
                self.assertEqual((await browser.get(endpoint)).json(), {"state": "enabled"})

                values[APPROVAL_KEY][VALUE_NAME] = blocked
                foreign = ('"C:/Other/Jaro.exe" --background', registry.REG_SZ)
                values[RUN_KEY][VALUE_NAME] = foreign
                for enabled in (True, False):
                    self.assertEqual(registration.set_state(enabled)["state"], "unknown")
                self.assertEqual(values[RUN_KEY][VALUE_NAME], foreign)
                values[RUN_KEY][VALUE_NAME] = (registration.command, registry.REG_SZ)
                disabled = await browser.post(endpoint, headers=headers, json={"enabled": False})
                self.assertEqual(disabled.json(), {"state": "disabled"})
                self.assertNotIn(VALUE_NAME, values[RUN_KEY])
                self.assertEqual(values[APPROVAL_KEY][VALUE_NAME], blocked)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires POSIX no-follow file access")
    def test_launch_at_login_respects_the_users_autostart_entries(self):
        from portable_login import LinuxLogin

        with TemporaryDirectory() as folder:
            entry = Path(folder) / "autostart" / "jaro.desktop"
            login = LinuxLogin(entry)
            self.assertEqual(login.set_state(None), {"state": "disabled"})
            self.assertFalse(entry.exists())
            self.assertEqual(login.set_state(True), {"state": "enabled"})
            self.assertIn("Exec=/usr/bin/jaro --background", entry.read_text())
            with entry.open("a") as config:
                config.write("Hidden=true\n")
            self.assertEqual(login.set_state(None), {"state": "disabled"})
            self.assertEqual(login.set_state(True), {"state": "enabled"})
            self.assertEqual(login.set_state(False), {"state": "disabled"})
            self.assertFalse(entry.exists())

            external = Path(folder) / "user-entry.desktop"
            external.write_text("[Desktop Entry]\nExec=/usr/bin/jaro --background\n")
            entry.symlink_to(external)
            for enabled in (True, False):
                self.assertEqual(login.set_state(enabled)["state"], "unknown")
            self.assertTrue(entry.is_symlink())
            self.assertNotIn("X-Jaro-Managed", external.read_text())

    async def test_capture_power_releases_a_late_grant_and_skips_a_canceled_tap(self):
        from recording_power import keep_awake_until

        loop = asyncio.get_running_loop()
        stopped = loop.create_future()
        requested = asyncio.Event()
        grant = loop.create_future()
        release = Mock()

        async def inhibit(_reason):
            requested.set()
            return await grant

        with patch("recording_power.sys.platform", "linux"), patch("recording_power.inhibit_linux", side_effect=inhibit) as start:
            guard = keep_awake_until(stopped, "Recording")
            await requested.wait()
            stopped.set_result(None)
            grant.set_result(release)
            await guard
            release.assert_called_once_with()

            stopped = loop.create_future()
            guard = keep_awake_until(stopped, "Tap", delay=10)
            stopped.cancel()
            await guard
            start.assert_awaited_once_with("Recording")

    async def test_desktop_pen_keeps_final_stroke_before_stopping(self):
        from desktop_pen import PenProcess
        from screencast_ink import RecordingDrawing

        with TemporaryDirectory() as folder:
            pen = PenProcess(RecordingDrawing(Path(folder) / "ink.jsonl"))
            seen = []
            child = "import sys; print('READY', flush=True); sys.stdin.read(); print('{\"released\":12}', flush=True)"
            self.assertTrue(await pen.start([sys.executable, "-c", child], seen.append))
            self.assertTrue(pen.active)
            await pen.close()
            self.assertFalse(pen.active)
            self.assertEqual(seen, [{"released": 12}])

    async def test_drawing_preview_reads_ffmpeg_and_gstreamer_headers(self):
        from screencast_ink import BOUNDARY_LINE, RecordingDrawing

        reader = asyncio.StreamReader()
        drawing = RecordingDrawing(Path("unused-ink.jsonl"))
        worker = asyncio.create_task(drawing.read_frames(reader))
        view = drawing.stream(lambda: True)
        try:
            for media, length, pixel in ((b"Content-type", b"Content-length", b"j"),
                                         (b"Content-Type", b"Content-Length", b"p")):
                frame = BOUNDARY_LINE + media + b": image/jpeg\r\n" + length + b": 1\r\n\r\n" + pixel + b"\r\n"
                reader.feed_data(frame)
                self.assertEqual(await asyncio.wait_for(anext(view), 1), frame)
            drawing.close_view()
            with self.assertRaises(StopAsyncIteration):
                await anext(view)
        finally:
            reader.feed_eof()
            await worker
            await view.aclose()

    async def test_windows_screen_picker_rejects_external_and_stale_targets(self):
        from windows_screencast import capture_targets
        from server import select_annotation_folder

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screencast"
        app.add_api_route(endpoint, begin_screencast, methods=["POST"], status_code=202)
        app.add_api_route(endpoint + "/targets", screencast_targets, methods=["GET"])
        app.add_api_route(endpoint + "/microphones", screencast_microphones, methods=["GET"])
        app.state.draining = False
        app.state.status = "ready"
        app.state.portable_recording = app.state.portable_dictation = app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        app.state.jobs = set()
        configure_workspace(app, record, origin)
        listed = capture_targets("1a\t12345\tAperçu\r\n2b\t0\tDisplay\r\ninvalid\t1\tSkip".encode(), "window")
        self.assertEqual(listed, [{"id": "1a:12345", "label": "Aperçu"}])
        opener = AsyncMock(return_value=listed)
        inputs = [{"id": "usb input", "label": "USB Microphone", "default": False}]
        inventory = AsyncMock(return_value=inputs)
        headers = {"Origin": origin}
        with (TemporaryDirectory() as staging, patch("server.URL", origin),
              patch("server.sys", SimpleNamespace(platform="win32")),
              patch("server.transcription_languages", return_value=()),
              patch("windows_capture.microphone_inputs", inventory),
              patch("windows_screencast.targets", opener),
              patch("windows_screencast.WindowsScreenRecording.record", new_callable=AsyncMock) as record_screen):
            app.state.screen_draft_dir = Path(staging)
            app.state.library = SessionStore(Path(staging) / "library")
            folder = Path(staging) / "captures"
            folder.mkdir()
            location = select_annotation_folder(app.state.library, str(folder.resolve()))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                list_windows = endpoint + "/targets?source=window"
                start = endpoint + "?source=window&target=1a%3A12345"
                self.assertEqual((await browser.get(list_windows)).status_code, 403)
                self.assertEqual((await browser.get(endpoint + "/microphones")).status_code, 403)
                self.assertEqual((await browser.post(start, headers=headers)).status_code, 403)
                opener.assert_not_awaited()
                inventory.assert_not_awaited()
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(list_windows)).json(), {"targets": listed})
                self.assertEqual((await browser.post(start)).status_code, 403)
                self.assertEqual((await browser.post(endpoint + "?source=area", headers=headers)).status_code, 422)
                self.assertEqual((await browser.post(endpoint + "?source=window&target=1a%3A8", headers=headers)).status_code, 409)
                self.assertEqual((await browser.get(endpoint + "/microphones")).json(), {"inputs": inputs})
                self.assertEqual((await browser.post(start + "&microphone_device=usb+input", headers=headers)).status_code, 422)
                audio = "&microphone=true&computer_audio=true"
                self.assertEqual((await browser.post(start + audio + "&microphone_device=removed", headers=headers)).status_code, 409)
                self.assertIsNone(app.state.portable_screencast)
                result = await browser.post(start + audio + "&microphone_device=usb+input", headers=headers)
                self.assertEqual(result.status_code, 202)
                self.assertEqual(result.json()["phase"], "starting")
                self.assertEqual(result.json()["id"], app.state.portable_screencast.draft.key)
                self.assertEqual(app.state.portable_screencast.target, "1a:12345")
                self.assertEqual(app.state.portable_screencast.microphone_device, inputs[0]["id"])
                self.assertEqual(app.state.portable_screencast.draft.annotation_location, location["storageId"])
                await app.state.portable_screencast.task
                record_screen.assert_awaited_once()

    async def test_windows_area_uses_native_pixels_and_expires_the_preview(self):
        from windows_screencast import Snapshot, WindowsScreenRecording

        picture = Snapshot(b"preview", (1920, 1080), (3840, 2160))
        with TemporaryDirectory() as folder, patch("windows_screencast.snapshot", new=AsyncMock(return_value=picture)):
            take = WindowsScreenRecording(Path(folder), "area", "1a:0")
            choose = asyncio.create_task(take.choose_region())
            await asyncio.sleep(0)
            self.assertEqual(take.status()["preview"]["width"], 1920)
            self.assertEqual(take.preview, b"preview")
            with self.assertRaises(ValueError):
                take.select_region((1900, 0, 30, 20))
            take.select_region((1451, 879, 469, 201))
            self.assertEqual(await choose, (2902, 1758, 938, 402, 3840, 2160))
            self.assertEqual(take.phase, "starting")
            self.assertIsNone(take.preview_id)
            self.assertIsNone(take.preview)
            self.assertFalse(list(Path(folder).iterdir()))

    async def test_windows_desktop_pen_uses_selected_display_area_and_keeps_renderable_strokes(self):
        from screencast_ink import load_events
        from windows_screencast import WindowsScreenRecording

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        path = "/api/portable/screencast/{key}"
        app.add_api_route(path + "/pen", screencast_pen, methods=["POST", "DELETE"])
        app.add_api_route("/api/portable/screencast", discard_screencast, methods=["DELETE"], status_code=204)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with (TemporaryDirectory() as folder, patch("server.URL", origin),
              patch("server.sys", SimpleNamespace(platform="win32")),
              patch("windows_screencast.helper_path", return_value=Path("jaro-screen.exe")),
              patch("windows_screencast.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True)):
            take = WindowsScreenRecording(Path(folder), "area", "dad:0")
            window = WindowsScreenRecording(Path(folder), "window", "dad:1")
            self.assertIsNone(window.pen.binary)
            take.draft.create()
            app.state.portable_screencast = take
            app.state.portable_recording_lock = asyncio.Lock()
            app.state.jobs = set()
            take.region = (100, 70, 500, 400, 1920, 1080)
            take.phase, take.started = "recording", 5
            take.frame_bytes, take.bytes_encoded = 100, 4800
            take.pen.video_clock = 1000
            endpoint = path.format(key=take.draft.key) + "/pen"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.post(endpoint, headers=headers)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                with patch.object(take.pen, "start", new=AsyncMock(return_value=True)) as start:
                    self.assertTrue((await browser.post(endpoint, headers=headers)).json()["desktopDrawing"])
                    start.assert_awaited_once_with(
                        (Path("jaro-screen.exe"), "pen", "display", "dad", "0", "100", "70", "500", "400", "1920", "1080"),
                        take.pen.keep_stroke, creationflags=0x08000000,
                    )
                take.pen.keep_stroke({"released": 1_001_700_000,
                                      "points": [[.2, .25, 1_001_200_000], [.5, .25, 1_001_600_000]]})
                initial, final = load_events(take.draft.ink)
                self.assertEqual((initial["time"], final["liftedAt"]), (1.2, 1.7))
                self.assertEqual((final["from"], final["to"]), ([.2, .75], [.5, .75]))
                self.assertNotIn("captured", final)
                with patch.object(take.pen, "start", new=AsyncMock(return_value=False)):
                    self.assertEqual((await browser.post(endpoint, headers=headers)).status_code, 409)
                    self.assertFalse(take.status()["desktopDrawing"])
                self.assertEqual((await browser.delete("/api/portable/screencast", headers=headers)).status_code, 204)
                self.assertFalse(take.draft.folder.exists())

    async def test_screen_discard_owns_the_take_after_the_request_is_cancelled(self):
        from server import finish_screencast

        async def discard():
            stopping.set()
            await release.wait()
            stopped.set()

        key = uuid4()
        stopping, release, stopped = (asyncio.Event() for _ in range(3))
        take = SimpleNamespace(draft=SimpleNamespace(key=str(key)), phase="recorded", saving=False, discard=discard)
        origin = "http://127.0.0.1:43721"
        app = FastAPI()
        app.state.portable_screencast = take
        app.state.portable_recording_lock = asyncio.Lock()
        app.state.jobs = set()
        endpoint = "/api/portable/screencast"
        app.add_api_route(endpoint, discard_screencast, methods=["DELETE"], status_code=204)
        app.add_api_route(endpoint + "/finish", finish_screencast, methods=["POST"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
            with patch("server.screencast_action", return_value=app.state):
                request = asyncio.create_task(browser.delete(f"{endpoint}?key={key}"))
                try:
                    await asyncio.wait_for(stopping.wait(), 1)
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                    self.assertIs(app.state.portable_screencast, take)
                    self.assertTrue(take.saving)
                    self.assertEqual((await browser.delete(f"{endpoint}?key={key}")).status_code, 409)
                finally:
                    release.set()
                    await asyncio.gather(request, *app.state.jobs, return_exceptions=True)
                self.assertTrue(stopped.is_set())
                self.assertIsNone(app.state.portable_screencast)
                take.draft.key = str(uuid4())
                app.state.portable_screencast = take
                self.assertEqual((await browser.post(f"{endpoint}/finish?key={key}")).status_code, 409)
                self.assertEqual((await browser.delete(f"{endpoint}?key={key}")).status_code, 409)
                self.assertIs(app.state.portable_screencast, take)

    async def test_windows_screenshot_requires_a_current_target_and_workspace_origin(self):
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screenshots"
        app.add_api_route(endpoint, portable_screenshot, methods=["POST"])
        app.state.draining = False
        app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        configure_workspace(app, record, origin)
        jpeg = bytes.fromhex("ffd8 ffc0 000b 08 0002 0003 01 01 1100 ffd9")
        command = AsyncMock(return_value=struct.pack("<IIII", 3, 2, 6, 4) + jpeg)
        headers = {"Origin": origin}
        with (TemporaryDirectory() as directory, patch("server.URL", origin),
              patch("server.sys", SimpleNamespace(platform="win32")),
              patch("windows_screencast.targets", new=AsyncMock(return_value=[{"id": "ab:42"}])),
              patch("windows_screencast.output", command)):
            app.state.screen_draft_dir = Path(directory)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                path = endpoint + "?source=window&target=ab%3A42"
                self.assertEqual((await browser.post(path, headers=headers)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(path)).status_code, 403)
                self.assertEqual((await browser.post(path.replace("target=ab", "target=ff"), headers=headers)).status_code, 409)
                command.assert_not_awaited()
                result = await browser.post(path, headers=headers)
                self.assertEqual(result.content, jpeg)
                self.assertEqual(result.headers["cache-control"], "no-store")
                self.assertEqual(result.headers["content-type"], "image/jpeg")
                command.assert_awaited_once_with("snapshot", "window", "ab", "42", timeout=10)
            self.assertFalse(app.state.screenshot_pending)
            self.assertFalse(list(Path(directory).iterdir()))

    async def test_annotation_command_requires_its_credential_even_with_a_workspace_cookie(self):
        from server import windows_annotation_command
        from windows_dictation_commands import ANNOTATION_COMMAND_ROUTE

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(ANNOTATION_COMMAND_ROUTE, windows_annotation_command, methods=["POST"])
        configure_workspace(app, record, origin)
        app.state.windows_dictation_control = {"token": "fixture desktop token"}
        app.state.portable_annotation_shortcut = SimpleNamespace(saved=None)
        app.state.portable_screencast = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            request = {"json": {"action": "status"}}
            credentials = {"Origin": origin, "Authorization": "Bearer fixture desktop token"}
            with patch("server.URL", origin), patch("server.require_annotation"):
                self.assertEqual((await client.post(ANNOTATION_COMMAND_ROUTE, **request)).status_code, 403)
                await client.post(BOOTSTRAP, headers={"Origin": origin,
                                                    "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                self.assertEqual((await client.post(ANNOTATION_COMMAND_ROUTE, **request)).status_code, 403)
                result = await client.post(ANNOTATION_COMMAND_ROUTE, headers=credentials, **request)
                self.assertEqual(result.json(), {"phase": "idle"})

    async def test_annotation_commands_share_the_take_and_keep_language_hints_for_the_save(self):
        from server import PortableAnnotationCommand, run_portable_annotation_command

        state = SimpleNamespace(portable_annotation_shortcut=SimpleNamespace(saved=None), portable_screencast=None,
                                portable_dictation=SimpleNamespace(platform="wayland", language="fr",
                                    output_context=AsyncMock(return_value=OutputContext(
                                        "Firefox", site_url="https://docs.example.test/project/work?private=1"))),
                                portable_recording_lock=asyncio.Lock(), screen_draft_dir=Path("fixture-screen-drafts"))
        take = SimpleNamespace(draft=SimpleNamespace(key=str(uuid4())), phase="recording", saving=False,
                               annotation_languages=("de",), observe_annotation_origin=True, stop=AsyncMock(), discard=AsyncMock())
        take.status = lambda: {"phase": take.phase, "id": take.draft.key}

        async def run(action, **options):
            return await run_portable_annotation_command(state, PortableAnnotationCommand(action=action, **options))

        with (patch("server.require_annotation"),
              patch("server.start_screen_take", return_value=take.status()) as start,
              patch("server.start_job", side_effect=lambda _, task: asyncio.create_task(task)),
              patch("server.save_screen_draft") as save):
            self.assertEqual(await run("start", languages=["de", "de"]), take.status())
            start.assert_awaited_once_with(state, "display", True, False, "", notice=None,
                                           require_shortcut=False, annotation_languages=("de",), draw_on_start=False, area=None,
                                           show_area_picker=None, observe_origin=True)
            with self.assertRaisesRegex(HTTPException, "native X11"):
                await run("start", source="area", area=[10, 20, 320, 240])
            start.assert_awaited_once()
            state.portable_screencast = take
            self.assertEqual(await run("status"), take.status())
            take.phase = "choosing"
            self.assertEqual(await run("toggle"), take.status())
            take.phase = "recording"
            take.saving = True
            self.assertEqual(await run("toggle"), take.status())
            save.assert_not_awaited()
            take.saving = False
            result = await run("toggle")
            self.assertEqual(result, {"phase": "saved", "id": take.draft.key})
            save.assert_awaited_once_with(state, take.draft, ("de",),
                                          origin={"app": "linux.firefox", "name": "Firefox", "site": "docs.example.test",
                                                  "siteUrl": "https://docs.example.test/project/work"})
            state.portable_dictation.output_context.assert_awaited_once_with(read_site=True, annotation=True)
            self.assertIsNone(state.portable_screencast)
            self.assertEqual(await run("status"), result)
            self.assertEqual(await run("cancel"), {"phase": "idle"})
            state.portable_screencast = take
            take.phase = "cancelled"
            await run("draw")
            take.discard.assert_awaited_once()
            self.assertIsNone(state.portable_screencast)
            start.assert_awaited_with(state, "display", True, False, "", notice=None,
                                     require_shortcut=False, annotation_languages=("fr",), draw_on_start=True, area=None,
                                      show_area_picker=None, observe_origin=True)
            state.portable_dictation.platform = "x11"
            with patch.dict(os.environ, {"JARO_DESKTOP_TRAY": ""}):
                await run("start", source="area", area=[10, 20, 320, 240])
            self.assertEqual(start.call_args.kwargs["area"], [10, 20, 320, 240])
            state.portable_dictation.platform = "wayland"
            with TemporaryDirectory() as tray, patch.dict(os.environ, {"JARO_DESKTOP_TRAY": tray}):
                await run("start", source="area")
                marker = Path(tray) / "open"
                self.assertFalse(marker.exists())
                start.call_args.kwargs["show_area_picker"]()
                self.assertTrue(marker.is_file())

    async def test_saving_from_a_project_validates_the_folder_before_stopping(self):
        from server import PortableAnnotationCommand, run_portable_annotation_command

        take = SimpleNamespace(draft=SimpleNamespace(key=str(uuid4())), phase="recording", saving=False,
                               annotation_languages=("de",), observe_annotation_origin=True, stop=AsyncMock())
        state = SimpleNamespace(portable_annotation_shortcut=SimpleNamespace(saved=None), portable_screencast=take,
                                portable_dictation=SimpleNamespace(platform="x11", output_context=AsyncMock()),
                                portable_recording_lock=asyncio.Lock(), screen_draft_dir=Path("fixture-screen-drafts"), jobs=set())
        with TemporaryDirectory() as folder, patch("server.require_annotation"), patch("server.require_opencode"), patch("server.save_screen_draft") as save:
            project = Path(folder).resolve() / "project"
            (project / ".git").mkdir(parents=True)
            work = project / "src"
            work.mkdir()
            with self.assertRaisesRegex(HTTPException, "only when stopping"):
                await run_portable_annotation_command(state, PortableAnnotationCommand(action="status", terminalFolder=str(work)))
            with self.assertRaisesRegex(HTTPException, "project or working folder"):
                await run_portable_annotation_command(state, PortableAnnotationCommand(action="stop", terminalFolder=str(project / "missing")))
            take.stop.assert_not_awaited()
            result = await run_portable_annotation_command(state, PortableAnnotationCommand(action="stop", terminalFolder=str(work)))
            self.assertEqual(result, {"phase": "saved", "id": take.draft.key})
            take.stop.assert_awaited_once()
            save.assert_awaited_once_with(state, take.draft, ("de",), origin={"app": "linux.shell", "name": "Terminal",
                "terminal": {"folder": str(work), "project": str(project)}})
            state.portable_dictation.output_context.assert_not_awaited()

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux workspace")
    async def test_annotation_binding_requires_the_desktop_and_refuses_changes_during_capture(self):
        from server import configure_annotation_shortcut, start_screen_take

        origin = "http://127.0.0.1:43721"
        record, headers = {"port": 43721}, {"Origin": origin}
        app = FastAPI()
        configure_workspace(app, record, origin)
        endpoint = "/api/portable/screencast/shortcut"
        app.add_api_route(endpoint, configure_annotation_shortcut, methods=["POST"])
        state = app.state
        state.draining = False
        state.portable_screencast = None
        state.screen_draft_dir = Path("fixture-screen-drafts")
        state.portable_recording_lock = asyncio.Lock()
        state.portable_dictation = SimpleNamespace(control=asyncio.Lock())
        state.desktop_annotation = SimpleNamespace(configure=AsyncMock(), status=lambda: {"enabled": True})
        with patch("server.URL", origin):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                payload = {"enabled": True, "shortcut": "Ctrl+Alt+Shift+S"}
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(endpoint, headers=headers, json=payload)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                state.portable_screencast = object()
                self.assertEqual((await browser.post(endpoint, headers=headers, json=payload)).status_code, 409)
                state.desktop_annotation.configure.assert_not_awaited()
                state.portable_screencast = None
                self.assertEqual((await browser.post(endpoint, headers=headers, json=payload)).json(), {"enabled": True})
                state.desktop_annotation.configure.assert_awaited_once_with(enabled=True, shortcut="Ctrl+Alt+Shift+S")
                state.desktop_annotation.status = lambda: {"enabled": False}
                with self.assertRaises(HTTPException) as blocked:
                    await start_screen_take(state, "display", True, False, "", notice=object())
                self.assertEqual(blocked.exception.detail, "The Annotate shortcut is disabled.")

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_live_ink_is_owned_and_recovers_completed_strokes(self):
        from time import monotonic
        from linux_screencast import ScreenRecording
        from screencast_ink import load_events

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        path = "/api/portable/screencast/{key}/ink"
        app.add_api_route(path, append_screencast_ink, methods=["POST"], status_code=204)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            take = ScreenRecording(Path(directory), "window", False)
            app.state.portable_screencast = take
            take.draft.create()
            take.phase, take.started = "recording", monotonic() - 2
            endpoint = path.format(key=take.draft.key)
            stroke = {"time": 1.5, "points": [[.1, .2, 1], [.3, .4, 1.25]]}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.post(endpoint, headers=headers, json=stroke)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(endpoint, headers=headers, json=stroke)).status_code, 204)
                self.assertEqual((await browser.post(endpoint, headers=headers, json=stroke)).status_code, 422)
                self.assertEqual((await browser.post(endpoint, headers=headers, json={
                    "time": 1.8, "points": [],
                })).status_code, 204)
            with take.draft.ink.open("ab") as file:
                file.write(b'[{"time":')
            first, second, clear = load_events(take.draft.ink)
            self.assertEqual(first["from"], [.1, .8])
            self.assertEqual(second["to"], [.3, .6])
            self.assertEqual(second["liftedAt"], 1.5)
            self.assertEqual(clear, {"time": 1.8, "clear": True})

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_desktop_pen_uses_workspace_origin_and_marks_captured_strokes(self):
        from time import monotonic
        from dbus_fast import Variant
        from linux_pen import monitor_arguments
        from linux_screencast import ScreenRecording
        from screencast_ink import load_events
        from server import close_live_screencast_preview, live_screencast_preview

        self.assertEqual(monitor_arguments({"mapping_id": Variant("s", "DP-1"),
                                            "position": Variant("(ii)", [-1280, 160]),
                                            "size": Variant("(ii)", [1280, 720])}), ("DP-1", "-1280,160,1280,720"))
        self.assertEqual(monitor_arguments({"size": Variant("(ii)", [1280, 720])}), ("", ""))
        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        path = "/api/portable/screencast/{key}"
        app.add_api_route("/api/portable/screencast", screencast_status, methods=["GET"])
        app.add_api_route(path + "/pen", screencast_pen, methods=["POST", "DELETE"])
        app.add_api_route(path + "/preview", live_screencast_preview, methods=["GET"])
        app.add_api_route(path + "/preview", close_live_screencast_preview, methods=["DELETE"], status_code=204)
        app.add_api_route(path + "/ink", append_screencast_ink, methods=["POST"], status_code=204)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            take = ScreenRecording(Path(directory), "display", False)
            app.state.portable_screencast = take
            take.draft.create()
            take.phase, take.started = "recording", monotonic() - 2
            take.pen.monitor = ("DP-1", "-1280,160,1280,720")
            endpoint = path.format(key=take.draft.key)
            with patch.object(take.pen, "open", new_callable=AsyncMock) as start:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                    self.assertEqual((await browser.post(endpoint + "/pen", headers=headers)).status_code, 403)
                    start.assert_not_awaited()
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertTrue((await browser.post(endpoint + "/pen", headers=headers)).json()["desktopDrawing"])
                    start.assert_awaited_once_with(take.started, None)
                    take.pen.task = asyncio.create_task(asyncio.sleep(30))
                    try:
                        self.assertEqual((await browser.post(endpoint + "/ink", headers=headers, json={
                            "time": 1.5, "points": [[.1, .2, 1.2]],
                        })).status_code, 409)
                    finally:
                        take.pen.task.cancel()
                        await asyncio.gather(take.pen.task, return_exceptions=True)
                    with patch.object(take.pen, "close", new_callable=AsyncMock) as close:
                        self.assertEqual((await browser.delete(endpoint + "/pen", headers=headers)).status_code, 200)
                        close.assert_awaited_once()
                    take.pen.monitor = None
                    with patch.dict(os.environ, {"JARO_DESKTOP_TRAY": directory}):
                        await take.toggle_drawing()
                    self.assertTrue((Path(directory) / "open").is_file())
                    view = take.status()["drawingView"]
                    self.assertNotIn("drawingView", (await browser.get("/api/portable/screencast")).json())
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                    self.assertEqual((await browser.get("/api/portable/screencast")).json()["drawingView"], view)
                    self.assertEqual((await browser.get(endpoint + "/preview", params={"view": "stale"})).status_code, 409)
                    await browser.delete(endpoint + "/preview", headers=headers, params={"view": "stale"})
                    self.assertEqual(take.status()["drawingView"], view)
                    await browser.delete(endpoint + "/preview", headers=headers, params={"view": view})
                    self.assertNotIn("drawingView", take.status())
            take.pen.keep_stroke({
                "released": (take.started + 1.5) * 1_000_000,
                "points": [[.1, .2, (take.started + 1.2) * 1_000_000]],
            }, take.started)
            self.assertEqual(load_events(take.draft.ink), [{
                "time": 1.2, "stroke": 0, "from": [.1, .8], "to": [.1, .8], "liftedAt": 1.5, "captured": True,
            }])

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_native_x11_screen_sources_work_without_screen_share_but_xwayland_cannot_bypass_it(self):
        from linux_desktop_portal import ScreenCastPortal
        from server import screencast_sources

        origin = "http://127.0.0.1:43721"
        record, headers = {"port": 43721}, {"Origin": origin}
        app = FastAPI()
        state = app.state
        state.draining = False
        state.status = "ready"
        state.portable_recording = state.portable_dictation = state.portable_screencast = None
        state.portable_recording_lock = asyncio.Lock()
        state.jobs = set()
        endpoint = "/api/portable/screencast"
        app.add_api_route(endpoint + "/sources", screencast_sources, methods=["GET"])
        app.add_api_route(endpoint + "/microphones", screencast_microphones, methods=["GET"])
        app.add_api_route(endpoint, begin_screencast, methods=["POST"], status_code=202)
        app.add_api_route(endpoint, discard_screencast, methods=["DELETE"], status_code=204)
        configure_workspace(app, record, origin)
        portal = AsyncMock(spec=ScreenCastPortal)
        portal.connect.side_effect = OSError("No portal")
        inputs = [{"id": "usb", "label": "USB microphone", "default": False}]
        with (TemporaryDirectory() as staging, patch("server.URL", origin),
              patch.dict(os.environ, {"DISPLAY": ":88", "WAYLAND_DISPLAY": "", "XDG_SESSION_TYPE": "tty"}),
              patch("server.transcription_languages", return_value=()),
              patch("linux_capture.microphone_inputs", new=AsyncMock(return_value=inputs)),
              patch("linux_screencast.supported", return_value=True),
              patch("linux_desktop_portal.ScreenCastPortal", return_value=portal),
              patch("linux_screencast.ScreenCastPortal", return_value=portal) as recording_portal,
              patch("linux_screencast.ScreenRecording.record", new_callable=AsyncMock)):
            state.screen_draft_dir = Path(staging)
            state.library = SessionStore(Path(staging) / "library")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.get(endpoint + "/microphones")).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(endpoint + "/microphones")).json(), {"inputs": inputs})
                self.assertEqual((await browser.get(endpoint + "/sources")).json(), {"sources": ["display", "area"]})
                audio = {"microphone": "true", "microphone_device": "usb"}
                stale = {**audio, "source": "display", "microphone_device": "removed"}
                self.assertEqual((await browser.post(endpoint, params=stale, headers=headers)).status_code, 409)
                for source in ("display", "area"):
                    response = await browser.post(endpoint, params={**audio, "source": source}, headers=headers)
                    self.assertEqual(response.status_code, 202)
                    self.assertIsNone(state.portable_screencast.portal)
                    self.assertEqual(state.portable_screencast.microphone_device, "usb")
                    key = response.json()["id"]
                    self.assertEqual((await browser.delete(f"{endpoint}?key={key}", headers=headers)).status_code, 204)
                recording_portal.assert_not_called()
                with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                    self.assertEqual((await browser.get(endpoint + "/sources")).json(), {"sources": []})
                with patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}):
                    self.assertEqual((await browser.get(endpoint + "/sources")).json(), {"sources": []})
                portal.connect.side_effect = None
                portal.sources.return_value = ["window"]
                self.assertEqual((await browser.get(endpoint + "/sources")).json(), {"sources": ["display", "area", "window"]})
                response = await browser.post(endpoint, params={**audio, "source": "window"}, headers=headers)
                self.assertEqual(response.status_code, 202)
                self.assertIs(state.portable_screencast.portal, portal)
                self.assertEqual(state.portable_screencast.microphone_device, "usb")
                self.assertEqual((await browser.delete(endpoint, headers=headers)).status_code, 204)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux screen recorder")
    async def test_linux_screen_rechecks_narration_input_after_source_selection(self):
        from linux_screencast import ScreenRecording

        with (TemporaryDirectory() as staging,
              patch("linux_capture.microphone_sources", new=AsyncMock(return_value=[{"name": "builtin"}])),
              patch("linux_screencast.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn):
            take = ScreenRecording(Path(staging), "display", True, microphone_device="disconnected", native_x11=True)
            take.target = (0, 0, 800, 600)
            with self.assertRaisesRegex(CaptureError, "selected microphone is unavailable"):
                await take.start_encoder(9)
            spawn.assert_not_awaited()
            self.assertFalse(take.draft.folder.exists())

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_screen_recording_dismissal_releases_the_workspace(self):
        from linux_desktop_portal import PortalResponseError, ScreenCastPortal

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screencast"
        app.add_api_route(endpoint, begin_screencast, methods=["POST"], status_code=202)
        app.add_api_route(endpoint, screencast_status, methods=["GET"])
        app.add_api_route(endpoint, discard_screencast, methods=["DELETE"], status_code=204)
        app.state.draining = False
        app.state.portable_recording = app.state.portable_dictation = app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        app.state.jobs = set()
        configure_workspace(app, record, origin)
        portal = AsyncMock(spec=ScreenCastPortal)
        portal.select.side_effect = PortalResponseError(1, {})
        headers = {"Origin": origin}
        with (TemporaryDirectory() as staging, patch("server.URL", origin),
              patch("linux_screencast.supported", return_value=True),
              patch("linux_screencast.ScreenCastPortal", return_value=portal)):
            app.state.screen_draft_dir = Path(staging)
            app.state.library = SessionStore(Path(staging) / "library")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                start = endpoint + "?source=display"
                self.assertEqual((await browser.post(start, headers=headers)).status_code, 403)
                portal.connect.assert_not_called()
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.post(start)).status_code, 403)
                started = await browser.post(start, headers=headers)
                self.assertEqual(started.status_code, 202)
                self.assertEqual(started.json()["levels"], {})
                await app.state.portable_screencast.task
                self.assertEqual((await browser.get(endpoint)).json()["phase"], "cancelled")
                self.assertEqual((await browser.post(start, headers=headers)).status_code, 409)
                portal.close.assert_awaited_once()
                self.assertEqual((await browser.delete(endpoint, headers=headers)).status_code, 204)
                self.assertEqual((await browser.get(endpoint)).json(), {"phase": "idle"})
                self.assertFalse(list(Path(staging).iterdir()))

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_screen_preview_is_private_and_expires_when_refreshed(self):
        from linux_screencast import ScreenRecording

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        endpoint = "/api/portable/screencast/area"
        app.add_api_route(endpoint + "/{key}", screencast_preview, methods=["GET"])
        app.add_api_route(endpoint + "/{key}", set_screencast_region, methods=["POST"], status_code=202)
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as directory, patch("server.URL", origin):
            take = ScreenRecording(Path(directory), "area", False)
            app.state.portable_screencast = take
            take.phase = "selecting"
            take.preview, take.preview_size = b"preview", (800, 600)
            take.preview_id = str(uuid4())
            take.selection = asyncio.get_running_loop().create_future()
            first = endpoint + "/" + take.preview_id
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.get(first)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                response = await browser.get(first)
                self.assertEqual(response.content, b"preview")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual((await browser.post(first, headers=headers, json={
                    "x": 700, "y": 0, "width": 101, "height": 100,
                })).status_code, 422)
                self.assertFalse(take.selection.done())
                self.assertEqual((await browser.post(first, headers=headers)).json()["phase"], "previewing")
                self.assertIsNone(await take.selection)
                take.phase = "selecting"
                take.preview_id = str(uuid4())
                take.selection = asyncio.get_running_loop().create_future()
                self.assertEqual((await browser.get(first)).status_code, 404)
                self.assertEqual((await browser.post(first, headers=headers)).status_code, 409)
                accepted = await browser.post(endpoint + "/" + take.preview_id, headers=headers,
                                               json={"x": 3, "y": 5, "width": 160, "height": 100})
                self.assertEqual(accepted.json()["phase"], "starting")
                self.assertEqual((await take.selection).width, 160)
            self.assertFalse(list(Path(directory).iterdir()))

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    def test_screen_region_cannot_include_pixels_outside_the_preview(self):
        from linux_screencast import Region

        region = Region(101, 99, 180, 102, 800, 600)
        self.assertIn("right=519", region.crop())
        self.assertIn("bottom=399", region.crop())
        self.assertIn("video/x-raw,format=RGBx,width=800,height=600", region.crop())
        for bounds in ((-1, 0, 2, 2), (0, 0, 1, 2), (799, 0, 2, 2), (0, 598, 2, 3)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                Region(*bounds, 800, 600)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    async def test_screen_video_saves_after_audio_setup_failure_and_legacy_recovery(self):
        from annotation_assets import annotation_movie_tracks
        from screen_draft import ScreenDraft, writer_lock
        from server import finish_screencast, portable_recording_drafts, save_portable_recording, select_annotation_folder, session_annotation_asset
        from windows_screencast import WindowsScreenRecording

        app = FastAPI()
        app.add_api_route("/api/portable/screencast/finish", finish_screencast, methods=["POST"])
        app.add_api_route("/api/portable/recording/{key}/finish", save_portable_recording, methods=["POST"])
        app.add_api_route("/api/portable/recording/drafts", portable_recording_drafts, methods=["GET"])
        app.add_api_route("/api/sessions/{key}/annotation/{kind}", session_annotation_asset, methods=["GET"])
        with TemporaryDirectory() as folder:
            root = Path(folder)
            state = app.state
            state.library = SessionStore(root / "library")
            state.screen_draft_dir = root
            state.portable_recording = None
            state.portable_recording_dir = root / "microphone"
            state.portable_recording_dir.mkdir()
            state.portable_recording_lock = asyncio.Lock()
            state.jobs = set()
            state.draining = False
            state.status = "loading"
            state.diarizer = None

            async def generate_video(draft, *, audio=False):
                with writer_lock(draft.folder, create=True):
                    source = ["-f", "lavfi", "-i", "color=black:s=64x64:r=10:d=1"]
                    if audio:
                        source += ["-f", "lavfi", "-i", "sine=frequency=523:duration=1", "-c:a", "aac", "-shortest"]
                    process = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-v", "error", *source, "-c:v", "mpeg4",
                        "-movflags", "+frag_keyframe+empty_moov", draft.movie,
                    )
                    self.assertEqual(await process.wait(), 0)

            take = state.portable_screencast = WindowsScreenRecording(root, "display", "1a:0", microphone=True)
            take.draft.create()
            await generate_video(take.draft)
            take.phase = "recorded"
            failed = SimpleNamespace(start=AsyncMock(side_effect=CaptureError("Microphone unavailable.")), stop=AsyncMock())
            with patch("windows_screencast.WindowsScreenAudio", return_value=failed):
                await take.start_audio(100_000)
            self.assertIsNone(take.audio)
            self.assertEqual(ScreenDraft.read(take.draft.folder).audio_sources, ())
            self.assertEqual(take.status()["error"], "Microphone unavailable.")

            drive = root / "Annotations drive"
            drive.mkdir()
            pinned = select_annotation_folder(state.library, str(drive.resolve()))["storageId"]
            previous = ScreenDraft(root / str(uuid4()), "window", ("mic", "system"), pinned)
            previous.create()
            await generate_video(previous)
            self.assertEqual(ScreenDraft.read(previous.folder).annotation_location, pinned)
            select_annotation_folder(state.library, None)
            audible = ScreenDraft(root / str(uuid4()), "window", ("mic", "system"))
            audible.create()
            await generate_video(audible, audio=True)
            metadata = audible.folder / "draft.json"
            metadata.write_text(json.dumps({"version": 1, "source": "window", "audio": ["mic", "system"]}))
            self.assertIsNone(ScreenDraft.read(audible.folder).annotation_location)
            without_preview = ScreenDraft(root / str(uuid4()), "area", ())
            without_preview.create()
            await generate_video(without_preview)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                with patch("server.screencast_action", return_value=state), patch("server.portable_capture_state", return_value=state):
                    drafts = (await client.get("/api/portable/recording/drafts")).raise_for_status().json()
                    self.assertEqual({draft["id"]: draft["videoOnly"] for draft in drafts},
                                     {previous.key: True, audible.key: False, without_preview.key: True})
                    saved = await client.post(f"/api/portable/screencast/finish?key={take.draft.key}")
                    self.assertEqual(saved.status_code, 202, saved.text)
                    self.assertEqual(saved.json(), {"id": take.draft.key})
                    self.assertIsNone(state.portable_screencast)
                    disconnected = drive.rename(root / "Disconnected drive")
                    drive.mkdir()
                    state.library = SessionStore(state.library.root)
                    waiting = await client.post(f"/api/portable/recording/{previous.key}/finish")
                    self.assertEqual(waiting.status_code, 503)
                    self.assertIn("Reconnect", waiting.json()["detail"])
                    self.assertTrue(previous.movie.is_file())
                    self.assertFalse(state.library.exists(previous.key))
                    self.assertEqual(list(drive.iterdir()), [])
                    drive.rmdir()
                    disconnected.rename(drive)
                    recovered = await client.post(f"/api/portable/recording/{previous.key}/finish")
                    self.assertEqual(recovered.status_code, 202, recovered.text)
                    self.assertEqual(recovered.json(), {"id": previous.key})
                    self.assertEqual(state.library.media(previous.key)[0].parent.parent, drive.resolve())
                    self.assertEqual(state.library.media(take.draft.key)[0].parent.parent, state.library.audio)
                    pending = await client.post(f"/api/portable/recording/{audible.key}/finish")
                    self.assertEqual(pending.status_code, 503, pending.text)
                    self.assertTrue(audible.movie.is_file())
                    with patch("screen_contact_sheet.read_preview_frame", side_effect=TimeoutError):
                        saved_without_preview = await client.post(f"/api/portable/recording/{without_preview.key}/finish")
                    self.assertEqual(saved_without_preview.status_code, 202, saved_without_preview.text)

                for draft in (take.draft, previous):
                    annotation = state.library.get(draft.key)["annotation"]
                    manifest = (await client.get(annotation["manifestUrl"])).raise_for_status().json()
                    self.assertEqual(annotation["frames"], manifest["frames"])
                    self.assertEqual(len(manifest["frames"]), 2)
                    self.assertAlmostEqual(manifest["frames"][0], 0.)
                    self.assertAlmostEqual(manifest["frames"][1], .9)
                    sheet = (await client.get(annotation["contactSheetUrl"])).raise_for_status()
                    self.assertEqual(sheet.headers["Content-Type"], "image/png")
                    self.assertEqual(sheet.content[:8], b"\x89PNG\r\n\x1a\n")
                    self.assertEqual(struct.unpack(">II", sheet.content[16:24]), (872, 282))
                    self.assertEqual(annotation["keyframes"], [1, 2])
                    self.assertEqual(annotation["keyframes"], manifest["keyframes"])
                    still = (await client.get(f"/api/sessions/{draft.key}/annotation/frame-01")).raise_for_status()
                    self.assertEqual(still.headers["Content-Type"], "image/png")
                    self.assertEqual(struct.unpack(">II", still.content[16:24]), (64, 64))
                annotation = state.library.get(without_preview.key)["annotation"]
                self.assertEqual(annotation["frames"], [])
                self.assertIsNone(annotation["contactSheetUrl"])

            for draft in (take.draft, previous, without_preview):
                self.assertFalse(draft.folder.exists())
                path, mime = state.library.media(draft.key)
                self.assertEqual(mime, "video/mp4")
                duration, audio_tracks = await annotation_movie_tracks(path)
                self.assertAlmostEqual(duration, 1.0)
                self.assertEqual(audio_tracks, 0)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    def test_screen_draft_can_be_opened_only_after_its_recorder_releases_it(self):
        from linux_screencast import ScreenDraft, writer_lock

        with TemporaryDirectory() as staging:
            folder = Path(staging) / str(uuid4())
            draft = ScreenDraft(folder, "area", ("mic", "system"))
            draft.create()
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertIsNone(ScreenDraft.read(folder))
            with writer_lock(folder, create=True):
                draft.movie.write_bytes(b"fragment fixture")
                self.assertIsNone(ScreenDraft.read(folder))
            self.assertEqual(ScreenDraft.read(folder), draft)
            draft.movie.unlink()
            outside = Path(staging) / "outside.mp4"
            outside.write_bytes(b"other file")
            draft.movie.symlink_to(outside)
            self.assertIsNone(ScreenDraft.read(folder))
            draft.discard()
            self.assertFalse(folder.exists())
            self.assertTrue(outside.is_file())

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux ScreenCast portal client")
    async def test_screen_recording_meters_select_the_loudest_channel(self):
        from linux_screencast import ScreenRecording, input_level

        prefix = b'Got message #42 from element "system_level" (element): level, endtime=(guint64)200000000, '
        self.assertEqual(input_level(prefix + b"peak=(GValueArray)< -699, -699 >;"), ("system", 0))
        source, peak = input_level(prefix + b"peak=(GValueArray)< -50, -6.020599913279624 >;")
        self.assertEqual(source, "system")
        self.assertAlmostEqual(peak, 0.5)
        self.assertIsNone(input_level(prefix + b"peak=(GValueArray)< invalid >;"))
        self.assertIsNone(input_level(b'Got message #11 from element "filesink0" (state-changed): '))

        with TemporaryDirectory() as folder:
            take = ScreenRecording(Path(folder), "display", True, True, native_x11=True)
            take.phase = "recording"
            stdout = asyncio.StreamReader()
            take.recorder = SimpleNamespace(stdout=stdout)
            watcher = asyncio.create_task(take.read_levels())
            try:
                stdout.feed_data(prefix + b"peak=(GValueArray)<-6, -18>;\njaro-screen-audio mic unavailable\n")
                await asyncio.sleep(0)
                status = take.status()
                self.assertEqual(status["phase"], "recording")
                self.assertIn("microphone to reconnect", status["error"])
                self.assertGreater(status["levels"]["system"], 0.5)
                stdout.feed_data(b"jaro-screen-audio mic ready\n")
                await asyncio.sleep(0)
                self.assertIsNone(take.status()["error"])
            finally:
                stdout.feed_eof()
                await asyncio.wait_for(watcher, 2)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux screenshot portal client")
    async def test_screenshot_picker_requires_its_workspace_and_handles_cancel(self):
        from linux_desktop_portal import PortalResponseError, ScreenshotPortal

        origin = "http://127.0.0.1:43721"
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route("/api/portable/screenshots", screenshot_targets, methods=["GET"])
        app.add_api_route("/api/portable/screenshots", portable_screenshot, methods=["POST"])
        app.state.draining = False
        app.state.portable_screencast = None
        app.state.portable_recording_lock = asyncio.Lock()
        configure_workspace(app, record, origin)
        endpoint = "/api/portable/screenshots"
        portal = AsyncMock(spec=ScreenshotPortal)
        portal.targets.return_value = ["picker"]
        image = b"\x89PNG\r\n\x1a\nfixture"
        with (patch("server.URL", origin), patch("linux_desktop_portal.ScreenshotPortal", return_value=portal),
              patch("linux_desktop_portal.read_screenshot", return_value=(image, "image/png")) as load):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.post(endpoint + "?source=picker", headers={"Origin": origin})).status_code, 403)
                portal.connect.assert_not_called()
                await browser.post(BOOTSTRAP, headers={"Origin": origin, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(endpoint)).json(), {"targets": ["picker"]})
                self.assertEqual((await browser.post(endpoint + "?source=picker")).status_code, 403)
                portal.screenshot.side_effect = PortalResponseError(1, {})
                approved = {"Origin": origin}
                self.assertEqual((await browser.post(endpoint + "?source=picker", headers=approved)).status_code, 204)
                load.assert_not_called()
                portal.screenshot.side_effect = None
                portal.screenshot.return_value = "file:///fixture.png"
                response = await browser.post(endpoint + "?source=picker", headers=approved)
                self.assertEqual(response.content, image)
                self.assertEqual(response.headers["content-type"], "image/png")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertFalse(app.state.screenshot_pending)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux screenshot portal client")
    async def test_linux_screenshot_and_screen_share_cannot_open_two_pickers(self):
        from linux_desktop_portal import PortalResponseError, ScreenshotPortal, ScreenCastPortal

        async def choose_still(_source):
            picking_still.set()
            await release.wait()
            raise PortalResponseError(1, {})

        async def choose_video(_source):
            picking_video.set()
            await release.wait()
            raise PortalResponseError(1, {})

        still_portal, video_portal = AsyncMock(spec=ScreenshotPortal), AsyncMock(spec=ScreenCastPortal)
        still_portal.screenshot.side_effect, video_portal.select.side_effect = choose_still, choose_video
        picking_still, picking_video, release = (asyncio.Event() for _ in range(3))
        origin = "http://127.0.0.1:43721"
        app = FastAPI()
        state = app.state
        state.draining = False
        state.portable_recording = state.portable_dictation = state.portable_screencast = None
        state.portable_recording_lock = asyncio.Lock()
        state.jobs = set()
        still, video = "/api/portable/screenshots", "/api/portable/screencast"
        app.add_api_route(still, portable_screenshot, methods=["POST"])
        app.add_api_route(video, begin_screencast, methods=["POST"], status_code=202)
        app.add_api_route(video, discard_screencast, methods=["DELETE"], status_code=204)
        record, headers = {"port": 43721}, {"Origin": origin}
        configure_workspace(app, record, origin)
        with (TemporaryDirectory() as staging, patch("server.URL", origin),
              patch("linux_screencast.supported", return_value=True),
              patch("linux_desktop_portal.ScreenshotPortal", return_value=still_portal),
              patch("linux_screencast.ScreenCastPortal", return_value=video_portal)):
            state.screen_draft_dir = Path(staging)
            state.library = SessionStore(Path(staging) / "library")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                shot = asyncio.create_task(browser.post(still + "?source=picker", headers=headers))
                try:
                    await asyncio.wait_for(picking_still.wait(), 1)
                    self.assertEqual((await browser.post(video + "?source=display", headers=headers)).status_code, 409)
                    video_portal.connect.assert_not_awaited()
                finally:
                    shot.cancel()
                    await asyncio.gather(shot, return_exceptions=True)
                    if take := state.portable_screencast:
                        take.task.cancel()
                        await asyncio.gather(take.task, return_exceptions=True)
                self.assertFalse(state.screenshot_pending)
                still_portal.close.assert_awaited_once()

                started = await browser.post(video + "?source=display", headers=headers)
                started.raise_for_status()
                try:
                    await asyncio.wait_for(picking_video.wait(), 1)
                    self.assertEqual((await asyncio.wait_for(browser.post(still + "?source=picker", headers=headers), 1)).status_code, 409)
                    still_portal.connect.assert_awaited_once()
                finally:
                    release.set()
                    await state.portable_screencast.task
                key = started.json()["id"]
                self.assertEqual((await browser.delete(f"{video}?key={key}", headers=headers)).status_code, 204)
                self.assertEqual((await browser.post(still + "?source=picker", headers=headers)).status_code, 204)

    @unittest.skipUnless(sys.platform == "linux" and find_spec("dbus_fast"), "requires the Linux screenshot portal client")
    async def test_screenshot_targets_and_untrusted_file_uris(self):
        from linux_desktop_portal import SCREENSHOT, ScreenshotPortal, read_screenshot

        portal = ScreenshotPortal()
        with patch.object(portal, "property", side_effect=[2, 3, 5, 3, 5]):
            self.assertEqual(await portal.targets(), ["picker"])
            self.assertEqual(await portal.targets(), ["area", "display"])
            with patch.object(portal, "request", new_callable=AsyncMock, return_value={}) as screenshot:
                with self.assertRaisesRegex(CaptureError, "did not return"):
                    await portal.screenshot("area")
                args = screenshot.call_args.args
                self.assertEqual(args[:2], (SCREENSHOT, "Screenshot"))
                self.assertTrue(args[-1]["interactive"].value)
                self.assertEqual(args[-1]["target"].value, 4)

        with TemporaryDirectory() as folder:
            image = Path(folder) / "screen shot.png"
            data = b"\x89PNG\r\n\x1a\nfixture"
            image.write_bytes(data)
            self.assertEqual(read_screenshot(image.as_uri()), (data, "image/png"))
            for uri in ("https://example.invalid/file.png", "file://remotehost/a.png", "file:///tmp/%00a"):
                with self.subTest(uri=uri), self.assertRaises(CaptureError):
                    read_screenshot(uri)
            link = image.with_name("symlink.png")
            link.symlink_to(image)
            with self.assertRaises(OSError):
                read_screenshot(link.as_uri())

    async def test_only_workspace_owner_can_start_one_resumable_model_install(self):
        origin = "http://127.0.0.1:43721"
        app = FastAPI()
        app.add_api_route("/api/portable/models", start_model_setup, methods=["POST"], status_code=202)
        app.state.jobs = set()
        app.state.draining = False
        app.state.model_setup = {"phase": "needed", "total": 100}
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        installing = asyncio.Event()
        finish = asyncio.Event()

        async def install(report, *, repair):
            self.assertFalse(repair)
            report(25, 100)
            installing.set()
            await finish.wait()
            raise RuntimeError("Interrupted download.")

        async def resume(report, *, repair):
            report(100, 100)

        async def load(owner):
            self.assertEqual(owner.state.model_setup["phase"], "starting")
            owner.state.status = "ready"

        with (patch("server.URL", origin), patch("server.install_workspace_models", side_effect=install) as download,
              patch("server.load_speech", side_effect=load), patch("server.additional_models_ready", return_value=True),
              patch("server.logger.exception")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as webview:
                endpoint = "/api/portable/models"
                headers = {"Origin": origin}
                self.assertEqual((await webview.post(endpoint, headers=headers)).status_code, 403)
                await webview.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await webview.post(endpoint, headers=headers)).status_code, 403)
                download.assert_not_awaited()
                await webview.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                self.assertEqual((await webview.post(endpoint)).status_code, 403)
                self.assertEqual((await webview.post(endpoint, headers={"Origin": "https://other.example"})).status_code, 403)
                response = await webview.post(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["modelSetup"]["phase"], "downloading")
                await asyncio.wait_for(installing.wait(), 1)
                again = await webview.post(endpoint, headers=headers)
                self.assertEqual(again.json()["modelSetup"]["completed"], 25)
                download.assert_awaited_once()
                finish.set()
                await asyncio.gather(*app.state.jobs)
                self.assertEqual(app.state.model_setup["phase"], "download-error")
                download.side_effect = resume
                response = await webview.post(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202)
                await asyncio.gather(*app.state.jobs)
                self.assertIsNone(app.state.model_setup)
                self.assertEqual(app.state.status, "ready")
                self.assertEqual(download.await_count, 2)
                app.state.model_setup = {"phase": "load-error", "total": 100}
                download.side_effect = RuntimeError("Interrupted repair.")
                response = await webview.post(endpoint, headers=headers)
                self.assertEqual(response.status_code, 202)
                self.assertTrue(response.json()["modelSetup"]["repair"])
                await asyncio.gather(*app.state.jobs)
                self.assertEqual(app.state.model_setup["phase"], "download-error")
                self.assertTrue(download.await_args.kwargs["repair"])
                download.side_effect = resume
                await webview.post(endpoint, headers=headers)
                await asyncio.gather(*app.state.jobs)
                self.assertTrue(download.await_args.kwargs["repair"])
                self.assertIsNone(app.state.model_setup)
                self.assertEqual(download.await_count, 4)

    async def test_model_setup_distinguishes_missing_models_from_failed_startup(self):
        app = FastAPI()
        app.state.model_setup = {"phase": "checking"}

        async def load(owner):
            owner.state.status = status

        with (patch("server.load_speech", side_effect=load), patch("server.model_status") as installed,
              patch("server.additional_models_ready", return_value=True) as additional):
            status = "ready"
            await start_workspace_speech(app)
            self.assertIsNone(app.state.model_setup)
            installed.assert_not_called()

            additional.return_value = False
            installed.return_value = {"ready": False, "bytes": 4100}
            await start_workspace_speech(app)
            self.assertEqual(app.state.model_setup, {"phase": "needed", "total": 4100, "additional": True})

            status = "error"
            installed.return_value = {"ready": False, "bytes": 4100}
            await start_workspace_speech(app)
            self.assertEqual(app.state.model_setup, {"phase": "needed", "total": 4100})

            installed.return_value = {"ready": True, "bytes": 4100}
            await start_workspace_speech(app)
            self.assertEqual(app.state.model_setup, {"phase": "load-error", "total": 4100})

    async def test_additional_setup_keeps_ready_speech_and_attaches_to_the_existing_model(self):
        from server import SpeechInference, setup_workspace_speech

        app = FastAPI()
        state = app.state
        state.model = model = SimpleNamespace(aligner=None)
        state.diarizer = None
        state.status = "ready"
        state.inference = SpeechInference()
        state.model_setup = {"phase": "downloading", "additional": True}

        async def attach(owner):
            self.assertEqual(owner.status, "ready")
            self.assertIs(owner.model, model)
            self.assertTrue(owner.inference.active)
            owner.diarizer = object()
            owner.model.aligner = object()

        with (patch("server.install_workspace_models", new_callable=AsyncMock, side_effect=RuntimeError) as install,
              patch("server.load_speaker_tools", side_effect=attach) as extras,
              patch("server.load_speech", new_callable=AsyncMock) as speech, patch("server.logger.exception"),
              patch("server.writing_model_installed", return_value=False) as writing):
            await setup_workspace_speech(app)
            self.assertEqual(state.model_setup["phase"], "download-error")
            self.assertEqual(state.status, "ready")
            extras.assert_not_awaited()

            install.side_effect = None
            await setup_workspace_speech(app)
            self.assertEqual(state.model_setup["phase"], "load-error")
            self.assertEqual(state.status, "ready")
            writing.return_value = True
            await setup_workspace_speech(app, repair=True)
            self.assertIsNone(state.model_setup)
            self.assertIs(state.model, model)
            speech.assert_not_awaited()

    async def test_linux_capture_requires_a_microphone_instead_of_a_playback_monitor(self):
        sources = '[{"name":"mic","properties":{}},{"name":"speaker.monitor","properties":{"device.class":"monitor"}}]'
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["mic\n", sources])):
            self.assertEqual(await default_microphone(), "mic")
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["speaker.monitor\n", sources])):
            with self.assertRaisesRegex(CaptureError, "Select a microphone"):
                await default_microphone()

    async def test_linux_sdk_capture_stops_when_the_input_disappears(self):
        capture = LinuxRecording(uuid4(), Path("fixture.wav"), "mic", microphone="usb")
        capture.capture_source = "usb"
        capture.process = SimpleNamespace(returncode=None, terminate=Mock())
        with patch("linux_capture.pulse_info", side_effect=CaptureError("Device removed")) as pulse:
            await capture.watch_input()
        pulse.assert_awaited_once_with("get-source-volume", "usb")
        self.assertTrue(capture.interrupted)
        capture.process.terminate.assert_called_once()

    async def test_dictation_microphones_list_and_capture_only_input_sources(self):
        sources = ('[{"name":"builtin","description":"Built-in microphone"},'
                   '{"name":"usb","description":"USB microphone"},'
                   '{"name":"speaker.monitor"},'
                   '{"name":"playback","properties":{"device.class":"monitor"}},'
                   '{"name":"speaker_return","monitor_of_sink":12}]')
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=[sources, "builtin\n"])):
            self.assertEqual(await microphone_inputs(), [
                {"id": "builtin", "label": "Built-in microphone", "default": True},
                {"id": "usb", "label": "USB microphone", "default": False},
            ])
        with patch("linux_capture.pulse_info", new=AsyncMock(return_value=sources)):
            self.assertEqual((await capture_args("mic", "usb"))[:4], ("-f", "pulse", "-i", "usb"))
            with self.assertRaisesRegex(CaptureError, "unavailable"):
                await capture_args("mic", "speaker.monitor")
        self.assertEqual(parse_microphones("1\t{endpoint-a}\tIntégré\n0\t{endpoint-b}\tUSB\n".encode()), [
            {"id": "{endpoint-a}", "label": "Intégré", "default": True},
            {"id": "{endpoint-b}", "label": "USB", "default": False},
        ])

    async def test_dictation_microphone_persists_and_falls_back_without_losing_the_selection(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_microphone, portable_microphone

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation/microphone"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(endpoint, portable_microphone, methods=["GET"])
        app.add_api_route(endpoint, change_portable_microphone, methods=["POST"])
        configure_workspace(app, record, origin)

        class Dictation(PortableDictation):
            platform = "x11"
            enabled = True
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        with TemporaryDirectory() as folder:
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.draining = False
            state.status = "ready"
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recording = None
            history = PortableDictationHistory(folder)
            history.initialize()
            with history.connection(write=True) as db, db:
                db.execute("ALTER TABLE preferences DROP COLUMN microphone")
            history.initialize()
            self.assertEqual(history.microphone(), "")
            transcribe = AsyncMock(return_value={"text": "Fixture speech", "inferenceMs": 80})
            state.portable_dictation = Dictation(state, transcribe, history)
            inputs = AsyncMock(return_value=[{"id": "usb", "label": "USB", "default": False}])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                with patch("server.URL", origin), patch("linux_capture.microphone_inputs", inputs):
                    self.assertEqual((await browser.get(endpoint)).status_code, 403)
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await browser.get(endpoint)).json()["selected"], "")
                    self.assertEqual((await browser.post(endpoint, json={"selected": "usb"})).status_code, 403)
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": "stale"})).status_code, 400)
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": "usb"})).status_code, 200)
                    state.portable_dictation = Dictation(state, transcribe, history)
                    self.assertEqual(state.portable_dictation.microphone, "usb")
                    inputs.side_effect = CaptureError("Input service unavailable.")
                    self.assertEqual((await browser.get(endpoint)).json(),
                                     {"selected": "usb", "inputs": [], "available": False})

                    attempted = []
                    worker = asyncio.Future()

                    def recorder(_key, path, mode, *, seconds, microphone):
                        self.assertEqual((mode, seconds), ("mic", 120))
                        attempted.append(microphone)
                        async def start():
                            if microphone:
                                raise CaptureError("USB disconnected.")
                            with private_wav(path) as (wav, _):
                                wav.writeframes(b"\x01\x02" * 1600)
                        return SimpleNamespace(
                            path=path, worker=worker, frames=1600, interrupted=False, stop=AsyncMock(), start=start,
                        )

                    state.portable_recorder = recorder
                    await state.portable_dictation.toggle()
                    self.assertEqual(state.portable_dictation.phase, "recording")
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": ""})).status_code, 409)
                    await state.portable_dictation.toggle()
                    self.assertEqual(attempted, ["usb", ""])
                    self.assertEqual(transcribe.await_args.args[1].name, "fallback.wav")
                    state.portable_dictation.deliver.assert_awaited_once_with("Fixture speech")
                    self.assertIn("Used the system microphone", state.portable_dictation.feedback)
                    self.assertEqual(history.microphone(), "usb")
                    row, = history.page(legacy=True, include_audio=True)["entries"]
                    self.assertEqual(row["text"], "Fixture speech")
                    self.assertIn("audioExpiresAt", row)
                    self.assertEqual((await browser.post(endpoint, headers=headers, json={"selected": ""})).status_code, 200)
                    self.assertEqual(history.microphone(), "")

    async def test_dictation_audio_uses_the_private_workspace_and_supports_webkit_ranges(self):
        from time import time
        from portable_dictation_history import SECONDS_PER_DAY, PortableDictationHistory
        from server import portable_history, portable_history_audio

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation/history"
        identifier = str(uuid4())
        url = f"{endpoint}/{identifier}/audio"
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(endpoint, portable_history, methods=["GET"])
        app.add_api_route(endpoint + "/{key}/audio", portable_history_audio, methods=["GET"])
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder:
            history = PortableDictationHistory(folder)
            history.initialize()
            app.state.portable_dictation = SimpleNamespace(history=history)
            app.state.portable_recording_dir = Path(folder)
            source = Path(folder) / "original.wav"
            with private_wav(source) as (wav, _):
                wav.writeframes(b"\x02\x01" * 1600)
            original = source.read_bytes()
            created = time()
            history.record({"id": identifier, "time": created, "text": "Original audio", "app": "",
                            "seconds": .1, "inferenceMs": 40, "totalMs": 100}, source)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                self.assertEqual((await browser.get(url)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={"Origin": origin, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await browser.get(url, headers={"Origin": "https://elsewhere.test"})).status_code, 403)
                response = await browser.get(url)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.content, original)
                response = await browser.get(url, headers={"Range": "bytes=0-1"})
                self.assertEqual((response.status_code, response.content), (206, b"RI"))
                self.assertEqual(response.headers["content-range"], f"bytes 0-1/{len(original)}")
                self.assertEqual((await browser.get(url, headers={"Range": "bytes=-4"})).content, original[-4:])
                self.assertEqual((await browser.get(url, headers={"Range": "bytes=100000-"})).status_code, 416)
                with patch("portable_dictation_history.time", return_value=created + SECONDS_PER_DAY):
                    self.assertEqual((await browser.get(url)).status_code, 404)
                    row, = (await browser.get(endpoint)).json()["entries"]
                    self.assertEqual(row["text"], "Original audio")
                    self.assertNotIn("audioExpiresAt", row)

    async def test_restart_restores_only_an_explicitly_enabled_shortcut_when_speech_is_ready(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_dictation, portable_dictation_status, restore_portable_shortcut

        class Dictation(PortableDictation):
            platform = "windows"
            enabled = False
            refuse = False

            async def enable(self):
                if self.refuse:
                    raise CaptureError("The shortcut could not register.")
                self.enabled = True

            async def disable(self):
                self.enabled = False

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation"
        app = FastAPI()
        state = app.state
        state.status = "ready"
        state.draining = False
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/{action}", change_portable_dictation, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        headers = {"Origin": origin}
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            state.portable_recording_dir = Path(folder)
            history = PortableDictationHistory(folder)
            history.initialize()

            def reopen():
                state.portable_dictation = Dictation(state, None, history)
                state.portable_shortcut_ready = asyncio.Event()
                return state.portable_dictation

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                dictation = reopen()
                self.assertEqual((await browser.post(endpoint + "/enable", headers=headers)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                state.portable_shortcut_ready.set()
                await restore_portable_shortcut(state)
                self.assertFalse(dictation.enabled)
                response = await browser.post(endpoint + "/enable", headers=headers)
                self.assertTrue(response.raise_for_status().json()["shortcutRequested"])
                await dictation.disable()  # Runtime shutdown keeps the preference.

                dictation = reopen()
                restore = asyncio.create_task(restore_portable_shortcut(state))
                await asyncio.sleep(0)
                self.assertFalse(dictation.enabled)
                state.portable_shortcut_ready.set()
                await restore
                self.assertTrue(dictation.enabled)

                dictation = reopen()
                dictation.refuse = True
                state.portable_shortcut_ready.set()
                await restore_portable_shortcut(state)
                self.assertFalse(dictation.enabled)
                self.assertEqual(dictation.feedback, "The shortcut could not register.")
                self.assertTrue(dictation.shortcut_requested)

                response = await browser.post(endpoint + "/disable", headers=headers)
                self.assertFalse(response.raise_for_status().json()["shortcutRequested"])
                dictation = reopen()
                state.portable_shortcut_ready.set()
                await restore_portable_shortcut(state)
                self.assertFalse(dictation.enabled)

    async def test_portable_language_and_voice_input_are_saved_and_used_for_each_take(self):
        from linux_dictation import LinuxDictation
        from portable_dictation_history import PortableDictationHistory
        from server import (change_portable_language, change_portable_shortcut_mode, change_portable_voice_input,
                            portable_dictation_status, transcribe_portable_dictation)

        class Dictation(LinuxDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        async def capture_fixture(path):
            with private_wav(path) as (wav, _):
                wav.writeframes(b"\x01\x02" * 1600)

        def recorder(_key, path, _mode, **_options):
            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, stop=AsyncMock(),
                                   start=lambda: capture_fixture(path))

        origin = "http://127.0.0.1:43721"
        endpoint = "/api/portable/dictation"
        headers = {"Origin": origin}
        record = {"port": 43721}
        app = FastAPI()
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/language", change_portable_language, methods=["POST"])
        app.add_api_route(endpoint + "/voice-input", change_portable_voice_input, methods=["POST"])
        app.add_api_route(endpoint + "/shortcut-mode", change_portable_shortcut_mode, methods=["POST"])
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder:
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder = recorder
            state.portable_recording = None
            state.draining = False
            state.status = "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
            transcript = {"text": "Bonjour, ceci est un essai.", "inferenceMs": 80}
            recognized = AsyncMock(return_value=transcript)
            cleanup = state.stupase_cleanup = Mock()
            cleanup.status.return_value = "downloading"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                with patch("server.URL", origin), patch("server.transcribe_dictation", recognized):
                    url = endpoint + "/language"
                    voice_url = endpoint + "/voice-input"
                    mode_url = endpoint + "/shortcut-mode"
                    boost = {"setting": "boostMicrophone", "enabled": True}
                    noise = {"setting": "noiseCleanup", "enabled": True}
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    defaults = (await browser.get(endpoint)).json()
                    self.assertEqual(defaults["shortcutMode"], "tap")
                    self.assertEqual((defaults["language"], defaults["noiseCleanup"], defaults["boostMicrophone"]),
                                     ("", False, False))
                    self.assertEqual((await browser.post(voice_url, json=noise)).status_code, 403)
                    self.assertEqual((await browser.post(voice_url, headers=headers,
                                                         json={**boost, "enabled": "false"})).status_code, 422)
                    self.assertEqual((await browser.post(url, json={"language": "fr"})).status_code, 403)
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": "never"})).status_code, 400)
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": "fr"})).json()["language"], "fr")
                    self.assertEqual((await browser.post(mode_url, headers=headers, json={"mode": "hold"})).json()["shortcutMode"], "hold")
                    dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                    self.assertEqual(dictation.language, "fr")
                    self.assertEqual(dictation.shortcut_mode, "hold")
                    await dictation.toggle()
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": "es"})).status_code, 409)
                    self.assertEqual((await browser.post(voice_url, headers=headers, json=boost)).status_code, 409)
                    self.assertEqual((await browser.post(mode_url, headers=headers, json={"mode": "tap"})).status_code, 409)
                    await dictation.toggle()
                    self.assertEqual(recognized.await_args.args[2], ("fr",))
                    self.assertTrue(recognized.await_args.kwargs["screen_recording"])
                    self.assertFalse(recognized.await_args.kwargs["boost"])
                    self.assertIsNone(recognized.await_args.kwargs.get("cleanup"))
                    self.assertEqual(history.page(legacy=True)["entries"][0]["text"], "Bonjour, ceci est un essai.")
                    self.assertEqual((await browser.post(url, headers=headers, json={"language": ""})).json()["language"], "")
                    await dictation.toggle()
                    await dictation.toggle()
                    self.assertEqual(recognized.await_args.args[2], ())
                    self.assertEqual(history.language(), "")
                    status = (await browser.post(voice_url, headers=headers, json=boost)).json()
                    self.assertTrue(status["boostMicrophone"])
                    self.assertFalse(status["noiseCleanup"])
                    cleanup.prepare.assert_not_called()
                    status = (await browser.post(voice_url, headers=headers, json=noise)).json()
                    self.assertTrue(status["boostMicrophone"])
                    self.assertTrue(status["noiseCleanup"])
                    self.assertEqual(status["cleanupStatus"], "downloading")
                    cleanup.prepare.assert_called_once_with(retry=True)
                    dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                    recognized.reset_mock()
                    await dictation.toggle()
                    await dictation.toggle()
                    recognized.assert_awaited_once()
                    self.assertIs(recognized.await_args.kwargs["cleanup"], cleanup)
                    self.assertTrue(recognized.await_args.kwargs["boost"])
                    dictation.deliver.assert_awaited_with(transcript["text"])
                    status = (await browser.post(voice_url, headers=headers, json={**noise, "enabled": False})).json()
                    self.assertFalse(status["noiseCleanup"])
                    self.assertTrue(status["boostMicrophone"])

    async def test_portable_dictation_applies_saved_vocabulary_and_replacements_to_paste_and_history(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import (change_portable_profiles, change_portable_replacements, change_portable_vocabulary,
                            describe_portable_profiles, portable_dictation_status, preview_portable_profiles,
                            transcribe_portable_dictation)

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        pcm = b"\x01\x02" * 1600

        def recorder(_key, path, _mode, **_options):
            async def start():
                with private_wav(path) as (wav, _):
                    wav.writeframes(pcm)
            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, stop=AsyncMock(), start=start)

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        endpoint = "/api/portable/dictation"
        app = FastAPI()
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/vocabulary", change_portable_vocabulary, methods=["POST"])
        app.add_api_route(endpoint + "/replacements", change_portable_replacements, methods=["POST"])
        app.add_api_route(endpoint + "/output-profiles", change_portable_profiles, methods=["POST"])
        app.add_api_route(endpoint + "/output-profiles/describe", describe_portable_profiles, methods=["POST"])
        app.add_api_route(endpoint + "/output-profiles/preview", preview_portable_profiles, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
            vocabulary = [{"term": "Jaro Notebook", "soundsLike": ["red notebook"]}]
            utterance = "The red notebook and a blue wheel. red notebooks."
            expected = "The Jaro Notebook and a Bluewheel. red notebooks."
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                request = {"base": [], "entries": vocabulary}
                self.assertEqual((await browser.post(endpoint + "/vocabulary", headers=headers, json=request)).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                async def set_rules(rules):
                    return await browser.post(endpoint + "/replacements", headers=headers, json={"text": rules})
                saved = await browser.post(endpoint + "/vocabulary", headers=headers, json=request)
                self.assertEqual(saved.raise_for_status().json()["vocabularyEntries"], vocabulary)
                self.assertEqual((await browser.post(endpoint + "/vocabulary", headers=headers, json=request)).status_code, 409)
                self.assertEqual((await set_rules("incorrect syntax")).status_code, 400)
                (await set_rules("blue => Azure\nblue wheel => Bluewheel\nBluewheel => Chained")).raise_for_status()
                dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                with patch("server.transcribe_dictation", new_callable=AsyncMock,
                           return_value={"text": utterance, "inferenceMs": 42}) as recognize:
                    await dictation.toggle()
                    (await set_rules("blue wheel => Changed during take")).raise_for_status()
                    await dictation.toggle()
                    self.assertEqual(recognize.await_args.args[3], ("Jaro Notebook",))
                    dictation.deliver.assert_awaited_with(expected)
                    saved = history.page(legacy=True, include_audio=True)["entries"][0]
                    self.assertEqual(saved["text"], expected)
                    with history.open_audio(saved["id"]) as (handle, _size), wave.open(handle, "rb") as clip:
                        self.assertEqual(clip.readframes(1600), pcm)

                (await set_rules("blue wheel => Bluewheel")).raise_for_status()
                async def set_profiles(rows, base=""):
                    profiles = json.dumps(rows)
                    return await browser.post(endpoint + "/output-profiles", headers=headers,
                                              json={"profiles": profiles, "base": base})

                profiles = [{"target": "default", "steps": ["lowercase"]},
                            {"target": "app:Example", "steps": ["rewrite", "lowercase"],
                             "vocabulary": ["Example project"], "corrections": "Bluewheel => Cog",
                             "instruction": "Remove filler while keeping all numbers."},
                            {"target": "site:*.example.net", "steps": ["no-trailing-punctuation"], "vocabulary": ["Website project"]}]
                self.assertEqual((await set_profiles([{"target": "default", "steps": ["script"]}])).status_code, 400)
                self.assertEqual((await set_profiles([{"target": "default", "steps": ["rewrite"]}])).status_code, 400)
                (await set_profiles(profiles)).raise_for_status()
                self.assertEqual((await set_profiles([])).status_code, 409)
                preview = {"profiles": json.dumps(profiles), "app": "example", "text": "The blue wheel, 12 times."}
                simulated = await browser.post(endpoint + "/output-profiles/preview", headers=headers, json=preview)
                self.assertEqual(simulated.raise_for_status().json(), {
                    "profile": "app:Example", "steps": ["rewrite", "lowercase"], "skipped": ["rewrite"],
                    "text": "the cog, 12 times.", "warnings": [], "vocabulary": ["Example project", "Jaro Notebook"],
                })
                dictation.platform = "wayland"
                simulated = await browser.post(endpoint + "/output-profiles/preview", headers=headers, json=preview)
                self.assertEqual(simulated.raise_for_status().json()["profile"], "app:Example")
                simulated = await browser.post(endpoint + "/output-profiles/preview", headers=headers,
                                               json={**preview, "site": "https://work.example.net/private"})
                self.assertEqual(simulated.raise_for_status().json()["profile"], "site:*.example.net")

                async def generate(*_args, **_kwargs):
                    yield json.dumps({"text": rewritten})

                model = SimpleNamespace(lock=asyncio.Lock(), ready=AsyncMock(), fit_prompt=AsyncMock(), generate=generate)
                state.insights = SimpleNamespace(chat_count=0, model=model)
                dictation = state.portable_dictation = Dictation(state, transcribe_portable_dictation, history)
                capture_text = "The blue wheel, 12 times."
                with patch("server.transcribe_dictation", new_callable=AsyncMock,
                           return_value={"text": capture_text, "inferenceMs": 42}) as recognize, \
                     patch.object(dictation, "output_context", new=AsyncMock(return_value=OutputContext("Example"))) as context:
                    for rewritten, delivered in (("Polished 12 times.", "polished 12 times."),
                                                ("Polished 13 times.", "the cog, 12 times.")):
                        dictation.application = "Example"
                        await dictation.toggle()
                        await dictation.toggle()
                        self.assertEqual(recognize.await_args.args[3], ("Example project", "Jaro Notebook"))
                        dictation.deliver.assert_awaited_with(delivered)
                        self.assertEqual(history.page(legacy=True)["entries"][0]["text"], delivered)
                    self.assertIn("changed a number", dictation.feedback)
                    self.assertEqual(state.insights.chat_count, 0)
                    self.assertEqual(model.ready.await_count, 2)
                    dictation.application = "Original"
                    await dictation.toggle()
                    context.return_value = OutputContext("Endpoint", "work.example.net")
                    await dictation.toggle()
                    dictation.deliver.assert_awaited_with("The Bluewheel, 12 times")
                    self.assertEqual(recognize.await_args.args[3], ("Website project", "Jaro Notebook"))
                    self.assertEqual(history.page(legacy=True)["entries"][0]["app"], "Endpoint")
                    context.return_value = OutputContext()
                    await dictation.toggle()
                    await dictation.toggle()
                    dictation.deliver.assert_awaited_with("the bluewheel, 12 times.")
                    self.assertEqual(model.ready.await_count, 2)

    @unittest.skipUnless(os.name == "posix", "uses an executable indicator fixture")
    async def test_dictation_indicator_observes_the_take_and_cannot_prevent_paste(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_indicator, portable_dictation_status

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Pasted.")

        def recorder(_key, path, _mode, **_options):
            worker = asyncio.get_running_loop().create_future()

            async def stop():
                if not worker.done():
                    worker.set_result(None)

            return SimpleNamespace(path=path, levels=(.60,), worker=worker, frames=1600, interrupted=False, stop=stop, start=AsyncMock())

        async def wait_for_line(path, line):
            async with asyncio.timeout(3):
                while not path.is_file() or line not in path.read_text().splitlines():
                    await asyncio.sleep(.02)

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        endpoint = "/api/portable/dictation"
        app = FastAPI()
        app.add_api_route(endpoint, portable_dictation_status, methods=["GET"])
        app.add_api_route(endpoint + "/indicator", change_portable_indicator, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            root = Path(folder)
            helper, activity = root / "indicator", root / "activity"
            helper.write_text("#!/usr/bin/env python3\n"
                              "from pathlib import Path\nimport sys\n"
                              f"with Path({str(activity)!r}).open('a', buffering=1) as log:\n"
                              "    print(sys.argv[1], file=log)\n"
                              "    for line in sys.stdin:\n"
                              "        print(line.strip(), file=log)\n"
                              "    print('closed', file=log)\n")
            helper.chmod(0o700)
            state = app.state
            state.library = SimpleNamespace(root=root)
            state.portable_recording_dir = root
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(root)
            history.initialize()
            history.configure(retention=0)
            processing = asyncio.Event()

            async def recognize(*_args, **_kwargs):
                await processing.wait()
                return {"text": "One take.", "inferenceMs": 42}

            with patch.dict(os.environ, {"JARO_DICTATION_INDICATOR": str(helper)}):
                dictation = state.portable_dictation = Dictation(state, recognize, history)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                async def position(value):
                    return await browser.post(endpoint + "/indicator", headers=headers, json={"position": value})

                self.assertEqual((await position("bottom")).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await position("cursor")).status_code, 422)
                self.assertTrue((await position("bottom")).raise_for_status().json()["indicatorAvailable"])
                await dictation.toggle()
                self.assertTrue(dictation.capture.measure_level)
                await wait_for_line(activity, "R60")
                self.assertEqual((await position("off")).status_code, 409)
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(dictation.toggle())
                    try:
                        await wait_for_line(activity, "P")
                    finally:
                        processing.set()
                self.assertEqual(activity.read_text().splitlines(), ["bottom", "R60", "P", "closed"])
                dictation.deliver.assert_awaited_once_with("One take.")
                self.assertEqual((await position("off")).raise_for_status().json()["indicatorPosition"], "off")
                await dictation.toggle()
                self.assertFalse(dictation.capture.measure_level)
                self.assertIsNone(dictation.indicator_task)
                await dictation.cancel_capture()
                (await position("top")).raise_for_status()
                helper.unlink()
                await dictation.toggle()
                await dictation.indicator_task
                self.assertFalse(dictation.snapshot()["indicatorAvailable"])
                await dictation.toggle()
                self.assertEqual(dictation.deliver.await_count, 2)

    async def test_dictation_sound_lifecycle_and_workspace_preferences(self):
        from portable_dictation import PASTE_SENT, PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from portable_dictation_sounds import SoundPreferences
        from server import change_portable_sound, preview_portable_sound

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value=PASTE_SENT)

        def recorder(_key, path, _mode, **_options):
            worker = asyncio.get_running_loop().create_future()

            async def stop():
                if not worker.done():
                    worker.set_result(None)

            return SimpleNamespace(path=path, worker=worker, frames=1600, interrupted=False, stop=stop, start=AsyncMock())

        events = []
        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        endpoint = "/api/portable/dictation/sound"
        app = FastAPI()
        app.add_api_route(endpoint, change_portable_sound, methods=["POST"])
        app.add_api_route(endpoint + "/preview", preview_portable_sound, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin), patch("portable_dictation.DONE_DELAY", 0):
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = state.library.root
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            history.configure(retention=0)
            async def transcribe(*_args, **_options):
                await asyncio.sleep(0)
                return {"text": "Hello.", "inferenceMs": 42}

            dictation = state.portable_dictation = Dictation(state, transcribe, history)
            self.assertEqual(dictation.sound, SoundPreferences())
            dictation.sounds.available = True

            async def play(cue, settings, delay, while_valid):
                if while_valid is None or while_valid():
                    events.append((cue, settings.volume, dictation.capture is None or dictation.capture.worker.done()))
                return True

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                with patch.object(dictation.sounds, "_play", side_effect=play):
                    async def change(values):
                        return await browser.post(endpoint, headers=headers, json=values)

                    self.assertEqual((await change({"style": "wood"})).status_code, 403)
                    await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                    self.assertEqual((await change({"style": "../other"})).status_code, 422)
                    self.assertEqual((await change({"volume": 101})).status_code, 422)
                    (await change({"style": "wood", "volume": 41})).raise_for_status()
                    self.assertEqual(history.sound_preferences(), SoundPreferences(True, "wood", 41))
                    (await browser.post(endpoint + "/preview", headers=headers)).raise_for_status()
                    await dictation.toggle()
                    await dictation.sounds.task
                    self.assertEqual((await browser.post(endpoint + "/preview", headers=headers)).status_code, 409)
                    self.assertEqual((await change({"enabled": False})).status_code, 409)
                    await dictation.toggle()
                    await dictation.sounds.task
                    self.assertEqual(events, [("start", 41, True), ("start", 41, False),
                                              ("stop", 41, True), ("done", 41, True)])
                    await dictation.toggle()
                    await dictation.sounds.task
                    await dictation.cancel_capture(audible=True)
                    await dictation.sounds.task
                    self.assertEqual(events[-1], ("cancel", 41, True))
                    previous = len(events)
                    (await change({"enabled": False})).raise_for_status()
                    await dictation.toggle()
                    await dictation.toggle()
                    self.assertEqual(len(events), previous)
                    self.assertEqual(dictation.deliver.await_count, 2)
                    (await change({"enabled": True})).raise_for_status()
                with patch.object(dictation.sounds, "_play", return_value=False):
                    await dictation.toggle()
                    self.assertFalse(await dictation.sounds.task)
                    await dictation.toggle()
                    await dictation.sounds.task
                    self.assertEqual(dictation.deliver.await_count, 3)

    async def test_windows_cues_stop_before_the_next_in_process_playback(self):
        from portable_dictation_sounds import DictationSounds, SoundPreferences

        with TemporaryDirectory() as folder:
            path = Path(folder) / "cue.wav"
            with wave.open(str(path), "wb") as clip:
                clip.setparams((1, 2, 16000, 0, "NONE", ""))
                clip.writeframes(struct.pack("<4h", 16000, -16000, 8000, -8000) * 800)
            player = DictationSounds()
            player.available = True
            player.pulse_player = None
            started = asyncio.Event()

            def play_sound(buffer, _module, _flags):
                if buffer is not None:
                    self.assertEqual(struct.unpack_from("<4h", buffer, 44), (8000, -8000, 4000, -4000))
                    started.set()
                return 1

            native = Mock(side_effect=play_sound)
            winmm = SimpleNamespace(PlaySoundW=native)
            winsound = SimpleNamespace(SND_ASYNC=1, SND_NODEFAULT=2, SND_MEMORY=4)
            with (patch("ctypes.windll", SimpleNamespace(winmm=winmm), create=True),
                  patch.dict(sys.modules, {"winsound": winsound}),
                  patch("portable_dictation_sounds.sound_path", return_value=path)):
                first = player.play("start", SoundPreferences(volume=50))
                await asyncio.wait_for(started.wait(), 1)
                started.clear()
                second = player.play("stop", SoundPreferences(volume=50))
                await asyncio.wait_for(started.wait(), 1)
                self.assertTrue(first.cancelled())
                self.assertTrue(await second)
                self.assertEqual([call.args[2] for call in native.call_args_list], [7, 0, 7, 0])

    async def test_cancel_discards_a_processing_take_and_never_interrupts_delivery(self):
        from portable_dictation import PortableDictation
        from portable_dictation_history import PortableDictationHistory
        from server import change_portable_dictation

        class Dictation(PortableDictation):
            enabled = True
            platform = "x11"
            deliver = AsyncMock(return_value="Paste shortcut sent.")

        microphone_open = asyncio.Event()
        inference = asyncio.Event()
        release_inference = asyncio.Event()
        delivery = asyncio.Event()
        release_delivery = asyncio.Event()

        def recorder(_key, path, _mode, **_options):
            async def start():
                await microphone_open.wait()

            return SimpleNamespace(path=path, worker=asyncio.Future(), frames=1600, interrupted=False, start=start, stop=AsyncMock())

        async def transcribe(*_args, **_kwargs):
            inference.set()
            await release_inference.wait()
            return {"text": "Keep this text.", "inferenceMs": 20}

        async def retain(*_args):
            delivery.set()
            await release_delivery.wait()
            return ""

        async def wait_until(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)

        origin = "http://127.0.0.1:43721"
        headers = {"Origin": origin}
        app = FastAPI()
        app.add_api_route("/api/portable/dictation/{action}", change_portable_dictation, methods=["POST"])
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        with TemporaryDirectory() as folder, patch("server.URL", origin):
            state = app.state
            state.library = SimpleNamespace(root=Path(folder))
            state.portable_recording_dir = Path(folder)
            state.portable_recording_lock = asyncio.Lock()
            state.portable_recorder, state.portable_recording = recorder, None
            state.draining, state.status = False, "ready"
            history = PortableDictationHistory(folder)
            history.initialize()
            dictation = state.portable_dictation = Dictation(state, transcribe, history)
            dictation.retain = AsyncMock(side_effect=retain)
            dictation.text = "Previous dictation."
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                async def cancel():
                    return await browser.post("/api/portable/dictation/cancel", headers=headers)

                self.assertEqual((await cancel()).status_code, 403)
                await browser.post(BOOTSTRAP, headers={**headers, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await cancel()).status_code, 409)
                dictation.schedule()
                await wait_until(lambda: dictation.phase == "starting")
                self.assertTrue(dictation.can_cancel)
                stopped = (await cancel()).raise_for_status().json()
                self.assertEqual(stopped["phase"], "idle")
                self.assertFalse(stopped["canCancel"])
                self.assertEqual(stopped["message"], "Dictation canceled.")
                self.assertIsNone(dictation.capture)
                self.assertFalse(list(Path(folder).glob(".dictation-*")))

                microphone_open.set()
                dictation.schedule()
                await dictation.action
                dictation.schedule()
                await asyncio.wait_for(inference.wait(), 3)
                self.assertEqual(dictation.phase, "transcribing")
                (await cancel()).raise_for_status()
                dictation.retain.assert_not_awaited()
                dictation.deliver.assert_not_awaited()
                self.assertEqual(dictation.text, "Previous dictation.")

                release_inference.set()
                dictation.schedule()
                await dictation.action
                dictation.schedule()
                try:
                    await asyncio.wait_for(delivery.wait(), 3)
                    self.assertEqual(dictation.phase, "delivering")
                    self.assertFalse(dictation.can_cancel)
                    self.assertEqual((await cancel()).status_code, 409)
                finally:
                    release_delivery.set()
                    await dictation.output_task
                dictation.retain.assert_awaited_once()
                dictation.deliver.assert_awaited_once_with("Keep this text.")

    async def test_computer_audio_uses_only_the_default_outputs_reported_monitor(self):
        sinks = '[{"name":"usb","monitor_source":"USB.output-monitor"},{"name":"hdmi","monitor_source":"hdmi.monitor"}]'
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["usb\n", sinks])) as pulse:
            self.assertEqual(await default_computer_audio(), "USB.output-monitor")
            pulse.assert_any_await("get-default-sink")
            pulse.assert_any_await("--format=json", "list", "sinks")
        with patch("linux_capture.pulse_info", new=AsyncMock(side_effect=["missing\n", sinks])):
            with self.assertRaisesRegex(CaptureError, "Select an audio output"):
                await default_computer_audio()

    def test_dual_capture_preserves_sources_and_averages_the_mix(self):
        mixed, mic, system, levels = split_audio(struct.pack("<6h", 32_000, 32_000, 100, -100, -20_000, 0))
        self.assertEqual(struct.unpack("<3h", mixed), (32_000, 0, -10_000))
        self.assertEqual(struct.unpack("<3h", mic), (32_000, 100, -20_000))
        self.assertEqual(struct.unpack("<3h", system), (32_000, -100, 0))
        self.assertEqual(levels, (1, 1))

    def test_recovery_trims_uncommitted_track_frames_and_keeps_valid_sources(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "capture.wav"
            tracks = source_tracks(path)
            for target, frames in ((path, 2), (tracks["mic"], 3), (tracks["system"], 1)):
                with private_wav(target) as (writer, _):
                    writer.writeframes(b"\x01\x00" * frames)
            self.assertEqual(draft_tracks(path, draft_frames(path)), {"mic": tracks["mic"]})
            with wave.open(str(tracks["mic"]), "rb") as mic:
                self.assertEqual(mic.getnframes(), 2)
                self.assertEqual(mic.readframes(3), b"\x01\x00" * 2)

    @unittest.skipUnless(os.name == "posix", "requires an executable script for the capture helper fixture")
    async def test_windows_adapter_drains_partial_audio_when_its_control_pipe_closes(self):
        with TemporaryDirectory() as folder:
            helper = Path(folder) / "capture-fixture"
            helper.write_text("""#!/usr/bin/env python3
import struct
import sys
assert sys.argv[1:] == ["both", "120"]
frame = struct.pack("<2h", 1200, -500)
sys.stdout.buffer.write(frame * 1600)
sys.stdout.buffer.flush()
assert sys.stdin.buffer.read() == b""
sys.stdout.buffer.write(frame * 31)
""")
            helper.chmod(0o700)
            path = Path(folder) / "capture.wav"
            capture = WindowsRecording(uuid4(), path, "both", seconds=120)
            with (patch("windows_capture.helper_path", return_value=helper),
                  patch("windows_capture.subprocess.CREATE_NO_WINDOW", 0, create=True)):
                await capture.start()
                try:
                    self.assertEqual(capture.frames, 1600)
                finally:
                    await capture.stop()
            self.assertEqual(capture.frames, 1631)
            for audio, sample in ((path, 350), (source_tracks(path)["mic"], 1200),
                                  (source_tracks(path)["system"], -500)):
                self.assertEqual(draft_frames(audio), 1631)
                with wave.open(str(audio), "rb") as stream:
                    self.assertEqual(stream.readframes(1631), struct.pack("<h", sample) * 1631)

    async def test_only_detected_meeting_passes_call_executables_to_windows_recorder(self):
        helper = Path("jaro-capture.exe")
        path = Path("capture.wav")
        with (patch("windows_capture.helper_path", return_value=helper),
              patch("windows_capture.subprocess.CREATE_NO_WINDOW", 0, create=True),
              patch("windows_capture.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn):
            await WindowsMeetingRecording(uuid4(), path, "both", seconds=120).spawn()
            await WindowsMeetingRecording(uuid4(), path, "both", seconds=120,
                                          call_binaries=("chrome", "google-chrome")).spawn()
        self.assertEqual([call.args for call in spawn.await_args_list], [
            (helper, "meeting", "120"),
            (helper, "meeting", "120", "call", "chrome.exe", "google-chrome.exe"),
        ])

    async def test_windows_screen_audio_passes_clock_and_exact_microphone(self):
        helper = Path("jaro-capture.exe")
        with (patch("windows_capture.helper_path", return_value=helper),
              patch("windows_capture.subprocess.CREATE_NO_WINDOW", 0, create=True),
              patch("windows_capture.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn):
            for mode, selected in (("mic", ""), ("mic", "USB microphone"), ("both", "USB microphone"), ("system", "")):
                audio = WindowsScreenAudio(uuid4(), Path("narration.wav"), mode, seconds=120, microphone=selected)
                await audio.spawn()
        self.assertEqual([call.args for call in spawn.await_args_list], [
            (helper, "mic", "120", "clock"),
            (helper, "mic", "120", "clock", "device", "USB microphone"),
            (helper, "both", "120", "clock", "device", "USB microphone"),
            (helper, "system", "120", "clock"),
        ])

    async def test_windows_screen_audio_keeps_playback_through_microphone_reconnect(self):
        from windows_screencast import WindowsScreenRecording

        with TemporaryDirectory() as folder:
            take = WindowsScreenRecording(Path(folder), "display", "1a:0", True, True)
            take.draft.create()
            take.phase = "recording"
            audio = take.audio = WindowsScreenAudio(uuid4(), take.draft.narration, "both")
            pcm, updates = asyncio.StreamReader(), asyncio.StreamReader()
            audio.process = SimpleNamespace(stdout=pcm, stderr=updates)
            worker = asyncio.create_task(audio.collect())
            try:
                pcm.feed_data(struct.pack("<q", 100_000) + struct.pack("<hh", 500, 1000) * 1600)
                await asyncio.wait_for(audio.ready.wait(), 2)
                self.assertEqual(audio.started_ticks, 100_000)
                self.assertIsNone(take.status()["error"])

                updates.feed_data(b"jaro-screen-audio mic unavailable\r\n")
                pcm.feed_data(struct.pack("<hh", 0, 1000) * 1600)
                await asyncio.sleep(0)
                status = take.status()
                self.assertEqual(status["phase"], "recording")
                self.assertIn("Microphone unavailable", status["error"])
                self.assertEqual(status["levels"]["mic"], 0)
                self.assertGreater(status["levels"]["system"], 0)

                updates.feed_data(b"jaro-screen-audio mic ready\r\n")
                pcm.feed_data(struct.pack("<hh", 500, 1000) * 1600)
                await asyncio.sleep(0)
                self.assertIsNone(take.status()["error"])
            finally:
                pcm.feed_eof()
                updates.feed_eof()
                await asyncio.wait_for(worker, 2)

            tracks = source_tracks(audio.path)
            with wave.open(str(tracks["system"]), "rb") as stream:
                self.assertEqual(stream.readframes(4800), struct.pack("<h", 1000) * 4800)
            with wave.open(str(tracks["mic"]), "rb") as stream:
                self.assertEqual(stream.readframes(4800), struct.pack("<h", 500) * 1600 + b"\0" * 3200 + struct.pack("<h", 500) * 1600)

    async def test_owner_key_unlocks_only_its_loopback_workspace(self):
        origin = "http://127.0.0.1:43721"
        app = FastAPI()

        @app.get("/api/sessions")
        def sessions():
            return ["Private recording"]

        record = {"port": 43721}
        configure_workspace(app, record, origin)
        auth = {"X-Jaro-Workspace-Key": record["workspaceKey"], "Origin": origin}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as webview:
            self.assertEqual((await webview.get("/api/sessions")).status_code, 403)
            page = await webview.get(BOOTSTRAP)
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(record["workspaceKey"], page.text)
            self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
            self.assertEqual((await webview.post(BOOTSTRAP, headers={
                **auth, "Origin": "http://127.0.0.1:43722",
            })).status_code, 403)
            self.assertEqual((await webview.post(BOOTSTRAP, headers={
                **auth, "X-Jaro-Workspace-Key": "a" * 64,
            })).status_code, 403)
            connected = await webview.post(BOOTSTRAP, headers=auth)
            self.assertEqual(connected.status_code, 204)
            self.assertIn("HttpOnly", connected.headers["set-cookie"])
            self.assertIn("SameSite=strict", connected.headers["set-cookie"])
            self.assertEqual((await webview.get("/api/sessions")).json(), ["Private recording"])
            self.assertEqual((await webview.get("/api/sessions", headers={
                "Origin": "http://127.0.0.1:43722",
            })).status_code, 403)
            self.assertEqual((await webview.get("/api/sessions", headers={
                "Host": "alternate.localhost:43721",
            })).status_code, 403)
            self.assertEqual((await webview.get("/api/sessions", headers={
                "Cookie": "jaro-workspace-43722=" + record["workspaceKey"],
            })).status_code, 403)

    async def test_annotation_export_requires_the_desktop_and_rejects_overlapping_saves(self):
        from server import export_portable_annotation

        origin = "http://127.0.0.1:43721"
        app = FastAPI()
        app.post("/export/{key}")(export_portable_annotation)
        record = {"port": 43721}
        configure_workspace(app, record, origin)
        app.state.draining = False
        app.state.jobs = set()
        app.state.library = object()
        key = str(uuid4())
        route = "/export/" + key
        entered, finish = asyncio.Event(), asyncio.Event()

        async def save(*_):
            entered.set()
            await finish.wait()
            return {"saved": True}

        auth = {"Origin": origin}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            with (patch("server.URL", origin), patch("desktop_file_dialog.file_dialog", return_value=Path("jaro-save-video")),
                  patch("desktop_file_dialog.choose_file", new_callable=AsyncMock) as choose,
                  patch("annotation_package.write_package", return_value={"saved": True}) as package,
                  patch("portable_video_export.save_annotation_video", new_callable=AsyncMock, side_effect=save) as export):
                await client.post(BOOTSTRAP, headers={**auth, "X-Jaro-Workspace-Key": record["workspaceKey"]})
                self.assertEqual((await client.post(route, headers=auth, json={})).status_code, 403)
                await client.post(BOOTSTRAP, headers={**auth, "X-Jaro-Workspace-Key": record["workspaceDesktopKey"]})
                self.assertEqual((await client.post(route, json={})).status_code, 403)
                self.assertEqual((await client.post(route, headers=auth,
                    json={"kind": "package", "span": {"startMs": 0, "endMs": 800}})).status_code, 422)
                for span in ({"startMs": True, "endMs": 800}, {"startMs": 350, "endMs": 800}):
                    self.assertEqual((await client.post(route, headers=auth, json={"span": span})).status_code, 422)
                export.assert_not_called()

                pending = asyncio.create_task(client.post(route, headers=auth, json={"span": {"startMs": 100, "endMs": 800}}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.assertEqual((await client.post(route, headers=auth, json={"kind": "package"})).status_code, 409)
                    self.assertTrue(app.state.jobs)
                finally:
                    finish.set()
                self.assertEqual((await pending).json(), {"saved": True})
                export.assert_awaited_once_with(app.state.library, Path("jaro-save-video"), key, (100, 800))
                choose.return_value = None
                self.assertEqual((await client.post(route, headers=auth, json={"kind": "package"})).json(), {"saved": False})
                package.assert_not_called()
                choose.return_value = Path("/export-folder")
                self.assertEqual((await client.post(route, headers=auth, json={"kind": "package"})).json(), {"saved": True})
                choose.assert_awaited_with(Path("jaro-save-video"), "--choose-annotation-export-folder")
                package.assert_called_once_with(app.state.library, key, Path("/export-folder"))

    async def test_stupase_worker_passes_silent_chunks_and_guards_stale_retire(self):
        from dictation_input import StuPASECleanup
        from stupase_runner import HEADER

        with TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup = StuPASECleanup(root)
            worker_script = (
                "import struct, sys\n"
                "h = struct.Struct('<I')\n"
                "sys.stdout.buffer.write(h.pack(0)); sys.stdout.buffer.flush()\n"
                "while hdr := sys.stdin.buffer.read(4):\n"
                "    n, = h.unpack(hdr)\n"
                "    data = sys.stdin.buffer.read(n)\n"
                "    sys.stdout.buffer.write(h.pack(n) + data); sys.stdout.buffer.flush()\n"
            )
            fake_runner = root / "fake_runner.py"
            fake_runner.write_text(worker_script, encoding="utf-8")
            pcm = np.zeros(160, dtype="<f4").tobytes()
            with patch("dictation_input.checkpoints_ready", return_value=True), patch("dictation_input.RUNNER", fake_runner):
                try:
                    self.assertEqual(await cleanup.clean(pcm), pcm)
                    first_pid = cleanup.worker.pid
                    stale_gen = cleanup.worker_generation - 1
                    await cleanup.retire_worker(stale_gen)
                    self.assertIsNotNone(cleanup.worker)
                    self.assertEqual(cleanup.worker.pid, first_pid)
                finally:
                    await cleanup.close()

    async def test_stupase_startup_failure_waits_for_verified_repair(self):
        import hashlib

        from dictation_input import CleanupFailed, StuPASECleanup
        import stupase_runner

        with TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup = StuPASECleanup(root)
            cleanup.root.mkdir(parents=True)
            checkpoint = cleanup.root / "weights.pt"
            checkpoint.write_bytes(b"wrong")
            marker = cleanup.root / ".verified"
            marker.write_text(stupase_runner.REVISION)
            # Repair can promote a previously downloaded, verified replacement.
            checkpoint.with_suffix(".pt.partial").write_bytes(b"right")
            expected = ("weights.pt", 5, hashlib.sha256(b"right").hexdigest())
            launches = root / "launches"
            worker = root / "worker.py"
            worker.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "import struct\n"
                "action, folder = sys.argv[1:]\n"
                "folder = Path(folder)\n"
                "if action == 'download':\n"
                f"    sys.path.insert(0, {str(Path(stupase_runner.__file__).parent)!r})\n"
                "    import stupase_runner\n"
                f"    stupase_runner.CHECKPOINTS = ({expected!r},)\n"
                "    stupase_runner.download(folder)\n"
                "    sys.exit(0)\n"
                f"with Path({str(launches)!r}).open('a') as log: log.write('start\\n')\n"
                "if (folder / 'weights.pt').read_bytes() != b'right': sys.exit(1)\n"
                "sys.stdout.buffer.write(bytes(4)); sys.stdout.buffer.flush()\n"
                "while header := sys.stdin.buffer.read(4):\n"
                "    data = sys.stdin.buffer.read(struct.unpack('<I', header)[0])\n"
                "    sys.stdout.buffer.write(header + data); sys.stdout.buffer.flush()\n",
                encoding="utf8",
            )
            pcm = bytes(4)
            with patch("stupase_runner.CHECKPOINTS", (expected,)), patch("dictation_input.RUNNER", worker):
                try:
                    self.assertEqual(cleanup.status(), "ready")
                    failures = await asyncio.gather(cleanup.clean(pcm), cleanup.clean(pcm), return_exceptions=True)
                    self.assertTrue(all(isinstance(error, CleanupFailed) for error in failures))
                    self.assertEqual(launches.read_text().splitlines(), ["start"])
                    self.assertEqual(cleanup.prepare(), "error")
                    self.assertFalse(marker.exists())
                    self.assertEqual(checkpoint.read_bytes(), b"wrong")
                    self.assertEqual(cleanup.prepare(retry=True), "downloading")
                    await cleanup.downloading
                    self.assertEqual(cleanup.status(), "ready")
                    self.assertEqual(checkpoint.read_bytes(), b"right")
                    self.assertEqual(await cleanup.clean(pcm), pcm)
                    self.assertEqual(launches.read_text().splitlines(), ["start", "start"])
                finally:
                    await cleanup.close()

    async def test_linux_meeting_starts_with_silent_playback_monitor(self):
        from linux_meeting_audio import LinuxMeetingRecording, Source

        with TemporaryDirectory() as directory:
            recording = LinuxMeetingRecording(uuid4(), Path(directory) / "meeting.wav", "both", seconds=10)
            recording.epoch = asyncio.get_running_loop().time()

            class FakeSource:
                def __init__(self, name, *, received=False, optional=False):
                    self.name = name
                    self.optional = optional
                    self.received = received
                    self.task = asyncio.create_task(asyncio.sleep(10))

                def add(self, target, _start):
                    if not self.optional:
                        target[:] = 100

            async def watch_routes():
                recording.mic = FakeSource("mic", received=True, optional=False)
                recording.outputs["monitor"] = FakeSource("monitor", received=False, optional=True)
                await asyncio.sleep(10)

            recording.watch_routes = watch_routes
            blocks = recording.capture_blocks(640, 4)
            try:
                block = await asyncio.wait_for(anext(blocks), 1)
                self.assertEqual(len(block), 640)
            finally:
                recording.end_frame = 0
                await blocks.aclose()

    async def test_linux_ordinary_dual_recording_keeps_microphone_while_playback_is_silent(self):
        from linux_capture import LinuxRecording
        from linux_meeting_audio import LinuxDualRecording, Source, linux_recording

        with TemporaryDirectory() as directory:
            path = Path(directory) / "ordinary.wav"
            capture = linux_recording(uuid4(), path, "both", seconds=10)
            self.assertIsInstance(capture, LinuxDualRecording)
            self.assertIsInstance(linux_recording(uuid4(), path, "mic"), LinuxRecording)
            playback = asyncio.Event()

            async def read(source):
                if source.name == "default-monitor":
                    await playback.wait()
                tone = 6000 if source.name == "microphone" else 12000
                data = struct.pack("<h", tone) * (SAMPLE_RATE // 50)
                while True:
                    source.push(data)
                    await asyncio.sleep(.02)

            with (patch.object(Source, "read", read),
                  patch("linux_meeting_audio.default_microphone", AsyncMock(return_value="microphone")),
                  patch("linux_meeting_audio.default_computer_audio", AsyncMock(return_value="default-monitor")),
                  patch("linux_meeting_audio.read_monitors", AsyncMock()) as enumerate_monitors):
                try:
                    await asyncio.wait_for(capture.start(), 2)
                    self.assertFalse(capture.meeting)
                    self.assertGreater(capture.levels[0], 0)
                    self.assertEqual(capture.levels[1], 0)
                    playback.set()
                    async with asyncio.timeout(2):
                        while capture.levels[1] == 0:
                            await asyncio.sleep(.02)
                    enumerate_monitors.assert_not_awaited()
                finally:
                    await capture.stop()

            originals = draft_tracks(path, capture.frames)
            self.assertEqual(set(originals), {"mic", "system"})
            arrays = []
            for wav_path in (path, originals["mic"], originals["system"]):
                with wave.open(str(wav_path), "rb") as wav:
                    self.assertEqual(wav.getnframes(), capture.frames)
                    arrays.append(np.frombuffer(wav.readframes(capture.frames), dtype="<i2"))
            mix, mic, system = arrays
            self.assertTrue(np.any((mic > 0) & (system == 0)))
            self.assertTrue(np.any(system > 0))
            np.testing.assert_array_equal(mix, (mic.astype(np.int32) + system) // 2)

    def test_tray_quit_focuses_workspace_while_busy_and_windows_encoder_falls_back(self):
        from engine_lifecycle import ClientLeases
        from portable_tray import MeetingTray
        from screen_draft import windows_h264_encoder

        with TemporaryDirectory() as directory:
            tray_dir = Path(directory)
            shutdown = Mock()
            state = SimpleNamespace(request_shutdown=shutdown, leases=ClientLeases())
            busy_flag = True
            tray = MeetingTray(state, tray_dir, lambda: busy_flag, lambda: None, lambda _: "")
            tray.submit("quit")
            shutdown.assert_not_called()
            self.assertTrue((tray_dir / "open").is_file())
            busy_flag = False
            tray.submit("quit")
            shutdown.assert_called_once()

        with patch("screen_draft.subprocess.check_output", return_value=" V..... libopenh264 Cisco OpenH264\n"):
            self.assertEqual(windows_h264_encoder(), "libopenh264")
            self.assertEqual(windows_h264_encoder(prefer_software=True), "libopenh264")
