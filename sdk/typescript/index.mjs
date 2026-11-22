import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto"
import { spawn } from "node:child_process"
import { constants } from "node:fs"
import { open, realpath, stat } from "node:fs/promises"
import { Agent, request } from "node:http"
import { homedir } from "node:os"
import { join } from "node:path"
import { setTimeout as delay } from "node:timers/promises"
import { fileURLToPath } from "node:url"

const DISCOVERY_FILE = ".jard-sdk.json"
const SDK_VERSION = 1
const API = "/api/sdk/v1"
const MAX_RESPONSE = 8 * 1024 * 1024
const DEFAULT_HELPER = fileURLToPath(new URL("../../../Helpers/jard-engine", import.meta.url))

export class JardSDKError extends Error {
  constructor(code, message, status) {
    super(message)
    this.name = "JardSDKError"
    this.code = code
    this.status = status
  }
}

async function discover(path) {
  const folder = path.startsWith("~/") ? join(homedir(), path.slice(2)) : path
  let root, file
  try {
    root = await realpath(folder)
    file = await open(join(root, DISCOVERY_FILE), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  } catch (error) {
    if (error.code === "ENOENT") throw new JardSDKError("not-running", "Start Jaro to use its SDK.")
    throw new JardSDKError("invalid-discovery", "Could not open Jard’s private SDK discovery file.")
  }
  let record
  try {
    const info = await file.stat()
    if (!info.isFile() || info.uid !== process.geteuid() || (info.mode & 0o077) !== 0) throw new Error()
    const buffer = Buffer.alloc(4097)
    const { bytesRead } = await file.read(buffer, 0, buffer.length, 0)
    if (bytesRead === buffer.length) throw new Error()
    record = JSON.parse(buffer.subarray(0, bytesRead).toString("utf8"))
    const folder = await stat(root, { bigint: true })
    const libraryId = createHash("sha256").update(`${folder.dev}:${folder.ino}`).digest("hex")
    if (!record || record.libraryId !== libraryId || !/^[0-9a-f]{64}$/.test(record.token)
        || !Number.isSafeInteger(record.pid) || record.pid < 1 || !Number.isInteger(record.port)
        || record.port < 1 || record.port > 65535) throw new Error()
  } catch {
    throw new JardSDKError("invalid-discovery", "Jard’s SDK discovery record is invalid or has unsafe permissions.")
  } finally {
    await file.close()
  }
  if (record.apiVersion !== SDK_VERSION) {
    throw new JardSDKError("incompatible-service", "This Jard SDK version cannot connect to the running engine.")
  }
  return record
}

function exchange(port, agent, path, { token, owner, body, method = body ? "POST" : "GET", signal, timeoutMs = 3000, verifiedSocket } = {}) {
  return new Promise((resolve, reject) => {
    const headers = {}
    if (token) headers.Authorization = `Bearer ${token}`
    if (owner) headers["X-Jard-Dictation-Token"] = owner
    if (body) {
      headers["Content-Type"] = "application/octet-stream"
      headers["Content-Length"] = body.byteLength
    }
    let socket
    const outgoing = request({ host: "127.0.0.1", port, path, agent, method, headers, signal },
      response => {
        const chunks = []
        let size = 0
        response.on("data", chunk => {
          size += chunk.byteLength
          if (size > MAX_RESPONSE) {
            outgoing.destroy(new JardSDKError("invalid-response", "Jard’s SDK response was too large."))
            return
          }
          chunks.push(chunk)
        })
        response.once("error", reject)
        response.once("end", () => {
          let data
          try {
            data = JSON.parse(Buffer.concat(chunks).toString("utf8"))
          } catch {
            reject(new JardSDKError("invalid-response", "The local service returned invalid JSON.", response.statusCode))
            return
          }
          if (response.statusCode < 200 || response.statusCode >= 300) {
            const detail = data?.detail
            reject(new JardSDKError(detail?.code ?? "request-failed",
              detail?.message ?? (typeof detail === "string" ? detail : "The local request failed."), response.statusCode))
            return
          }
          resolve({ data, socket })
        })
      })
    const timer = setTimeout(() => outgoing.destroy(new JardSDKError("timeout", "The local Jard request timed out.")), timeoutMs)
    outgoing.once("close", () => clearTimeout(timer))
    outgoing.once("error", error => {
      reject(error instanceof JardSDKError || error.name === "AbortError"
        ? error : new JardSDKError("not-running", "The local Jard engine is unreachable."))
    })
    outgoing.once("socket", incoming => {
      socket = incoming
      if (verifiedSocket && socket !== verifiedSocket) {
        // The agent opened a new socket. No headers or body have been sent to it.
        outgoing.destroy(new JardSDKError("unverified-service", "Jard disconnected after endpoint verification."))
        return
      }
      outgoing.end(body)
    })
  })
}

async function verifiedExchange(record, path, options = {}) {
  const agent = new Agent({ keepAlive: true, maxSockets: 1 })
  try {
    const challenge = randomBytes(16).toString("hex")
    const { data, socket } = await exchange(record.port, agent, `${API}/identify?challenge=${challenge}`, { signal: options.signal })
    const binding = [record.apiVersion, record.pid, record.port, record.libraryId, challenge].join(":")
    const expected = createHmac("sha256", record.token).update(binding).digest()
    if (!/^[0-9a-f]{64}$/.test(data?.proof) || !timingSafeEqual(Buffer.from(data.proof, "hex"), expected)) {
      throw new JardSDKError("unverified-service", "Could not verify the owner of the local Jard endpoint.")
    }
    const result = await exchange(record.port, agent, API + path, { ...options, token: record.token, verifiedSocket: socket })
    return result.data
  } finally {
    agent.destroy()
  }
}

function clientFor(record) {
  return Object.freeze({
    pid: record.pid,
    apiVersion: record.apiVersion,
    health: (options) => verifiedExchange(record, "/health", options),
    capabilities: (options) => verifiedExchange(record, "/capabilities", options),
    models: Object.freeze({ list: async (options) => (await verifiedExchange(record, "/models", options)).models }),
    async transcribe(audio, { languages = [], signal, timeoutMs = 180000 } = {}) {
      const body = audio instanceof ArrayBuffer ? Buffer.from(audio)
        : audio instanceof Uint8Array ? Buffer.from(audio.buffer, audio.byteOffset, audio.byteLength) : null
      if (!body?.byteLength) throw new JardSDKError("invalid-audio", "Pass the recorded audio as non-empty bytes.")
      if (!Array.isArray(languages) || languages.some(language => typeof language !== "string")) {
        throw new JardSDKError("invalid-language", "Pass the language codes as an array.")
      }
      if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 2147483647) {
        throw new JardSDKError("invalid-timeout", "Pass a positive request timeout in milliseconds.")
      }
      const query = new URLSearchParams(languages.map(language => ["language", language]))
      return verifiedExchange(record, "/transcriptions" + (query.size ? `?${query}` : ""), { body, signal, timeoutMs })
    },
  })
}

