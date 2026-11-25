import { EMPTY_TRANSCRIPT, previewFrames, type AudioFrames, type Transcript } from "./audio"
import type { RecordingDraft, RecordingMode } from "./desktop"

const endpoint = "/api/portable/recording"

export const hasPortableAudio = document.documentElement.hasAttribute("data-jaro-portable-audio")

export type PortableRecording = AudioFrames & { captureId: string; close: () => void }
export type SourceLevels = { microphone: number; computerAudio: number }
export type CaptureTarget = { id: string; label: string }
export type ScreenArea = { x: number; y: number; width: number; height: number }

export async function portableScreenTargets(source: "display" | "window", signal?: AbortSignal): Promise<CaptureTarget[]> {
  const response = await fetch(`/api/portable/screencast/targets?source=${source}`, { cache: "no-store", signal })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === "string" ? error.detail : "Could not list available screen sources.")
  }
  return (await response.json()).targets
}

async function request(path = "", options?: RequestInit) {
  const response = await fetch(endpoint + path, { cache: "no-store", ...options })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === "string" ? error.detail : "Could not access the desktop audio recorder.")
  }
  return response
}

export async function openPortableRecording(
  mode: RecordingMode,
  onFrame: (audio: AudioFrames, samples: Float32Array, levels?: SourceLevels) => void,
  onEnded: () => void,
  options: { meeting: boolean; live: boolean; diarization: boolean; languages: readonly string[] },
): Promise<PortableRecording> {
  const parameters = new URLSearchParams({ mode })
  if (options.meeting) parameters.set("meeting", "true")
  if (options.live) parameters.set("live", "true")
  if (options.diarization) parameters.set("diarization", "true")
  for (const code of options.languages) parameters.append("language", code)
  const { id, sampleRate }: { id: string; sampleRate: number } = await (await request(`?${parameters}`, { method: "POST" })).json()
  return followPortableRecording(id, sampleRate, mode, onFrame, onEnded)
}

export function followPortableRecording(
  id: string, sampleRate: number, mode: RecordingMode,
  onFrame: (audio: AudioFrames, samples: Float32Array, levels?: SourceLevels) => void,
  onEnded: () => void,
): PortableRecording {
  const history = previewFrames(sampleRate)
  const controller = new AbortController()
  let timer: number

  async function poll() {
    try {
      const response = await request(`/${id}/frames?offset=${history.length}`, { signal: controller.signal })
      const offset = Number(response.headers.get("X-Audio-Offset"))
      const bytes = new DataView(await response.arrayBuffer())
      if (controller.signal.aborted) return
      if (bytes.byteLength) {
        const samples = new Float32Array(bytes.byteLength / 2)
        for (let index = 0; index < samples.length; index++) samples[index] = bytes.getInt16(index * 2, true) / 32_768
        history.append(samples, offset)
        const levels = mode === "both" ? {
          microphone: Number(response.headers.get("X-Microphone-Level")),
          computerAudio: Number(response.headers.get("X-Computer-Audio-Level")),
        } : undefined
        onFrame(history, samples, levels)
      }
      if (controller.signal.aborted) return
      if (response.headers.get("X-Audio-Finished") === "1") onEnded()
      else timer = window.setTimeout(poll, 100)
    } catch {
      if (!controller.signal.aborted) onEnded()
    }
  }

  timer = window.setTimeout(poll, 0)
  return {
    captureId: id,
    sampleRate,
    get length() { return history.length },
    read: history.read,
    close() {
      controller.abort()
      window.clearTimeout(timer)
      history.clear()
    },
  }
}

export async function setPortableRecordingPreview(id: string, live: boolean) {
  await request(`/${id}/preview`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ live }),
  })
}

export function portableRecordingPreview(id: string, onText: (transcript: Transcript) => void, onSlow: () => void) {
  const controller = new AbortController()
  let nextCheck = 0
  let pending = false
  let failures = 0
  let through = 0
  return {
    async update() {
      if (controller.signal.aborted || pending || performance.now() < nextCheck) return
      pending = true
      nextCheck = performance.now() + 900
      try {
        const response = await request(`/${id}/preview?after=${through}`, { signal: controller.signal })
        failures = 0
        if (response.status === 204) return
        const data: { through: number; text: string; segments: Transcript["segments"] } = await response.json()
        if (controller.signal.aborted) return
        through = data.through
        onText({ ...EMPTY_TRANSCRIPT, text: data.text, segments: data.segments })
      } catch {
        if (!controller.signal.aborted && ++failures === 2) onSlow()
      } finally {
        pending = false
      }
    },
    stop() { controller.abort() },
  }
}

export async function finishPortableRecording(
  id: string, options: { name?: string; languages: string[]; diarization: boolean; live?: boolean; session?: string },
): Promise<{ id: string }> {
  const params = new URLSearchParams()
  for (const code of options.languages) params.append("language", code)
  if (options.name) params.set("name", options.name)
  if (options.diarization) params.set("diarization", "true")
  if (options.live) params.set("live", "true")
  if (options.session) params.set("session", options.session)
  return (await request(`/${id}/finish?${params}`, { method: "POST" })).json()
}

export async function portableRecordingDrafts(signal?: AbortSignal): Promise<RecordingDraft[]> {
  return (await request("/drafts", { signal })).json()
}

export async function discardPortableRecording(id: string) {
  await request(`/${id}`, { method: "DELETE" })
}
