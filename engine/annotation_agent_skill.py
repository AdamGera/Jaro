"""Inspect and install the bundled annotation guide in a local agent's skills directory."""

from contextlib import suppress
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile
from threading import Lock

from engine_paths import WORKSPACE_ROOT

SKILL = Path("skills/jaro-annotate/SKILL.md")
AGENTS = ("claude", "codex", "opencode", "pi")
MAX_SKILL_SIZE = 64 * 1024
INSTALL_LOCK = Lock()


def guide():
    return (WORKSPACE_ROOT / SKILL).read_bytes()


def config_home(variable, default):
    value = Path(os.environ.get(variable) or default)
    return value if value.is_absolute() else default


def targets():
    home = Path.home()
    claude = config_home("CLAUDE_CONFIG_DIR", home / ".claude")
    codex = config_home("CODEX_HOME", home / ".codex")
    opencode = config_home("XDG_CONFIG_HOME", home / ".config") / "opencode"
    pi = config_home("PI_CODING_AGENT_DIR", home / ".pi/agent")
    pi_found = pi.is_dir() if pi != home / ".pi/agent" else pi.parent.is_dir()
    shared = home / ".agents" / SKILL
    return {
        "claude": (claude.is_dir(), claude / SKILL),
        "codex": (codex.is_dir(), shared),
        "opencode": (opencode.is_dir(), opencode / SKILL),
        "pi": (pi_found, shared if os.path.lexists(shared) or linked(shared.parent) else pi / SKILL),
    }


def linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def skill_state(path, content):
    try:
        if any(linked(item) for item in (path.parent.parent, path.parent, path)):
            return "managed", None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SKILL_SIZE:
            return "managed", None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            after = os.fstat(stream.fileno())
            if not stat.S_ISREG(after.st_mode) or not os.path.samestat(info, after):
                return "managed", None
            current = stream.read(MAX_SKILL_SIZE + 1)
            if len(current) > MAX_SKILL_SIZE:
                return "managed", None
            return ("installed" if current == content else "different"), current
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "managed", None


def status(agents, content):
    return [{"agent": agent, "path": str(path), "found": found, "state": skill_state(path, content)[0]}
            for agent, (found, path) in agents.items()]


def write_guide(path, content, expected):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".jaro-guide-", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if skill_state(path, content) != expected:
            raise ValueError("The guide changed on disk. Refresh its status and try again.")
        os.replace(temporary, path)
    finally:
        if temporary:
            with suppress(FileNotFoundError):
                os.unlink(temporary)


def manage(action, agent=None):
    content = guide()
    if action == "guide" and agent is None:
        return {"markdown": content.decode("utf-8")}
    with INSTALL_LOCK:
        agents = targets()
        if action == "status" and agent is None:
            return {"agents": status(agents, content)}
        if action not in ("install", "update") or not isinstance(agent, str) or agent not in agents:
            raise ValueError("Choose a guide action and a supported agent.")
        found, path = agents[agent]
        if not found:
            raise ValueError("Set up this agent before installing its guide.")
        snapshot = skill_state(path, content)
        state, _ = snapshot
        if state == "managed":
            raise ValueError("This guide is managed elsewhere.")
        if action == "install" and state == "different":
            raise ValueError("A different guide already exists. Use Update guide to replace it.")
        if action == "update" and state == "missing":
            raise ValueError("The guide is missing. Use Install guide to add it.")
        if state != "installed":
            write_guide(path, content, snapshot)
        return {"agents": status(agents, content)}
