"""Read the focused Linux editor's accessibility identity and caret geometry."""

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
from struct import Struct

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError, InvalidAddressError

from wayland_clipboard import run_tool


ACCESSIBLE = "org.a11y.atspi.Accessible"
TEXT = "org.a11y.atspi.Text"
REGISTRY = "org.a11y.atspi.Registry"
REGISTRY_ROOT = "/org/a11y/atspi/accessible/root"
NULL_OBJECT = "/org/a11y/atspi/null"
OBJECT_EVENTS = "org.a11y.atspi.Event.Object"
WINDOW_EVENTS = "org.a11y.atspi.Event.Window"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"
EVENTS = ("object:state-changed:focused", "object:text-caret-moved", "object:text-selection-changed",
          "object:text-changed", "window:activate", "window:deactivate")
ACTIVE = 1 << 1
EDITABLE_AND_FOCUSED = (1 << 7) | (1 << 12)
MAX_FOCUS_OBJECTS = 96
FOCUS_LOOKUP_SECONDS = .25
SWAY_TREE_LIMIT = 1024 * 1024
SWAY_HEADER = Struct("=6sII")
SWAY_MAGIC = b"i3-ipc"
SWAY_SUBSCRIBE, SWAY_TREE = 2, 4
SWAY_WORKSPACE, SWAY_WINDOW = 0x80000000, 0x80000003
ERRORS = (DBusError, InvalidAddressError, OSError, TimeoutError, EOFError, ValueError)


@dataclass(frozen=True)
class Field:
    focus: tuple
    revision: int
    caret: int
    length: int


async def call(bus, destination, path, interface, member, signature="", body=None):
    result = await bus.call(Message(destination=destination, path=path, interface=interface,
                                    member=member, signature=signature, body=body or []))
    if result is None or result.message_type != MessageType.METHOD_RETURN:
        raise ValueError("Accessibility query was unavailable.")
    return result.body


@dataclass(frozen=True)
class FocusWindow:
    id: int | None
    pid: int | None
    title: str
    application: str = ""


def sway_focused_window(tree):
    pending = [tree]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        if node.get("focused") is True:
            pid, ident, title = node.get("pid"), node.get("id"), node.get("name")
            if type(pid) is int and pid > 0 and type(ident) is int:
                application = node.get("app_id")
                return FocusWindow(ident, pid, title if isinstance(title, str) else "",
                                   application[:256] if isinstance(application, str) else "")
            return None
        for key in ("nodes", "floating_nodes"):
            nodes = node.get(key)
            if isinstance(nodes, list):
                pending.extend(nodes)
    return None


