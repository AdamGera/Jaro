"""A transcription-only process whose lifetime is owned by its parent."""
import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import hmac
from http.client import HTTPConnection, HTTPException as HTTPProtocolError
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Event, Thread
from urllib.parse import urlencode

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from sdk_discovery import SDK_VERSION, sdk_identity, sdk_proof

SDK_PATH = "/api/sdk/v1"
logger = logging.getLogger(__name__)


@contextmanager
def embedded_installation_lock(payload):
    """Keep the installed runtime and decoders available for the owned helper."""
    if not (payload / "portable-release.json").is_file():
        yield
    elif sys.platform == "win32":
        from portable_update import windows_installation_lock

        with windows_installation_lock(exclusive=False):
            yield
    elif sys.platform == "linux":
        from linux_direct import installation_lock, managed_root

        if (lock_path := payload / "in-use").is_file():
            import fcntl

            with lock_path.open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if Path("/run/jaro-package-changing").is_dir():
                    raise RuntimeError("Jaro is being installed or removed. Try again when it finishes.")
                yield
        elif root := managed_root(payload):
            with installation_lock(root):
                if managed_root(payload) != root:
                    raise RuntimeError("Import the SDK from the current Jaro installation.")
                yield
        else:
            raise RuntimeError("Import the SDK from the current Jaro installation.")
    else:
        yield


async def prepare_embedded_speech(state, source, repair):
    from model_installation import install_models

    state.startup_event({"type": "progress", "phase": "checking"})

    def progress(completed, total):
        state.startup_event({"type": "progress", "phase": "installing", "completedBytes": completed, "totalBytes": total})

    install = asyncio.create_task(install_models(progress, repair=repair, source=source))
    try:
        while not install.done():
            await asyncio.wait((install,), timeout=.1)
            if not install.done() and state.is_shutting_down():
                raise asyncio.CancelledError
        await install
    finally:
        if not install.done():
            install.cancel()
            with suppress(asyncio.CancelledError):
                await install


def run_embedded(router, inference_factory, model_factory, *, prepare=False, source=None, repair=False):
    from saved_dictations import shared_vocabulary

    def build_app(root, record):
        @asynccontextmanager
        async def lifespan(app):
            if prepare:
                try:
                    await prepare_embedded_speech(app.state, source, repair)
                except (OSError, ValueError, RuntimeError, TypeError, KeyError):
                    logger.exception("Could not prepare speech for the embedded SDK.")
                    app.state.startup_event({"type": "error", "code": "model-prepare-failed"})
                    raise
                app.state.startup_event({"type": "progress", "phase": "loading"})
            app.state.embedded_vocabulary = await asyncio.to_thread(shared_vocabulary)
            try:
                app.state.model = await asyncio.to_thread(model_factory)
            except Exception:
                if prepare:
                    app.state.startup_event({"type": "error", "code": "model-load-failed"})
                raise
            app.state.status = "ready"
            yield

        app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(router)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
        app.state.sdk = record
        app.state.sdk_embedded = True
        app.state.sdk_upload_root = root
        app.state.inference = inference_factory()
        app.state.sdk_pending = 0
        app.state.draining = False
        return app

    return run_owned_service(build_app)


