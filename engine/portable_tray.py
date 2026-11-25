"""Local meeting and update commands for the portable desktop tray."""

import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
from secrets import token_hex
import sqlite3
import subprocess

from fastapi import HTTPException
from engine_lifecycle import begin_draining
from portable_call_pages import Call

logger = logging.getLogger(__name__)
COMMANDS = {"open", "record", "stop", "copy", "quit"}
UPDATE_COMMANDS = {"check", "restart", "download"}


class MeetingTray:
    def __init__(self, state, directory, busy, latest, copy, *, restart=None, meetings=True,
                 shutdown_busy=None):
        self.state = state
        self.directory = directory
        self.busy = busy
        self.shutdown_busy = shutdown_busy or busy
        self.latest = latest
        self.copy = copy
        self.restart = restart
        self.meetings = meetings
        self.update_job = None
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

    def meeting_view(self):
        if not self.meetings:
            return "hidden", False
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
        return phase, bool(self.key)

    def view(self):
        phase, can_copy = self.meeting_view()
        try:
            pid = int((self.directory / "window-pid").read_text())
        except (OSError, ValueError):
            pid = 0
        return phase, can_copy, pid, self.update_view()

    def update_view(self):
        updater = getattr(self.state, "portable_updater", None)
        if self.restart is None or updater is None:
            return "none"
        status = updater.status()
        phase = status["phase"]
        if status.get("updatesDisabled"):
            return "managed"
        if phase == "applying" or status.get("restartInProgress") or status.get("trayRestart"):
            return "restarting"
        if status.get("staged"):
            return "wait" if self.shutdown_busy() else "restart"
        if phase in ("checking", "downloading", "staging", "download"):
            return phase
        return "check"

    def open_workspace(self):
        with suppress(OSError):
            (self.directory / "open").touch()

    def submit(self, command):
        if command in UPDATE_COMMANDS:
            if (self.update_job is None or self.update_job.done()) and command == self.update_view():
                self.update_job = asyncio.create_task(self.update(command))
            return
        if command not in COMMANDS:
            return
        if command == "open":
            self.open_workspace()
            return
        if command == "quit":
            if self.shutdown_busy():
                self.open_workspace()
                return
            begin_draining(self.state)
            self.state.request_shutdown()
            return
        if self.job and not self.job.done():
            return
        phase, copy = self.meeting_view()
        if (command, phase) in (("record", "idle"), ("stop", "recording")) or command == "copy" and copy:
            self.action = command
            self.job = asyncio.create_task(self.execute(command))

    async def update(self, command):
        updater = self.state.portable_updater
        updater.publish(restartError="")
        try:
            if command == "check":
                updater.publish(openUpdateSettings=token_hex(12))
                self.open_workspace()
                await updater.check()
            elif command == "download":
                await updater.download()
            else:
                await self.restart(updater)
        except HTTPException as error:
            updater.publish(restartError=str(error.detail), openUpdateSettings=token_hex(12))
            self.open_workspace()
        except (OSError, ValueError):
            message = "Could not open the Jaro download." if command == "download" else "Could not complete the update request."
            updater.publish(restartError=message, openUpdateSettings=token_hex(12))
            self.open_workspace()

    async def execute(self, command):
        try:
            watcher = self.state.portable_calls
            if command == "record":
                async with watcher.transition:
                    await watcher.begin(Call("", ""), ())
                    watcher.offer = None
            elif command == "stop":
                self.transcribing = await watcher.stop_requested()
            else:
                await self.copy()
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
            jobs = [job for job in (self.job, self.update_job) if job is not None]
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


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
                phase, can_copy, pid, update = view
                child.stdin.write(f"{phase} {int(can_copy)} {pid} {update}\n".encode("ascii"))
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
