"""Read browser pages for call detection and focused dictation profiles."""
import asyncio
from functools import partial
import os
from pathlib import Path

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from dictation_output import canonical_host
from portable_call_pages import BrowserPage, displayed_meet_room, page_identity, select_call
from wayland_field import (ACCESSIBLE, DBUS, DBUS_PATH, ERRORS, PROPERTIES, REGISTRY, TEXT,
                           REGISTRY_ROOT, accessible_active_window, accessible_children, call, has_accessible_state)


COLLECTION = "org.a11y.atspi.Collection"
DOCUMENT = "org.a11y.atspi.Document"
SHOWING = 1 << 25
FOCUSED = 1 << 12
ALERT = 2
WEB_DOCUMENT = 95
# Collection match rule: showing state, web-document role, canonical order.
DOCUMENT_RULE = [[SHOWING, 0], 1, {}, 1, [0, 0, -(1 << 31), 0], 2, [], 1, False]
# Role 79 is a native text entry. Match the Chromium class, which stays
# the same when the browser's interface language changes.
OMNIBOX_CLASS = "OmniboxViewViews"
OMNIBOX_RULE = [[SHOWING, 0], 1, {"class": OMNIBOX_CLASS}, 1, [0, 0, 1 << 15, 0], 2, [], 1, False]
MAX_WINDOWS = 12
CALL_TIMEOUT = .18
SAMPLE_TIMEOUT = .85


def browser_binary_matches(process, binaries):
    return ((process / "exe").readlink().name in binaries or
            (process / "comm").read_text().rstrip("\n") in binaries)


def browser_owner(pid, *, binaries):
    process = Path("/proc") / str(pid)
    try:
        return process.stat().st_uid == os.getuid() and browser_binary_matches(process, binaries)
    except OSError:
        return False


