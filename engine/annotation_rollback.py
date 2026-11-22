"""Guarded undo for a Jard annotation that has entered an OpenCode v2 turn."""

import asyncio
from dataclasses import dataclass
from time import time

import httpx

from annotation_deliveries import ROLLBACK_REVIEW
from annotation_routing import (PAGE_COUNT, PAGE_SIZE, RoutingError, check_status, decoded, discover,
                                make_client, owns_listener, read_receipt, receipt_target, request,
                                session_record, state_directory, value_of, withdraw_annotation)
from managed_preferences import require_opencode


TURN_MARKERS = {"agent-switched", "model-switched", "location-switched", "system"}
TURN_OUTPUT = {"assistant", "idle"} | TURN_MARKERS
FILE_TOOLS = {"edit", "write", "patch", "apply_patch", "multiedit"}


@dataclass(frozen=True)
class Timeline:
    found: bool
    own_turn: bool = False
    edited: bool = False


def edited_files(row):
    parts = row.get("content")
    return isinstance(parts, list) and any(
        isinstance(part, dict) and part.get("type") == "tool" and part.get("name") in FILE_TOOLS
        and isinstance(part.get("state"), dict) and part["state"].get("status") == "completed"
        for part in parts
    )


class OpenCodeTurn:
    def __init__(self, client, record, saved):
        self.client, self.record = client, record
        self.session, self.message = receipt_target(saved)
        self.path = f"/api/session/{self.session}"

    async def read(self, suffix, **options):
        status, body = await request(self.client, self.record, "GET", self.path + suffix, seconds=1.5, **options)
        check_status(status)
        return decoded(body)

    async def staged(self):
        return (await session_record(self.client, self.record, self.path, self.session)).get("revert")

    async def timeline(self):
        params = {"order": "desc", "limit": PAGE_SIZE}
        cursors = set()
        found = later_input = edited = False
        for _ in range(PAGE_COUNT):
            value = await self.read("/message", params=params)
            rows = value_of(value)
            if not isinstance(rows, list):
                break
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                    raise RoutingError("OpenCode returned an unreadable turn.")
                kind = row["type"]
                if found:
                    if kind not in TURN_MARKERS:
                        return Timeline(True, kind == "idle" and not later_input, edited)
                elif row.get("id") == self.message:
                    if kind != "user":
                        raise RoutingError("OpenCode returned a different message type for this annotation.")
                    found = True
                else:
                    later_input |= kind not in TURN_OUTPUT
                    edited |= kind == "assistant" and edited_files(row)
            cursor = value.get("cursor") if isinstance(value, dict) else None
            next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
            if next_cursor is None:
                return Timeline(found, found and not later_input, edited)
            if not rows or not isinstance(next_cursor, str) or not 0 < len(next_cursor) <= 4096 or next_cursor in cursors:
                break
            cursors.add(next_cursor)
            params = {"cursor": next_cursor, "limit": PAGE_SIZE}
        raise RoutingError("Jard could not verify the annotation’s turn boundary.")

    async def has_waiting_input(self):
        inbox = value_of(await self.read("/inbox"))
        if not isinstance(inbox, list):
            raise RoutingError("OpenCode returned an unreadable inbox.")
        return bool(inbox)

    async def standing(self):
        return await self.timeline(), await self.has_waiting_input(), await self.staged()

    async def mutate(self, method, suffix, **options):
        if not await asyncio.to_thread(owns_listener, self.record):
            raise RoutingError("OpenCode closed while Jard was undoing the annotation.")
        return await request(self.client, self.record, method, self.path + suffix, seconds=2.5, **options)

    async def clear(self, stage):
        if await self.staged() != stage:
            return False
        status, _ = await self.mutate("DELETE", "/revert")
        return 200 <= status < 300 and await self.staged() is None

    def matches_stage(self, stage):
        return isinstance(stage, dict) and stage.get("messageID") == self.message


async def review_rollback(turn, saved):
    stage = await turn.staged()
    if stage is not None:
        return "rollback-staged" if turn.matches_stage(stage) and saved["status"] != "rollback-commit-unconfirmed" else None
    timeline, waiting, stage = await turn.standing()
    if waiting or stage is not None:
        return None
    if saved["status"] in ("rollback-staged", "rollback-commit-unconfirmed") and not timeline.found:
        return "reverted"
    if saved["status"] == "rollback-staged" and timeline.found:
        return "received"  # The staged action was cleared in OpenCode.
    return None


