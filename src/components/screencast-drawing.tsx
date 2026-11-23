import { useEffect, useRef, useState, type PointerEvent } from "react"
import { Dialog } from "radix-ui"
import { Button } from "./ui/button"

type InkPoint = [x: number, y: number, time: number]
type InkStroke = { id: number; points: InkPoint[] }

function strokePath(points: InkPoint[]) {
  const path = points.length === 1 ? [points[0], points[0]] : points
  return path.map(([x, y], index) => `${index ? "L" : "M"}${x * 1000},${y * 1000}`).join(" ")
}

export function ScreencastDrawing({ url, view, seconds, onDone }: { url: string; view: string; seconds: number; onDone: () => void }) {
  const epoch = useRef(0)
  const last = useRef(0)
  const nextId = useRef(0)
  const active = useRef<{ pointer: number; points: InkPoint[] }>(undefined)
  const [canvas, setCanvas] = useState<{ width: number; height: number }>()
  const [points, setPoints] = useState<InkPoint[]>([])
  const [finished, setFinished] = useState<InkStroke[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const image = useRef<HTMLImageElement>(null)

  useEffect(() => { epoch.current = performance.now() / 1000 - seconds }, [seconds])
  useEffect(() => {
    // Some WebKit builds update the natural size before reporting load for MJPEG.
    const timer = window.setInterval(() => {
      const picture = image.current
      if (!picture?.naturalWidth || !picture.naturalHeight) return
      setCanvas({ width: picture.naturalWidth, height: picture.naturalHeight })
      window.clearInterval(timer)
    }, 100)
    return () => window.clearInterval(timer)
  }, [])

  function now() { return Math.max(last.current, performance.now() / 1000 - epoch.current) }

  function point(event: PointerEvent<HTMLDivElement>): InkPoint {
    const bounds = event.currentTarget.getBoundingClientRect()
    const unit = (value: number) => Math.max(0, Math.min(1, value))
    return [unit((event.clientX - bounds.left) / bounds.width), unit((event.clientY - bounds.top) / bounds.height), now()]
  }

  function extend(event: PointerEvent<HTMLDivElement>) {
    if (active.current?.pointer !== event.pointerId) return
    const samples = active.current.points
    const end = point(event)
    const previous = samples[samples.length - 1]
    if (Math.hypot(end[0] - previous[0], end[1] - previous[1]) < .002) return
    end[2] = Math.max(end[2], previous[2])
    if (samples.length < 1024) samples.push(end)
    else samples[samples.length - 1] = end
    setPoints([...samples])
  }

  async function save(stroke: InkPoint[]) {
    const time = Math.max(now(), stroke[stroke.length - 1]?.[2] ?? 0)
    last.current = time
    setBusy(true)
    setError("")
    try {
      const response = await fetch(url + "/ink", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ time, points: stroke }),
      })
      if (!response.ok) {
        const error = await response.json().catch(() => ({}))
        throw new Error(typeof error.detail === "string" ? error.detail : "Could not save this drawing.")
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save this drawing.")
    } finally {
      setBusy(false)
    }
  }

  function end(event: PointerEvent<HTMLDivElement>) {
    if (active.current?.pointer !== event.pointerId) return
    extend(event)
    const stroke = active.current.points
    active.current = undefined
    setPoints([])
    const item = { id: nextId.current++, points: stroke }
    setFinished(existing => [...existing.slice(-31), item])
    void save(stroke)
  }

  async function close() {
    setBusy(true)
    await fetch(url + "/preview", { method: "DELETE" }).catch(() => {})
    onDone()
  }

  return <Dialog.Root open onOpenChange={open => { if (!open && !busy) void close() }}>
    <Dialog.Portal>
      <Dialog.Overlay className="note-language-scrim" />
      <Dialog.Content className="note-language-confirm screen-area-dialog" onInteractOutside={event => event.preventDefault()}
        onEscapeKeyDown={event => { if (busy) event.preventDefault() }}>
        <Dialog.Title>Draw on recording</Dialog.Title>
        <Dialog.Description>Drag on the live preview to draw. Strokes fade after you lift the pointer.</Dialog.Description>
        <div className="screen-area-preview screencast-drawing"
          style={{ width: canvas ? `min(100%, calc(64dvh * ${canvas.width / canvas.height}))` : "100%" }}
          onPointerDown={event => {
            if (!canvas || busy || active.current || event.button !== 0) return
            const first = point(event)
            active.current = { pointer: event.pointerId, points: [first] }
            event.currentTarget.setPointerCapture(event.pointerId)
            setPoints([first])
          }}
          onPointerMove={extend} onPointerUp={end}
          onLostPointerCapture={event => {
            if (active.current?.pointer !== event.pointerId) return
            active.current = undefined
            setPoints([])
          }}>
          <img ref={image} src={`${url}/preview?view=${view}`} alt="Live preview of the recorded area" draggable={false}
            style={{ visibility: canvas ? "visible" : "hidden", position: canvas ? undefined : "absolute" }}
            onError={() => { setCanvas(undefined); setError("The live preview is unavailable. The screen recording is still running.") }} />
          <svg viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
            {finished.map(stroke => <path key={stroke.id} className="screencast-finished-stroke" d={strokePath(stroke.points)}
              vectorEffect="non-scaling-stroke" />)}
            {points.length > 0 && <path d={strokePath(points)} vectorEffect="non-scaling-stroke" />}
          </svg>
        </div>
        {!canvas && !error && <p role="status">Opening live preview…</p>}
        {error && <p role="alert">{error}</p>}
        <div className="note-language-actions">
          <Button size="sm" variant="ghost" disabled={!canvas || busy} onClick={() => { setFinished([]); void save([]) }}>Clear</Button>
          <Button size="sm" disabled={busy} onClick={() => void close()}>Done drawing</Button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
