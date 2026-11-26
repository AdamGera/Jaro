import type { SpeakerSegment, Transcript, WordTiming } from "@/audio"

export const PAUSE_DIVIDER_SECONDS = 30
const SENTENCE_END = /[.?!…؟。！？]["'’”»)]*$/
const UNTIMED_TOKEN = /\S+/g

export function mergeTranscriptSegments(segments: SpeakerSegment[]): SpeakerSegment[] {
  const turns: SpeakerSegment[] = []
  for (const segment of segments) {
    const previous = turns.at(-1)
    const gap = previous?.end != null && Number.isFinite(segment.start) ? segment.start - previous.end : 0
    if (previous && previous.speaker === segment.speaker && gap < PAUSE_DIVIDER_SECONDS) {
      turns[turns.length - 1] = {
        ...previous,
        end: segment.end == null ? previous.end : previous.end == null ? segment.end : Math.max(previous.end, segment.end),
        text: `${previous.text.trimEnd()} ${segment.text.trimStart()}`.trim(),
      }
    } else {
      turns.push(segment)
    }
  }
  return turns
}

export function normalizeTranscriptSegments(segments: SpeakerSegment[], words: WordTiming[]): SpeakerSegment[] {
  const merged = mergeTranscriptSegments(segments)
  if (!merged.length || !words.length) return merged
  const result: SpeakerSegment[] = []
  let index = 0
  for (const turn of merged) {
    let from = 0
    let splitStart = 0
    let currentStart = turn.start
    let lastEnd: number | null = null
    let lastPos = 0
    let split = false
    while (index < words.length && from < turn.text.length) {
      const word = words[index]
      if (word.speaker && turn.speaker && word.speaker !== turn.speaker) break
      if (!word.text) { index++; continue }
      const pos = turn.text.indexOf(word.text, from)
      if (pos < 0) break
      if (word.start != null && lastEnd != null && word.start - lastEnd >= PAUSE_DIVIDER_SECONDS && lastPos > splitStart) {
        const piece = turn.text.slice(splitStart, pos).trim()
        if (piece) {
          result.push({ ...turn, start: currentStart, end: lastEnd, text: piece })
          splitStart = pos
          currentStart = word.start
          split = true
        }
      }
      from = pos + word.text.length
      lastPos = from
      if (word.end != null) lastEnd = word.end
      index++
    }
    if (split) {
      const rest = turn.text.slice(splitStart).trim()
      if (rest) result.push({ ...turn, start: currentStart, text: rest })
    } else {
      result.push(turn)
    }
  }
  return result
}

// Offsets always address the original segment string, including spaces and punctuation.
export type TranscriptSpan = { from: number; to: number; index?: number; seek?: number }
type Utterance = { from: number; spans: TranscriptSpan[] }

function pushUntimedBubbles(text: string, from: number, bubbles: Utterance[]) {
  let bubble = bubbles[bubbles.length - 1]
  let spanStart = from
  let previousToken = ""
  UNTIMED_TOKEN.lastIndex = from
  for (let match = UNTIMED_TOKEN.exec(text); match; match = UNTIMED_TOKEN.exec(text)) {
    const pos = match.index
    const length = pos - bubble.from
    const sentence = SENTENCE_END.test(previousToken)
    if (length > 0 && ((sentence && length >= 105) || length >= 390)) {
      if (spanStart < pos) bubble.spans.push({ from: spanStart, to: pos })
      bubble = { from: pos, spans: [] }
      bubbles.push(bubble)
      spanStart = pos
    }
    previousToken = match[0]
  }
  if (spanStart < text.length) bubble.spans.push({ from: spanStart, to: text.length })
}

export function transcriptTurns({ segments, words, text }: Transcript) {
  if (!segments.length && !text) return []
  const turns = segments.length ? segments : [{ text, start: 0, end: null, speaker: undefined }]
  const nextAnchor: (number | null)[] = []
  for (let i = words.length - 1; i >= 0; i--) {
    const sameTurn = i + 1 < words.length && words[i].speaker === words[i + 1].speaker
    nextAnchor[i] = words[i].start ?? (sameTurn ? nextAnchor[i + 1] : null)
  }
  let index = 0
  return turns.map(turn => {
    const bubbles: Utterance[] = [{ from: 0, spans: [] }]
    let bubble = bubbles[0]
    let from = 0
    let lastTime: number | null = null
    while (index < words.length && from < turn.text.length) {
      const word = words[index]
      if (word.speaker && turn.speaker && word.speaker !== turn.speaker) break
      if (!word.text) { index++; continue }
      const pos = turn.text.indexOf(word.text, from)
      if (pos < 0) break
      const block = word.blockStart ?? turn.start
      const next = nextAnchor[index]
      const nearby = lastTime !== null && lastTime >= block ? lastTime : next !== null && next < block + 25 ? next : block
      const seek = word.start ?? nearby
      const previous = words[index - 1]
      const paused = word.start != null && previous?.end != null && word.start - previous.end >= 1.4
      const length = pos - bubble.from
      const sentence = SENTENCE_END.test(previous?.text ?? "")
      if (length > 0 && (paused || (sentence && length >= 105) || length >= 390)) {
        if (from < pos) bubble.spans.push({ from, to: pos })
        bubble = { from: pos, spans: [] }
        bubbles.push(bubble)
      } else if (from < pos) bubble.spans.push({ from, to: pos })
      from = pos + word.text.length
      bubble.spans.push({ from: pos, to: from, index, seek })
      lastTime = word.start ?? lastTime
      index++
    }
    if (from < turn.text.length) pushUntimedBubbles(turn.text, from, bubbles)
    return { ...turn, bubbles }
  })
}