async def rollback_turn(turn, saved, restore_files, store):
    deadline = saved["undoUntil"]
    timeline, waiting, stage = await turn.standing()
    if stage is not None:
        raise RoutingError("Resolve the staged revert in OpenCode before undoing this annotation.")
    if not timeline.own_turn or waiting:
        return await store("received")
    if timeline.edited and not restore_files:
        return await store("rollback-files")
    if time() * 1000 >= deadline:
        return saved

    await store("rollback-unconfirmed")
    status, _ = await turn.mutate("POST", "/interrupt", params={"resume": "false"})
    if not 200 <= status < 300:
        if status >= 500:
            return None
        return await store("received")

    for _ in range(4):
        timeline, waiting, stage = await turn.standing()
        if not timeline.own_turn or waiting or stage is not None or time() * 1000 >= deadline:
            return await store("received")
        if timeline.edited and not restore_files:
            return await store("rollback-files")
        status, body = await turn.mutate("POST", "/revert/stage", json={"messageID": turn.message, "files": timeline.edited})
        if status != 409:
            break
        await asyncio.sleep(0.2)
    if not 200 <= status < 300:
        return await store("received") if status < 500 else None
    stage = value_of(decoded(body))
    if not turn.matches_stage(stage):
        return None
    await store("rollback-staged")

    # A prompt can commit a staged revert. Only touch the stage this operation acknowledged.
    try:
        observed, waiting, staged = await turn.standing()
    except (RoutingError, httpx.HTTPError, TimeoutError):
        return await store("received") if await turn.clear(stage) else None
    if staged != stage:
        # OpenCode can commit an acknowledged stage when it admits a new prompt.
        return await store("reverted") if staged is None and not waiting and not observed.found else None
    if (not observed.own_turn or waiting or observed.edited != timeline.edited or
            time() * 1000 >= deadline):
        if await turn.clear(stage):
            return await store("rollback-files" if observed.own_turn and not waiting and observed.edited and not restore_files
                               else "received")
        return None
    await store("rollback-commit-unconfirmed")
    if not await asyncio.to_thread(owns_listener, turn.record):
        return await store("rollback-staged")
    # Make the final reads after persisting the claim and verifying the listener.
    if await turn.staged() != stage:
        return await store("rollback-staged")
    if await turn.has_waiting_input() or time() * 1000 >= deadline:
        return await store("received" if await turn.clear(stage) else "rollback-staged")
    status, _ = await request(turn.client, turn.record, "POST", turn.path + "/revert/commit", seconds=2.5)
    if 200 <= status < 300:
        return await store("reverted")
    if status < 500 and await turn.clear(stage):
        return await store("received")
    return None


async def undo_annotation(root, delivery, *, restore_files=False, directory=None):
    require_opencode()
    receipts, saved = await read_receipt(root, delivery)
    if saved.get("rememberedRule", {}).get("status") == "saved":
        saved = await asyncio.to_thread(receipts.undo_remembered_rule, saved["deliveryId"])
        if not saved["message"]:
            return saved
    if saved["status"] in ("queued", "withdrawal-unconfirmed"):
        saved = await withdraw_annotation(root, delivery, directory=directory)
    reviewing = saved["status"] in ROLLBACK_REVIEW
    if not reviewing and saved["status"] not in ("received", "rollback-files"):
        return saved
    if not reviewing and time() * 1000 >= saved["undoUntil"]:
        return saved
    latest = saved

    async def store(state):
        nonlocal latest
        latest = await asyncio.to_thread(receipts.change, saved["deliveryId"], state)
        return latest

    async with make_client() as client:
        record = await discover(client, saved["service"], directory or state_directory())
        turn = OpenCodeTurn(client, record, saved)
        try:
            async with asyncio.timeout(12):
                if reviewing:
                    if state := await review_rollback(turn, saved):
                        return await store(state)
                    return saved
                return await rollback_turn(turn, saved, restore_files, store) or latest
        except (RoutingError, httpx.HTTPError, TimeoutError) as error:
            if latest["status"] in ROLLBACK_REVIEW:
                return latest
            if isinstance(error, RoutingError):
                raise
            raise RoutingError("OpenCode could not finish checking the annotation’s turn.") from error