def run_owned_service(build_app, *, allow_audio_restore=False):
    import uvicorn

    if os.isatty(0) or os.isatty(1):
        raise RuntimeError("Start the embedded Jard helper with piped stdin and stdout.")
    if os.name == "nt":
        from windows_job import own_process_tree

        own_process_tree(allow_audio_restore=allow_audio_restore)
    elif os.getpgrp() != os.getpid():
        os.setsid()
    finished = Event()
    try:
        with (tempfile.TemporaryDirectory(prefix="jard-embedded-") as scratch,
              socket.socket() as listener,
              os.fdopen(os.dup(1), "w") as handshake):
            # Preserve stdout for the ready record even if a model prints during startup.
            os.dup2(2, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            root = Path(scratch)
            # Keep model-generated cache files with the owned uploads so exit cleans up both.
            os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(root / "torch")
            record = sdk_identity(root, port)

            app = build_app(root, record)
            server = uvicorn.Server(uvicorn.Config(app, access_log=False, ws="none", timeout_graceful_shutdown=3))
            app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
            app.state.is_shutting_down = lambda: server.should_exit

            def startup_event(event):
                print(json.dumps(event), file=handshake, flush=True)

            app.state.startup_event = startup_event

            def watch_parent():
                try:
                    while os.read(0, 4096):
                        pass
                except OSError:
                    pass
                server.should_exit = True
                if not finished.wait(5):
                    shutil.rmtree(root, ignore_errors=True)
                    if os.name == "nt":
                        os._exit(1)  # Windows closes the job handle and ends the decoder processes.
                    os.killpg(os.getpgrp(), signal.SIGKILL)

            def report_ready():
                while not finished.wait(.02):
                    if server.should_exit:
                        return
                    if server.started:
                        try:
                            startup_event({"type": "ready", "url": f"http://127.0.0.1:{port}", **record})
                        except OSError:
                            server.should_exit = True
                        return

            Thread(target=watch_parent, daemon=True).start()
            Thread(target=report_ready, daemon=True).start()
            server.run(sockets=[listener])
            return 0 if server.started else 1
    finally:
        finished.set()


def read_ready(process, timeout):
    output = Queue(maxsize=1)

    def read():
        try:
            data = bytearray()
            while b"\n" not in data and len(data) < 4097:
                block = process.stdout.read1(4097 - len(data))
                if not block:
                    break
                data.extend(block)
            output.put(data)
        except OSError as error:
            output.put(error)

    # Windows selectors can only wait on sockets. The owner closes or kills
    # the child on timeout, which also ends this pipe read.
    Thread(target=read, daemon=True).start()
    try:
        data = output.get(timeout=timeout)
    except Empty:
        raise RuntimeError("The embedded Jard helper did not become ready.") from None
    if isinstance(data, OSError) or (len(data) <= 4096 and b"\n" not in data):
        raise RuntimeError("The embedded Jard helper stopped before it was ready. Check that Jard’s model is installed.")
    try:
        if len(data) > 4096:
            raise ValueError("oversized handshake")
        line, _, rest = data.partition(b"\n")
        record = json.loads(line)
        port = record["port"]
        if (record["type"] != "ready" or record["pid"] != process.pid or record["apiVersion"] != SDK_VERSION
                or type(port) is not int or not 1 <= port <= 65535 or record["url"] != f"http://127.0.0.1:{port}"
                or not re.fullmatch(r"[0-9a-f]{64}", record["token"]) or not re.fullmatch(r"[0-9a-f]{64}", record["libraryId"])
                or rest.strip()):
            raise ValueError("unexpected helper")
        return record
    except (TypeError, ValueError, KeyError) as error:
        raise RuntimeError("Jard returned an invalid embedded startup record.") from error


@contextmanager
def owned_transcriber():
    helper = os.environ.get("JARD_EMBEDDED_HELPER")
    command = ([helper, "service", "--embedded"] if helper else
               [sys.executable, str(Path(__file__).with_name("server.py")), "--embedded"])
    with owned_service(command) as (record, _):
        yield record


@contextmanager
def owned_service(command, *, stderr=subprocess.DEVNULL, env=None):
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                          start_new_session=os.name != "nt", env=env,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
        try:
            yield read_ready(process, 60), process
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5.5)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("The owned Jard helper did not exit.") from error


def verify_service(connection, record, *, identify_path=SDK_PATH + "/identify"):
    challenge = secrets.token_hex(16)
    connection.request("GET", identify_path + "?" + urlencode({"challenge": challenge}))
    identity = connection.getresponse()
    received = json.loads(identity.read(4096)).get("proof")
    if (identity.status != 200 or not isinstance(received, str)
            or not received.isascii() or not hmac.compare_digest(received, sdk_proof(record, challenge))
            or connection.sock is None):
        raise RuntimeError("Could not verify the local Jaro transcription service.")
    # Send credentials and audio on the socket that proved its identity.
    connection.auto_open = False


def sdk_service_status(record):
    """Return the verified service's health, or None if it stopped listening."""
    connection = HTTPConnection("127.0.0.1", record["port"], timeout=3)
    try:
        try:
            connection.connect()
        except ConnectionRefusedError:
            return None
        verify_service(connection, record)
        connection.request("GET", SDK_PATH + "/health", headers={"Authorization": "Bearer " + record["token"]})
        response = connection.getresponse()
        status = json.loads(response.read(4096)).get("status")
        if response.status != 200 or status not in ("loading", "ready", "error", "stopping"):
            raise ValueError("Invalid service health.")
        return status
    except (OSError, HTTPProtocolError, ValueError, AttributeError) as error:
        raise RuntimeError("Could not check the local Jaro transcription service.") from error
    finally:
        connection.close()


def sdk_transcription(record, audio, languages):
    connection = HTTPConnection("127.0.0.1", record["port"], timeout=3)
    try:
        verify_service(connection, record)
        connection.sock.settimeout(180)
        query = urlencode({"language": languages, "speech_screen": "true"}, doseq=True)
        route = SDK_PATH + "/transcriptions?" + query
        connection.request("POST", route, body=audio, headers={
            "Authorization": "Bearer " + record["token"], "Content-Type": "audio/wav",
        })
        response = connection.getresponse()
        result = json.loads(response.read(8 * 1024 * 1024))
        if response.status != 200:
            detail = result.get("detail", "Local transcription failed.")
            raise RuntimeError(detail.get("message", "Local transcription failed.") if isinstance(detail, dict) else detail)
        text = result["transcript"]
        if not isinstance(text, str):
            raise ValueError("Invalid transcript.")
        return text
    except (HTTPProtocolError, ValueError, AttributeError, KeyError) as error:
        raise RuntimeError("The local Jaro transcription service returned an invalid response.") from error
    finally:
        connection.close()
