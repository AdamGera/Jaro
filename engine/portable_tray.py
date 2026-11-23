"""Local meeting commands for the portable desktop's notification area."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import sqlite3
import subprocess

from fastapi import HTTPException
from desktop_clipboard import copy_text
from portable_call_pages import Call

logger = logging.getLogger(__name__)
COMMANDS = {"open", "record", "stop", "copy", "quit"}


class MeetingTray:
    def __init__(self, state, directory, busy, latest, transcript, finish):
        self.state = state
        self.directory = directory
        self.busy = busy
        self.latest = latest
        self.transcript = transcript
        self.finish = finish
        self.action = None
        self.job = None
        self.transcribing = None
        self.key = None

    def available(self, enabled):
        marker = self.directory / "ready"
        try:
            if enabled:
                marker.touch()
            else:
                marker.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not publish the tray availability.")

    def view(self):
        with suppress(OSError, sqlite3.Error):
            self.key = self.latest()
        recording = self.state.portable_recording
        if self.transcribing:
            with suppress(OSError, sqlite3.Error):
                with self.state.library.connection() as db:
                    row = db.execute("SELECT status FROM sessions WHERE id=?", (str(self.transcribing),)).fetchone()
                if not row or row[0] not in ("processing", "queued"):
                    self.transcribing = None
        if self.action in ("record", "stop"):
            phase = "starting" if self.action == "record" else "saving"
        elif self.transcribing:
            phase = "saving"
        elif recording and recording.meeting and not self.state.draining:
            phase = "recording"
        else:
            phase = "unavailable" if self.state.status != "ready" or self.busy() else "idle"
        try:
            pid = int((self.directory / "window-pid").read_text())
        except (OSError, ValueError):
            pid = 0
        return phase, bool(self.key), pid

    def submit(self, command):
        if command not in COMMANDS:
            return
        if command == "open":
            with suppress(OSError):
                (self.directory / "open").touch()
            return
        if command == "quit":
            if self.busy():
                with suppress(OSError):
                    (self.directory / "open").touch()
                return
            self.state.request_shutdown()
            return
        if self.job and not self.job.done():
            return
        phase, copy, _ = self.view()
        if (command, phase) in (("record", "idle"), ("stop", "recording")) or command == "copy" and copy:
            self.action = command
            self.job = asyncio.create_task(self.execute(command))

    async def execute(self, command):
        try:
            watcher = self.state.portable_calls
            if command == "record":
                async with watcher.transition:
                    await watcher.begin(Call("", ""), ())
                    watcher.offer = None
            elif command == "stop":
                recording = self.state.portable_recording
                if recording and recording.meeting:
                    key = recording.key
                    if watcher.active and watcher.active.id == key:
                        await watcher.stop(key)
                    else:
                        await self.finish(key)
                        watcher.completed = str(key)
                    self.transcribing = key
            else:
                text = await asyncio.to_thread(self.transcript, self.key)
                await copy_text(text)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            self.state.portable_calls.message = ("Could not copy the meeting transcript." if command == "copy"
                                                  else "Could not complete the meeting tray action.")
            logger.warning("Meeting tray action failed (%s).", type(error).__name__)
        except HTTPException as error:
            self.state.portable_calls.message = str(error.detail)
        finally:
            self.action = None

    async def run(self):
        try:
            if os.name == "nt":
                await windows_tray(self)
            else:
                from linux_tray import status_notifier

                await status_notifier(self)
        except (ImportError, OSError, RuntimeError):
            logger.info("The desktop tray is unavailable.")
        finally:
            self.available(False)
            if self.job:
                self.job.cancel()
                await asyncio.gather(self.job, return_exceptions=True)


async def windows_tray(tray):
    helper = Path(os.environ.get("JARO_TRAY_HELPER", ""))
    if not helper.is_absolute() or not helper.is_file():
        return
    child = await asyncio.create_subprocess_exec(
        helper, helper.with_name("jaro-tray.ico"), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        if await asyncio.wait_for(child.stdout.readline(), 12) != b"ready\n":
            return
        tray.available(True)
        previous = None
        while child.returncode is None:
            view = tray.view()
            if view != previous:
                phase, can_copy, pid = view
                child.stdin.write(f"{phase} {int(can_copy)} {pid}\n".encode("ascii"))
                await child.stdin.drain()
                previous = view
            try:
                data = await asyncio.wait_for(child.stdout.readline(), .4)
            except TimeoutError:
                continue
            if not data:
                return
            tray.submit(data.decode("ascii", errors="ignore").strip())
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        pass
    finally:
        child.stdin.close()
        try:
            await asyncio.wait_for(child.wait(), 2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                child.kill()
            await child.wait()
