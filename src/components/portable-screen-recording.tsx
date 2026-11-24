import { useEffect, useState } from "react"
import { AppWindow, Monitor, Pencil, Scan, Video } from "lucide-react"
import { Popover } from "radix-ui"
import { portableScreenTargets, type CaptureTarget, type ScreenArea } from "@/portable-capture"
import { clock } from "@/speaker-activity"
import { Button } from "./ui/button"
import { ActionTooltip } from "./ui/tooltip"
import { ScreenAreaDialog } from "./screen-area-dialog"
import { ScreencastDrawing } from "./screencast-drawing"
import { useInterfaceLanguage } from "@/interface-language"

const endpoint = "/api/portable/screencast"
export const hasPortableScreenRecording = document.documentElement.hasAttribute("data-jaro-screen-recording")
type Source = "display" | "window" | "area"
type Sources = { sources: Source[]; picker?: "workspace" }
type Preview = { id: string; width: number; height: number }
type Take = {
  phase: "idle" | "choosing" | "previewing" | "selecting" | "starting" | "recording" | "recorded" | "saving" | "cancelled" | "error"
  seconds?: number
  id?: string
  error?: string
  preview?: Preview
  desktopDrawing?: boolean
  desktopDrawingActive?: boolean
  levels?: Partial<Record<"mic" | "system", number>>
}
const idle: Take = { phase: "idle" }
const phaseLabels: Record<Exclude<Take["phase"], "idle">, string> = {
  choosing: "Choose what to share in the desktop picker",
  previewing: "Getting a preview of the display…",
  selecting: "Select a recording area",
  starting: "Starting screen recording…",
  recording: "Recording screen",
  recorded: "Screen recording ready to save",
  saving: "Saving screen recording…",
  cancelled: "Screen recording cancelled",
  error: "Screen recording stopped",
}

async function request(path = "", options?: RequestInit) {
  const response = await fetch(endpoint + path, { cache: "no-store", ...options })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === "string" ? error.detail : "Could not access screen recording.")
  }
  return response
}

