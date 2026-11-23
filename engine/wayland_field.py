"""Read the focused Wayland editor's accessibility identity and caret geometry."""

import asyncio
from dataclasses import dataclass

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError, InvalidAddressError


ACCESSIBLE = "org.a11y.atspi.Accessible"
TEXT = "org.a11y.atspi.Text"
REGISTRY = "org.a11y.atspi.Registry"
OBJECT_EVENTS = "org.a11y.atspi.Event.Object"
WINDOW_EVENTS = "org.a11y.atspi.Event.Window"
DBUS = "org.freedesktop.DBus"
PROPERTIES = "org.freedesktop.DBus.Properties"
EVENTS = ("object:state-changed:focused", "object:text-caret-moved", "object:text-selection-changed",
          "object:text-changed", "window:deactivate")
EDITABLE_AND_FOCUSED = (1 << 7) | (1 << 12)
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


class WaylandField:
    def __init__(self):
        self.bus = None
        self.target = None
        self.focus_revision = 0
        self.revision = 0

    @property
    def focus(self):
        if self.target and self.bus is not None and self.bus.connected:
            return (*self.target, self.focus_revision)
        return None

    def change_focus(self, target):
        if target != self.target:
            self.focus_revision += 1
            self.revision += 1
            self.target = target

    def signal(self, message):
        if message.message_type != MessageType.SIGNAL or not message.sender or not message.path:
            return
        target = (message.sender, message.path)
        if message.interface == WINDOW_EVENTS and message.member == "Deactivate":
            if self.target and target[0] == self.target[0]:
                self.change_focus(None)
        elif message.interface == OBJECT_EVENTS and message.member == "StateChanged" and len(message.body) >= 2:
            state, enabled = message.body[:2]
            if state == "focused" and enabled == 1:
                self.change_focus(target)
            elif state == "focused" and enabled == 0 and target == self.target:
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
                    await call(self.bus, DBUS, "/org/freedesktop/DBus", DBUS, "AddMatch", "s",
                               [f"type='signal',interface='{interface}'"])
                for event in EVENTS:
                    await call(self.bus, REGISTRY, "/org/a11y/atspi/registry", REGISTRY, "RegisterEvent",
                               "sass", [event, [], ""])
        except ERRORS:
            self.close()
        finally:
            if session is not None:
                session.disconnect()

    def close(self):
        self.change_focus(None)
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    def matches(self, field):
        return self.focus == field.focus and self.revision == field.revision

    async def snapshot(self):
        focus, revision, bus = self.focus, self.revision, self.bus
        if focus is None:
            return None
        name, path, _ = focus
        try:
            async with asyncio.timeout(.25):
                state, = await call(bus, name, path, ACCESSIBLE, "GetState")
                if (not isinstance(state, list) or not state or type(state[0]) is not int
                        or state[0] & EDITABLE_AND_FOCUSED != EDITABLE_AND_FOCUSED):
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
