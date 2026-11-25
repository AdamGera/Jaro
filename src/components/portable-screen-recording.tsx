import { useEffect, useState } from "react"
import { AppWindow, Keyboard, Monitor, Pencil, Scan, Terminal, Video } from "lucide-react"
import { Popover } from "radix-ui"
import { portableScreenTargets, type CaptureTarget, type ScreenArea } from "@/portable-capture"
import { clock } from "@/speaker-activity"
import { Button } from "./ui/button"
import { ActionTooltip } from "./ui/tooltip"
import { ScreenAreaDialog } from "./screen-area-dialog"
import { ScreencastDrawing } from "./screencast-drawing"
import { PortableShortcut } from "./portable-dictation-workspace"
import { SettingGroup, SettingRow } from "./settings-rows"
import { useInterfaceLanguage } from "@/interface-language"
import { AnnotationRules } from "./annotation-rules"

const endpoint = "/api/portable/screencast"
export const hasPortableScreenRecording = document.documentElement.hasAttribute("data-jaro-screen-recording")
const watchDesktopShortcut = document.documentElement.hasAttribute("data-jaro-annotation-shortcut")
const hasOpenCodeWsl = document.documentElement.hasAttribute("data-jaro-opencode-wsl")
const hasOpenCodeRules = document.documentElement.hasAttribute("data-jaro-opencode-handoff")
const hasDesktopAnnotationShortcut = document.documentElement.hasAttribute("data-jaro-desktop-annotation")
export const hasDesktopAnnotationSettings = hasDesktopAnnotationShortcut || hasOpenCodeWsl || hasOpenCodeRules
type Source = "display" | "window" | "area"
type Sources = { sources: Source[]; picker?: "workspace" }
type Microphone = { id: string; label: string }
type Preview = { id: string; width: number; height: number }
type Take = {
  phase: "idle" | "choosing" | "previewing" | "selecting" | "starting" | "recording" | "recorded" | "saving" | "cancelled" | "error"
  seconds?: number
  id?: string
  error?: string
  preview?: Preview
  desktopDrawing?: boolean
  desktopDrawingActive?: boolean
  drawingView?: string
  levels?: Partial<Record<"mic" | "system", number>>
  shortcut?: string
  shortcutError?: string
  shortcutSaved?: string
  gnomeShortcut?: boolean
  desktopShortcut?: DesktopAnnotationShortcut
  shortcutPlatform?: "x11" | "windows"
}
const idle: Take = { phase: "idle" }
type DesktopAnnotationShortcut = {
  shortcut: string
  defaultShortcut: string
  enabled: boolean
  requested: boolean
  message: string
}
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

export function DesktopAnnotationSettings({ busy }: { busy: boolean }) {
  return <div className="dictation-preferences">
    {hasDesktopAnnotationShortcut ? <AnnotationShortcutSetting busy={busy} /> : null}
    {hasOpenCodeWsl ? <OpenCodeWslSetting disabled={busy} /> : null}
    {hasOpenCodeRules ? <AnnotationRules disabled={busy} /> : null}
  </div>
}

function AnnotationShortcutSetting({ busy }: { busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const [options, setOptions] = useState<DesktopAnnotationShortcut>()
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    void request("", { signal: controller.signal }).then(response => response.json()).then((take: Take) => {
      if (!controller.signal.aborted) setOptions(take.desktopShortcut)
    }).catch(() => {
      if (!controller.signal.aborted) setFeedback("Could not read the Annotate shortcut.")
    })
    return () => controller.abort()
  }, [])

  async function configure(change: { enabled: boolean } | { shortcut: string }, reportError: (message: string) => void = setFeedback) {
    setFeedback("")
    setPending(true)
    try {
      const response = await request("/shortcut", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(change) })
      setOptions(await response.json() as DesktopAnnotationShortcut)
      return true
    } catch (error) {
      reportError(error instanceof Error ? error.message : "Could not change the Annotate shortcut.")
      return false
    } finally {
      setPending(false)
    }
  }

  return <SettingGroup id="desktop-annotation-shortcut" title={t("Screen recording")}>
    <SettingRow icon={Keyboard} title={t("Enable Annotate shortcut")} label={false}
      detail={t("Tap to record the display under the pointer with microphone narration. Tap again to save.")}>
      {options?.requested && !options.enabled && <Button size="sm" variant="ghost" disabled={pending || busy}
        onClick={() => void configure({ enabled: false })}>{t("Disable on launch")}</Button>}
      <Button size="sm" variant="outline" disabled={!options || pending || busy}
        onClick={() => void configure({ enabled: !options?.enabled })}>
        {t(options?.enabled ? "Disable shortcut" : "Enable shortcut")}
      </Button>
    </SettingRow>
    {options && <PortableShortcut title="Annotate shortcut" editLabel="Edit Annotate shortcut" shortcut={options.shortcut}
      defaultShortcut={options.defaultShortcut} disabled={pending || busy} save={(shortcut, reportError) => configure({ shortcut }, reportError)} />}
    {(feedback || options?.message) && <p className="update-settings-feedback" role="status">{t(feedback || options?.message || "")}</p>}
  </SettingGroup>
}

