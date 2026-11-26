"""Client for Jaro's lightweight Rust + transcribe.cpp speech daemon (`jaro-speech`).

The packaged Mac engine can start a managed loopback helper. Source runs can
point to a developer-managed service with `JARD_SPEECH_URL`.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import json
import os
from secrets import token_hex
from tempfile import TemporaryDirectory
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx


DEFAULT_SPEECH_HOST = "127.0.0.1"
DEFAULT_SPEECH_PORT = 8766
RUMBLE_GGUF_NAME = "Rumble-2-Flash-Q8_0.gguf"
RUMBLE_GGUF_BYTES = 557329984
RUMBLE_GGUF_SHA256 = "bef26807d155ddb50a92aac5c985d51e7cd9206e311b51d640482d1d3e5431af"
RUMBLE_GGUF_URL = ("https://huggingface.co/nicolamanzini/Rumble-2-Flash-transcribe-cpp-GGUF/"
                  "resolve/16579a78af16f526b0a949f8616d6038901686b0/" + RUMBLE_GGUF_NAME)


def configured_speech_url() -> str | None:
    url = os.environ.get("JARD_SPEECH_URL", "").strip().rstrip("/")
    return url or None


def configured_speech_token() -> str | None:
    token = os.environ.get("JARD_SPEECH_TOKEN", "").strip()
    return token or None


def discover_speech_binary(workspace_root: Path) -> Path | None:
    override = os.environ.get("JARD_SPEECH_BIN", "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    candidates = (
        workspace_root / "desktop/speech-engine/target/release/jaro-speech",
        workspace_root / "desktop/speech-engine/target/debug/jaro-speech",
        Path("/Applications/Jaro.app/Contents/Helpers/jaro-speech"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class RustSpeechStream:
    """Handle for an active real-time dictation stream on `jaro-speech`."""

    def __init__(
        self,
        client: RustSpeechClient,
        stream_id: int,
        owner_token: str,
        *,
        native_streaming: bool,
        partials: bool,
    ):
        self._client = client
        self.stream_id = stream_id
        self.owner_token = owner_token
        self.native_streaming = native_streaming
        self.partials = partials
        self._closed = False

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = self._client._headers({"X-Jaro-Stream-Token": self.owner_token})
        if extra:
            headers.update(extra)
        return headers

    async def feed(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes or self._closed:
            return
        response = await self._client.http.post(
            f"{self._client.base_url}/v1/streams/{self.stream_id}/audio",
            content=pcm_bytes,
            headers=self._headers({"Content-Type": "application/octet-stream"}),
            timeout=10.0,
        )
        response.raise_for_status()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        url = f"{self._client.base_url}/v1/streams/{self.stream_id}/events"
        async with self._client.http.stream(
            "GET",
            url,
            headers=self._headers({"Accept": "text/event-stream"}),
            timeout=None,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                yield event
                if event.get("type") in ("final", "error", "cancelled"):
                    return

    async def finish(self) -> dict[str, Any]:
        self._closed = True
        response = await self._client.http.post(
            f"{self._client.base_url}/v1/streams/{self.stream_id}/finish",
            headers=self._headers(),
            timeout=120.0,
        )
        response.raise_for_status()
        return response.json()

    async def cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.http.post(
                f"{self._client.base_url}/v1/streams/{self.stream_id}/cancel",
                headers=self._headers(),
                timeout=5.0,
            )
        except httpx.HTTPError:
            pass


class RustSpeechClient:
    """Async HTTP + SSE client for `jaro-speech`."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        http: httpx.AsyncClient | None = None,
        trust_env: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=30.0, trust_env=trust_env)

    @classmethod
    def from_env(cls, http: httpx.AsyncClient | None = None) -> RustSpeechClient | None:
        url = configured_speech_url()
        if not url:
            return None
        return cls(url, configured_speech_token(), http=http)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.token:
            headers["X-Jard-Token"] = self.token
        if extra:
            headers.update(extra)
        return headers

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def capabilities(self) -> dict[str, Any]:
        response = await self.http.get(
            f"{self.base_url}/capabilities",
            headers=self._headers(),
            timeout=3.0,
        )
        response.raise_for_status()
        return response.json()

    async def prewarm(self) -> bool:
        try:
            response = await self.http.post(
                f"{self.base_url}/v1/prewarm",
                headers=self._headers(),
                timeout=2.0,
            )
            return response.status_code in (200, 202)
        except httpx.HTTPError:
            return False

    async def transcribe_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = 16_000,
        sample_format: str = "f32le",
        languages: tuple[str, ...] = (),
        vocabulary: tuple[str, ...] = (),
        screen_speech: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "sampleRate": sample_rate,
            "format": sample_format,
            "screenSpeech": "true" if screen_speech else "false",
        }
        if len(languages) == 1:
            params["language"] = languages[0]
        if vocabulary:
            params["vocabulary"] = ",".join(vocabulary)
        response = await self.http.post(
            f"{self.base_url}/v1/transcribe",
            params=params,
            content=pcm,
            headers=self._headers({"Content-Type": "application/octet-stream"}),
            timeout=120.0,
        )
        response.raise_for_status()
        return response.json()

    async def open_stream(
        self,
        *,
        sample_rate: int = 48_000,
        sample_format: str = "s16le",
        languages: tuple[str, ...] = (),
        vocabulary: tuple[str, ...] = (),
        partials: bool = True,
        screen_speech: bool = True,
    ) -> RustSpeechStream:
        payload: dict[str, Any] = {
            "sampleRate": sample_rate,
            "sampleFormat": sample_format,
            "vocabulary": list(vocabulary),
            "partials": partials,
            "screenSpeech": screen_speech,
        }
        if len(languages) == 1:
            payload["language"] = languages[0]
        response = await self.http.post(
            f"{self.base_url}/v1/streams",
            json=payload,
            headers=self._headers(),
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        return RustSpeechStream(
            self,
            int(data["id"]),
            str(data["ownerToken"]),
            native_streaming=bool(data.get("nativeStreaming", False)),
            partials=bool(data.get("partials", partials)),
        )


async def stop_speech_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def download_rumble_gguf(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    model = directory / RUMBLE_GGUF_NAME

    def verified(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size != RUMBLE_GGUF_BYTES:
            return False
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == RUMBLE_GGUF_SHA256

    if await asyncio.to_thread(verified, model):
        return model

    partial = model.with_suffix(".partial")
    if partial.is_file() and partial.stat().st_size >= RUMBLE_GGUF_BYTES:
        if await asyncio.to_thread(verified, partial):
            partial.replace(model)
            return model
        partial.unlink()
    download = await asyncio.create_subprocess_exec(
        "/usr/bin/curl", "--disable", "--fail", "--location", "--silent", "--show-error",
        "--connect-timeout", "15", "--speed-limit", "1024", "--speed-time", "30",
        "--retry", "3", "--continue-at", "-",
        "--output", partial, RUMBLE_GGUF_URL,
    )
    try:
        if await download.wait():
            raise RuntimeError("Could not download the Rust Rumble2 Flash weights.")
    finally:
        if download.returncode is None:
            with suppress(ProcessLookupError):
                download.kill()
            await download.wait()
    if not await asyncio.to_thread(verified, partial):
        partial.unlink(missing_ok=True)
        raise ValueError("The downloaded Rust speech model failed verification.")
    partial.replace(model)
    return model


async def start_bundled_speech(
    resources: Path, library: Path,
) -> tuple[RustSpeechClient, asyncio.subprocess.Process] | None:
    binary = resources.parent / "Helpers/jaro-speech"
    if not binary.is_file():
        return None
    model = await download_rumble_gguf(library / "speech-models")
    token = token_hex(32)
    with TemporaryDirectory(prefix="jaro-speech-") as folder:
        ready = Path(folder) / "port"
        process = await asyncio.create_subprocess_exec(
            binary, "serve", "--port", "0", "--model", model, "--ready-file", ready,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            env={**os.environ, "JARD_SPEECH_TOKEN": token},
        )
        try:
            async with asyncio.timeout(20):
                while not ready.is_file() and process.returncode is None:
                    await asyncio.sleep(0.05)
                if process.returncode is not None:
                    raise RuntimeError("The bundled Rust speech process exited before startup.")
                port = int(ready.read_text())
                return RustSpeechClient(f"http://127.0.0.1:{port}", token, trust_env=False), process
        except BaseException:
            await stop_speech_process(process)
            raise
