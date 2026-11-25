"""Present detected call controls without opening the workspace."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import subprocess
from uuid import UUID

from fastapi import HTTPException
from portable_calls import OFFER_ACTIONS, POLICY_ACTIONS, recording_label

logger = logging.getLogger(__name__)
ACTIONS = {
    "offer": tuple(action.encode("ascii") for action in OFFER_ACTIONS),
    "offer-options": tuple(action.encode("ascii") for action in POLICY_ACTIONS),
    "recording": (b"stop",),
    "automatic": (b"stop",),
    "ending": (b"stop", b"keep"),
    "switching": (b"stop", b"keep"),
    "saving": (),
}


@dataclass(frozen=True)
class CallControl:
    key: str | UUID
    mode: str
    name: str
    began: float
    can_always: bool = False

    def seconds(self, now):
        if self.mode == "offer-options":
            return 0
        if self.mode in ("offer", "ending", "switching"):
            return max(1, round(self.began - now))
        return max(0, int(now - self.began))


def current_control(watcher):
    if offer := watcher.visible_offer():
        mode = "offer-options" if offer.choosing else "offer"
        return CallControl(offer.token, mode, recording_label(offer.call), offer.expires,
                           can_always=offer.choosing and offer.call.verified)
    if active := watcher.active:
        if active.finishing:
            mode = "saving"
        elif active.stop_at is not None:
            mode = "switching" if active.ending_reason == "page" else "ending"
            return CallControl(active.id, mode, active.name, active.stop_at)
        else:
            mode = "automatic" if active.automatic else "recording"
        return CallControl(active.id, mode, active.name, active.started)
    return None


async def act(watcher, control, action):
    if action not in ACTIONS[control.mode] or current_control(watcher) != control:
        return
    try:
        if control.mode in ("offer", "offer-options"):
            # The workspace's session language selection is unavailable here.
            await watcher.answer(control.key, action.decode("ascii"), ())
        elif action == b"keep":
            watcher.keep_recording(control.key)
        else:
            await watcher.stop(control.key)
    except (HTTPException, OSError) as failure:
        if control.mode in ("offer", "offer-options") and isinstance(failure, HTTPException) and failure.status_code != 409:
            watcher.message = str(failure.detail)


async def present(watcher, executable, control):
    options = ("always",) if control.can_always else ()
    child = await asyncio.create_subprocess_exec(
        executable, control.mode, control.name, str(control.seconds(watcher.clock())), *options,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    line = b""
    try:
        while current_control(watcher) == control:
            try:
                line = await asyncio.wait_for(child.stdout.readline(), timeout=0.25)
            except TimeoutError:
                continue
            break
    finally:
        child.stdin.close()
        try:
            await asyncio.wait_for(child.wait(), timeout=3)
        except TimeoutError:
            with suppress(ProcessLookupError):
                child.kill()
            await child.wait()
    if child.returncode:
        logger.info("Desktop call controls were unavailable. The call remains accessible in the workspace.")
        return b""
    return line.rstrip(b"\r\n")


async def watch_desktop_calls(watcher):
    helper = Path(os.environ.get("JARO_CALL_SURFACE", ""))
    if not helper.is_absolute() or not helper.is_file():
        return
    shown = None
    actions = set()
    try:
        while True:
            control = current_control(watcher)
            if control is None or control == shown:
                shown = control
                await asyncio.sleep(.25)
                continue
            shown = control
            try:
                action = await present(watcher, helper, control)
                if action in ACTIONS[control.mode]:
                    task = asyncio.create_task(act(watcher, control, action))
                    actions.add(task)
                    task.add_done_callback(actions.discard)
            except (OSError, ValueError):
                logger.warning("Could not show the desktop call controls.", exc_info=True)
    finally:
        for task in actions:
            task.cancel()
        await asyncio.gather(*actions, return_exceptions=True)
