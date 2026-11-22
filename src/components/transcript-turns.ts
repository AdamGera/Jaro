import type { SpeakerSegment, Transcript } from "@/audio"

export function meetingSegments(segments: SpeakerSegment[]) {
  const turns: SpeakerSegment[] = []
  for (const segment of segments) {
    const previous = turns.at(-1)
    if (previous && previous.speaker === segment.speaker && previous.end !== null && segment.end !== null &&
        segment.start - previous.end <= 3) {
      turns[turns.length - 1] = { ...previous, end: Math.max(previous.end, segment.end),
        text: `${previous.text.trimEnd()} ${segment.text.trimStart()}` }
    } else {
      turns.push(segment)
    }
  }
  return turns
}

// Offsets always address the original segment string, including spaces and punctuation.
export type TranscriptSpan = { from: number; to: number; index?: number; seek?: number }
type Utterance = { from: number; spans: TranscriptSpan[] }

export function transcriptTurns({ segments, words, text }: Transcript) {
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
      const sentence = /[.?!…؟。！？]["'’”»)]*$/.test(previous?.text ?? "")
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
    if (from < turn.text.length) bubble.spans.push({ from, to: turn.text.length })
    return { ...turn, bubbles }
  })
}