function recordingFor(record, { id, ownerToken, sampleRate }) {
  if (typeof id !== "string" || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(id)
      || typeof ownerToken !== "string" || !/^[0-9a-f]{64}$/.test(ownerToken) || !Number.isInteger(sampleRate) || sampleRate < 1) {
    throw new JardSDKError("invalid-response", "Jard returned an invalid recording handle.")
  }
  const send = (action, method, suffix = "") => verifiedExchange(record, `/dictations/${id}/${action}${suffix}`, { owner: ownerToken, method })
  let finished = false
  let timer
  let terminal
  const schedule = () => { timer = setTimeout(heartbeat, 3000).unref() }
  const stop = () => { finished = true; clearTimeout(timer) }
  const heartbeat = async () => {
    try {
      await send("heartbeat", "POST")
    } catch (error) {
      if (error.code === "dictation-not-found") stop()
    } finally {
      if (!finished) schedule()
    }
  }
  schedule()

  async function* observe(action) {
    let cursor = 0
    while (!finished) {
      let page
      try {
        page = await send(action, "GET", `?cursor=${cursor}`)
      } catch (error) {
        if (error.code === "dictation-not-found") return
        throw error
      }
      if (finished || page.done) return
      if (!Number.isInteger(page.cursor) || page.cursor < cursor) {
        throw new JardSDKError("invalid-response", "Jard returned an invalid recording cursor.")
      }
      if (page.cursor > cursor) {
        cursor = page.cursor
        if (action === "levels") {
          yield { rmsDb: page.rmsDb, peakDb: page.peakDb }
        } else {
          const bytes = Buffer.from(page.samples, "base64")
          yield new Float32Array(new Uint8Array(bytes).buffer)
        }
      }
      await delay(action === "audio" ? 65 : 80)
    }
  }

  async function end(action) {
    let result = await send(action, "POST")
    stop()
    const deadline = performance.now() + 185_000
    while (result.phase === "finishing" && performance.now() < deadline) {
      await delay(180)
      result = await send(action, "POST")
    }
    if (result.phase === "finishing") throw new JardSDKError("timeout", "Jard is still finishing the recording.")
    return result
  }
  const complete = action => terminal ??= end(action).catch(error => {
    terminal = undefined
    if (error.code === "dictation-not-found") stop()
    throw error
  })
  return Object.freeze({
    id, sampleRate, levels: observe("levels"), audio: observe("audio"),
    async finish() {
      const result = await complete("finish")
      if (result.phase === "cancelled") throw new JardSDKError("dictation-cancelled", "The recording was discarded.")
      if (result.phase !== "finished" || typeof result.transcript !== "string" || !Number.isInteger(result.durationMs)) {
        throw new JardSDKError("invalid-response", "Jard returned an invalid recording result.")
      }
      return { transcript: result.transcript, durationMs: result.durationMs }
    },
    async cancel() { await complete("cancel") },
  })
}