const wslPreference = "/api/portable/annotation-handoff/wsl"

function OpenCodeWslSetting({ disabled }: { disabled: boolean }) {
  const { t } = useInterfaceLanguage()
  const [enabled, setEnabled] = useState<boolean>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    void fetch(wslPreference, { signal: controller.signal, cache: "no-store" }).then(async response => {
      if (!response.ok) throw new Error("Could not read the OpenCode in WSL setting.")
      const value = await response.json() as { enabled: boolean }
      if (!controller.signal.aborted) setEnabled(value.enabled)
    }).catch(() => {
      if (!controller.signal.aborted) setError("Could not read the OpenCode in WSL setting.")
    })
    return () => controller.abort()
  }, [])

  async function save(next: boolean) {
    setPending(true)
    setError("")
    try {
      const response = await fetch(wslPreference, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }) })
      const value = await response.json() as { enabled: boolean; detail?: string }
      if (!response.ok) throw new Error(value.detail || "Could not save the OpenCode in WSL setting.")
      setEnabled(value.enabled)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save the OpenCode in WSL setting.")
    } finally {
      setPending(false)
    }
  }

  return <SettingGroup id="annotation-agent-destinations" title={t("Agent destinations")}>
    <SettingRow icon={Terminal} title={t("OpenCode in WSL")}
      detail={t("Prefer OpenCode in your default WSL 2 distribution. Requires Python 3 in that distribution.")}>
      {labels => <input {...labels} type="checkbox" role="switch" checked={enabled ?? false}
        disabled={disabled || pending || enabled === undefined} onChange={event => void save(event.target.checked)} />}
    </SettingRow>
    {error && <p className="update-settings-feedback" role="alert">{t(error)}</p>}
  </SettingGroup>
}

