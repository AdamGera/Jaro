import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException
from starlette.datastructures import State
from starlette.requests import ClientDisconnect

from sdk_discovery import SDK_DISCOVERY, SDK_MAX_REQUESTS, publish_sdk, read_sdk_record, remove_sdk, sdk_identity, sdk_proof
from sdk_embedded import read_ready, sdk_transcription
from server import app, quiesce_update


class EmbeddedStartupTests(unittest.TestCase):
    def test_reads_fragmented_handshake_and_checks_the_owned_pid(self):
        script = """
import json, os, sys, time
port = 12345
record = json.dumps(dict(type="ready", url=f"http://127.0.0.1:{port}", port=port, apiVersion=1,
                         pid=os.getpid() + int(sys.argv[1]), token="a" * 64, libraryId="b" * 64))
print(record[:40], end="", flush=True)
time.sleep(0.02)
print(record[40:], flush=True)
"""
        for pid_offset in (0, 1):
            with subprocess.Popen([sys.executable, "-c", script, str(pid_offset)], stdout=subprocess.PIPE) as child:
                if pid_offset == 0:
                    self.assertEqual(read_ready(child, 5)["pid"], child.pid)
                else:
                    with self.assertRaisesRegex(RuntimeError, "invalid embedded startup record"):
                        read_ready(child, 5)

    def test_readiness_wait_is_bounded_on_a_child_with_an_open_pipe(self):
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            try:
                with self.assertRaisesRegex(RuntimeError, "did not become ready"):
                    read_ready(child, 0.05)
            finally:
                child.stdin.close()


class SDKDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name).resolve()

    def test_generated_identity_is_private_and_cleanup_checks_ownership(self):
        first = publish_sdk(self.root, 8765)
        last = publish_sdk(self.root, 8765)
        self.assertNotEqual(first["token"], last["token"])
        path = self.root / SDK_DISCOVERY
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertEqual(read_sdk_record(self.root, 8765), last)
        with self.assertRaises(RuntimeError):
            read_sdk_record(self.root, 9000)
        path.chmod(0o644)
        with self.assertRaises(RuntimeError):
            read_sdk_record(self.root, 8765)
        path.chmod(0o600)
        remove_sdk(self.root, first)
        self.assertTrue(path.exists())
        remove_sdk(self.root, last)
        self.assertFalse(path.exists())

    async def test_offline_cli_does_not_send_audio_after_proof_socket_closes(self):
        requests = []

        async def identify(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                requests.append(headers)
                _, target, _ = headers.splitlines()[0].decode().split(" ")
                challenge = parse_qs(urlsplit(target).query).get("challenge", [""])[0]
                body = json.dumps({"proof": sdk_proof(record, challenge)}).encode()
                writer.write(f"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(identify, "127.0.0.1", 0) as listener:
            record = sdk_identity(self.root, listener.sockets[0].getsockname()[1])
            with self.assertRaisesRegex(RuntimeError, "Could not verify the local Jaro transcription service"):
                await asyncio.to_thread(sdk_transcription, record, b"private audio", ["en"])
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].startswith(b"GET /api/sdk/v1/identify?"))
        self.assertNotIn(b"authorization:", requests[0].lower())

    async def test_sdk_authentication_and_busy_rejection_before_reading_audio(self):
        record = publish_sdk(self.root, 8765)
        original = app.state
        app.state = State({"sdk": record, "status": "ready", "draining": False, "sdk_pending": SDK_MAX_REQUESTS,
                           "native_token": "a separate staging token"})
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                base = "/api/sdk/v1"
                challenge = "a" * 32
                proof = await client.get(base + "/identify", params={"challenge": challenge})
                self.assertEqual(proof.json(), {"proof": sdk_proof(record, challenge)})
                self.assertEqual((await client.get(base + "/models")).status_code, 401)
                self.assertEqual((await client.get(base + "/models", headers={
                    "Authorization": "Bearer " + app.state.native_token})).status_code, 401)
                headers = {"Authorization": "Bearer " + record["token"], "Content-Type": "application/octet-stream"}
                self.assertEqual((await client.get(base + "/health", headers={**headers, "Origin": "https://example.com"})).status_code, 403)
                self.assertEqual((await client.get(base + "/health", headers=headers)).json()["status"], "ready")
                self.assertEqual((await client.get(base + "/models", headers=headers)).json()["models"][0]["id"], "Reson8/Rumble-2-Flash")
                self.assertEqual((await client.post("/api/native/staging", headers={
                    "X-Jard-Token": record["token"]})).status_code, 403)

                async def body():
                    self.fail("Busy requests must be rejected before reading the upload")
                    yield b""

                response = await client.post(base + "/transcriptions", content=body(), headers=headers)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.json()["detail"]["code"], "queue-full")
        finally:
            app.state = original

    async def test_restart_waits_for_an_admitted_sdk_upload(self):
        state = SimpleNamespace(jobs=set(), native_token="fixture", draining=False, submitting=set(), status="ready",
                                sdk_pending=1, insights=SimpleNamespace(active_job=False, chat_count=0,
                                                                        model=SimpleNamespace(audio_users=0)),
                                leases=SimpleNamespace(streams=set(), deadlines={}), request_shutdown=Mock())
        request = SimpleNamespace(app=SimpleNamespace(state=state), headers={"x-jard-token": "fixture"})
        with self.assertRaises(HTTPException) as busy:
            await quiesce_update(request, uuid4())
        self.assertEqual(busy.exception.status_code, 409)
        self.assertFalse(state.draining)

    async def test_disconnected_sdk_request_releases_admission_and_upload(self):
        record = publish_sdk(self.root, 8765)
        original = app.state
        app.state = State({"sdk": record, "status": "ready", "draining": False, "sdk_pending": 0,
                           "library": SimpleNamespace(root=self.root)})
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                with patch("server.dictate_audio", side_effect=ClientDisconnect):
                    response = await client.post("/api/sdk/v1/transcriptions", content=b"temporary fixture", headers={
                        "Authorization": "Bearer " + record["token"], "Content-Type": "application/octet-stream",
                    })
                self.assertEqual(response.status_code, 499)
                self.assertEqual(app.state.sdk_pending, 0)
                self.assertFalse(list(self.root.glob(".upload-*")))
        finally:
            app.state = original
