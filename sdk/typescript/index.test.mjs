import assert from "node:assert/strict"
import { createHash, createHmac } from "node:crypto"
import { chmod, mkdtemp, readFile, realpath, rm, stat, writeFile } from "node:fs/promises"
import { createServer } from "node:http"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"
import { connect, create } from "./index.mjs"

test("only the socket that proved ownership receives audio and credentials", { skip: process.platform === "win32" }, async () => {
  const library = await realpath(await mkdtemp(join(tmpdir(), "jard-sdk-")))
  const file = join(library, ".jard-sdk.json")
  const folder = await stat(library, { bigint: true })
  const record = {
    pid: process.pid, token: "a".repeat(64), apiVersion: 1,
    libraryId: createHash("sha256").update(`${folder.dev}:${folder.ino}`).digest("hex"),
  }
  const verified = new WeakSet()
  let wrongProof = false
  let closeBeforeRequest = false
  let authenticated = 0
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1")
    let data = { status: "ready", model: "Reson8/Rumble-2-Flash" }
    if (url.pathname.endsWith("/identify")) {
      assert.equal(req.headers.authorization, undefined)
      const challenge = url.searchParams.get("challenge")
      const binding = [record.apiVersion, record.pid, record.port, record.libraryId, challenge].join(":")
      data = { proof: createHmac("sha256", wrongProof ? "incorrect" : record.token).update(binding).digest("hex") }
      if (closeBeforeRequest) res.setHeader("Connection", "close")
      verified.add(req.socket)
    } else {
      assert.equal(verified.has(req.socket), true)
      assert.equal(req.headers.authorization, `Bearer ${record.token}`)
      authenticated++
      if (url.pathname.endsWith("/transcriptions")) {
        const chunks = []
        for await (const chunk of req) chunks.push(chunk)
        assert.deepEqual(Buffer.concat(chunks), Buffer.from([1, 2, 3]))
        assert.equal(url.searchParams.get("language"), "en")
        data = { transcript: "Test text.", durationMs: 123 }
      }
    }
    res.setHeader("Content-Type", "application/json")
    res.end(JSON.stringify(data))
  })
  try {
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
    record.port = server.address().port
    await writeFile(file, JSON.stringify(record), { mode: 0o600 })
    const client = await connect({ library })
    assert.deepEqual(await client.transcribe(new Uint8Array([1, 2, 3]), { languages: ["en"] }),
      { transcript: "Test text.", durationMs: 123 })
    assert.equal(authenticated, 2)
    wrongProof = true
    await assert.rejects(client.health(), { code: "unverified-service" })
    wrongProof = false
    closeBeforeRequest = true
    await assert.rejects(client.health(), { code: "unverified-service" })
    assert.equal(authenticated, 2)
    await chmod(file, 0o644)
    await assert.rejects(connect({ library }), { code: "invalid-discovery" })
  } finally {
    await new Promise(resolve => server.close(resolve))
    await rm(library, { recursive: true, force: true })
  }
})

const embeddedFixture = String.raw`
const { createHmac } = require("node:crypto")
const { createServer } = require("node:http")
const record = { token: "a".repeat(64), apiVersion: 1, libraryId: "b".repeat(64), pid: process.pid }
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1")
  if (url.pathname.endsWith("/identify")) {
    const binding = [record.apiVersion, record.pid, record.port, record.libraryId, url.searchParams.get("challenge")].join(":")
    res.end(JSON.stringify({ proof: createHmac("sha256", record.token).update(binding).digest("hex") }))
  } else if (req.headers.authorization !== "Bearer " + record.token) {
    res.writeHead(401).end("{}")
  } else if (url.pathname.endsWith("/transcriptions")) {
    // Leave this request in flight until the parent closes its lease.
    for await (const _ of req) { /* drain */ }
  } else {
    res.end(JSON.stringify({ status: "ready", model: "Reson8/Rumble-2-Flash" }))
  }
})
server.listen(0, "127.0.0.1", () => {
  record.port = server.address().port
  const pid = process.argv[1] === "wrong-pid" ? record.pid + 1 : record.pid
  console.log(JSON.stringify({ type: "ready", url: "http://127.0.0.1:" + record.port, ...record, pid }))
})
process.stdin.resume()
process.stdin.on("end", () => process.exit(0))
`

test("embedded clients own their exact child and close on an in-flight cancellation", async () => {
  const command = [process.execPath, "-e", embeddedFixture]
  await assert.rejects(create({ command: [...command, "wrong-pid"] }), { code: "invalid-handshake" })
  const [first, second] = await Promise.all([create({ command }), create({ command })])
  try {
    assert.notEqual(first.pid, second.pid)
    await assert.rejects(first.transcribe(new Uint8Array([1]), { signal: AbortSignal.abort() }), { name: "AbortError" })
    assert.equal((await first.health()).status, "ready")
    const abort = new AbortController()
    const work = first.transcribe(new Uint8Array([1]), { signal: abort.signal })
    const cancelled = assert.rejects(work, { name: "AbortError" })
    setTimeout(() => abort.abort(), 45)
    await cancelled
    assert.throws(() => process.kill(first.pid, 0), { code: "ESRCH" })
    assert.equal((await second.health()).status, "ready")
  } finally {
    await Promise.all([first.close(), second.close()])
  }
  assert.throws(() => process.kill(second.pid, 0), { code: "ESRCH" })
})

test("opt-in model preparation reports framed progress and stops on cancellation or failure", async () => {
  const root = await mkdtemp(join(tmpdir(), "jaro-sdk-prepare-"))
  const pidFile = join(root, "pid")
  const preparing = String.raw`
const args = process.argv.slice(1)
if (args.join(" ") !== ["--prepare-speech-model", "--speech-model-source", args[2], "--repair-speech-model"].join(" ")) {
  process.exit(2)
}
require("node:fs").writeFileSync(args[2], String(process.pid))
const events = [{type:"progress",phase:"checking"}, ...Array.from({length:70}, (_, completedBytes) =>
  ({type:"progress",phase:"installing",completedBytes,totalBytes:70})), {type:"progress",phase:"loading"}]
process.stdout.write(events.map(event => JSON.stringify(event) + "\n").join(""))
` + embeddedFixture
  const options = { command: [process.execPath, "-e", preparing, "--"], prepare: { from: pidFile, repair: true } }
  const seen = []
  try {
    const client = await create({ ...options, onProgress: event => seen.push(event) })
    try {
      assert.deepEqual(seen[0], { phase: "checking" })
      assert.deepEqual(seen[1], { phase: "installing", completedBytes: 0, totalBytes: 70 })
      assert.equal(seen.length, 72)
      assert.deepEqual(seen.at(-1), { phase: "loading" })
    } finally { await client.close() }

    const abort = new AbortController()
    await assert.rejects(create({ ...options, signal: abort.signal, onProgress: () => abort.abort() }), { name: "AbortError" })
    const pid = Number(await readFile(pidFile, "utf8"))
    assert.throws(() => process.kill(pid, 0), { code: "ESRCH" })

    const failure = 'process.stdout.write(\'{"type":"error","code":"model-prepare-failed"}\\n\', () => process.exit(1))'
    await assert.rejects(create({ command: [process.execPath, "-e", failure, "--"], prepare: true }),
      { code: "model-prepare-failed" })
  } finally { await rm(root, { recursive: true, force: true }) }
})
