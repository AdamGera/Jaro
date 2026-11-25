export class JardSDKError extends Error {
  constructor(code: string, message: string, status?: number)
  readonly code: string
  readonly status?: number
}

export type RequestOptions = { signal?: AbortSignal }
export type TranscriptionOptions = RequestOptions & { languages?: string[]; timeoutMs?: number }
export type SpeechModel = {
  id: string
  ready: boolean
  supportsLanguageDetection: boolean
  languages: { code: string; name: string }[]
}
export type JardClient = {
  readonly pid: number
  readonly apiVersion: number
  health(options?: RequestOptions): Promise<{ status: "loading" | "ready" | "error" | "stopping"; model: string }>
  capabilities(options?: RequestOptions): Promise<{
    apiVersion: number; transcription: boolean; serviceCapture: boolean; maxUploadBytes: number; maxAudioSeconds: number
  }>
  models: { list(options?: RequestOptions): Promise<SpeechModel[]> }
  transcribe(audio: Uint8Array | ArrayBuffer, options?: TranscriptionOptions): Promise<{ transcript: string; durationMs: number }>
}

export type DesktopRecording = {
  readonly id: string
  readonly sampleRate: number
  levels: AsyncIterable<{ rmsDb: number; peakDb: number }>
  audio: AsyncIterable<Float32Array>
  finish(): Promise<{ transcript: string; durationMs: number }>
  cancel(): Promise<void>
}
export type DesktopJardClient = JardClient & {
  dictation: { start(options?: RequestOptions & { source?: string; languages?: string[] }): Promise<DesktopRecording> }
}
export function connect(options?: RequestOptions & { library?: string }): Promise<DesktopJardClient>
export type EmbeddedJardClient = JardClient & { close(): Promise<void> }
export type ModelPreparationProgress = { phase: "checking" | "loading" }
  | { phase: "installing"; completedBytes: number; totalBytes: number }
export function create(options?: RequestOptions & {
  command?: string[]; library?: string; startupTimeoutMs?: number
  prepare?: boolean | { from?: string; repair?: boolean }
  onProgress?: (progress: ModelPreparationProgress) => void
}): Promise<EmbeddedJardClient>
