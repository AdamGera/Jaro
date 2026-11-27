import type { SpeakerActivity } from "./speaker-activity"

export const MAX_SECONDS = 3 * 60 * 60
const SAMPLE_RATE = 16_000
const PREVIEW_BUFFER_SECONDS = 12

export type SpeakerSegment = { speaker: number | null; start: number; end: number | null; text: string }
export type WordTiming = { speaker?: number | null; separator?: string; blockStart?: number; start: number | null; end: number | null; text: string }
export type Transcript = {
  text: string
  tentative?: string
  durationSeconds: number | null
  detectedLanguages: string[]
  segments: SpeakerSegment[]
  words: WordTiming[]
  speakerActivity: SpeakerActivity[] | null
  speakerNames?: Record<number, string>
  echoSuppressedUtterances?: number
  transcriptSource?: "final" | "rolling" | "draft"
  interruption?: string
}
export const EMPTY_TRANSCRIPT: Transcript = {
  text: "", durationSeconds: null, detectedLanguages: [], segments: [], words: [], speakerActivity: null,
}

type TranscriptChunk = { text?: string; segments?: SpeakerSegment[] }
type ProgressEvent =
  | { done: true; duration?: number }
  | { error: string }
  | { seconds: number; duration: number | null }

type AudioOptions = { name?: string; live?: boolean; recording?: boolean; diarization?: boolean; session?: string; offset?: number; notesDraft?: string }

export type AudioFrames = {
  sampleRate: number
  readonly length: number
  read: (start: number, end: number) => Float32Array
}

export type Microphone = AudioFrames & {
  stop: () => Promise<Blob>
  close: () => void
}

export function previewFrames(sampleRate: number) {
  const frames: { offset: number; samples: Float32Array }[] = []
  let length = 0
  return {
    sampleRate,
    get length() { return length },
    append(samples: Float32Array, offset = length) {
      frames.push({ offset, samples })
      length = offset + samples.length
      const cutoff = length - PREVIEW_BUFFER_SECONDS * sampleRate
      while (frames[0] && frames[0].offset + frames[0].samples.length <= cutoff) frames.shift()
    },
    read(start: number, end: number) {
      const result = new Float32Array(end - start)
      for (const { offset, samples } of frames) {
        const from = Math.max(offset, start)
        const to = Math.min(offset + samples.length, end)
        if (to > from) result.set(samples.subarray(from - offset, to - offset), from - start)
        if (offset + samples.length >= end) break
      }
      return result
    },
    clear() { frames.length = 0 },
  }
}

export async function openMicrophone(
  onFrame: (microphone: Microphone, samples: Float32Array) => void,
  onEnded: () => void,
): Promise<Microphone> {
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode || !window.MediaRecorder) {
    throw new Error("Open Jaro in Safari or Chrome to use the microphone.")
  }

  const context = new AudioContext()
  let stream: MediaStream | undefined
  let source: MediaStreamAudioSourceNode
  let worklet: AudioWorkletNode
  let recorder: MediaRecorder
  try {
    await context.resume()
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } })
    await context.audioWorklet.addModule("/capture-worklet.js")
    source = context.createMediaStreamSource(stream)
    worklet = new AudioWorkletNode(context, "soundfolio-capture", {
      channelCount: 1,
      channelCountMode: "explicit",
    })
    recorder = new MediaRecorder(stream, { audioBitsPerSecond: 96_000 })
  } catch (error) {
    stream?.getTracks().forEach((track) => track.stop())
    void context.close()
    throw error
  }

  const limit = context.sampleRate * MAX_SECONDS
  const history = previewFrames(context.sampleRate)
  const chunks: Blob[] = []
  let closing = false
  let closed = false
  let recordingError: Error | undefined
  const finished = new Promise<void>((resolve) => {
    recorder.addEventListener("stop", () => resolve(), { once: true })
  })
  recorder.addEventListener("dataavailable", ({ data }) => {
    if (!closed && data.size) chunks.push(data)
  })
  recorder.addEventListener("error", () => {
    recordingError = new Error("The browser could not record this audio.")
    if (!closing) onEnded()
  })

  const close = () => {
    if (closed) return
    closing = true
    closed = true
    if (recorder.state !== "inactive") recorder.stop()
    stream.getTracks().forEach((track) => track.stop())
    worklet.port.close()
    source.disconnect()
    worklet.disconnect()
    history.clear()
    chunks.length = 0
    void context.close()
  }

  const microphone: Microphone = {
    sampleRate: context.sampleRate,
    get length() { return history.length },
    read: history.read,
    async stop() {
      closing = true
      if (recorder.state !== "inactive") recorder.stop()
      stream.getTracks().forEach((track) => track.stop())
      try {
        await finished
        if (recordingError && !chunks.length) throw recordingError
        return new Blob(chunks, { type: recorder.mimeType || chunks[0]?.type })
      } finally {
        close()
      }
    },
    close,
  }

  worklet.port.onmessage = ({ data }: MessageEvent<Float32Array>) => {
    if (closing || history.length >= limit) return
    const samples = data.subarray(0, limit - history.length)
    history.append(samples)
    onFrame(microphone, samples)
  }
  for (const track of stream.getAudioTracks()) {
    track.addEventListener("ended", () => {
      if (!closing) onEnded()
    })
  }
  try {
    source.connect(worklet)
    worklet.connect(context.destination)
    recorder.start(1000)
  } catch (error) {
    close()
    throw error
  }
  return microphone
}