export async function connect({ library = process.env.RUMBLE_DATA_DIR || join(homedir(), "Library", "Application Support", "Rumble"),
  signal } = {}) {
  const record = await discover(library)
  await verifiedExchange(record, "/health", { signal })
  return Object.freeze({
    ...clientFor(record),
    dictation: Object.freeze({
      async start({ source = "SDK", languages = [], signal } = {}) {
        if (typeof source !== "string" || source.length > 80 || !source.trim()) {
          throw new JardSDKError("invalid-source", "Pass a recording source name of up to 80 characters.")
        }
        if (!Array.isArray(languages) || languages.some(language => typeof language !== "string")) {
          throw new JardSDKError("invalid-language", "Pass the language codes as an array.")
        }
        const query = new URLSearchParams({ source })
        for (const language of languages) query.append("language", language)
        const take = await verifiedExchange(record, "/dictations?" + query, { method: "POST", signal, timeoutMs: 12_000 })
        return recordingFor(record, take)
      },
    }),
  })
}

function readReady(child, exited, signal, timeoutMs) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0)
    let finished = false
    const finish = (error, record) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      signal?.removeEventListener("abort", abort)
      child.stdout.off("data", receive)
      child.stdout.resume()
      if (error) reject(error)
      else resolve(record)
    }
    const abort = () => finish(signal.reason || new JardSDKError("aborted", "The embedded Jard startup was cancelled."))
    const receive = chunk => {
      if (buffer.length + chunk.length > 4096) {
        finish(new JardSDKError("invalid-handshake", "Jard returned an invalid embedded startup record."))
        return
      }
      buffer = Buffer.concat([buffer, chunk])
      const newline = buffer.indexOf(10)
      if (newline < 0) return
      try {
        const record = JSON.parse(buffer.subarray(0, newline).toString("utf8"))
        if (record.type !== "ready" || record.pid !== child.pid || record.apiVersion !== SDK_VERSION
            || !Number.isInteger(record.port) || record.port < 1 || record.port > 65535
            || record.url !== `http://127.0.0.1:${record.port}` || !/^[0-9a-f]{64}$/.test(record.token)
            || !/^[0-9a-f]{64}$/.test(record.libraryId) || buffer.subarray(newline + 1).toString("utf8").trim()) throw new Error()
        finish(undefined, record)
      } catch {
        finish(new JardSDKError("invalid-handshake", "Jard returned an invalid embedded startup record."))
      }
    }
    const timer = setTimeout(() => finish(new JardSDKError("startup-timeout", "The embedded Jard helper did not become ready.")), timeoutMs)
    signal?.addEventListener("abort", abort, { once: true })
    void exited.then(() => finish(new JardSDKError("startup-failed", "The embedded Jard helper stopped before it was ready. Check that Jard’s model is installed.")))
    child.stdout.on("data", receive)
    if (signal?.aborted) abort()
  })
}

async function exitsWithin(exited, milliseconds) {
  let timer
  try {
    return await Promise.race([exited.then(() => true), new Promise(resolve => {
      timer = setTimeout(() => resolve(false), milliseconds)
    })])
  } finally {
    clearTimeout(timer)
  }
}

export async function create({ command = [DEFAULT_HELPER, "service", "--embedded"], library, signal, startupTimeoutMs = 60_000 } = {}) {
  signal?.throwIfAborted()
  if (!Array.isArray(command) || !command.length || command.some(part => typeof part !== "string" || !part)) {
    throw new JardSDKError("invalid-command", "Pass an executable and its arguments to start embedded Jard.")
  }
  if (!Number.isInteger(startupTimeoutMs) || startupTimeoutMs < 1 || startupTimeoutMs > 2147483647) {
    throw new JardSDKError("invalid-timeout", "Pass a positive startup timeout in milliseconds.")
  }
  const child = spawn(command[0], command.slice(1), {
    detached: process.platform !== "win32", windowsHide: true, stdio: ["pipe", "pipe", "ignore"],
    env: library === undefined ? process.env : { ...process.env, RUMBLE_DATA_DIR: library },
  })
  child.stdin.on("error", () => {})
  const exited = new Promise(resolve => {
    child.once("exit", resolve)
    child.once("error", resolve)
  })
  let closing
  const close = () => closing ??= (async () => {
    child.stdin.end()
    if (await exitsWithin(exited, 5500)) return
    try {
      if (process.platform === "win32") child.kill("SIGKILL")
      else process.kill(-child.pid, "SIGKILL")
    } catch (error) {
      if (error.code !== "ESRCH") throw new JardSDKError("shutdown-failed", "Could not stop the owned Jard helper.")
    }
    if (!await exitsWithin(exited, 1500)) {
      throw new JardSDKError("shutdown-failed", "The owned Jard helper did not exit.")
    }
  })()
  try {
    const record = await readReady(child, exited, signal, startupTimeoutMs)
    const health = await verifiedExchange(record, "/health", { signal })
    if (health?.status !== "ready") throw new JardSDKError("model-unavailable", "The embedded Jard model is unavailable.")
    const client = clientFor(record)
    return Object.freeze({
      ...client,
      close,
      async transcribe(audio, options) {
        options?.signal?.throwIfAborted()
        try {
          return await client.transcribe(audio, options)
        } catch (error) {
          if (options?.signal?.aborted) await close()
          throw error
        }
      },
    })
  } catch (error) {
    await close()
    throw error
  }
}