async def sway_window():
    if not os.environ.get("SWAYSOCK"):
        return None
    process = await asyncio.create_subprocess_exec(
        "swaymsg", "-t", "get_tree", "-r", stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        try:
            data = await process.stdout.readexactly(SWAY_TREE_LIMIT + 1)
        except asyncio.IncompleteReadError as end:
            data = end.partial
        if len(data) > SWAY_TREE_LIMIT or await process.wait() != 0:
            return None
        return sway_focused_window(json.loads(data))
    except (ValueError, RecursionError):
        return None
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            # Drain the pipe so the killed process can be reaped even after a
            # size limit or timeout while more of the tree was being written.
            await process.communicate()


async def output_window():
    """Resolve profile context only. Paste uses the separately tracked editor."""
    if os.environ.get("SWAYSOCK"):
        with suppress(*ERRORS):
            async with asyncio.timeout(.15):
                if window := await sway_window():
                    return window
    helper = Path(os.environ.get("JARO_WAYLAND_FOCUS", ""))
    if not helper.is_absolute():
        return None
    with suppress(OSError, TimeoutError, UnicodeError):
        data = await run_tool(helper, timeout=.15)
        if data:
            application, separator, title = data.partition(b"\0")
            if separator and 0 < len(application) <= 256 and len(title) <= 4096 and b"\0" not in title:
                return FocusWindow(None, None, title.decode(), application.decode())
    return None


class SwayFocus:
    """Track only window identity. Focus events invalidate any earlier destination."""

    def __init__(self):
        self.focus = None
        self.revision = 0
        self.writer = None
        self.listener = None
        self.tree = None
        self.lock = asyncio.Lock()

    def change(self, identity):
        previous = self.focus[:2] if self.focus else None
        if identity != previous:
            self.revision += 1
            self.focus = (*identity, self.revision) if identity else None

    def send(self, kind, payload=b""):
        self.writer.write(SWAY_HEADER.pack(SWAY_MAGIC, len(payload), kind) + payload)

    @staticmethod
    async def read(reader):
        magic, length, kind = SWAY_HEADER.unpack(await reader.readexactly(SWAY_HEADER.size))
        if magic != SWAY_MAGIC or length > SWAY_TREE_LIMIT:
            raise ValueError("Invalid Sway IPC message.")
        return kind, json.loads(await reader.readexactly(length))

    async def start(self, socket):
        try:
            async with asyncio.timeout(FOCUS_LOOKUP_SECONDS):
                reader, self.writer = await asyncio.open_unix_connection(socket)
                self.send(SWAY_SUBSCRIBE, b'["window","workspace"]')
                kind, reply = await self.read(reader)
                if kind != SWAY_SUBSCRIBE or reply != {"success": True}:
                    raise ValueError("Sway did not accept the focus subscription.")
                self.listener = asyncio.create_task(self.listen(reader))
                await self.refresh()
        except (OSError, EOFError, ValueError, RecursionError):
            self.close()

    async def listen(self, reader):
        try:
            while True:
                kind, reply = await self.read(reader)
                if kind == SWAY_TREE and self.tree is not None:
                    window = sway_focused_window(reply)
                    self.change((window.id, window.pid) if window else None)
                    if not self.tree.done():
                        self.tree.set_result(True)
                elif kind == SWAY_WORKSPACE and reply.get("change") == "focus":
                    self.change(None)
                elif kind == SWAY_WINDOW:
                    container = reply.get("container", {})
                    ident, pid = container.get("id"), container.get("pid")
                    if reply.get("change") == "focus":
                        self.change((ident, pid) if type(ident) is int and type(pid) is int and pid > 0 else None)
                    elif reply.get("change") == "close" and self.focus and ident == self.focus[0]:
                        self.change(None)
        except (OSError, EOFError, ValueError, AttributeError, RecursionError):
            pass
        finally:
            if self.writer is not None:
                self.writer.close()
                self.writer = None
            if self.tree is not None and not self.tree.done():
                self.tree.set_result(False)
            self.change(None)

    async def refresh(self):
        async with self.lock:
            if self.writer is None:
                return False
            self.tree = asyncio.get_running_loop().create_future()
            try:
                async with asyncio.timeout(FOCUS_LOOKUP_SECONDS):
                    self.send(SWAY_TREE)
                    return await self.tree
            except OSError:
                self.close()
                return False
            except asyncio.CancelledError:
                self.close()
                raise
            finally:
                self.tree = None

    async def confirm(self, focus):
        # The tree response follows all focus events already sent on this connection.
        return self.focus == focus and await self.refresh() and self.focus == focus

    def close(self):
        if self.listener is not None:
            self.listener.cancel()
            self.listener = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        if self.tree is not None and not self.tree.done():
            self.tree.set_result(False)
        self.change(None)


async def accessible_children(bus, target):
    children, = await call(bus, *target, ACCESSIBLE, "GetChildren")
    if not isinstance(children, list) or len(children) > MAX_FOCUS_OBJECTS:
        raise ValueError("The accessibility tree is too large to inspect.")
    refs = []
    for reference in children:
        if not isinstance(reference, (tuple, list)) or len(reference) != 2:
            raise ValueError("Invalid accessibility reference.")
        name, path = reference
        if not isinstance(name, str) or not isinstance(path, str):
            raise ValueError("Invalid accessibility reference.")
        if name.startswith(":") and path != NULL_OBJECT:
            refs.append((name, path))
    return refs


async def has_accessible_state(bus, target, flags):
    state, = await call(bus, *target, ACCESSIBLE, "GetState")
    return isinstance(state, list) and bool(state) and type(state[0]) is int and state[0] & flags == flags


async def accessible_active_window(bus, window, *, require_active=True, accepts_pid=None):
    root = REGISTRY, REGISTRY_ROOT
    active, matching = [], []
    for app in await accessible_children(bus, root):
        pid, = await call(bus, DBUS, DBUS_PATH, DBUS, "GetConnectionUnixProcessID", "s", [app[0]])
        if accepts_pid is not None:
            if not accepts_pid(pid):
                continue
        elif pid != window.pid:
            continue
        for target in await accessible_children(bus, app):
            if target[0] != app[0]:
                continue
            title, = await call(bus, *target, PROPERTIES, "Get", "ss", [ACCESSIBLE, "Name"])
            if not isinstance(title, Variant) or title.signature != "s" or title.value != window.title:
                continue
            if await has_accessible_state(bus, target, ACTIVE):
                active.append(target)
            elif not require_active:
                matching.append(target)
    matches = active + matching if window.pid is None else active or matching
    return matches[0] if len(matches) == 1 else None


async def focused_editor(bus, window):
    """Look for one focused editor in the window named by the compositor."""
    active = await accessible_active_window(bus, window)
    if active is None:
        return None
    pending = deque([active])
    visited = set()
    editor = None
    while pending:
        target = pending.popleft()
        if target in visited or target[0] != active[0]:
            continue
        visited.add(target)
        if len(visited) > MAX_FOCUS_OBJECTS:
            return None
        if await has_accessible_state(bus, target, EDITABLE_AND_FOCUSED):
            if editor is not None:
                return None
            editor = target
        pending.extend(await accessible_children(bus, target))
        if len(pending) > MAX_FOCUS_OBJECTS:
            return None
    return editor


class WaylandField:
    def __init__(self, window_probe=None):
        self.window_probe = window_probe
        self.bus = None
        self.target = None
        self.seeded_window = None
        self.focus_revision = 0
        self.focus_events = 0
        self.revision = 0

    @property
    def focus(self):
        if self.target and self.bus is not None and self.bus.connected:
            return (*self.target, self.focus_revision)
        return None

    def change_focus(self, target):
        self.seeded_window = None
        if target != self.target:
            self.focus_revision += 1
            self.revision += 1
            self.target = target

    def signal(self, message):
        if message.message_type != MessageType.SIGNAL or not message.sender or not message.path:
            return
        target = (message.sender, message.path)
        if message.interface == WINDOW_EVENTS and message.member in ("Activate", "Deactivate"):
            self.focus_events += 1
            if message.member == "Deactivate" and self.target and target[0] == self.target[0]:
                self.change_focus(None)
        elif message.interface == OBJECT_EVENTS and message.member == "StateChanged" and len(message.body) >= 2:
            state, enabled = message.body[:2]
            if state == "focused":
                self.focus_events += 1
                if enabled == 1:
                    self.change_focus(target)
                elif enabled == 0 and target == self.target:
                    self.change_focus(None)
        elif (message.interface == OBJECT_EVENTS and target == self.target
              and message.member in ("TextCaretMoved", "TextSelectionChanged", "TextChanged")):
            # No event text is read or retained.
            self.revision += 1

    async def start(self):
        session = None
        try:
            session = MessageBus(bus_type=BusType.SESSION)
            async with asyncio.timeout(2):
                await session.connect()
                address, = await call(session, "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")
                self.bus = MessageBus(bus_address=address)
                await self.bus.connect()
                self.bus.add_message_handler(self.signal)
                for interface in (OBJECT_EVENTS, WINDOW_EVENTS):
                    await call(self.bus, DBUS, DBUS_PATH, DBUS, "AddMatch", "s",
                               [f"type='signal',interface='{interface}'"])
                for event in EVENTS:
                    await call(self.bus, REGISTRY, "/org/a11y/atspi/registry", REGISTRY, "RegisterEvent",
                               "sass", [event, [], ""])
        except ERRORS:
            self.close()
        finally:
            if session is not None:
                session.disconnect()
        await self.seed_focus()

    async def seed_focus(self):
        bus = self.bus
        if bus is None or self.target is not None:
            return
        events = self.focus_events
        window_probe = self.window_probe or sway_window
        try:
            async with asyncio.timeout(FOCUS_LOOKUP_SECONDS):
                window = await window_probe()
                if window is None or not window.title:
                    return
                target = await focused_editor(bus, window)
                if target is None or await window_probe() != window:
                    return
                if self.bus is bus and self.focus_events == events and self.target is None:
                    self.change_focus(target)
                    self.seeded_window = window
        except ERRORS:
            pass

    def close(self):
        self.change_focus(None)
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    def matches(self, field):
        return self.focus == field.focus and self.revision == field.revision

    async def confirm_seeded_focus(self, focus):
        seeded = self.seeded_window
        if self.focus != focus:
            return False
        if seeded is None:
            return True
        try:
            async with asyncio.timeout(FOCUS_LOOKUP_SECONDS):
                current = await (self.window_probe or sway_window)()
        except ERRORS:
            current = None
        if self.focus != focus:
            return False
        if self.seeded_window == seeded and current != seeded:
            self.change_focus(None)
            return False
        return True

    async def snapshot(self):
        focus, revision, bus = self.focus, self.revision, self.bus
        if focus is None or not await self.confirm_seeded_focus(focus):
            return None
        name, path, _ = focus
        try:
            async with asyncio.timeout(.25):
                if not await has_accessible_state(bus, (name, path), EDITABLE_AND_FOCUSED):
                    return None
                # The role name is the protocol name, independent of the app's locale.
                role, = await call(bus, name, path, ACCESSIBLE, "GetRoleName")
                if role == "password text":
                    return None
                selection, = await call(bus, name, path, TEXT, "GetNSelections")
                if selection:
                    return None
                caret, = await call(bus, name, path, PROPERTIES, "Get", "ss", [TEXT, "CaretOffset"])
                size, = await call(bus, name, path, PROPERTIES, "Get", "ss", [TEXT, "CharacterCount"])
                if (not isinstance(caret, Variant) or not isinstance(size, Variant)
                        or caret.signature != "i" or size.signature != "i" or not 0 <= caret.value <= size.value):
                    return None
                field = Field(focus, revision, caret.value, size.value)
                return field if self.matches(field) else None
        except ERRORS:
            return None
