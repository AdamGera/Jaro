import { useInterfaceLanguage } from "@/interface-language"
import { useEffect, useRef, useState } from "react"
import { AppWindow, AudioLines, Crop, FolderOpen, HardDrive, History, Keyboard, Mic, ScreenShare, Smartphone, Square, X, type LucideIcon } from "lucide-react"
import { AlertDialog } from "radix-ui"
import type { DesktopAnnotation, DesktopAnnotationAction, DesktopAnnotationSource, DesktopBridge } from "@/desktop"
import type { SystemDictationControls } from "@/system-dictation"
import { AnnotationAccess } from "./annotation-access"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const sources: { source: DesktopAnnotationSource; icon: LucideIcon; title: string; detail: string; action: string }[] = [
  { source: "display", icon: ScreenShare, title: "Display", detail: "Record an entire Mac display.", action: "Choose Display…" },
  { source: "window", icon: AppWindow, title: "Window", detail: "Point to a window to preview what will be recorded.", action: "Choose Window…" },
  { source: "area", icon: Crop, title: "Area", detail: "Drag to select part of a display, or click to record it all.", action: "Choose Area…" },
  { source: "phone", icon: Smartphone, title: "iPhone or iPad", detail: "Connect your device to this Mac with a USB cable.", action: "Wired iPhone or iPad" },
]