async function renderMono(audio: AudioBuffer, length: number): Promise<AudioBuffer> {
  const context = new OfflineAudioContext(1, length, SAMPLE_RATE)
  const source = context.createBufferSource()
  source.buffer = audio
  source.connect(context.destination)
  source.start()
  return context.startRendering()
}

async function transcribe(
  samples: Float32Array,
  sampleRate: number,
  languages: readonly string[],
  options: AudioOptions,
  signal: AbortSignal,
): Promise<TranscriptChunk> {
  let audio = new Float32Array(samples)
  if (sampleRate !== SAMPLE_RATE) {
    const buffer = new AudioBuffer({ length: audio.length, numberOfChannels: 1, sampleRate })
    buffer.copyToChannel(audio, 0)
    const length = Math.ceil(audio.length * SAMPLE_RATE / sampleRate)
    const mono = await renderMono(buffer, length)
    audio = new Float32Array(mono.getChannelData(0))
  }

  const response = await sendAudio("/api/transcribe", audio.buffer, languages, options, signal)
  return response.json()
}

async function sendAudio(
  path: string,
  audio: Blob | ArrayBuffer,
  languages: readonly string[],
  options: AudioOptions,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams()
  for (const language of languages) params.append("language", language)
  if (options.name) params.set("name", options.name)
  if (audio instanceof Blob) {
    params.set("mime", audio.type.split(";")[0])
    params.set("progress_only", "true")
  }
  if (options.live) params.set("live", "true")
  if (options.recording) params.set("recording", "true")
  if (options.diarization) params.set("diarization", "true")
  if (options.session) params.set("session", options.session)
  if (options.offset) params.set("offset", String(options.offset))
  if (options.notesDraft) params.set("notes_draft", options.notesDraft)
  const url = params.size ? `${path}?${params}` : path
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: audio,
    signal,
  }).catch(() => {
    throw new Error("Jaro disconnected. Reopen Jaro and try again.")
  })
  if (!response.ok) {
    const result = await response.json()
    throw new Error(result.detail ?? "Transcription failed.")
  }
  return response
}

export async function transcribeDictation(audio: Blob, languages: readonly string[], signal: AbortSignal): Promise<{ text: string }> {
  if (!audio.size) throw new Error("No microphone audio was captured.")
  const response = await sendAudio("/api/personal-notes/dictation", audio, languages, {}, signal)
  return response.json()
}

export type MeetingBrief = { status: "queued" | "ready" | "error"; title: string | null; summary: string | null; error: string | null }
export type MeetingExport = { status: "pending" | "ready" | "error"; filename: string | null; error: string | null; refreshing?: boolean }

export type SavedSession = {
  personalNotes?: import("./personal-notes").PersonalNotes
  overview?: import("./overview").Overview | null
  id: string; name: string; created: string; status: "complete" | "interrupted" | "processing"
  languageHints: string[]; recording: boolean; diarization: boolean; live: boolean
  audioUrl: string | null; audioPath: string | null; audioName: string; transcript: Transcript
  meeting?: boolean; meetingProvider?: string | null; audioRemovedAt?: string | null
  annotationCustomStorage?: boolean
  annotationStorageUnavailable?: boolean
  meetingTurns?: SpeakerSegment[]
  transcriptEditRevision?: string
  meetingsManaged?: boolean
  finalizationPending?: boolean
  meetingRetentionDays?: number | null
  meetingCalendar?: { title: string; attendees: string[] }
  meetingTitle?: string
  meetingMicrophoneWarning?: "digitalSilence"
  provisionalTitle?: string
  meetingBrief?: MeetingBrief | null
  meetingExport?: MeetingExport | null
  recordingTracks?: Partial<Record<"mic" | "system", string>>
  annotation?: { version: 1; source: "display" | "window" | "area" | "phone"; strokeCount: number; frames: number[]; keyframes?: number[];
    missingVideo?: "noFrames" | "permissionDenied" | "screenInterrupted";
    manifestUrl: string | null; contactSheetUrl: string | null }
  model: string; modelRevision: string; speakerModel: string | null; speakerModelRevision: string | null
}

