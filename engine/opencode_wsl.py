"""Windows preference and process adapter for the WSL OpenCode transport."""
import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

import httpx

from opencode_wsl_worker import MAX_REQUEST, MAX_RESPONSE

PREFERENCES = r"Software\Jaro\OpenCode"
LXSS = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
PREFERENCE = "UseWSL"


@dataclass(frozen=True)
class Distribution:
    name: str
    scope: str


def enabled():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PREFERENCES) as key:
            return winreg.QueryValueEx(key, PREFERENCE) == (1, winreg.REG_DWORD)
    except OSError:
        return False


def set_enabled(value):
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, PREFERENCES, access=winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, PREFERENCE, 0, winreg.REG_DWORD, int(value))


def default_distribution():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LXSS) as key:
            guid, kind = winreg.QueryValueEx(key, "DefaultDistribution")
            if kind != winreg.REG_SZ or not isinstance(guid, str):
                return None
            scope = str(UUID(guid))
            with winreg.OpenKey(key, "{" + scope + "}") as distribution:
                name, kind = winreg.QueryValueEx(distribution, "DistributionName")
                if (kind == winreg.REG_SZ and name and not any(ord(char) < 32 for char in name) and
                        winreg.QueryValueEx(distribution, "Version") == (2, winreg.REG_DWORD)):
                    return Distribution(name, scope)
    except (OSError, ValueError):
        pass
    return None


def exchange(distribution, payload, seconds):
    # Recheck the opt-in and current default before handing data to a guest.
    if not enabled() or default_distribution() != distribution:
        raise OSError("The selected WSL distribution is unavailable")
    command = json.dumps({**payload, "scope": distribution.scope}, ensure_ascii=False).encode()
    if len(command) > MAX_REQUEST:
        raise ValueError("The OpenCode request is too large")
    worker = Path(__file__).with_name("opencode_wsl_worker.py").read_text(encoding="utf-8")
    if payload.get("action") == "match-project":
        matcher = Path(__file__).with_name("annotation_projects.py").read_text(encoding="utf-8")
        worker = matcher + "\n" + worker
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/wsl.exe"
    process = subprocess.run([executable, "--distribution", distribution.name, "--exec", "/usr/bin/python3", "-I", "-S", "-c", worker],
                             input=command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=seconds, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    return json.loads(process.stdout)


async def discover(distribution, expected, channel):
    try:
        value = await asyncio.to_thread(exchange, distribution, {"action": "find", "expected": expected, "channel": channel}, 2.5)
        if isinstance(value, dict):
            return value
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


async def request(distribution, record, method, path, seconds, *, params=None, json=None):
    command = {"action": "request", "expected": record.key, "channel": record.channel.removeprefix("wsl:"),
               "method": method, "path": path, "params": params, "body": json}
    try:
        status, encoded = await asyncio.to_thread(exchange, distribution, command, seconds)
        if type(status) is not int or not 100 <= status <= 599 or len(encoded) > ((MAX_RESPONSE + 2) // 3) * 4:
            raise ValueError("Invalid response from OpenCode in WSL")
        return status, base64.b64decode(encoded, validate=True)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("OpenCode in WSL did not respond in time") from error
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise httpx.RequestError("OpenCode in WSL did not confirm the request") from error


async def match_project(record, target, directories):
    command = {"action": "match-project", "expected": record.key, "channel": record.channel.removeprefix("wsl:"),
               "target": target, "directories": directories}
    try:
        matches = await asyncio.to_thread(exchange, record.wsl, command, 2.5)
        if not isinstance(matches, list) or len(matches) > len(directories) or any(
            type(index) is not int or not 0 <= index < len(directories) for index in matches
        ):
            raise ValueError("Invalid OpenCode project matches")
        return matches
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise httpx.RequestError("Could not verify the OpenCode project in WSL") from error