export function DesktopAnnotationControls({ native, busy, languages, controls, allowConcurrentCapture }: {
  native: DesktopBridge; busy: boolean; languages: string[]; controls: SystemDictationControls; allowConcurrentCapture: boolean
}) {
  const { t } = useInterfaceLanguage()
  const [annotation, setAnnotation] = useState<DesktopAnnotation>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [discarding, setDiscarding] = useState(false)
  const revision = useRef(0)
  useEffect(() => {
    let active = true
    const observed = revision.current
    void native.request("annotation", { action: "status" }).then(result => {
      if (active && observed === revision.current) setAnnotation(result)
    }).catch(() => {})
    const receive = (event: WindowEventMap["native-sdk:jard-annotation"]) => {
      revision.current++
      setAnnotation(event.detail)
      if (event.detail.phase !== "recording") setDiscarding(false)
    }
    window.addEventListener("native-sdk:jard-annotation", receive)
    return () => { active = false; window.removeEventListener("native-sdk:jard-annotation", receive) }
  }, [native])

  async function act(command: DesktopAnnotationAction) {
    if (command.action === "start" && annotation?.managed) return
    setError("")
    setPending(true)
    const observed = revision.current
    try {
      const result = await native.request("annotation", command)
      if (observed === revision.current) setAnnotation(result)
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not open screen annotation.") }
    finally { setPending(false) }
  }

  if (!annotation?.phase) return null
  const choosing = annotation.phase === "picking" || annotation.phase === "selecting" || annotation.phase === "starting"
  const finalizing = annotation.phase === "saving" || annotation.phase === "cancelling"
  const notice = error || (!annotation.busy && annotation.message)
  const enabled = controls.status?.annotationEnabled === undefined || !!controls.status.annotationEnabled
  const audio = annotation.audio
  return <div className="dictation-settings dictation-preferences annotation-settings">
    <SettingGroup id="annotation-title" title={t("Screen Annotation")}>
      {!annotation.managed && <SettingRow icon={ScreenShare} title={t("Enable Annotate")}
        detail={t("Record a screen, draw on it, and transcribe your narration.")}>
        {labels => (
          <input {...labels} type="checkbox" role="switch" checked={enabled}
            disabled={!controls.status || busy || pending || controls.pending || !!annotation.busy || !!controls.status.choosingShortcut}
            onChange={event => void controls.run({ action: "setAnnotationEnabled", enabled: event.currentTarget.checked })} />
        )}
      </SettingRow>}
      {!annotation.managed && enabled && <SettingRow icon={AppWindow} title={t("Show in screen recordings")}
        detail={t("Include Jaro’s floating Annotate controls in display and area recordings.")}>
        {labels => (
          <input {...labels} type="checkbox" role="switch" checked={!!controls.status?.annotationShowOverlays}
            disabled={!controls.status || controls.pending}
            onChange={event => void controls.run({ action: "setAnnotationOverlays", show: event.currentTarget.checked })} />
        )}
      </SettingRow>}
      {notice && <p className="annotation-status" role={error || annotation.phase === "error" ? "alert" : "status"}>{t(notice)}</p>}
      {annotation.busy ? <SettingRow icon={ScreenShare} label={false} title={t("Current capture")}
        detail={annotation.message && <span role="status">{t(annotation.message)}</span>}>
        <div className="dictation-row-actions">
          <Button size="sm" variant="outline" disabled={pending || finalizing} onClick={() => void act({ action: choosing ? "cancel" : "stop" })}>
            {choosing ? <X aria-hidden="true" /> : <Square aria-hidden="true" />}
            {t(choosing ? "Cancel" : annotation.phase === "saving" ? "Saving…" : finalizing ? "Discarding…" : "Stop & Save")}
          </Button>
          {annotation.phase === "recording" && <Button size="sm" variant="ghost" disabled={pending}
            onClick={() => setDiscarding(true)}>{t("Discard…")}</Button>}
        </div>
      </SettingRow> : (enabled || !!annotation.managed) && sources.map(({ source, icon, title, detail, action }) =>
        <SettingRow key={source} icon={icon} title={t(title)} detail={t(detail)} find={`${t(action)} ${t(detail)}`} label={false}>
          <Button size="sm" variant="outline" disabled={pending || (busy && !allowConcurrentCapture) || !!annotation.managed || controls.status?.shortcutTarget === "annotate"}
            onClick={() => void act({ action: "start", source, languages })}>{t(action)}</Button>
        </SettingRow>)}
      {!!annotation.recoverable && <SettingRow icon={FolderOpen} title={t("Recovered recording")} label={false}>
        <Button size="sm" variant="outline" disabled={pending} onClick={() => void act({ action: "reveal" })}>{t("Show Recording in Finder")}</Button>
      </SettingRow>}
    </SettingGroup>
    {!annotation.managed && enabled && audio && <SettingGroup id="annotation-audio" title={t("Recording audio")}>
      <SettingRow icon={Mic} title={t("Microphone narration")} detail={t("Include your voice from the selected microphone.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={audio.microphone}
          disabled={pending || busy || !!annotation.busy}
          onChange={event => void act({ ...audio, action: "setAudio", microphone: event.currentTarget.checked })} />}
      </SettingRow>
      <SettingRow icon={AudioLines} title={t("Mac audio")} detail={t("Include computer sound in display, window, and area recordings.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={audio.computerAudio}
          disabled={pending || busy || !!annotation.busy}
          onChange={event => void act({ ...audio, action: "setAudio", computerAudio: event.currentTarget.checked })} />}
      </SettingRow>
    </SettingGroup>}
    {!annotation.managed && enabled && <AnnotationAccess native={native} busy={busy || pending || !!annotation.busy} />}
    {!annotation.managed && <AnnotationShortcut native={native} controls={controls} busy={busy || pending || !!annotation.busy} />}
    {!annotation.managed && <AnnotationStorage native={native} busy={busy || pending || !!annotation.busy} />}
    <AlertDialog.Root open={discarding} onOpenChange={setDiscarding}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="note-language-scrim" />
        <AlertDialog.Content className="note-language-confirm">
          <AlertDialog.Title>{t("Discard this annotation?")}</AlertDialog.Title>
          <AlertDialog.Description>{t("The current recording and drawings will be deleted.")}</AlertDialog.Description>
          <div className="note-language-actions">
            <AlertDialog.Cancel asChild><Button variant="ghost" size="sm">{t("Keep recording")}</Button></AlertDialog.Cancel>
            <AlertDialog.Action asChild><Button variant="destructive" size="sm" onClick={() => void act({ action: "cancel" })}>{t("Discard")}</Button></AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </div>
}

type AnnotationRetention = { mediaDays: number | null; recordDays: number | null; includeExisting: boolean }

const retentionEndpoint = "/api/annotations/retention"
const retentionPresets = [
  { id: "week", label: "Video for 7 days", mediaDays: 7, recordDays: 30 },
  { id: "month", label: "Video for 30 days", mediaDays: 30, recordDays: 90 },
  { id: "forever", label: "Forever", mediaDays: null, recordDays: null },
] as const

function AnnotationStorage({ native, busy }: { native: DesktopBridge; busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const [policy, setPolicy] = useState<AnnotationRetention>()
  const [choice, setChoice] = useState<AnnotationRetention>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    const request = new AbortController()
    void fetch(retentionEndpoint, { cache: "no-store", signal: request.signal }).then(async response => {
      if (!response.ok) throw new Error()
      const saved: AnnotationRetention = await response.json()
      if (!request.signal.aborted) setPolicy(saved)
    }).catch(() => {
      if (!request.signal.aborted) setError("Could not load annotation retention.")
    })
    return () => request.abort()
  }, [])

  async function save(next: AnnotationRetention) {
    setSaving(true)
    setError("")
    try {
      const response = await fetch(retentionEndpoint, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not save annotation retention.")
      setPolicy(result)
      setChoice(undefined)
      window.dispatchEvent(new Event("jard-annotation-retention"))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not save annotation retention.")
    } finally { setSaving(false) }
  }

  function choose(next: AnnotationRetention) {
    if (!policy) return
    setError("")
    const shortens = (days: number | null, before: number | null) => days !== null && (before === null || days < before)
    if (shortens(next.mediaDays, policy.mediaDays) || shortens(next.recordDays, policy.recordDays) ||
        (next.includeExisting && !policy.includeExisting && (next.mediaDays !== null || next.recordDays !== null))) {
      setChoice(next)
    } else {
      void save(next)
    }
  }

  const preset = policy && retentionPresets.find(item => item.mediaDays === policy.mediaDays && item.recordDays === policy.recordDays)
  const title = t("Keep annotations")
  return <SettingGroup id="annotation-storage" title={t("Storage")}>
    <AnnotationFolder native={native} busy={busy} />
    <SettingRow icon={HardDrive} title={title} find={t("Annotation retention video recording Trash")}
      detail={t("After the video expires, narration, notes, and the contact sheet stay in Sessions until the annotation expires.")}>
      {labels => (
        <select {...labels} value={policy ? preset?.id ?? "custom" : ""} disabled={!policy || busy || saving} onChange={event => {
          const target = retentionPresets.find(item => item.id === event.currentTarget.value)
          if (target && policy) choose({ mediaDays: target.mediaDays, recordDays: target.recordDays, includeExisting: policy.includeExisting })
        }}>
          {!policy && <option value="">{error ? t("Unavailable") : t("Checking…")}</option>}
          {policy && !preset && <option value="custom" disabled>{t("Custom")}</option>}
          {retentionPresets.map(item => <option key={item.id} value={item.id}>{t(item.label)}</option>)}
        </select>
      )}
    </SettingRow>
    {!!policy && <SettingRow icon={History} title={t("Include older annotations")}
      detail={t("Apply this policy to annotations saved before retention was added.")}>
      {labels => (
        <input {...labels} type="checkbox" role="switch" checked={policy.includeExisting} disabled={busy || saving}
          onChange={event => choose({ ...policy, includeExisting: event.currentTarget.checked })} />
      )}
    </SettingRow>}
    {policy && <p className="annotation-status" role="status">{policy.recordDays === null
      ? t("Annotation records stay in Sessions.")
      : `${t("Annotation records move to Trash after")} ${policy.recordDays} ${t("days")}.`}</p>}
    {error && !choice && <p className="annotation-status" role="alert">{t(error)}</p>}
    <AlertDialog.Root open={!!choice} onOpenChange={open => { if (!open && !saving) setChoice(undefined) }}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="note-language-scrim" />
        <AlertDialog.Content className="note-language-confirm">
          <AlertDialog.Title>{t("Apply annotation retention?")}</AlertDialog.Title>
          <AlertDialog.Description>{t("Expired videos and annotations covered by this policy move to the macOS Trash.")}</AlertDialog.Description>
          {error && <p role="alert">{t(error)}</p>}
          <div className="note-language-actions">
            <AlertDialog.Cancel asChild><Button variant="outline" disabled={saving}>{t("Cancel")}</Button></AlertDialog.Cancel>
            <Button variant="destructive" disabled={busy || saving} onClick={() => { if (choice) void save(choice) }}>
              {t(saving ? "Saving…" : "Apply retention")}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </SettingGroup>
}

function AnnotationFolder({ native, busy }: { native: DesktopBridge; busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const [location, setLocation] = useState<{ folder: string; custom: boolean; available: boolean }>()
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const revision = useRef(0)
  useEffect(() => {
    let current = true
    function read() {
      const version = revision.current
      void native.request("annotationStorage", { action: "status" }).then(value => {
        if (current && revision.current === version) setLocation(value)
      }).catch(() => {})
    }
    read()
    window.addEventListener("focus", read)
    return () => { current = false; window.removeEventListener("focus", read) }
  }, [native])

  async function change(action: "choose" | "reveal" | "reset") {
    revision.current++
    setPending(true)
    setError("")
    try {
      setLocation(await native.request("annotationStorage", { action }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not open the Annotations folder.")
    } finally { revision.current++; setPending(false) }
  }

  return <SettingRow icon={FolderOpen} title={t("Annotations folder")} label={false} find={t("Choose or reveal where new annotation files are saved")}
    detail={<>
      {location?.custom ? <bdi style={{ overflowWrap: "anywhere" }} title={location.folder}>{location.folder}</bdi> : t("Jaro Session library")}
      {location?.available === false && <span className="annotation-access-error" role="status">{t("Reconnect this folder or choose another for new captures.")}</span>}
      {error && <span className="annotation-access-error" role="alert">{t(error)}</span>}
    </>}>
    <div className="dictation-row-actions">
      <Button size="sm" variant="outline" disabled={busy || pending} onClick={() => void change("choose")}>{t("Choose Folder…")}</Button>
      <Button size="sm" variant="outline" disabled={pending || !location?.available} onClick={() => void change("reveal")}>{t("Reveal")}</Button>
      {location?.custom && <Button size="sm" variant="ghost" disabled={busy || pending} onClick={() => void change("reset")}>{t("Use Default")}</Button>}
    </div>
  </SettingRow>
}

function AnnotationShortcut({ native, controls, busy }: { native: DesktopBridge; controls: SystemDictationControls; busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const { status, run, error, pending } = controls
  const section = useRef<HTMLDivElement>(null)
  const choosing = status?.shortcutTarget === "annotate"
  const label = status?.annotateLabel || "⌥⇧S"
  useEffect(() => {
    if (!choosing) return
    const panel = section.current?.closest<HTMLElement>("[data-settings-page]")
    if (!panel) return
    const cancel = () => { void native.request("systemDictation", { action: "cancelAnnotationShortcut" }).catch(() => {}) }
    const observer = new MutationObserver(() => { if (panel.hidden) cancel() })
    observer.observe(panel, { attributes: true, attributeFilter: ["hidden"] })
    if (panel.hidden) cancel()
    return () => { observer.disconnect(); if (!panel.isConnected) cancel() }
  }, [native, choosing])

  const detail = choosing ? "Press a key with Command, Option, Control or Shift, or use a function key." :
    status?.annotationShortcutConflict ? "Another Jaro shortcut uses these keys. Choose a different shortcut to start a capture." :
    status?.annotationEnabled !== undefined && !status.annotationEnabled ? "The shortcut is saved for when Annotate is on." :
    status?.annotationListener ? "Tap to record the display under the pointer or save. Hold while recording to draw." :
    "Allow Accessibility for Jaro to use the shortcut."
  return <div ref={section}>
    <SettingGroup id="annotation-shortcut" title={t("Shortcuts")}>
      <SettingRow icon={Keyboard} title={t("Annotate shortcut")} label={false} find={t("Option Shift S draw keyboard Accessibility")}
        detail={<>{t(detail)}{choosing && status?.message && <span className="annotation-access-error" role="alert">{t(status.message)}</span>}</>}>
        <div className="dictation-row-actions">
          <Button size="sm" variant="outline" className="dictation-keycap" data-setting-control
            disabled={pending || (!choosing && (busy || !(status?.annotationRecorderAvailable ?? status?.annotationListener) || !!status?.choosingShortcut))}
            aria-label={choosing ? t("Cancel shortcut recording") : `${t("Change Annotate shortcut")} (${label})`}
            onClick={() => void run(choosing ? { action: "cancelAnnotationShortcut" } : { action: "chooseShortcut", target: "annotate" })}>
            {choosing ? t("Press a key or Esc") : <bdi dir="ltr">{label}</bdi>}
          </Button>
          {!choosing && label !== "⌥⇧S" && <Button size="sm" variant="ghost" disabled={busy || pending || status?.choosingShortcut}
            onClick={() => void run({ action: "resetAnnotationShortcut" })}>{t("Restore default")}</Button>}
        </div>
      </SettingRow>
      {error && <p className="annotation-status" role="alert">{t(error)}</p>}
    </SettingGroup>
  </div>
}
