import { useRef, useState, type PointerEvent } from "react"
import { Dialog } from "radix-ui"
import type { ScreenArea } from "@/portable-capture"
import { Button } from "./ui/button"

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

export function ScreenAreaDialog({ size, imageUrl, pending, error, action, onCancel, onRefresh, onConfirm }: {
  size: { width: number; height: number }
  imageUrl: string
  action: "record" | "capture"
  pending: boolean
  error: string
  onCancel: () => void
  onRefresh: () => void
  onConfirm: (area: ScreenArea) => void
}) {
  const { width, height } = size
  const [area, setArea] = useState<ScreenArea>(() => ({
    x: Math.floor(width / 4), y: Math.floor(height / 4),
    width: Math.ceil(width / 2), height: Math.ceil(height / 2),
  }))
  const [image, setImage] = useState<"ready" | "error">()
  const drag = useRef<{ x: number; y: number; previous: ScreenArea; pointer: number }>(undefined)
  const valid = area.width >= 2 && area.height >= 2

  function point(event: PointerEvent<HTMLDivElement>) {
    const bounds = event.currentTarget.getBoundingClientRect()
    return {
      x: clamp(Math.round((event.clientX - bounds.left) * width / bounds.width), 0, width),
      y: clamp(Math.round((event.clientY - bounds.top) * height / bounds.height), 0, height),
    }
  }

  function fromDrag(event: PointerEvent<HTMLDivElement>, finished = false) {
    if (drag.current?.pointer !== event.pointerId) return
    const end = point(event)
    const { x, y } = drag.current
    const next = { x: Math.min(x, end.x), y: Math.min(y, end.y), width: Math.abs(end.x - x), height: Math.abs(end.y - y) }
    setArea(finished && (next.width < 2 || next.height < 2) ? drag.current.previous : next)
  }

  return <Dialog.Root open onOpenChange={open => { if (!open && !pending) onCancel() }}>
    <Dialog.Portal>
      <Dialog.Overlay className="note-language-scrim" />
      <Dialog.Content className="note-language-confirm screen-area-dialog" onInteractOutside={event => event.preventDefault()}
        onEscapeKeyDown={event => { if (pending) event.preventDefault() }}>
        <Dialog.Title>{action === "record" ? "Select an area to record" : "Select a screenshot area"}</Dialog.Title>
        <Dialog.Description>Drag to select an area. Use arrow keys to move it and Shift with arrow keys to resize it.</Dialog.Description>
        <div className="screen-area-preview" role="group" aria-label="Display preview" tabIndex={0}
          style={{ width: `min(${width}px, 100%, calc(64dvh * ${width / height}))` }}
          onPointerDown={event => {
            if (event.button !== 0 || image !== "ready" || pending || drag.current) return
            const start = point(event)
            drag.current = { ...start, previous: area, pointer: event.pointerId }
            event.currentTarget.setPointerCapture(event.pointerId)
            setArea({ ...start, width: 0, height: 0 })
          }}
          onPointerMove={fromDrag}
          onPointerUp={event => {
            fromDrag(event, true)
            if (drag.current?.pointer === event.pointerId) drag.current = undefined
          }}
          onLostPointerCapture={() => {
            if (drag.current) setArea(drag.current.previous)
            drag.current = undefined
          }}
          onKeyDown={event => {
            if (image !== "ready" || pending || !valid) return
            const dx = event.key === "ArrowLeft" ? -8 : event.key === "ArrowRight" ? 8 : 0
            const dy = event.key === "ArrowUp" ? -8 : event.key === "ArrowDown" ? 8 : 0
            if (!dx && !dy) return
            event.preventDefault()
            setArea(rect => event.shiftKey ? {
              ...rect, width: clamp(rect.width + dx, 2, width - rect.x), height: clamp(rect.height + dy, 2, height - rect.y),
            } : { ...rect, x: clamp(rect.x + dx, 0, width - rect.width), y: clamp(rect.y + dy, 0, height - rect.height) })
          }}>
          <img src={imageUrl} alt="Preview of the shared display" draggable={false} width={width} height={height}
            onLoad={() => setImage("ready")} onError={() => setImage("error")} />
          {image === "ready" && <div className="screen-area-selection" aria-hidden="true" style={{
            left: `${area.x / width * 100}%`, top: `${area.y / height * 100}%`,
            width: `${area.width / width * 100}%`, height: `${area.height / height * 100}%`,
          }} />}
        </div>
        {(error || image === "error") && <p role="alert">{error || "Could not load the display preview."}</p>}
        <div className="note-language-actions">
          <span className="screen-area-size">{valid ? `${area.width} × ${area.height} px` : "Drag to select"}</span>
          <Button size="sm" variant="ghost" disabled={pending} onClick={onRefresh} title="Refresh in 3 seconds so you can switch windows">Refresh preview</Button>
          <Button size="sm" variant="ghost" disabled={pending} onClick={onCancel}>Cancel</Button>
          <Button size="sm" disabled={pending || image !== "ready" || !valid} onClick={() => onConfirm(area)}>
            {action === "record" ? "Record area" : "Capture area"}
          </Button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
