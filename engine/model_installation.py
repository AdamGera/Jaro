"""Prepare the pinned speech model through the desktop's verified installer."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys

from engine_paths import WORKSPACE_ROOT
from rumble_model import MODEL_DIR


class ModelUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Rumble2 Flash is unavailable. Run `jardctl model install` or open Jaro to set up the model.")


def model_command(action, *, all_models=False):
    helper = os.environ.get("JARD_EMBEDDED_HELPER")
    if helper:
        return [helper, action, *([] if all_models else ["--speech-only"])]
    command = [sys.executable, str(WORKSPACE_ROOT / "desktop/build-tools/model_setup.py"), action]
    if all_models:
        if sys.platform not in ("linux", "win32") or platform.machine().lower() not in ("x86_64", "amd64"):
            raise RuntimeError("Use the installed macOS app or an x64 Linux or Windows workspace to set up the full local model set.")
        return [*command, "--portable"]
    return [*command, "--speech-only"]


def model_environment():
    return {**os.environ, "JARD_MODEL_DIR": str(MODEL_DIR.resolve())}


def model_status(*, all_models=False):
    result = subprocess.run(model_command("model-status", all_models=all_models),
                            env=model_environment(), capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        status = json.loads(result.stdout)
        if result.returncode or type(status["ready"]) is not bool or type(status["bytes"]) is not int:
            raise ValueError("Invalid model status.")
        return status
    except (TypeError, KeyError, ValueError) as error:
        raise RuntimeError("Could not read the pinned Jaro model status.") from error


async def install_workspace_models(report):
    # Descendants stay in the desktop's owned process group or Windows Job.
    process = await asyncio.create_subprocess_exec(
        *model_command("models", all_models=True), env=model_environment(),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    ready = False
    failure = None
    try:
        async for line in process.stdout:
            event = json.loads(line)
            match event.get("event"):
                case "progress":
                    report(event["completed"], event["total"])
                case "error":
                    failure = event.get("message")
                case "ready":
                    ready = event.get("ready") is True
        if await process.wait() or not ready:
            raise RuntimeError(failure or "Could not verify the installed models.")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.communicate()


def install_model(source=None, *, progress=False, all_models=False):
    command = model_command("models", all_models=all_models)
    label = "Jaro’s local models" if all_models else "Rumble2 Flash"
    if source is not None:
        command.extend(("--from", str(Path(source).resolve(strict=True))))
    failure = None
    installed = None
    shown_progress = False
    try:
        with subprocess.Popen(command, env=model_environment(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt") as process:
            try:
                for line in process.stdout:
                    event = json.loads(line)
                    if event.get("event") == "ready":
                        installed = event
                    elif event.get("event") == "error":
                        failure = event.get("message")
                    elif event.get("event") == "progress" and progress:
                        fraction = event["completed"] / max(event["total"], 1)
                        print(f"\rPreparing {label}… {fraction:.0%}", end="", file=sys.stderr, flush=True)
                        shown_progress = True
                if process.wait() or not installed or installed.get("ready") is not True:
                    raise RuntimeError(failure or f"Could not install {label}. Retry to resume the download.")
            finally:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        if os.name == "nt":
                            process.terminate()
                        else:
                            os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            if os.name == "nt":
                                process.kill()
                            else:
                                os.killpg(process.pid, signal.SIGKILL)
    finally:
        if shown_progress:
            print(file=sys.stderr)
    return {key: installed[key] for key in ("ready", "bytes", "directory")}


def ensure_model(*, allow_prompt):
    status = model_status()
    if status["ready"]:
        return
    if allow_prompt and sys.stdin.isatty() and sys.stderr.isatty():
        size = status["bytes"] / 1_000_000_000
        print(f"Download Rumble2 Flash (up to {size:.1f} GB)? [Y/n] ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline()
        if answer and answer.strip().lower() in ("", "y", "yes"):
            install_model(progress=True)
            return
        if not answer:
            print(file=sys.stderr)
    raise ModelUnavailable()
