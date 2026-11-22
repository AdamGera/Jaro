import { previewFrames, type AudioFrames } from "./audio"
import type { RecordingDraft, RecordingMode } from "./desktop"

const endpoint = "/api/portable/recording"

export const hasPortableAudio = document.documentElement.hasAttribute("data-jaro-portable-audio")

export type PortableRecording = AudioFrames & { captureId: string; close: () => void }
export type SourceLevels = { microphone: number; computerAudio: number }

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
): Promise<PortableRecording> {
  const { id, sampleRate }: { id: string; sampleRate: number } = await (await request(`?mode=${mode}`, { method: "POST" })).json()
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