export function PortableScreenRecording({ disabled, hiddenWhenIdle = false, languageCodes, onBusy, onSave }: {
  disabled: boolean
  hiddenWhenIdle?: boolean
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
  const [microphoneDevice, setMicrophoneDevice] = useState("")
  const [microphones, setMicrophones] = useState<Microphone[] | null>()
  const [computerAudio, setComputerAudio] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [manualDrawing, setManualDrawing] = useState<{ id: string; view: string }>()
  const drawing = take.id && take.drawingView ? { id: take.id, view: take.drawingView } : manualDrawing
  const active = take.phase !== "idle"
  const chooseMicrophone = microphone && available.sources.length > 0
  const poll = ["choosing", "previewing", "selecting", "starting", "recording", "saving"].includes(take.phase)
  const drawingOpen = Boolean(drawing && drawing.id === take.id && take.phase === "recording")

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
    if (!expanded || !chooseMicrophone || active || picker) return
    const controller = new AbortController()
    void request("/microphones", { signal: controller.signal }).then(response => response.json()).then(result => {
      if (!controller.signal.aborted) setMicrophones(result.inputs)
    }).catch(() => { if (!controller.signal.aborted) setMicrophones(null) })
    return () => controller.abort()
  }, [expanded, chooseMicrophone, active, picker])

  useEffect(() => {
    if ((!poll && !watchDesktopShortcut) || pending) return
    const controller = new AbortController()
    let timer: number
    async function refresh() {
      try {
        const response = await request("", { signal: controller.signal })
        const status: Take = await response.json()
        if (controller.signal.aborted) return
        if (status.phase === "cancelled") {
          await request(status.id ? `?key=${status.id}` : "", { method: "DELETE", signal: controller.signal })
          if (controller.signal.aborted) return
          setTake(idle)
          setExpanded(false)
          onBusy(false)
        } else {
          setTake(status)
          onBusy(status.phase !== "idle")
        }
        timer = window.setTimeout(refresh, drawingOpen ? 125 : poll ? 350 : 1_000)
      } catch {
        if (!controller.signal.aborted) timer = window.setTimeout(refresh, 1_000)
      }
    }
    timer = window.setTimeout(refresh, drawingOpen ? 0 : poll ? 250 : 1_000)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [drawingOpen, poll, pending, onBusy])

  useEffect(() => {
    if (take.shortcutSaved) window.dispatchEvent(new Event("jaro-portable-annotation-saved"))
  }, [take.shortcutSaved])

  async function act(action: Source | "save" | "discard", target?: string) {
    setPending(true)
    setError("")
    onBusy(true)
    try {
      if (action === "discard") {
        await request(take.id ? `?key=${take.id}` : "", { method: "DELETE" })
        setTake(idle)
        setExpanded(false)
        onBusy(false)
      } else if (action === "save") {
        setTake({ ...take, phase: "saving" })
        const query = new URLSearchParams(languageCodes.map(code => ["language", code]))
        if (take.id) query.set("key", take.id)
        const response = await request(`/finish?${query}`, { method: "POST" })
        const { id } = await response.json()
        setTake(idle)
        setExpanded(false)
        await onSave(id)
        onBusy(false)
      } else {
        const query = new URLSearchParams({ source: action, microphone: String(microphone), computer_audio: String(computerAudio) })
        if (target) query.set("target", target)
        if (chooseMicrophone && microphoneDevice) query.set("microphone_device", microphoneDevice)
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
      setManualDrawing({ id, view: crypto.randomUUID() })
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
  if (!active && (hiddenWhenIdle || !sources.length)) return null
  const canSave = take.phase === "recording" || take.phase === "recorded"
  const shortcutHint = take.phase === "recording" && take.shortcutPlatform === "x11" ? "Tap to save. Use Draw to mark the preview." :
    take.phase === "recording" && !take.gnomeShortcut ? "Tap to save. Hold to draw on supported displays." :
    take.phase === "recording" ? "Tap the GNOME Annotate shortcut to save." :
    take.phase === "recorded" ? "Tap to save." :
    take.phase === "idle" ? (take.desktopShortcut ? "Tap to record the display under the pointer." :
      "Tap to choose a display and record with microphone narration.") : null
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
          {chooseMicrophone && <>
            <label>{t("Microphone")}
              <select className="portable-screen-microphone" value={microphoneDevice} disabled={disabled || pending}
                onChange={event => setMicrophoneDevice(event.target.value)}>
                <option value="">{t("System default")}</option>
                {microphoneDevice && !microphones?.some(input => input.id === microphoneDevice) &&
                  <option value={microphoneDevice} disabled>{t("Previously selected microphone")}</option>}
                {microphones?.map(input => <option key={input.id} value={input.id}>{input.label}</option>)}
              </select>
            </label>
            {microphones === null && <p className="feedback" role="status">{t("Could not list microphones.")}</p>}
          </>}
          <label><input type="checkbox" checked={computerAudio} disabled={disabled || pending}
            onChange={event => setComputerAudio(event.target.checked)} /> Computer audio <small>Default output</small></label>
          <div className="portable-screen-sources">
            {sources.includes("display") && <Button size="sm" variant="outline" disabled={disabled || pending} onClick={() => void choose("display")}><Monitor /> Display</Button>}
            {sources.includes("window") && <Button size="sm" variant="outline" disabled={disabled || pending} onClick={() => void choose("window")}><AppWindow /> Window</Button>}
            {sources.includes("area") && <Button size="sm" variant="outline" disabled={disabled || pending} onClick={() => void choose("area")}><Scan /> Area</Button>}
          </div>
        </div>}
        {(take.shortcut || take.gnomeShortcut) && shortcutHint && <p className="feedback">
          {take.shortcut && <><kbd>{take.shortcut}</kbd>{" "}</>}{shortcutHint}</p>}
        {(error || take.shortcutError) && <p className="feedback" role="alert">{error || take.shortcutError}</p>}
      </Popover.Content>
    </Popover.Portal>
  </Popover.Root>
  {preview && <ScreenAreaDialog key={preview.id} size={preview} imageUrl={endpoint + "/area/" + preview.id}
    action="record" pending={pending} error={error}
    onCancel={() => void act("discard")} onRefresh={() => void submitArea(preview, null)}
    onConfirm={area => void submitArea(preview, area)} />}
  {drawing && drawingOpen && <ScreencastDrawing key={drawing.view}
    url={endpoint + "/" + drawing.id} view={drawing.view} seconds={take.seconds ?? 0}
    onDone={() => {
      setManualDrawing(undefined)
      setTake(current => current.drawingView === drawing.view ? { ...current, drawingView: undefined } : current)
      setExpanded(true)
    }} />}
  </>
}
