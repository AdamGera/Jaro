"""Network and child-process operations scoped to an approved transformation call."""
import asyncio
from base64 import b64encode
from contextlib import suppress
import errno
import os
import signal

import httpx
import httpcore

from output_transform_network import LoopbackTransport, ProxyTransport


_MAX_BODY = 16 << 20
NETWORK_SLOTS = asyncio.Semaphore(8)
PROGRAM_SLOTS = asyncio.Semaphore(4)


def _text(value, maximum):
    if isinstance(value, str) and len(value.encode("utf-8")) <= maximum and "\0" not in value:
        return value
    raise ValueError("Invalid transformation operation.")


def _number(value, default, maximum):
    if value is None:
        return default
    if type(value) not in (int, float) or not 0 <= value <= maximum:
        raise ValueError("Invalid transformation operation limit.")
    return value


def _encoded(raw):
    return b64encode(raw).decode("ascii")


def _check_url(url):
    if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
        raise ValueError("Use an absolute HTTP or HTTPS URL without embedded credentials.")


async def fetch(spec, *, loopback=False):
    async def validate(request):
        _check_url(request.url)

    url = _text(spec.get("url"), 16_384)
    method = _text(spec.get("method"), 64)
    body = spec.get("body")
    if body is not None:
        if method in ("GET", "HEAD"):
            raise ValueError("GET and HEAD requests cannot have a body.")
        body = _text(body, 256 << 10).encode()
    headers = spec.get("headers")
    if not isinstance(headers, list) or len(headers) > 128 or any(
            not isinstance(pair, list) or len(pair) != 2 for pair in headers):
        raise ValueError("Invalid request headers.")
    headers = [(_text(name, 256), _text(value, 16_384)) for name, value in headers]
    async with NETWORK_SLOTS:
        try:
            transport = LoopbackTransport() if loopback else ProxyTransport()
            async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=True,
                                         max_redirects=20, timeout=60, event_hooks={"request": [validate]}) as client:
                async with client.stream(method, url, headers=headers, content=body) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=32_768):
                        if len(data) + len(chunk) > _MAX_BODY:
                            raise ValueError("The response is larger than 16 MB.")
                        data.extend(chunk)
                    return {"status": response.status_code, "statusText": response.reason_phrase,
                            "url": str(response.url), "headers": response.headers.multi_items(),
                            "body": _encoded(data)}
        except (httpx.HTTPError, httpx.InvalidURL, httpcore.NetworkError, httpcore.TimeoutException,
                httpcore.ProtocolError, UnicodeError) as error:
            raise ValueError("The network request failed.") from error


def _program_env(extra):
    if not isinstance(extra, dict) or len(extra) > 128:
        raise ValueError("Invalid program environment.")
    env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
           if key in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    env.update({_text(key, 256): _text(value, 16_384) for key, value in extra.items()})
    return env


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS may report EPERM for a group as its leader exits.
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.send_signal(sig)


async def _terminate(process, *, timed_out=False):
    try:
        if timed_out:
            _signal_group(process, signal.SIGTERM)
            await asyncio.sleep(0.05)
    finally:
        _signal_group(process, signal.SIGKILL)
        stdin = process.stdin
        stdin_transport = getattr(stdin, "transport", None)
        if stdin is not None and not stdin.is_closing() and stdin_transport is not None:
            stdin_transport.abort()
        # A descendant may have inherited these pipes even after the group is gone.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            for fd in (1, 2):
                pipe = transport.get_pipe_transport(fd)
                if pipe is not None:
                    pipe.close()
        await process.wait()


async def _start_program(args, cwd, env):
    started = asyncio.create_task(asyncio.create_subprocess_exec(
        *args, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    ))
    try:
        return await asyncio.shield(started)
    except asyncio.CancelledError:
        try:
            process = await started
        except OSError:
            pass
        else:
            await _terminate(process)
        raise


class BufferExceeded(Exception):
    def __init__(self, stream):
        self.stream = stream


async def run_program(spec, workspace):
    file = _text(spec.get("file"), 32_768)
    args = spec.get("args", [])
    if not file or not isinstance(args, list) or len(args) > 256 or type(spec.get("shell")) is not bool:
        raise ValueError("Invalid program arguments.")
    argv = ["/bin/sh", "-c", file] if spec["shell"] else [file, *(_text(arg, 32_768) for arg in args)]
    cwd = spec.get("cwd")
    cwd = workspace if cwd is None else workspace / _text(cwd, 4096)
    env = _program_env(spec.get("env", {}))
    timeout = _number(spec.get("timeout"), 0, 60_000) / 1000
    limit = int(_number(spec.get("maxBuffer"), 1 << 20, _MAX_BODY))
    raw = spec.get("input")
    raw = b"" if raw is None else _text(raw, 256 << 10).encode()
    reply = dict(spawnError=None, code=None, signal=None, stdout="", stderr="", maxBuffer=None, timedOut=False)
    async with PROGRAM_SLOTS:
        try:
            process = await _start_program(argv, cwd, env)
        except OSError as error:
            reply["spawnError"] = errno.errorcode.get(error.errno, "EIO")
            return reply

        parts = {"stdout": bytearray(), "stderr": bytearray()}

        async def read(source, name):
            real = parts[name]
            while chunk := await source.read(32_768):
                room = limit - len(real)
                real.extend(chunk[:room])
                if len(chunk) > room:
                    raise BufferExceeded(name)

        async def feed():
            try:
                process.stdin.write(raw)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(coro) for coro in
                 (read(process.stdout, "stdout"), read(process.stderr, "stderr"), feed(), process.wait())]
        try:
            async with asyncio.timeout(timeout or None):
                await asyncio.gather(*tasks)
        except BufferExceeded as error:
            reply["maxBuffer"] = error.stream
        except TimeoutError:
            reply["timedOut"] = True
            reply["signal"] = "SIGTERM"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _terminate(process, timed_out=reply["timedOut"])

        for name, data in parts.items():
            reply[name] = _encoded(data)
        if not reply["timedOut"]:
            code = process.returncode
            if code < 0:
                reply["signal"] = signal.Signals(-code).name
            else:
                reply["code"] = code
        return reply
