import type { Transcript } from "./audio"

export type Interval = { start: number; end: number }
export type SpeakerActivity = {
  speaker: number
  intervals: Interval[]
  seconds: number
  share: number
}

const colors = ["#8977cb", "#449587", "#cd8558", "#5b95c3", "#c47092", "#809447", "#ac8065", "#837eac"]
export function speakerColor(speaker: number) {
  return colors[(speaker - 1) % colors.length]
}

// Nemotron channels are numbered in order of first appearance.
export function speakerLabel(speaker: number, names?: Record<number, string>) {
  const raw = names?.[speaker]
  const clean = typeof raw === "string" ? raw.trim() : ""
  if (clean) return clean
  return `Speaker ${speaker >= 1 && speaker <= 26 ? String.fromCharCode(64 + speaker) : speaker}`
}

export function transcriptForClipboard({ text, segments, speakerNames }: Transcript) {
  return segments.length ? segments.map(segment => segment.speaker === null ? segment.text :
    `${speakerLabel(segment.speaker, speakerNames)}\n${segment.text}`).join("\n\n") : text
}

export function clock(seconds: number) {
  const whole = Math.floor(seconds)
  const minutes = Math.floor(whole / 60)
  const rest = String(whole % 60).padStart(2, "0")
  if (minutes < 60) return `${minutes}:${rest}`
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${rest}`
}