def browser_ancestors(pid, binaries):
    permitted = set()
    for _ in range(10):
        if not 0 < pid <= 0xFFFFFFFF:
            break
        process = Path("/proc") / str(pid)
        try:
            if process.stat().st_uid != os.getuid():
                break
            if browser_binary_matches(process, binaries):
                permitted.add(pid)
            # Field 2 is a parenthesized command name, which can contain spaces
            # and parentheses. After it come the state and the parent PID.
            parent = int((process / "stat").read_text().rsplit(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if parent == pid:
            break
        pid = parent
    return permitted


def observed_browsers(sources, microphones):
    observed = {}
    for source, binaries in sources.items():
        pids = {pid for binary in binaries for pid in microphones.get(binary, ())}
        if pids:
            observed[source] = [browser_ancestors(pid, binaries) for pid in sorted(pids)]
    return observed


async def query(bus, *args):
    async with asyncio.timeout(CALL_TIMEOUT):
        return await call(bus, *args)


async def process_id(bus, name):
    try:
        pid, = await query(bus, DBUS, DBUS_PATH, DBUS, "GetConnectionUnixProcessID", "s", [name])
        return pid
    except ERRORS:
        return None


async def outside_web_content(bus, window, target):
    for _ in range(12):
        parent, = await query(bus, *target, PROPERTIES, "Get", "ss", [ACCESSIBLE, "Parent"])
        if not isinstance(parent, Variant) or parent.signature != "(so)" or parent.value[0] != window[0]:
            return False
        target = tuple(parent.value)
        if target == window:
            return True
        role, = await query(bus, *target, ACCESSIBLE, "GetRole")
        if role == WEB_DOCUMENT:
            return False
    return False


async def readable_omnibox(bus, target):
    state, = await query(bus, *target, ACCESSIBLE, "GetState")
    return (isinstance(state, list) and bool(state) and type(state[0]) is int
            and state[0] & (SHOWING | FOCUSED) == SHOWING)


async def chromium_address(bus, window):
    matches, = await query(bus, *window, COLLECTION, "GetMatches", "(aiia{ss}iaiiasib)uib",
                           [OMNIBOX_RULE, 1, 2, True])
    if len(matches) != 1:
        return ""
    target = tuple(matches[0])
    if len(target) != 2 or target[0] != window[0] or not await outside_web_content(bus, window, target):
        return ""
    attributes, = await query(bus, *target, ACCESSIBLE, "GetAttributes")
    if (not isinstance(attributes, dict) or attributes.get("class") != OMNIBOX_CLASS
            or not await readable_omnibox(bus, target)):
        return ""
    count, = await query(bus, *target, PROPERTIES, "Get", "ss", [TEXT, "CharacterCount"])
    if not isinstance(count, Variant) or count.signature != "i" or not 0 < count.value <= 4096:
        return ""
    value, = await query(bus, *target, TEXT, "GetText", "ii", [0, count.value])
    if isinstance(value, str) and len(value) == count.value and await readable_omnibox(bus, target):
        return value
    return ""


async def browser_window(bus, window):
    missing_address = False
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            if not await has_accessible_state(bus, window, SHOWING):
                return None, True
        matches, = await query(bus, *window, COLLECTION, "GetMatches", "(aiia{ss}iaiiasib)uib",
                               [DOCUMENT_RULE, 1, 1, True])
        missing_address = not matches
        if matches:
            doc = tuple(matches[0])
            # Only the selected outer document counts. An embedded call site
            # in a different page must not trigger automatic recording.
            if len(doc) == 2 and doc[0] == window[0] and await outside_web_content(bus, window, doc):
                for attribute in ("DocURL", "URI"):
                    value, = await query(bus, *doc, DOCUMENT, "GetAttributeValue", "s", [attribute])
                    if isinstance(value, str) and value.strip():
                        return BrowserPage(address=value.strip()[:4097]), len(value) <= 4096
                missing_address = True
        else:
            # Firefox exposes its microphone sharing indicator as a top-level
            # alert. It has no page and does not compete with a browser window.
            role, = await query(bus, *window, ACCESSIBLE, "GetRole")
            if role == ALERT:
                return None, True
    except ERRORS:
        pass
    try:
        title, = await query(bus, *window, PROPERTIES, "Get", "ss", [ACCESSIBLE, "Name"])
        if isinstance(title, Variant) and title.signature == "s" and len(title.value) <= 512:
            if missing_address:
                if not title.value:
                    # Chromium also exports its unnamed native panels as empty documents.
                    return None, True
                try:
                    if address := await chromium_address(bus, window):
                        if address.startswith(("https://", "http://")):
                            return BrowserPage(address=address), True
                        room_hint = displayed_meet_room(address)
                        return BrowserPage(title=title.value, room_hint=room_hint), bool(room_hint)
                except ERRORS:
                    pass
            return BrowserPage(title=title.value), False
    except ERRORS:
        pass
    return None, False


async def browser_windows(bus, app):
    try:
        async with asyncio.timeout(CALL_TIMEOUT):
            children = await accessible_children(bus, app)
    except ERRORS:
        return [], False
    windows = [child for child in children if child[0] == app[0]]
    pages = await asyncio.gather(*(browser_window(bus, window) for window in windows[:MAX_WINDOWS]))
    # A missing or different visible page makes site attribution ambiguous.
    complete = 0 < len(windows) == len(children) <= MAX_WINDOWS and all(
        reliable and (page is None or page.room_hint or page_identity(page.address)) for page, reliable in pages
    )
    return [page for page, _ in pages if page is not None], complete


async def read_browsers(bus, observed):
    applications = await accessible_children(bus, (REGISTRY, REGISTRY_ROOT))
    pids = await asyncio.gather(*(process_id(bus, app[0]) for app in applications))
    owned = dict(zip(applications, pids, strict=True))
    selected = {}
    for source, chains in observed.items():
        selected[source] = [app for app, pid in owned.items() if any(pid in chain for chain in chains)]
    matches = {}
    for source, apps in selected.items():
        if apps:
            pages = await asyncio.gather(*(browser_windows(bus, app) for app in apps))
            visible_pids = {owned[app] for app in apps}
            complete = all(visible_pids & chain for chain in observed[source]) and all(known for _, known in pages)
            call = select_call(source, (page for windows, _ in pages for page in windows), complete)
            if call:
                matches[source] = call
    return matches


async def on_accessibility_bus(reader, *args, timeout=SAMPLE_TIMEOUT):
    session = bus = None
    try:
        async with asyncio.timeout(timeout):
            session = await MessageBus(bus_type=BusType.SESSION).connect()
            address, = await query(session, "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")
            bus = await MessageBus(bus_address=address).connect()
            return await reader(bus, *args)
    except ERRORS:
        return None
    finally:
        if bus is not None:
            bus.disconnect()
        if session is not None:
            session.disconnect()


async def browser_call_pages(sources, microphones):
    observed = await asyncio.to_thread(observed_browsers, sources, microphones)
    if not any(any(chains) for chains in observed.values()):
        return {}
    return await on_accessibility_bus(read_browsers, observed) or {}


def process_application(pid):
    try:
        return (Path("/proc") / str(pid) / "exe").readlink().name[:256]
    except OSError:
        return ""


async def focused_browser_address(window, window_probe):
    from portable_calls import APPS, BROWSER_CONTEXT_BINARIES

    if not window.title:
        return ""
    accepts_pid = None
    if window.pid is None:
        # Foreign toplevels expose only a desktop app ID and title. Require the
        # AT-SPI owner to be the same browser and the matching title to be unique.
        name = window.application.lower().rsplit(".", 1)[-1]
        binaries = next((app.binaries for app in APPS if app.browser and
                         (name == app.key or name in app.binaries)), ())
        if not binaries and name in BROWSER_CONTEXT_BINARIES:
            binaries = (name,)
        if not binaries:
            return ""
        accepts_pid = partial(browser_owner, binaries=binaries)
    elif not browser_owner(window.pid, binaries=BROWSER_CONTEXT_BINARIES):
        return ""

    async def read(bus):
        if target := await accessible_active_window(bus, window, require_active=False, accepts_pid=accepts_pid):
            page, reliable = await browser_window(bus, target)
            if (reliable and page and page.address.startswith(("https://", "http://"))
                    and await window_probe() == window):
                return page.address
        return ""

    return await on_accessibility_bus(read, timeout=.4) or ""


async def focused_browser_host(window, window_probe):
    return canonical_host(await focused_browser_address(window, window_probe))
