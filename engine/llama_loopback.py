"""Start the Windows text worker on an authenticated loopback listener."""
import asyncio
import os
from pathlib import Path
import re
import secrets
import subprocess
from tempfile import TemporaryDirectory

import httpx


LISTENING = re.compile(rb"(?:^| )listening on http://127\.0\.0\.1:([1-9][0-9]{0,4})\s*$")


async def listening_port(stream):
    while line := await stream.readline():
        match = LISTENING.search(line)
        if match and (port := int(match[1])) <= 65_535:
            return port
    raise RuntimeError("The local writing worker exited before opening its listener.")


async def drain(stream):
    while await stream.read(16_384):
        pass


async def start_worker(command):
    key = secrets.token_urlsafe(32)
    environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("LLAMA_")}
    flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
    with TemporaryDirectory(prefix="jaro-writing-") as folder:
        key_file = Path(folder) / "key"
        key_file.write_text(key + "\n", encoding="ascii")
        process = await asyncio.create_subprocess_exec(
            *command, "--host", "127.0.0.1", "--port", "0", "--api-key-file", str(key_file),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, creationflags=flags,
        )
        try:
            port = await asyncio.wait_for(listening_port(process.stdout), 75)
        except BaseException as error:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()
            if isinstance(error, TimeoutError):
                raise RuntimeError("The local writing worker did not open its listener in time.") from error
            raise
    output = asyncio.create_task(drain(process.stdout))
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=None, trust_env=False,
                              headers={"Authorization": f"Bearer {key}"})
    return process, client, output
