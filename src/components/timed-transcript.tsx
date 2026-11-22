import { Fragment, memo, useEffect, useMemo, useRef, type CSSProperties, type ReactNode } from "react"
import type { Transcript } from "@/audio"
import { useInterfaceLanguage } from "@/interface-language"
import type { TranscriptMatch } from "@/transcript-search"
import { clock, speakerColor, speakerLabel } from "@/speaker-activity"
import { transcriptTurns } from "./transcript-turns"

type Props = {
  result: Transcript
  meeting: boolean
  listening: boolean
  time: number
  citation?: { seconds: number } | null
  ready: boolean
  seek: (time: number, play?: boolean) => void
  matches?: readonly TranscriptMatch[]
  activeMatch?: number
}

const noMatches: TranscriptMatch[] = []

function reveal(target: HTMLElement, always = false) {
  const area = target.closest<HTMLElement>(".transcript-body")
  if (!area) return
  const box = area.getBoundingClientRect()
  const line = target.getBoundingClientRect()
  const header = area.parentElement?.querySelector(".panel-heading")?.clientHeight ?? 0
  if (always || line.top < box.top + header + 4 || line.bottom > box.bottom - 16) {
    area.scrollTop += line.top - box.top - header - Math.max(12, (box.height - header) / 3)
  }
}

export const TimedTranscript = memo(function TimedTranscript({
  result, meeting, listening, time, ready, seek, citation, matches = noMatches, activeMatch = -1,
}: Props) {
  const { t } = useInterfaceLanguage()
  const root = useRef<HTMLDivElement>(null)
  const elements = useRef<(HTMLButtonElement | null)[]>([])
  const { words } = result
  const turns = useMemo(() => transcriptTurns(result), [result])
  const timed = useMemo(() => words.flatMap((word, index) =>
    word.start == null || word.end == null ? [] : [{ index, start: word.start, end: word.end }]), [words])
  let lo = 0
  let hi = timed.length
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2)
    if (timed[mid].start <= time) lo = mid + 1
    else hi = mid
  }
  const match = timed[lo - 1]
  const active = match && time < match.end ? match.index : -1
  const finding = activeMatch >= 0 && activeMatch < matches.length

  useEffect(() => {
    const word = elements.current[active]
    if (!word) return
    word.setAttribute("aria-current", "true")
    if (!finding) reveal(word)
    return () => word.removeAttribute("aria-current")
  }, [active, result, ready, finding])

  useEffect(() => {
    if (!citation) return
    let nearest = -1
    let distance = Infinity
    for (let i = 0; i < words.length; i++) {
      const start = words[i].start ?? words[i].blockStart
      if (start == null || !elements.current[i]) continue
      const delta = Math.abs(start - citation.seconds)
      if (delta < distance) { distance = delta; nearest = i }
    }
    // A legacy session may have timestamps only at speaker-turn boundaries.
    const turnIndex = Math.max(0, turns.findLastIndex(turn => turn.start <= citation.seconds))
    const target = nearest >= 0 ? elements.current[nearest] :
      root.current?.querySelector<HTMLElement>(`[data-turn="${turnIndex}"]`)
    const focus = target instanceof HTMLButtonElement && target.disabled ? target.closest<HTMLElement>("[data-turn]") : target
    focus?.focus({ preventScroll: true })
    if (target) reveal(target, true)
  }, [citation, words, turns])

  useEffect(() => {
    if (!finding) return
    const marks = root.current?.querySelectorAll<HTMLElement>(`mark[data-match="${activeMatch}"]`)
    if (!marks?.length) return
    for (const mark of marks) mark.dataset.selected = "true"
    reveal(marks[0], true)
    return () => { for (const mark of marks) delete mark.dataset.selected }
  }, [activeMatch, finding, matches, turns, ready])

  // Neither time updates nor moving to the next search result rebuild the transcript.
  const content = useMemo(() => {
    let mi = 0
    let endOfSpeech: number | null = null
    return turns.map((turn, turnIndex) => {
      function highlight(start: number, end: number) {
        while (mi < matches.length && (matches[mi].turn < turnIndex ||
          (matches[mi].turn === turnIndex && matches[mi].end <= start))) mi++
        if (!matches[mi] || matches[mi].turn !== turnIndex || matches[mi].start >= end) return turn.text.slice(start, end)
        const pieces: ReactNode[] = []
        let from = start
        while (mi < matches.length && matches[mi].turn === turnIndex && matches[mi].start < end) {
          const found = matches[mi]
          const begin = Math.max(found.start, from)
          const until = Math.min(found.end, end)
          if (from < begin) pieces.push(turn.text.slice(from, begin))
          pieces.push(<mark key={mi} data-match={mi}>{turn.text.slice(begin, until)}</mark>)
          from = until
          if (found.end > end) break
          mi++
        }
        if (from < end) pieces.push(turn.text.slice(from, end))
        return pieces
      }
      let first = true
      const name = turn.speaker ? speakerLabel(turn.speaker, result.speakerNames) : meeting ? "Speaker" : undefined
      const label = name && meeting ? t(name) : name
      const pause = meeting && endOfSpeech !== null && Number.isFinite(turn.start) ? turn.start - endOfSpeech : 0
      if (meeting) {
        endOfSpeech = turn.end !== null && Number.isFinite(turn.end)
          ? Math.max(endOfSpeech ?? turn.end, turn.end) : null
      }
      return <Fragment key={turnIndex}>
        {pause >= 30 && <p className="meeting-transcript-pause">{t("{time} pause", { time: clock(pause) })}</p>}
        <article className="speaker-turn" data-turn={turnIndex} tabIndex={-1}
          data-side={turn.speaker && turn.speaker % 2 === 0 ? "end" : "start"}
          data-local={meeting && name === "You" ? true : undefined}
          style={turn.speaker ? { "--speaker-color": speakerColor(turn.speaker) } as CSSProperties : undefined}>
          {label && <header className="speaker-heading">
            <span className="turn-badge">{!meeting && <i aria-hidden="true" />}{label}</span>
            <button type="button" disabled={!ready} aria-label={`Play ${label} at ${clock(turn.start)}`}
              onClick={() => seek(turn.start, true)}><time dir="ltr">{clock(turn.start)}</time></button>
          </header>}
          {turn.bubbles.map(bubble => <p key={bubble.from} className="turn-bubble" dir="auto">
            {bubble.spans.map(span => {
              const label = highlight(span.from, span.to)
              const index = span.index
              if (index === undefined || span.seek === undefined) return <Fragment key={span.from}>{label}</Fragment>
              const tabIndex = first ? 0 : -1
              first = false
              const target = span.seek
              return <button key={span.from} type="button" className="transcript-word" data-word={index} tabIndex={tabIndex}
                ref={el => { elements.current[index] = el }} disabled={!ready}
                title={words[index].start == null ? "Play from nearby speech" : undefined}
                onClick={() => seek(target, true)} onKeyDown={event => {
                  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return
                  event.preventDefault()
                  const rtl = getComputedStyle(event.currentTarget).direction === "rtl"
                  const forward = (event.key === "ArrowRight") !== rtl
                  elements.current[index + (forward ? 1 : -1)]?.focus()
                }}>{label}</button>
            })}
          </p>)}
        </article>
      </Fragment>
    })
  }, [turns, matches, ready, seek, words, result.speakerNames, meeting, t])

  return <div ref={root} tabIndex={-1} className={`transcript-text speaker-transcript${meeting ? " meeting-transcript" : ""}`}>
    {content}
    {listening && <p className="meeting-transcript-listening" role="status">{t("Listening…")}</p>}
  </div>
})