export function isSettledMeeting(session?: Pick<SavedSession, "meeting" | "status" | "finalizationPending">) {
  return !!session?.meeting && session.status !== "processing" && !session.finalizationPending
}

export class MissingSessionError extends Error {}

export async function readSession(id: string, signal?: AbortSignal): Promise<SavedSession> {
  const response = await fetch(`/api/sessions/${id}`, { cache: "no-store", signal })
  if (response.status === 404) throw new MissingSessionError("Session no longer exists.")
  if (!response.ok) throw new Error("Could not open this session.")
  return response.json()
}

export async function transcribeMedia(
  audio: Blob,
  languages: readonly string[],
  onProgress: (seconds: number, duration: number | null) => void,
  options: AudioOptions,
  onSaved?: (id: string) => void,
): Promise<SavedSession> {
  if (!audio.size) throw new Error("The audio is empty.")
  const response = await sendAudio("/api/transcribe-file", audio, languages, options)
  const reader = response.body?.getReader()
  if (!reader) throw new Error("Jaro could not read the transcription response.")

  const decoder = new TextDecoder()
  const id = response.headers.get("X-Session-ID")
  if (!id) throw new Error("Jaro could not save the session. Restart the local launcher.")
  onSaved?.(id)
  let pending = ""
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) throw new Error("Transcription was interrupted. Try the audio again.")
      pending += decoder.decode(value, { stream: true })
      const lines = pending.split("\n")
      pending = lines.pop() ?? ""
      for (const line of lines) {
        const event: ProgressEvent = JSON.parse(line)
        if ("done" in event || "error" in event) return readSession(id)
        onProgress(event.seconds, event.duration)
      }
    }
  } catch {
    return readSession(id)
  } finally {
    void reader.cancel().catch(() => {})
  }
}

const PAUSE_SPLIT_SECONDS = 30

function appendSpeakers(segments: SpeakerSegment[], next: SpeakerSegment[] = []) {
  for (const segment of next) {
    const last = segments.at(-1)
    const gap = last?.end != null && Number.isFinite(segment.start) ? segment.start - last.end : 0
    if (last && last.speaker === segment.speaker && gap < PAUSE_SPLIT_SECONDS) {
      segments[segments.length - 1] = {
        ...last,
        end: segment.end == null ? last.end : last.end == null ? segment.end : Math.max(last.end, segment.end),
        text: `${last.text.trimEnd()} ${segment.text.trimStart()}`.trim(),
      }
    } else {
      segments.push(segment)
    }
  }
}

export function livePreview(
  languages: readonly string[],
  session: string | undefined,
  onText: (transcript: Transcript) => void,
  onSlow: () => void,
  startSample = 0,
  previous = EMPTY_TRANSCRIPT,
) {
  let committed = previous
  let start = startSample
  let shown = 0
  const controller = new AbortController()
  let retrying = false
  let pending: Promise<void> | undefined

  return {
    update(mic: AudioFrames) {
      if (controller.signal.aborted) return
      const seconds = (mic.length - start) / mic.sampleRate
      if (seconds > 8) {
        controller.abort()
        onSlow()
        return
      }
      if (pending || seconds < 2) return
      const mark = seconds >= 4 ? 4 : 2
      if (shown >= mark) return
      shown = mark
      const end = start + mark * mic.sampleRate
      const options = { session, offset: session ? Math.round(start / mic.sampleRate * SAMPLE_RATE) : undefined }
      pending = transcribe(mic.read(start, end), mic.sampleRate, languages, options, controller.signal)
        .then((transcript) => {
          if (controller.signal.aborted) return
          retrying = false
          const segments = [...committed.segments]
          appendSpeakers(segments, transcript.segments)
          const next = {
            ...EMPTY_TRANSCRIPT,
            text: [committed.text, transcript.text].filter(Boolean).join(" "),
            segments,
          }
          onText(next)
          if (mark === 4) {
            committed = next
            start = end
            shown = 0
          }
        })
        .catch(() => {
          if (controller.signal.aborted) return
          if (retrying) {
            controller.abort()
            onSlow()
          } else {
            retrying = true
            shown = 0
          }
        })
        .finally(() => {
          pending = undefined
        })
    },
    stop() {
      controller.abort()
    },
  }
}