export function PortableScreenRecording({ disabled, languageCodes, onBusy, onSave }: {
  disabled: boolean
  languageCodes: string[]
  onBusy: (busy: boolean) => void
  onSave: (id: string) => Promise<void>
}) {
  const { t } = useInterfaceLanguage()
  const [available, setAvailable] = useState<Sources>({ sources: [] })
  const [picker, setPicker] = useState<{ source: Source; targets: CaptureTarget[] }>()
  const [take, setTake] = useState<Take>(idle)
  const [expanded, setExpanded] = useState(false)
  const [microphone, setMicrophone] = useState(false)
  const [computerAudio, setComputerAudio] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [drawing, setDrawing] = useState<{ id: string; view: string }>()
  const active = take.phase !== "idle"
  const poll = ["choosing", "previewing", "selecting", "starting", "recording", "saving"].includes(take.phase)

  useEffect(() => {
    const controller = new AbortController()
    const options = { signal: controller.signal }
    void request("/sources", options).then(response => response.json()).then(setAvailable).catch(() => {})
    void request("", options).then(response => response.json()).then(status => {
      setTake(status)
      onBusy(status.phase !== "idle")
    }).catch(() => {})
    return () => controller.abort()
  }, [onBusy])

  useEffect(() => {
    if (!poll || pending) return
    const controller = new AbortController()
    let timer: number
    async function refresh() {
      try {
        const response = await request("", { signal: controller.signal })
        const status: Take = await response.json()
        if (controller.signal.aborted) return
        if (status.phase === "cancelled") {
          await request("", { method: "DELETE", signal: controller.signal })
          if (controller.signal.aborted) return
          setTake(idle)
          setExpanded(false)
          onBusy(false)
        } else {
          setTake(status)
          onBusy(status.phase !== "idle")
          timer = window.setTimeout(refresh, 350)
        }
      } catch {
        if (!controller.signal.aborted) timer = window.setTimeout(refresh, 1_000)
      }
    }
    timer = window.setTimeout(refresh, 250)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [poll, pending, onBusy])

  async function act(action: Source | "save" | "discard", target?: string) {
    setPending(true)
    setError("")
    onBusy(true)
    try {
      if (action === "discard") {
        await request("", { method: "DELETE" })
        setTake(idle)
        setExpanded(false)
        onBusy(false)
      } else if (action === "save") {
        setTake({ ...take, phase: "saving" })
        const query = new URLSearchParams(languageCodes.map(code => ["language", code]))
        const response = await request(`/finish?${query}`, { method: "POST" })
        const { id } = await response.json()
        setTake(idle)
        setExpanded(false)
        await onSave(id)
        onBusy(false)
      } else {
        const query = new URLSearchParams({ source: action, microphone: String(microphone), computer_audio: String(computerAudio) })
        if (target) query.set("target", target)
        const response = await request(`?${query}`, { method: "POST" })
        setTake(await response.json())
        setPicker(undefined)
      }
    } catch (failure) {
      if (action === "save") setTake(take)
      setError(failure instanceof Error ? failure.message : "Screen recording failed.")
      onBusy(active)
    } finally {
      setPending(false)
    }
  }

  async function choose(source: Source) {
    if (available.picker !== "workspace") {
      await act(source)
      return
    }
    setPending(true)
    setError("")
    try {
      const targets = await portableScreenTargets(source === "area" ? "display" : source)
      setPicker({ source, targets })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not list available screen sources.")
    } finally {
      setPending(false)
    }
  }

  async function submitArea(preview: Preview, area: ScreenArea | null) {
    setPending(true)
    setError("")
    try {
      const response = await request(`/area/${preview.id}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(area),
      })
      setTake(await response.json())
      setExpanded(true)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not submit the area selection.")
    } finally {
      setPending(false)
    }
  }

  async function draw(id: string) {
    if (!take.desktopDrawing && !take.desktopDrawingActive) {
      setDrawing({ id, view: crypto.randomUUID() })
      return
    }
    setPending(true)
    setError("")
    try {
      const response = await request(`/${id}/pen`, { method: take.desktopDrawingActive ? "DELETE" : "POST" })
      const status: Take = await response.json()
      setTake(status)
      if (status.desktopDrawingActive) setExpanded(false)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not open desktop drawing.")
    } finally {
      setPending(false)
    }
  }

  const sources = available.sources
  if (!sources.length && !active) return null
  const canSave = take.phase === "recording" || take.phase === "recorded"
  const preview = take.preview
  return <><Popover.Root open={expanded} onOpenChange={open => { setExpanded(open); if (!open) setPicker(undefined) }}>
    <ActionTooltip label={t(active ? "Manage screen recording" : "Record screen")}>
      <Popover.Trigger asChild>
        <Button size="sm" variant="ghost" aria-label={active ? "Manage screen recording" : "Record screen"}
          disabled={!active && (disabled || pending)}>
          <Video /><span>{active ? "Screen recording" : "Record screen"}</span>
        </Button>
      </Popover.Trigger>
    </ActionTooltip>
    <Popover.Portal>
      <Popover.Content align="end" sideOffset={8} className="portable-screen-recording" aria-label="Screen recording">
        {take.phase !== "idle" ? <>
          <div className="portable-screen-actions">
            <span aria-live="polite">{phaseLabels[take.phase]}</span>
            {canSave && <span className="timer" aria-label="Elapsed time">{clock(take.seconds ?? 0)}</span>}
            {take.phase === "recording" && take.levels && <div className="portable-screen-levels">
              {take.levels.mic !== undefined && <label>Microphone <meter min={0} max={1} value={take.levels.mic} /></label>}
              {take.levels.system !== undefined && <label>Computer audio <meter min={0} max={1} value={take.levels.system} /></label>}
            </div>}
            {canSave && <Button size="sm" disabled={pending} onClick={() => void act("save")}>
              {take.phase === "recording" ? "Stop and save" : "Save recording"}
            </Button>}
            {take.phase === "recording" && take.id && <Button size="sm" variant="outline" disabled={pending}
              title={take.desktopDrawing ? "Press Esc or right-click to finish drawing" : undefined}
              onClick={() => { if (take.id) void draw(take.id) }}>
              <Pencil /> {take.desktopDrawingActive ? "Finish drawing" : take.desktopDrawing ? "Draw on screen" : "Draw"}
            </Button>}
            <Button size="sm" variant="ghost" disabled={pending || take.phase === "saving"} onClick={() => void act("discard")}>
              {take.phase === "error" || take.phase === "cancelled" ? "Dismiss" : canSave ? "Discard" : "Cancel"}
            </Button>
          </div>
          {take.error && !error && <p className="feedback" role="alert">{take.error}</p>}
        </> : picker ? <div className="portable-screen-actions">
          <span className="portable-screen-picker-title">{picker.source === "window" ? "Choose a window to record" : "Choose a display to record"}</span>
          <div className="portable-screen-targets">
            {picker.targets.map(target => <Button key={target.id} size="sm" variant="ghost" disabled={disabled || pending}
              title={target.label} onClick={() => void act(picker.source, target.id)}><span>{target.label}</span></Button>)}
            {picker.targets.length === 0 && <p role="status">No sources are available.</p>}
          </div>
          <Button size="sm" variant="outline" disabled={pending} onClick={() => { setPicker(undefined); setError("") }}>Back</Button>
        </div> : <div className="portable-screen-actions">
          <label><input type="checkbox" checked={microphone} disabled={disabled || pending}
            onChange={event => setMicrophone(event.target.checked)} /> Microphone narration</label>
          <label><input type="checkbox" checked={computerAudio} disabled={disabled || pending}
            onChange={event => setComputerAudio(event.target.checked)} /> Computer audio <small>Default output</small></label>
          <div className="portable-screen-sources">
            {sources.includes("display") && <Button size="sm" variant="outline" disabled={disabled || pending} onClick={() => void choose("display")}><Monitor /> Display</Button>}
            {sources.includes("window") && <Button size="sm" variant="outline" disabled={disabled || pending} onClick={() => void choose("window")}><AppWindow /> Window</Button>}
            {sources.includes("area") && <Button size="sm" variant="outline" disabled={disabled || pending} onClick={() => void choose("area")}><Scan /> Area</Button>}
          </div>
        </div>}
        {error && <p className="feedback" role="alert">{error}</p>}
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>
  {preview && <ScreenAreaDialog key={preview.id} size={preview} imageUrl={endpoint + "/area/" + preview.id}
    action="record" pending={pending} error={error}
    onCancel={() => void act("discard")} onRefresh={() => void submitArea(preview, null)}
    onConfirm={area => void submitArea(preview, area)} />}
  {drawing && take.phase === "recording" && drawing.id === take.id && <ScreencastDrawing
    url={endpoint + "/" + drawing.id} view={drawing.view} seconds={take.seconds ?? 0}
    onDone={() => { setDrawing(undefined); setExpanded(true) }} />}
  </>
}
