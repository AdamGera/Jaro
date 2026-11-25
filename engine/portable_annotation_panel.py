"""Require a live desktop surface for an automatic annotation delivery."""

import asyncio
from contextlib import asynccontextmanager, suppress
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
from time import time

from annotation_automation import COUNTDOWN, AnnotationAutomation

logger = logging.getLogger(__name__)
QUICK_UNDO_SECONDS = 5
UNDO_RESULT_SECONDS = 3


class PortableAnnotationPanel:
    def __init__(self, executable, root, load_capture, deliver, undo):
        self.executable = executable
        self.undo = undo
        self.automation = AnnotationAutomation(root, load_capture, deliver, self.notify, library_locked=True)
        self.capture = None
        self.child = None
        self.presentation = None
        self.tasks = set()

    def notify(self, detail):
        capture = detail["capture"]
        if nonce := detail.get("nonce"):
            if self.capture == capture and self.child and self.child.returncode is None:
                with suppress(OSError):
                    self.child.stdin.write(f"confirm {nonce}\n".encode("ascii"))
                    return
            self.automation.confirm_from_panel(capture, nonce, "unavailable")
        elif detail["phase"] == "offer":
            self.show(capture, self.present(capture, detail["title"]))
        elif detail["phase"] == "sent" and detail.get("receiptStatus") == "queued" and detail.get("undoUntil", 0) > time() * 1000:
            self.show(capture, self.present_undo(detail))
        elif self.capture == capture and detail["phase"] not in ("countdown", "held", "preparing"):
            self.dismiss()

    def show(self, capture, work):
        self.dismiss()
        self.capture = capture
        task = asyncio.create_task(work)
        self.presentation = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def dismiss(self):
        self.capture = None
        self.presentation = None
        child, self.child = self.child, None
        if child:
            child.stdin.close()

    @asynccontextmanager
    async def launch(self, mode, title, seconds):
        child = await asyncio.create_subprocess_exec(
            str(self.executable), mode, title, str(seconds),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        current = self.presentation is asyncio.current_task()
        if current:
            self.child = child
        try:
            yield child if current else None
        finally:
            if self.child is child:
                self.child = None
            child.stdin.close()
            try:
                await asyncio.wait_for(child.wait(), timeout=2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    child.kill()
                await child.wait()

    async def present(self, capture, title):
        task = asyncio.current_task()
        try:
            async with self.launch("annotation-send", title, int(COUNTDOWN)) as child:
                while child and self.presentation is task and (line := await child.stdout.readline()):
                    if self.presentation is not task:
                        break
                    action, *values = line.decode("ascii", errors="replace").split() or ("",)
                    if action in ("shown", "hold", "resume", "cancel") and not values:
                        await self.automation.panel_action(capture, action)
                    elif action == "confirm" and len(values) == 2:
                        self.automation.confirm_from_panel(capture, *values)
        except (OSError, ValueError):
            pass
        finally:
            if self.presentation is task:
                self.dismiss()
                await self.automation.cancel(capture, "Jaro couldn’t keep the automatic send countdown visible.")

    async def present_undo(self, detail):
        task = asyncio.current_task()
        deadline = detail["undoUntil"] / 1000
        try:
            async with self.launch("annotation-undo", detail["title"], QUICK_UNDO_SECONDS) as child:
                remaining = deadline - time()
                if not child or remaining <= 0:
                    return
                action = await asyncio.wait_for(child.stdout.readline(), remaining)
                if self.presentation is not task or action.rstrip(b"\r\n") != b"undo" or time() >= deadline:
                    return
            try:
                receipt = await self.undo(detail["deliveryId"])
                message = "Annotation withdrawn" if receipt["status"] == "withdrawn" else "Review delivery in Sessions"
            except (OSError, ValueError, sqlite3.Error):
                message = "Couldn’t withdraw the annotation"
                logger.info("Quick annotation withdrawal was unavailable.", exc_info=True)
            if self.presentation is task:
                async with self.launch("annotation-result", message, UNDO_RESULT_SECONDS) as child:
                    if child:
                        await asyncio.wait_for(child.wait(), UNDO_RESULT_SECONDS)
        except (OSError, ValueError):
            pass
        finally:
            if self.presentation is task:
                self.dismiss()

    async def close(self):
        self.dismiss()
        await self.automation.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def annotation_panel_helper():
    path = Path(os.environ.get("JARO_CALL_SURFACE", ""))
    return path if path.is_absolute() and path.is_file() else None
