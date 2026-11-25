# Jaro local transcription SDK

Run Jaro, then import `sdk/typescript/index.mjs` from the installed payload in a trusted Node or Electron main process. The client uses the bundled Rumble2 Flash service. Supply audio bytes with `transcribe()`, or request a recording when the running app reports that microphone capture is available.

| Platform | Installed payload |
| --- | --- |
| macOS | `/Applications/Jaro.app/Contents/Resources` |
| Linux package | `/usr/lib/jaro` |
| Linux direct install | `${XDG_DATA_HOME:-$HOME/.local/share}/jaro-app/current` |
| Windows | `%LOCALAPPDATA%\Programs\Jaro\versions\<active version>` from `current-version` |

```js
import { readFile } from "node:fs/promises"
import { connect } from "/Applications/Jaro.app/Contents/Resources/sdk/typescript/index.mjs"

const jard = await connect()
console.log(await jard.health())
console.log(await jard.models.list())
const result = await jard.transcribe(await readFile("note.wav"), {
  languages: ["en"],
  signal: AbortSignal.timeout(60_000),
})
console.log(result.transcript, result.durationMs)
```

`connect({ library })` can target a custom Jaro library. By default it uses `RUMBLE_DATA_DIR` or the platform’s usual Jaro library. The SDK reads an owner-only discovery file on macOS and Linux or a current-user registry record on Windows. It verifies the engine over loopback and sends audio and credentials only on the verified connection.

Transcription accepts a `Uint8Array` or `ArrayBuffer` and returns final text and the duration of decoded audio in milliseconds. `capabilities()` reports the current upload and duration limits. A missing language lets Rumble2 Flash detect the language. Two requests can be admitted at a time. Errors expose `code`, an explanatory message, and an HTTP `status` when available.

File transcription creates no Session or history entry and never pastes. Aborting a request disconnects the client. An inference block already in progress finishes before Jaro discards the request’s temporary audio.

## Record with the desktop app

`connect()` can use Jaro’s microphone access and its selected input device. If `capabilities().serviceCapture` is `true`, request a take when the user chooses to record. The desktop app refuses the request if it needs microphone access or another capture is active. On Windows and Linux, takes last up to two minutes.

```js
const recording = await jard.dictation.start({ source: "My app" })
// When the user chooses Stop:
const { transcript, durationMs } = await recording.finish()
```

Use `recording.cancel()` to discard a take. The handle renews its ten-second lease every three seconds. Use `for await` with `recording.levels` for `rmsDb` and `peakDb`, or `recording.audio` for best-effort mono `Float32Array` batches at `recording.sampleRate`. The reader skips old batches if it falls behind.

If the recorder stops unexpectedly, another take can start right away. Call `finish()` on the interrupted handle to recover the audio already recorded. Jaro keeps the latest interrupted take until its lease expires, you cancel it, or another interrupted take replaces it. Finishing an empty or superseded take returns `microphone-lost`.

A finished take returns raw Rumble2 Flash text only to the caller. It stays out of the clipboard, History, and Sessions.

## Embedded transcription

Use `create()` from the packaged SDK to start a private Rumble2 Flash helper with the previously installed weights. The desktop app can stay closed. A source checkout can pass an explicit command such as `{ command: ["/path/to/.venv/bin/python", "/path/to/engine/server.py", "--embedded"] }`.

```js
import { create } from "/Applications/Jaro.app/Contents/Resources/sdk/typescript/index.mjs"

const jard = await create()
try {
  const result = await jard.transcribe(audio, { languages: ["en"] })
  console.log(result.transcript)
} finally {
  await jard.close()
}
```

Each embedded client owns a helper and an independent port. `close()` waits for that helper to exit. Cancelling an in-flight embedded transcription also closes the helper before the request rejects. Start a new client for the next take. The helper reads existing model files and discards temporary audio on exit. `jardctl transcribe` also uses an owned helper when the shared engine is stopped.

### Prepare the speech model

After the user agrees to install Rumble2 Flash, pass `prepare: true` to download missing pinned speech files before the helper starts. To install from an existing pinned cache, pass `prepare: { from: "/path/to/model-cache" }`. Include `repair: true` in that object to recheck and replace damaged files. Ordinary `create()` and `transcribe()` calls use installed weights.

```js
const jard = await create({
  prepare: true,
  signal: abortController.signal,
  onProgress: event => console.log(event),
})
```

Progress reports `checking`, `installing` with `completedBytes` and `totalBytes`, and `loading`. Preparation defaults to a 30-minute startup timeout. Cancelling startup closes the helper and leaves partial files available for a retry. A custom `command` must launch Jaro’s embedded helper and accept the appended preparation arguments.
