"""StatusNotifierItem and D-Bus menu for the portable Linux desktop."""

import asyncio
import os
from pathlib import Path

from dbus_fast import Message, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import MessageType, PropertyAccess
from dbus_fast.errors import DBusError, InvalidAddressError
from dbus_fast.service import ServiceInterface, dbus_property, method, signal

WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM = "/StatusNotifierItem"
MENU = "/MenuBar"
ICON = "local.jardaudio.desktop"
PHASE = {"idle": 10, "recording": 11, "starting": 12, "saving": 13, "unavailable": 14}
ENTRIES = {
    1: ("Open Jaro", "open"), 2: ("Copy last meeting transcript", "copy"), 3: ("Quit Jaro", "quit"),
    10: ("Record Meeting", "record"), 11: ("Stop & Save", "stop"), 12: ("Starting…", ""),
    13: ("Saving…", ""), 14: ("Record Meeting", ""),
}


class StatusItem(ServiceInterface):
    def __init__(self, tray):
        super().__init__("org.kde.StatusNotifierItem")
        self.tray = tray

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return ICON

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "Jaro"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        share_icon = Path(__file__).resolve().parents[1] / "share/jaro.png"
        return "jaro" if share_icon.is_file() else ICON

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        share = Path(__file__).resolve().parents[1] / "share"
        return str(share) if (share / "jaro.png").is_file() else ""

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return [ICON, [], "Jaro", "Meeting controls"]

    @method()
    def Activate(self, x: "i", y: "i"):
        self.tray.submit("open")


class TrayMenu(ServiceInterface):
    def __init__(self, tray):
        super().__init__("com.canonical.dbusmenu")
        self.tray = tray
        self.items = {}
        self.version = 0

    def refresh(self):
        phase, can_copy, _ = self.tray.view()
        buttons = (1, PHASE[phase], 2, 3)
        items = {}
        for key in buttons:
            title, action = ENTRIES[key]
            items[key] = {"label": Variant("s", title), "enabled": Variant("b", bool(action) and (key != 2 or can_copy))}
        if items != self.items:
            self.items = items
            self.version += 1
            self.LayoutUpdated(self.version, 0)

    def properties(self, key, names):
        values = {"children-display": Variant("s", "submenu")} if key == 0 else self.items.get(key, {})
        return {name: value for name, value in values.items() if not names or name in names}

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @signal()
    def LayoutUpdated(self, version, parent) -> "ui":
        return [version, parent]

    @method()
    def GetLayout(self, parent: "i", recursion: "i", names: "as") -> "u(ia{sv}av)":
        children = [Variant("(ia{sv}av)", [key, self.properties(key, names), []])
                    for key in self.items] if parent == 0 and recursion != 0 else []
        return [self.version, [parent, self.properties(parent, names), children]]

    @method()
    def GetGroupProperties(self, ids: "ai", names: "as") -> "a(ia{sv})":
        return [[key, self.properties(key, names)] for key in ids if key in self.items]

    @method()
    def GetProperty(self, item: "i", name: "s") -> "v":
        return self.items.get(item, {}).get(name, Variant("s", ""))

    @method()
    def AboutToShow(self, parent: "i") -> "b":
        previous = self.version
        self.refresh()
        return self.version != previous

    @method()
    def AboutToShowGroup(self, parents: "ai") -> "aiai":
        previous = self.version
        self.refresh()
        return [parents if self.version != previous else [], []]

    @method()
    def Event(self, item: "i", event: "s", data: "v", timestamp: "u"):
        self.click(item, event)

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        for item, event, _, _ in events:
            self.click(item, event)
        return []

    def click(self, item, event):
        if event == "clicked" and item in self.items and self.items[item]["enabled"].value:
            self.tray.submit(ENTRIES[item][1])


async def current_host(bus):
    try:
        reply = await asyncio.wait_for(bus.call(Message(
            destination=WATCHER, path=WATCHER_PATH, interface="org.freedesktop.DBus.Properties",
            member="Get", signature="ss", body=[WATCHER, "IsStatusNotifierHostRegistered"],
        )), 1)
        if reply.message_type == MessageType.METHOD_RETURN and reply.body and reply.body[0].value is True:
            return reply.sender
    except TimeoutError:
        pass
    return None


async def status_notifier(tray):
    try:
        bus = await MessageBus().connect()
    except (DBusError, InvalidAddressError):
        return
    instance = StatusItem(tray)
    menu = TrayMenu(tray)
    bus.export(ITEM, instance)
    bus.export(MENU, menu)
    registered = None
    count = 0
    try:
        while bus.connected:
            if count % 5 == 0:
                host = await current_host(bus)
                if host != registered:
                    registered = None
                    if host:
                        try:
                            result = await asyncio.wait_for(bus.call(Message(
                                destination=host, path=WATCHER_PATH, interface=WATCHER, member="RegisterStatusNotifierItem",
                                signature="s", body=[ITEM],
                            )), 1)
                            if result.message_type == MessageType.METHOD_RETURN:
                                registered = host
                        except TimeoutError:
                            pass
                    tray.available(bool(registered))
            if registered:
                menu.refresh()
            count += 1
            await asyncio.sleep(.4)
    finally:
        bus.disconnect()
