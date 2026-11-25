"""Read the foreground process of an unambiguous Linux terminal."""

import asyncio
from dataclasses import dataclass
from functools import partial
from itertools import islice
import os
from pathlib import Path
import re

from annotation_assets import LINUX_TERMINAL_BINARIES, annotation_origin, terminal_path


PROC = Path("/proc")
MAX_PROCESSES = 384
MAX_THREADS = 128
OPENCODE = frozenset(("opencode", "opencode-cli", "opencode-v2", "opencode-beta"))


def terminal_project(folder):
    try:
        path = Path(folder).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if terminal_path(str(path)) is None or not path.is_dir():
        return None
    project = path
    for parent in (path, *path.parents):
        if terminal_path(str(parent)) is None:
            break
        marker = parent / ".git"
        if marker.is_dir() or marker.is_file():
            project = parent
            break
    return {"folder": str(path), "project": str(project)}


def explicit_terminal_origin(folder, platform):
    path = terminal_path(folder)
    windows = platform == "windows"
    if path is None or (bool(path.drive) != windows):
        return None
    context = terminal_project(path)
    if not context:
        return None
    app = "windows.shell" if windows else "linux.shell"
    return annotation_origin({"app": app, "name": "Terminal", "terminal": context})


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    session: int
    tty: int
    foreground: int
    started: int


def process(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        directory = PROC / str(pid)
        if directory.stat().st_uid != os.geteuid():
            return None
        with (directory / "stat").open("rb") as stream:
            fields = stream.read(4096).rsplit(b") ", 1)[1].split()
        return Process(*(int(fields[index]) for index in (1, 2, 3, 4, 5, 19)))
    except (OSError, IndexError, ValueError):
        return None


def terminal_owner(pid, binaries):
    if not process(pid):
        return False
    try:
        name = (PROC / str(pid) / "exe").readlink().name
        return name in binaries
    except OSError:
        return False


def children(pid):
    directory = PROC / str(pid) / "task"
    threads = list(islice(directory.iterdir(), MAX_THREADS + 1))
    if len(threads) > MAX_THREADS:
        raise ValueError("Terminal has too many process threads.")
    found = set()
    for thread in threads:
        with (thread / "children").open("rb") as stream:
            listing = stream.read(4097)
        if len(listing) > 4096:
            raise ValueError("Terminal has too many child processes.")
        found.update(map(int, listing.split()))
    return found


def foreground_process(root):
    pending = [root]
    seen = {root}
    groups = set()
    for parent in pending:
        for pid in children(parent) - seen:
            info = process(pid)
            if info is None or info.parent != parent:
                return None
            if len(seen) >= MAX_PROCESSES:
                return None
            seen.add(pid)
            pending.append(pid)
            if info.tty and info.foreground > 0:
                groups.add((info.tty, info.session, info.foreground))
                if len(groups) > 1:
                    return None
    if len(groups) != 1:
        return None
    tty, session, pid = groups.pop()
    leader = process(pid) if pid in seen else None
    if leader and (leader.group, leader.foreground, leader.tty, leader.session) == (pid, pid, tty, session):
        return pid, leader
    return None


def opencode_identity(pid, title):
    directory = PROC / str(pid)
    executable = (directory / "exe").readlink().name
    if executable not in OPENCODE and executable not in ("node", "bun", "deno"):
        return {}
    with (directory / "cmdline").open("rb") as stream:
        command = stream.read(16_385)
    if len(command) > 16_384:
        return {}
    args = command.decode(errors="replace").rstrip("\0").split("\0")[1:]
    if executable not in OPENCODE and (not args or Path(args.pop(0)).name not in OPENCODE):
        return {}
    identity = {}
    if title.startswith("OC | "):
        identity["title"] = title.removeprefix("OC | ").strip()
    for index, option in enumerate(args):
        if option == "--":
            break
        value = args[index + 1] if option in ("--session", "-s") and index + 1 < len(args) else option.removeprefix("--session=")
        if (option.startswith("--session=") or option in ("--session", "-s")) and re.fullmatch(r"ses_[A-Za-z0-9_-]{1,123}", value):
            identity["session"] = value
            break
    return identity


def linux_terminal_context(root, binaries, title):
    if not terminal_owner(root, binaries):
        return None
    try:
        owner = process(root)
        observed = foreground_process(root)
        if not observed:
            return None
        pid, _ = observed
        folder = (PROC / str(pid) / "cwd").readlink()
        if str(folder).endswith(" (deleted)"):
            return None
        identity = opencode_identity(pid, title)
        project = terminal_project(folder)
        if (project and process(root) == owner and foreground_process(root) == observed
                and (PROC / str(pid) / "cwd").readlink() == folder):
            return {**project, **identity}
    except (OSError, ValueError):
        pass
    return None


async def focused_terminal_context(window, probe, application):
    binaries = LINUX_TERMINAL_BINARIES.get(application.casefold())
    if not binaries:
        return None
    if window.pid is not None:
        origin = await asyncio.to_thread(linux_terminal_context, window.pid, binaries, window.title)
    elif window.title:
        from linux_call_pages import on_accessibility_bus, process_id
        from wayland_field import accessible_active_window

        async def read_active_terminal(bus):
            verify = partial(terminal_owner, binaries=binaries)
            # The compositor reports focus. Some toolkits omit AT-SPI ACTIVE
            # for that same window, so use accessibility to prove its owner.
            find_window = partial(accessible_active_window, bus, window, require_active=False, accepts_pid=verify)
            target = await find_window()
            if not target or (pid := await process_id(bus, target[0])) is None:
                return None
            origin = await asyncio.to_thread(linux_terminal_context, pid, binaries, window.title)
            # Without a compositor window ID, the app and title can name a
            # replacement window. Require the same accessibility object.
            if origin and await find_window() == target:
                return origin
            return None

        origin = await on_accessibility_bus(read_active_terminal, timeout=.3)
    else:
        return None
    if origin and await probe() == window:
        return origin
    return None
