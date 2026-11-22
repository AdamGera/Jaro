import { useInterfaceLanguage } from "@/interface-language"
import { MicrophoneLevel } from "./microphone-level"
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"
import { Activity, ArrowLeft, ArrowRight, Bluetooth, Check, ChevronLeft, ChevronRight, CircleAlert, Clipboard, Clock, Copy, FileText, Keyboard, Languages, LockKeyhole, Mic, Search, ShieldCheck, SlidersHorizontal, Sparkles, Trash2, Type, Volume2, Zap } from "lucide-react"
import { AlertDialog, Dialog } from "radix-ui"
import type { DesktopBridge, DesktopInputDevices } from "@/desktop"
import { profileName, type DictationHistory, type DictationHistoryEntry, type DictationPreferences, type DictationShortcutSide, type DictationStep, type SystemDictationControls } from "@/system-dictation"
import { OutputOverview, OutputProfiles } from "./dictation-output-profiles"
import { DictationSoundPicker } from "./dictation-sound-picker"
import { DictationVocabulary } from "./dictation-vocabulary"
import { LaunchAtLogin } from "./launch-at-login"
import { SettingDisclosure, SettingGroup, SettingRow } from "./settings-rows"
import { SettingsSearch } from "./settings-search"
import { Button } from "./ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs"
import "./system-dictation.css"

const steps = ["welcome", "access", "practice", "ready"] as const
const levels = [8, 18, 34, 23, 42, 27, 36, 18, 11, 24, 13]
const historyDate = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })
const historyDay = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })
const historyRelative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "narrow" })
const audioSeconds = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1, style: "unit", unit: "second" })
const audioMinutes = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1, style: "unit", unit: "minute" })
const milliseconds = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0, style: "unit", unit: "millisecond" })
const modePrompt = {
  hold: "Hold while you speak, then release.",
  holdDouble: "Hold while you speak, then release. Double-tap to speak hands-free.",
  tap: "Press once to start, and again to finish.",
  doubleTap: "Double-tap to start, then press once to finish.",
}

type ShortcutMode = DictationPreferences["mode"]
type ShortcutSpan = readonly [start: number, end: number]
const shortcutModes = [
  { id: "holdDouble", title: "Hold or double-tap", presses: [[5, 29], [39, 42], [45, 48], [82, 85]], recording: [[5, 29], [48, 82]] },
  { id: "hold", title: "Hold", presses: [[12, 78]], recording: [[12, 78]] },
  { id: "tap", title: "Tap to start and stop", presses: [[12, 16], [78, 82]], recording: [[12, 78]] },
  { id: "doubleTap", title: "Double-tap only", presses: [[12, 15], [18, 21], [78, 82]], recording: [[21, 78]] },
] as const satisfies ReadonlyArray<{ id: ShortcutMode; title: string; presses: readonly ShortcutSpan[]; recording: readonly ShortcutSpan[] }>

function ShortcutTimeline({ presses, recording }: { presses: readonly ShortcutSpan[]; recording: readonly ShortcutSpan[] }) {
  return <span className="shortcut-timeline" aria-hidden="true">
    {[presses, recording].map((lane, index) => <span className="shortcut-timeline-lane" key={index}>
      {lane.map(([start, end]) => <span key={start} style={{ insetInlineStart: `${start}%`, width: `${end - start}%` }} />)}
    </span>)}
    <span className="shortcut-timeline-playhead" />
  </span>
}

function ShortcutModePicker({ controls }: { controls: SystemDictationControls }) {
  const { t } = useInterfaceLanguage()
  const { status, run, pending, error } = controls
  const [open, setOpen] = useState(false)
  const active = useRef<HTMLButtonElement>(null)
  const [attemptedSave, setAttemptedSave] = useState(false)

  useEffect(() => {
    if (!open) return
    const close = () => setOpen(false)
    window.addEventListener("native-sdk:jard-window-navigation", close)
    return () => window.removeEventListener("native-sdk:jard-window-navigation", close)
  }, [open])

  if (!status) return null
  const selected = shortcutModes.find(mode => mode.id === status.mode) ?? shortcutModes[0]
  function changeOpen(next: boolean) {
    if (next && pending) return
    setOpen(next)
    if (next) {
      setAttemptedSave(false)
      if (status?.choosingShortcut) void run({ action: "cancelShortcut" })
    }
  }

  return <Dialog.Root open={open} onOpenChange={changeOpen}>
    <SettingRow icon={SlidersHorizontal} title={t("Shortcut mode")} label={false} find="Hold, tap, or double-tap to dictate">
      <Dialog.Trigger asChild>
        <Button type="button" variant="outline" size="sm" aria-label={t("Shortcut mode")} aria-disabled={pending || undefined}>
          {t(selected.title)}<ChevronRight aria-hidden="true" />
        </Button>
      </Dialog.Trigger>
    </SettingRow>
    <Dialog.Portal>
      <Dialog.Overlay className="dictation-tour-overlay" />
      <Dialog.Content className="dictation-tour shortcut-mode-dialog"
        onOpenAutoFocus={event => {
          if (!active.current) return
          event.preventDefault()
          active.current.focus()
        }}
        onKeyDown={event => {
          if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return
          // WebKit can skip buttons during Tab navigation depending on the Mac's keyboard settings.
          const buttons = Array.from(event.currentTarget.querySelectorAll("button"))
          const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
          const next = current < 0 ? (event.shiftKey ? buttons.length - 1 : 0)
            : (current + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length
          event.preventDefault()
          buttons[next]?.focus()
        }}>
        <Dialog.Title>{t("Shortcut mode")}</Dialog.Title>
        <Dialog.Description className="shortcut-mode-legend">
          <kbd>{status.shortcutLabel}</kbd>
          <span><i />{t("Shortcut")}</span><span><i />{t("Recording")}</span>
        </Dialog.Description>
        <div className="shortcut-mode-options">
          {shortcutModes.map(mode => <button type="button" key={mode.id} className="shortcut-mode-choice"
            ref={mode.id === selected.id ? active : undefined} aria-pressed={mode.id === selected.id}
            aria-disabled={pending || status.choosingShortcut || undefined}
            onClick={() => {
              if (pending || status.choosingShortcut || mode.id === selected.id) return
              setAttemptedSave(true)
              void run({ action: "set", key: "mode", value: mode.id })
            }}>
            <span className="shortcut-mode-label">
              <strong>{t(mode.title)}</strong>
              <small>{t(modePrompt[mode.id])}</small>
            </span>
            <ShortcutTimeline presses={mode.presses} recording={mode.recording} />
            <Check className="shortcut-mode-check" aria-hidden="true" />
          </button>)}
        </div>
        {attemptedSave && error && <p className="dictation-error" role="alert">{error}</p>}
        <div className="shortcut-mode-footer">
          <Dialog.Close asChild><Button type="button" size="sm">{t("Done")}</Button></Dialog.Close>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

function Waveform({ live = false }: { live?: boolean }) {
  return <span className="dictation-wave" data-live={live} aria-hidden="true">
    {levels.map((level, i) => <span key={i} style={{ height: level, animationDelay: `${i * -73}ms` }} />)}
  </span>
}

type TourProps = { controls: SystemDictationControls; modelReady: boolean; modelFailed: boolean; native?: DesktopBridge }
export function SystemDictationTour({ controls, modelReady, modelFailed, native }: TourProps) {
  const { t } = useInterfaceLanguage()
  const { status, run, error, pending } = controls
  const [text, setText] = useState("")
  const [practiced, setPracticed] = useState(false)
  const field = useRef<HTMLTextAreaElement>(null)
  const title = useRef<HTMLHeadingElement>(null)
  const step = status?.onboarding
  const current = steps.findIndex(item => item === step)
  const open = current >= 0
  const microphoneAllowed = status?.microphone === "granted"
  const accessAllowed = microphoneAllowed && status.accessibility
  const ready = accessAllowed && status.listener && modelReady
  const recovering = (step === "practice" || step === "ready") && !ready
  const showAccess = step === "access" || recovering
  const accessStep = !microphoneAllowed ? "microphone" : !accessAllowed ? "accessibility" : "model"
  const shortcutNeedsHelp = accessAllowed && status.listenerNeedsRestart && !status.listener
  const busy = status?.phase === "starting" || status?.phase === "listening" || status?.phase === "processing"

  useEffect(() => {
    if (!showAccess && step !== "practice" && step !== "ready") return
    const frame = requestAnimationFrame(() => (showAccess ? title.current : field.current ?? title.current)?.focus())
    return () => cancelAnimationFrame(frame)
  }, [step, showAccess, accessStep])

  if (!open || !status) return null
  function go(next: DictationStep) {
    return run({ action: "set", key: "onboarding", value: next })
  }
  const heading = step === "welcome" ? "Speak where you write." : showAccess ? "Give Jard access" :
    step === "practice" ? "Try the shortcut" : "Dictation is ready"

  return <Dialog.Root open modal>
    <Dialog.Portal>
      <Dialog.Overlay className="dictation-tour-overlay" />
      <Dialog.Content className="dictation-tour" onEscapeKeyDown={event => event.preventDefault()} onPointerDownOutside={event => event.preventDefault()}>
        <div className="dictation-tour-top">
          <div className="dictation-tour-mark"><img src="/jard-icon.png" width="26" height="26" alt="" />{t("Jard Dictation")}</div>
          <Button size="sm" variant="ghost" disabled={pending || busy} onClick={() => void go("later")}>{t("Set up later")}</Button>
        </div>
        <div key={showAccess ? `${step}-${accessStep}` : step} className="dictation-tour-scene" data-step={showAccess ? "access" : step}>
          <Dialog.Title ref={title} tabIndex={-1}>{t(heading)}</Dialog.Title>
          {step === "welcome" && <>
            <Dialog.Description>{t("Hold a shortcut, speak, and watch your words land at the cursor. Your voice stays on your Mac.")}</Dialog.Description>
            <div className="dictation-demo" aria-hidden="true">
              <div className="dictation-demo-note"><div><span/><span/><span/></div>
                <strong>Project notes</strong>
                <p>Let’s review the first draft together tomorrow.<i /></p>
              </div>
              <div className="dictation-demo-tools">
                <kbd>{status.shortcutLabel}</kbd>
                <ArrowRight size={16} />
                <Waveform live />
              </div>
            </div>
          </>}
          {showAccess && <>
            <Dialog.Description>{t(accessAllowed ? "Your voice stays on your Mac." :
              "Two permissions connect your voice to the app you’re writing in.")}</Dialog.Description>
            <div className="dictation-permissions">
              {accessStep === "microphone" && <Permission icon={<Mic />} title={t("Microphone")}
                detail={t("Capture your voice while you dictate.")} ready={false}
                button={status.microphone === "request" ? "Allow microphone" : "Open settings"}
                pending={pending} onClick={() => void run({ action: "microphone" })} />}
              {accessStep === "accessibility" && <Permission icon={<LockKeyhole />} title={t("Accessibility")}
                detail={t("Listen for your shortcut and insert text at your cursor.")} ready={false}
                button="Open settings" pending={pending} onClick={() => void run({ action: "accessibility" })} />}
              {accessStep === "model" && <Permission icon={<Activity />} title="Rumble2 Flash"
                detail={t(modelFailed ? "The local speech model could not start." : "Transcribe directly on this Mac.")}
                ready={modelReady} unavailable={modelFailed} />}
            </div>
            {accessStep === "accessibility" && status.accessibilityStale && <>
              <p className="dictation-tour-hint">Jard still can’t use Accessibility. If its switch is already on, you can reopen Jard or reset its entry and grant access again.</p>
              <div className="dictation-settings-actions">
                <Button variant="outline" size="sm" disabled={pending} onClick={() => void run({ action: "repairAccessibility" })}>Reset Accessibility</Button>
                <Button variant="ghost" size="sm" disabled={pending} onClick={() => void run({ action: "reopen" })}>{t("Reopen Jard")}</Button>
              </div>
            </>}
            {shortcutNeedsHelp ? <>
              <p className="dictation-tour-hint">{t("Jaro is retrying the shortcut. If it stays unavailable, reopen the app.")}</p>
              <div className="dictation-settings-actions">
                <Button variant="outline" size="sm" disabled={pending} onClick={() => void run({ action: "reopen" })}>{t("Reopen Jard")}</Button>
                {!status.inputMonitoring && <Button variant="outline" size="sm" disabled={pending}
                  onClick={() => void run({ action: "inputMonitoring" })}>Check Input Monitoring</Button>}
              </div>
            </> : accessAllowed && !ready && !modelFailed && <p className="dictation-tour-hint" role="status">
              {t("Waiting for microphone, shortcut, and model…")}
            </p>}
          </>}
          {step === "practice" && !showAccess && <>
            <Dialog.Description><kbd>{status.shortcutLabel}</kbd> {t(modePrompt[status.mode])}</Dialog.Description>
            <div className="dictation-practice">
              <label htmlFor="dictation-practice">{t("Try saying “Here’s what I want to work on today.”")}</label>
              <textarea ref={field} id="dictation-practice" dir="auto" rows={3} value={text}
                onChange={event => setText(event.target.value)} onPaste={() => setPracticed(true)}
                placeholder={t("Your words will appear here…")} />
              {status.preview && <p className="dictation-preview" dir="auto">{status.preview}</p>}
              <div className="dictation-practice-state" aria-live="polite">
                <Waveform live={busy} />
                <span>{text && practiced ? t("Your words landed.") :
                  t(status.message || "Wait for Listening, then speak.")}</span>
                {text && <Button variant="ghost" size="sm" onClick={() => { setText(""); setPracticed(false); field.current?.focus() }}>
                  {t("Try again")}
                </Button>}
              </div>
            </div>
            {status.mode !== "doubleTap" && <p className="dictation-tour-hint">{status.mode === "tap"
              ? <>Press <kbd>{status.cancelLabel || "Escape"}</kbd> while recording to discard the take.</>
              : <>Press <kbd>Esc</kbd> while still holding your shortcut to discard the take.</>}</p>}
          </>}
          {step === "ready" && !showAccess && <>
            <Dialog.Description>{t("Your shortcut works anywhere Jard is running.")}</Dialog.Description>
            <div className="dictation-ready-mark"><Check size={36} strokeWidth={1.6} /></div>
            <div className="dictation-ready-key"><kbd>{status.shortcutLabel}</kbd><span>{t(modePrompt[status.mode])}</span></div>
            {native && <LaunchAtLogin native={native} onboarding />}
          </>}
          {error && <p role="alert" className="dictation-error">{error}</p>}
        </div>
        <div className="dictation-tour-footer">
          <Button size="sm" variant="ghost" disabled={current === 0 || pending || busy}
            onClick={() => void go(steps[current - 1])}><ArrowLeft />{t("Back")}</Button>
          <div className="dictation-progress" aria-label={t("Step {step} of {total}", { step: current + 1, total: steps.length })}>
            {steps.map((item, index) => <span key={item} data-active={index <= current} />)}
          </div>
          <Button disabled={pending || busy || (showAccess && !ready)}
            onClick={() => void go(current === steps.length - 1 ? "done" : steps[current + 1])}>
            {step === "welcome" ? t("Set up dictation") : showAccess ? t("Continue") : step === "practice" && !(text && practiced) ? t("Skip practice") :
              step === "ready" ? t("Start using Jard") : t("Continue")}<ArrowRight />
          </Button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

function Permission({ icon, title, detail, ready, button, pending, unavailable, onClick }: {
  icon: React.ReactNode; title: string; detail: string; ready: boolean; button?: string; pending?: boolean; unavailable?: boolean; onClick?: () => void
}) {
  const { t } = useInterfaceLanguage()
  return <div className="dictation-permission" data-ready={ready}>
    <span className="dictation-permission-icon" aria-hidden="true">{icon}</span>
    <div><strong>{title}</strong><small>{detail}</small></div>
    {ready ? <span className="dictation-granted"><Check size={14} />{t("Ready")}</span> :
      button ? <Button variant="outline" size="sm" disabled={pending} onClick={onClick}>{t(button)}</Button> :
      <span className="dictation-granted">{t(unavailable ? "Unavailable" : "Waiting")}</span>}
  </div>
}

type SettingsProps = {
  controls: SystemDictationControls
  native: DesktopBridge
  languages: { code: string; name: string }[]
}

export function SystemDictationSettings({ controls, native, languages, monitor = true, visible = true }: SettingsProps & {
  monitor?: boolean; visible?: boolean
}) {
  const { t } = useInterfaceLanguage()
  const { status, run, error, pending, setSettingsVisible } = controls
  const [devices, setDevices] = useState<DesktopInputDevices>()
  const refreshDevices = useRef<(() => void) | null>(null)
  const [feedbackVisible, setFeedbackVisible] = useState(false)
  const stopSoundPreview = useCallback(() => {
    void native.request("systemDictation", { action: "stopSoundPreview" }).catch(() => {})
  }, [native])
  const inputs = devices?.inputs ?? []

  useEffect(() => stopSoundPreview, [stopSoundPreview])

  useEffect(() => {
    if (!monitor) return
    setSettingsVisible(true)
    return () => { setSettingsVisible(false) }
  }, [monitor, setSettingsVisible])

  useEffect(() => {
    if (!visible) return
    let active = true
    let reading = false
    let invalidated = false
    async function refresh() {
      invalidated = true
      if (reading) return
      reading = true
      while (active && invalidated && !document.hidden) {
        invalidated = false
        try {
          const snapshot = await native.request("inputDevices")
          if (active && !invalidated) setDevices(snapshot)
        } catch { /* Keep the last available list until the next refresh. */ }
      }
      reading = false
    }
    refreshDevices.current = refresh
    window.addEventListener("native-sdk:jard-input-devices", refresh)
    document.addEventListener("visibilitychange", refresh)
    void refresh()
    return () => {
      active = false
      refreshDevices.current = null
      window.removeEventListener("native-sdk:jard-input-devices", refresh)
      document.removeEventListener("visibilitychange", refresh)
    }
  }, [native, visible])

  if (!status) return null
  function set<K extends keyof DictationPreferences>(key: K, value: DictationPreferences[K]) {
    return run({ action: "set", key, value } as Parameters<typeof run>[0])
  }
  const access = status.microphone === "granted" && status.accessibility
  const systemMicrophone = devices?.dictationMicrophones?.[status.avoidBluetooth ? 1 : 0] ?? devices?.microphone
  const warmInput = status.enabled && access && status.listener ? status.warmInput : undefined
  const selectedInput = inputs.find(input => input.id === status.inputDevice)
  const missingInput = !!status.inputDevice && !selectedInput
  const unavailableInput = missingInput || selectedInput?.unavailable
  const fallbackMicrophone = warmInput?.phase === "fallback" ? warmInput.microphone
    : !warmInput && unavailableInput ? systemMicrophone : undefined
  const lidBlocksInput = !warmInput && !systemMicrophone && (!status.inputDevice || unavailableInput)
    && inputs.some(input => input.unavailable)
  const microphoneProblem = warmInput?.phase === "unavailable" ? warmInput.message : ""
  const failureMessage = microphoneProblem || status.lastFailure
  const soundVolume = status.soundVolume ?? 70
  function saveSoundVolume(input: HTMLInputElement) {
    if (input.valueAsNumber !== soundVolume) void set("soundVolume", input.valueAsNumber)
  }

  return <section className="dictation-settings dictation-preferences" aria-labelledby="system-dictation-title"
    data-setting-group="Dictation" data-settings-search-blocked={status.choosingShortcut || undefined}>
    <div className="dictation-preference-group dictation-preference-intro">
      <div className="dictation-settings-header">
        <div className="dictation-intro-title">
          <span className="dictation-intro-icon"><Mic aria-hidden="true" /></span>
          <div><h2 id="system-dictation-title">{t("System Dictation")}</h2><p>Speak into the active app with Rumble2 Flash.</p></div>
        </div>
        <label className="dictation-setting-toggle" data-setting-title="Enable system dictation">
          <span>{status.enabled ? t("Enabled") : t("Disabled")}</span>
          <input type="checkbox" role="switch" aria-label="Enable system dictation" checked={status.enabled} disabled={pending}
            onChange={event => void set("enabled", event.target.checked)} />
        </label>
      </div>
      {status.enabled && (!access || !status.listener) && <p className="dictation-error">
        {access ? "Reopen Jard if the global shortcut stays unavailable." : "Complete setup to use the global shortcut."}
      </p>}
      {status.preferencesWarning && <p className="dictation-error" role="status">{t(status.preferencesWarning)}</p>}
      {failureMessage && <div className="dictation-last-failure" role="status">
        <CircleAlert size={16} aria-hidden="true" />
        <div><strong>{t(microphoneProblem ? "Microphone unavailable" : "Last dictation failed")}</strong><p>{t(failureMessage)}</p></div>
      </div>}
      <div className="dictation-settings-actions">
        <Button type="button" size="sm" variant="outline" disabled={pending}
          onClick={() => void set("onboarding", "welcome")}><Sparkles />{access ? "Replay setup" : t("Set up dictation")}</Button>
        <Button type="button" size="sm" variant="outline" disabled={!status.hasLast || pending}
          onClick={() => void run({ action: "copyLast" })}><Clipboard />Copy last</Button>
      </div>
      {visible && <LaunchAtLogin native={native} />}
    </div>

    <SettingGroup id="dictation-shortcuts-title" title={t("Shortcuts")}>
      <SettingRow icon={Keyboard} title={t("Shortcut")} find="Keys you press to dictate" detail={status.pasteLabel ? `Paste Last uses ${status.pasteLabel}.` : undefined} label={false}>
        <Button className="dictation-keycap" aria-label={status.shortcutTarget === "shortcut" ? undefined : `Change dictation shortcut (${status.shortcutLabel})`}
          size="sm" variant="outline" disabled={pending || !status.listener}
          onClick={() => void run({ action: status.shortcutTarget === "shortcut" ? "cancelShortcut" : "chooseShortcut" })}>
          {status.shortcutTarget === "shortcut" ? "Press a key or Esc" : status.shortcutLabel}
        </Button>
      </SettingRow>
      {visible && <ShortcutModePicker controls={controls} />}
      {status.shortcutSide && <SettingRow icon={Keyboard} title={t("Shortcut side")}>
        {labels => (
          <select {...labels} value={status.shortcutSide} disabled={pending || status.choosingShortcut}
            onChange={event => void run({ action: "setShortcutSide", value: event.target.value as DictationShortcutSide })}>
            <option value="left">{t("Left")}</option><option value="either">{t("Either")}</option><option value="right">{t("Right")}</option>
          </select>
        )}
      </SettingRow>}
      {status.mode === "tap" && <SettingRow icon={CircleAlert} title="Cancel shortcut" find="Discard the current take" detail="Discards the current take." label={false}>
        <div className="dictation-row-actions">
          <Button className="dictation-keycap" size="sm" variant="outline" disabled={pending || !status.listener}
            aria-label={status.shortcutTarget === "cancel" ? "Stop recording Cancel shortcut" : `Change Cancel shortcut (${status.cancelLabel || "Escape"})`}
            onClick={() => void run(status.shortcutTarget === "cancel" ? { action: "cancelShortcut" } : { action: "chooseShortcut", target: "cancel" })}>
            {status.shortcutTarget === "cancel" ? "Press a key or Esc" : status.cancelLabel || "Escape"}
          </Button>
          {status.cancelLabel && status.cancelLabel !== "Escape" &&
            <Button size="sm" variant="ghost" disabled={pending} onClick={() => void run({ action: "resetCancelShortcut" })}>Reset to Escape</Button>}
        </div>
      </SettingRow>}
      <SettingDisclosure icon={Keyboard} title="Other shortcuts">
        <SettingRow icon={Type} title={t("Dictate raw text")} find={t("Dictate without processing")} label={false}
          detail={status.shortcutTarget === "raw" ? "Press Delete to turn off." : undefined}>
          <Button className="dictation-keycap" size="sm" variant="outline" disabled={pending || !status.listener}
            aria-label={status.shortcutTarget === "raw" ? "Stop recording Dictate raw text shortcut" : `Change Dictate raw text shortcut (${status.rawLabel || "Off"})`}
            onClick={() => void run(status.shortcutTarget === "raw" ? { action: "cancelShortcut" } : { action: "chooseShortcut", target: "raw" })}>
            {status.shortcutTarget === "raw" ? "Press a key or Esc" : status.rawLabel || "Off"}
          </Button>
        </SettingRow>
        <SettingRow icon={Clipboard} title="Paste Last" find="Paste your last dictation again" label={false}
          detail={status.shortcutTarget === "paste" ? "Press Delete to turn off." : !status.pasteLabel ? "Default shortcut is ⌥⇧V." : undefined}>
          <div className="dictation-row-actions">
            {status.pasteLabel !== "⌥⇧V" && <Button size="sm" variant="ghost" disabled={pending}
              onClick={() => void run({ action: "resetPasteShortcut" })}>Reset Paste Last</Button>}
            <Button className="dictation-keycap" data-setting-control size="sm" variant="outline" disabled={pending || !status.listener}
              aria-label={status.shortcutTarget === "paste" ? "Stop recording Paste Last shortcut" : `Change Paste Last shortcut (${status.pasteLabel || "Off"})`}
              onClick={() => void run(status.shortcutTarget === "paste" ? { action: "cancelShortcut" } : { action: "chooseShortcut", target: "paste" })}>
              {status.shortcutTarget === "paste" ? "Press a key or Esc" : status.pasteLabel || "Off"}
            </Button>
          </div>
        </SettingRow>
        <SettingRow icon={Keyboard} title="Restore the default shortcut" label={false} find={false}>
          <Button size="sm" variant="ghost" disabled={pending} onClick={() => void run({ action: "resetShortcut" })}>Reset shortcut</Button>
        </SettingRow>
      </SettingDisclosure>
      {status.choosingShortcut && status.message && <p role="status" className="dictation-error dictation-shortcut-status">{status.message}</p>}
    </SettingGroup>

    <SettingGroup id="dictation-input-title" title="Voice input">
      <SettingRow icon={Languages} title={t("Language")}>
        {labels => (
          <select {...labels} disabled={pending} value={status.languages[0] || ""} onChange={event => void set("languages", event.target.value ? [event.target.value] : [])}>
            <option value="">Detect automatically</option>
            {languages.map(language => <option key={language.code} value={language.code}>{language.name}</option>)}
          </select>
        )}
      </SettingRow>
      <DictationVocabulary controls={controls} />
      <SettingRow icon={Mic} title={t("Microphone")} anchor="dictation-microphone"
        detail={fallbackMicrophone ? t("Using {microphone} for dictation.", { microphone: fallbackMicrophone })
          : lidBlocksInput ? t("The built-in microphone is off while the lid is closed. Open the lid or connect a microphone.") : undefined}>
        {labels => (
          <span className="dictation-microphone-control">
            <MicrophoneLevel native={native} enabled={Boolean(visible && status.enabled && access && status.listener)} />
            <select {...labels} value={status.inputDevice ?? ""} disabled={pending}
              onFocus={() => refreshDevices.current?.()}
              onChange={event => void set("inputDevice", event.target.value)}>
              <option value="">{t("System default")}{systemMicrophone && " · " + systemMicrophone}</option>
              {missingInput &&
                <option value={status.inputDevice}>{t("Previously selected microphone")}{devices && " · " + t("Unavailable")}</option>}
              {inputs.map(input => <option key={input.id} value={input.id} disabled={input.unavailable}>
                {input.name}{input.unavailable && " · " + t("Unavailable")}</option>)}
            </select>
          </span>
        )}
      </SettingRow>
      <SettingRow icon={Zap} title="Keep microphone ready" find={t("Turn off the mic when not dictating")} detail="macOS shows the mic in use while Jard is ready to dictate.">
        {labels => (
          <input {...labels} type="checkbox" role="switch" checked={status.keepMicrophoneReady ?? false} disabled={pending}
            onChange={event => void set("keepMicrophoneReady", event.target.checked)} />
        )}
      </SettingRow>
    </SettingGroup>

    <section className="dictation-preference-section dictation-advanced" aria-labelledby="dictation-advanced-title" data-setting-group="Advanced">
      <h3 id="dictation-advanced-title">{t("Advanced")}</h3>
      <SettingDisclosure icon={Volume2} title="Audio and feedback"
        onToggle={setFeedbackVisible}>
        <SettingRow icon={Volume2} title={t("While dictating")} find="Playback volume, mute, lower audio or pause media">
          {labels => (
            <select {...labels} value={status.audioWhileDictating ?? "none"} disabled={pending}
              onChange={event => void set("audioWhileDictating", event.target.value as DictationPreferences["audioWhileDictating"])}>
              <option value="none">{t("Do nothing")}</option><option value="mute">{t("Mute")}</option>
              <option value="lower">{t("Lower audio")}</option><option value="pause">{t("Pause media")}</option>
            </select>
          )}
        </SettingRow>
        {status.audioWhileDictating === "lower" && <SettingRow icon={Volume2} title={t("Lower to")} detail={t("Percent of the current output volume.")}>
          {labels => (
            <input {...labels} type="number" min={0} max={100} step={1} required disabled={pending}
              key={status.lowerAudioPercent} defaultValue={status.lowerAudioPercent ?? 20}
              onBlur={event => {
                const input = event.currentTarget
                const previous = status.lowerAudioPercent ?? 20
                if (!input.validity.valid) input.valueAsNumber = previous
                else if (input.valueAsNumber !== previous) void set("lowerAudioPercent", input.valueAsNumber)
              }}
              onKeyDown={event => { if (event.key === "Enter") event.currentTarget.blur() }} />
          )}
        </SettingRow>}
        {status.audioWhileDictating === "pause" && <SettingRow icon={LockKeyhole} title="Playback control" find={false}
          detail={status.mediaAccessMessage || "Open Music, Spotify, or VLC before allowing playback control."} label={false}>
          <div className="dictation-row-actions dictation-media-actions">
            <Button variant="outline" size="sm" disabled={pending}
              onClick={() => void run({ action: "requestMediaAccess" })}>Allow playback control</Button>
            <Button variant="ghost" size="sm" disabled={pending}
              onClick={() => void run({ action: "openMediaAutomationSettings" })}>Automation settings</Button>
          </div>
        </SettingRow>}
        <SettingRow icon={Bluetooth} title="Prefer the Mac’s mic over Bluetooth">
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={status.avoidBluetooth ?? false} disabled={pending || !!status.inputDevice}
              onChange={event => void set("avoidBluetooth", event.target.checked)} />
          )}
        </SettingRow>
        <SettingRow icon={Activity} title={t("Recording indicator")} find="Dictation HUD position">
          {labels => (
            <select {...labels} value={status.placement} disabled={pending}
              onChange={event => void set("placement", event.target.value as DictationPreferences["placement"])}>
              <option value="top">Top of screen</option><option value="bottom">Bottom of screen</option><option value="cursor">Near the cursor</option><option value="off">Hidden</option>
            </select>
          )}
        </SettingRow>
        <SettingRow icon={FileText} title="Show live words in practice" detail="Show a draft in the setup practice field while you speak.">
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={status.livePreview ?? false} disabled={pending}
              onChange={event => void set("livePreview", event.target.checked)} />
          )}
        </SettingRow>
        <SettingRow icon={Volume2} title="Play dictation sounds">
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={status.sound ?? false} disabled={pending}
              onChange={event => void set("sound", event.target.checked)} />
          )}
        </SettingRow>
        <DictationSoundPicker controls={controls} visible={feedbackVisible} />
        <SettingRow icon={Volume2} title={t("Sound volume")}>
          {labels => (
            <input {...labels} key={soundVolume} defaultValue={soundVolume} type="range" min={0} max={100}
              disabled={pending || !status.sound} onPointerUp={event => saveSoundVolume(event.currentTarget)}
              onKeyUp={event => saveSoundVolume(event.currentTarget)} />
          )}
        </SettingRow>
      </SettingDisclosure>

      <SettingDisclosure icon={Type} title="Output and vocabulary corrections">
        <SettingRow icon={Clipboard} title="Insert text by">
          {labels => (
            <select {...labels} value={status.output} disabled={pending}
              onChange={event => void set("output", event.target.value as DictationPreferences["output"])}>
              <option value="paste">{t("Paste")}</option><option value="type">Type</option><option value="copy">Copy only</option>
            </select>
          )}
        </SettingRow>
        <SettingRow icon={Type} title="Remove “um,” “uh,” and “erm”">
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={status.fillerWords ?? false} disabled={pending}
              onChange={event => void set("fillerWords", event.target.checked)} />
          )}
        </SettingRow>
        <SettingRow icon={FileText} title="Apply “new line” and “new paragraph”">
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={status.spokenFormatting ?? false} disabled={pending}
              onChange={event => void set("spokenFormatting", event.target.checked)} />
          )}
        </SettingRow>
        <RuleEditor label="Word replacements" help="Enter one correction per line, such as jard audio => Jard Audio." value={status.replacements}
          save={value => set("replacements", value)} />
        <RuleEditor label="Rewrite instructions" help="Used by the rewrite step with Jard’s local writing model." value={status.rewriteInstructions ?? ""}
          maxLength={2500} save={value => set("rewriteInstructions", value)} />
        <OutputProfiles native={native} value={status.outputProfiles ?? ""} script={status.outputScriptName} pending={pending}
          save={async value => { await set("outputProfiles", value) }} run={run} />
        <SettingRow icon={LockKeyhole} title={t("Browser tab access")} label={false}
          detail={status.browserAccessMessage ||
            (status.browserAccessTarget ? t("Use the active tab’s hostname in website profiles and local output steps.") :
              t("Switch to a supported browser, then return here to allow access."))}>
          <div className="dictation-row-actions dictation-media-actions">
            <Button variant="outline" size="sm" disabled={pending || !status.browserAccessTarget}
              onClick={() => void run({ action: "requestBrowserAccess" })}>
              {status.browserAccessTarget ? t("Allow {browser}", { browser: status.browserAccessTarget }) : t("Allow browser access")}
            </Button>
            <Button variant="ghost" size="sm" disabled={pending}
              onClick={() => void run({ action: "openBrowserAutomationSettings" })}>{t("Automation settings")}</Button>
          </div>
        </SettingRow>
        <RuleEditor label="App and website insertion" help="Use an app name or site:domain, followed by paste, type, or copy. For example, Terminal => type." value={status.appRules}
          save={value => set("appRules", value)} />
      </SettingDisclosure>
    </section>
    {error && <p className="dictation-error" role="alert">{error}</p>}
    <p className="dictation-private"><ShieldCheck size={14} />Dictation runs on this Mac.</p>
  </section>
}

export function SystemDictationWorkspace({ destination, ...props }: SettingsProps & {
  destination: "dictation" | "history" | "output"
}) {
  const { t } = useInterfaceLanguage()
  const [tab, setTab] = useState(destination === "dictation" ? "general" : destination)
  const general = useRef<HTMLDivElement>(null)
  const output = useRef<HTMLDivElement>(null)
  const { status, pending, run, setSettingsVisible } = props.controls
  useEffect(() => {
    setSettingsVisible(true)
    return () => { setSettingsVisible(false) }
  }, [setSettingsVisible])
  return <Tabs value={tab} onValueChange={setTab} className="dictation-workspace-tabs">
    <div className="dictation-workspace-header">
      <TabsList aria-label="Dictation tabs" variant="line">
        <TabsTrigger value="general">{t("General")}</TabsTrigger>
        <TabsTrigger value="output">{t("Output")}</TabsTrigger>
        <TabsTrigger value="history">{t("History")}</TabsTrigger>
      </TabsList>
      <div className="dictation-workspace-actions">
        <SettingsSearch scope={tab === "output" ? output : general} localFind={tab !== "history"}
          onReveal={() => setTab(tab === "output" ? "output" : "general")} />
      {tab === "history" && status && <HistoryRetention value={status.retention} pending={pending}
        onChange={value => void run({ action: "set", key: "retention", value })} />}
      </div>
    </div>
    <TabsContent value="general" forceMount hidden={tab !== "general"}>
      <div ref={general}><SystemDictationSettings {...props} monitor={false} visible={tab === "general"} /></div>
    </TabsContent>
    <TabsContent value="output"><div ref={output}>
      {status && <OutputOverview native={props.native} status={status} />}
    </div></TabsContent>
    <TabsContent value="history" tabIndex={-1}><DictationHistoryTab controls={props.controls} native={props.native} /></TabsContent>
  </Tabs>
}

export function HistoryRetention({ value, pending, onChange }: {
  value: DictationPreferences["retention"]; pending: boolean; onChange: (value: DictationPreferences["retention"]) => void
}) {
  const { t } = useInterfaceLanguage()
  return <label className="dictation-retention"><Clock aria-hidden="true" />
    <select aria-label={t("History retention")} value={value} disabled={pending}
      onChange={event => onChange(Number(event.target.value) as DictationPreferences["retention"])}>
      <option value={0}>{t("History off")}</option><option value={1}>{t("Keep for 1 day")}</option>
      <option value={7}>{t("Keep for 7 days")}</option><option value={30}>{t("Keep for 30 days")}</option>
    </select>
  </label>
}

function DictationHistoryTab({ controls, native }: Pick<SettingsProps, "controls" | "native">) {
  const { t } = useInterfaceLanguage()
  const { status, run, pending, error } = controls
  const loadHistory = useCallback(async (query: string, offset: number) => {
    const result = await native.request("systemDictation", { action: "history", query, offset })
    if (!("entries" in result)) throw new Error("Could not read dictation history.")
    return result
  }, [native])

  return <section className="dictation-history-tab" aria-label={t("Dictation history")}>
    <DictationHistoryBrowser loadHistory={loadHistory} pending={pending}
      copyHistory={ids => run({ action: "bulkHistory", operation: "copy", ids })}
      deleteHistory={ids => run(ids ? { action: "bulkHistory", operation: "delete", ids } : { action: "clearHistory" })}
      historyRevision={[status?.historyRevision, status?.retention].join(":")} />
    {error && <p className="dictation-error" role="alert">{error}</p>}
  </section>
}

function RuleEditor({ label, help, value, save, maxLength = 16000 }: { label: string; help: string; value: string; save: (value: string) => Promise<boolean>; maxLength?: number }) {
  const { t } = useInterfaceLanguage()
  const [draft, setDraft] = useState<string>()
  async function commit() {
    if (draft !== undefined && await save(draft)) setDraft(current => current === draft ? undefined : current)
  }
  return <div className="dictation-rule" data-setting-title={label} data-setting-description={help}>
    <label><strong>{label}</strong><small>{help}</small>
      <textarea dir="auto" rows={3} maxLength={maxLength} value={draft ?? value} onChange={event => setDraft(event.target.value)} />
    </label>
    <Button variant="outline" size="sm" disabled={draft === undefined || draft === value} onClick={() => void commit()}>{t("Save")}</Button>
  </div>
}

function relativeTime(timestamp: number) {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - timestamp) / 60))
  if (minutes < 60) return historyRelative.format(-minutes, "minute")
  if (minutes < 1440) return historyRelative.format(-Math.floor(minutes / 60), "hour")
  return historyRelative.format(-Math.floor(minutes / 1440), "day")
}

function HistoryTiming({ seconds, inferenceMs, totalMs }: DictationHistoryEntry) {
  const { t } = useInterfaceLanguage()
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return null
  if (typeof inferenceMs === "number" && Number.isFinite(inferenceMs) && inferenceMs >= 0 &&
      typeof totalMs === "number" && Number.isFinite(totalMs) && totalMs >= 0) {
    return <div><dt>{t("Latency")}</dt><dd>{milliseconds.format(seconds * 1000)} audio · {milliseconds.format(inferenceMs)} inference · {milliseconds.format(totalMs)} total</dd></div>
  }
  return <div><dt>Audio duration</dt><dd>{audioSeconds.format(seconds)}</dd></div>
}

function HistoryProcessing({ processing }: Pick<DictationHistoryEntry, "processing">) {
  const { t } = useInterfaceLanguage()
  if (!processing || !Number.isFinite(processing.latencyMs) || processing.latencyMs < 0) return null
  const fallbacks = Array.isArray(processing.fallbacks) ? processing.fallbacks : []
  const summary = [
    typeof processing.model === "string" ? processing.model : "",
    milliseconds.format(processing.latencyMs),
    fallbacks.includes("rewrite") && t("Rewrite fell back"),
    fallbacks.includes("transformations") && t("Transformations fell back"),
  ].filter(Boolean).join(" · ")
  return <div><dt>{t("Processing")}</dt><dd>{summary}</dd></div>
}

export function DictationHistoryBrowser({ loadHistory, copyHistory, deleteHistory, pending, historyRevision }: {
  loadHistory: (query: string, offset: number) => Promise<DictationHistory>
  copyHistory: (ids: string[], text: string) => Promise<boolean>
  deleteHistory: (ids?: string[]) => Promise<boolean>
  pending: boolean
  historyRevision: string
}) {
  const { t } = useInterfaceLanguage()
  const [query, setQuery] = useState("")
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [response, setResponse] = useState<{ query: string; offset: number; data?: DictationHistory }>()
  const [selected, setSelected] = useState<string[]>([])
  const [anchor, setAnchor] = useState<string>()
  const [focused, setFocused] = useState<string>()
  const [confirm, setConfirm] = useState<{ ids: string[] } | "all">()
  const [copied, setCopied] = useState<string>()
  const listRef = useRef<HTMLUListElement>(null)
  const pointerRange = useRef<{ id: number; start: string; x: number; y: number; end?: string }>(null)
  const suppressPointerClick = useRef(false)
  const deleteTrigger = useRef<HTMLButtonElement>(null)
  const searchInput = useRef<HTMLInputElement>(null)
  const pageSize = 15
  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      void loadHistory(query, offset).then(result => {
        if (!active) return
        if (offset > 0 && result.total <= offset) {
          setOffset(0)
          return
        }
        setResponse({ query, offset, data: result })
        setSelected(ids => ids.filter(id => result.entries.some(entry => entry.id === id)))
      }).catch(() => { if (active) setResponse({ query, offset }) })
    }, 125)
    return () => { active = false; window.clearTimeout(timer) }
  }, [loadHistory, query, offset, revision, historyRevision])

  const current = response?.query === query && response.offset === offset ? response : undefined
  const results = current?.data
  const error = !!current && !results
  const entries = results?.entries ?? []
  const today = new Date()
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1)
  const todayKey = today.toDateString()
  const yesterdayKey = yesterday.toDateString()
  const days = entries.map(item => new Date(item.time * 1000).toDateString())
  const entry = selected.length <= 1 ? entries.find(item => item.id === selected[0]) ?? entries[0] : undefined
  const tabStop = entries.find(item => item.id === focused)?.id ?? entry?.id ?? entries[0]?.id
  const affected = selected.length ? selected : entry ? [entry.id] : []
  const selectedIDs = new Set(affected)

  function selectRange(from: string, to: string) {
    const start = entries.findIndex(item => item.id === from)
    const end = entries.findIndex(item => item.id === to)
    if (start < 0 || end < 0) return
    setSelected(entries.slice(Math.min(start, end), Math.max(start, end) + 1).map(item => item.id))
  }

  function rowUnderPointer(event: PointerEvent<HTMLButtonElement>) {
    const row = document.elementFromPoint(event.clientX, event.clientY)?.closest(".dictation-history-row")
      ?.querySelector<HTMLButtonElement>("button[data-history-id]")
    return row?.closest("ul") === listRef.current ? row?.dataset.historyId : undefined
  }

  function beginRange(event: PointerEvent<HTMLButtonElement>, start: string) {
    suppressPointerClick.current = false
    if (event.button !== 0 || event.pointerType !== "mouse") return
    pointerRange.current = { id: event.pointerId, start, x: event.clientX, y: event.clientY }
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function extendRange(event: PointerEvent<HTMLButtonElement>) {
    const range = pointerRange.current
    if (!range || range.id !== event.pointerId) return
    if (!range.end && Math.hypot(event.clientX - range.x, event.clientY - range.y) < 4) return
    const end = rowUnderPointer(event)
    if (!end || end === (range.end ?? range.start)) return
    range.end = end
    setAnchor(range.start)
    setCopied(undefined)
    selectRange(range.start, end)
  }

  function endRange(event: PointerEvent<HTMLButtonElement>) {
    const range = pointerRange.current
    if (range?.id !== event.pointerId) return
    suppressPointerClick.current = !!range.end || rowUnderPointer(event) !== range.start
    pointerRange.current = null
  }

  function choose(id: string, event: React.MouseEvent) {
    if (event.detail > 0 && suppressPointerClick.current) return
    setFocused(id)
    setCopied(undefined)
    const from = anchor ?? entry?.id
    if (event.shiftKey && from && entries.some(item => item.id === from)) {
      selectRange(from, id)
      return
    }
    setAnchor(id)
    setSelected(old => (event.metaKey || event.ctrlKey) ? old.includes(id) ? old.filter(value => value !== id) : [...old, id] : [id])
  }

  function navigate(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = entries.length - 1
    const destinations: Record<string, number> = { ArrowUp: Math.max(0, index - 1), ArrowDown: Math.min(last, index + 1), Home: 0, End: last }
    if (!Object.hasOwn(destinations, event.key) || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
    event.preventDefault()
    const next = destinations[event.key]
    const id = entries[next].id
    setSelected([id])
    setAnchor(id)
    setCopied(undefined)
    listRef.current?.querySelectorAll<HTMLButtonElement>("button[data-history-id]")[next]?.focus()
  }

  function changePage(next: number) {
    pointerRange.current = null
    setOffset(next)
    setSelected([])
    setAnchor(undefined)
    setFocused(undefined)
    setCopied(undefined)
  }

  async function copy(ids: string[], key: string) {
    setCopied(undefined)
    const ordered = entries.filter(item => ids.includes(item.id))
    if (await copyHistory(ids, ordered.map(item => item.text).join("\n\n"))) setCopied(key)
  }

  async function remove() {
    if (!confirm) return
    if (!await deleteHistory(confirm === "all" ? undefined : confirm.ids)) return
    setConfirm(undefined)
    setSelected([])
    setAnchor(undefined)
    setOffset(0)
    setRevision(value => value + 1)
  }

  const detailCopyKey = JSON.stringify(affected)
  return <div className="dictation-history-browser">
    <span className="sr-only" role="status">{copied && t("Copied")}</span>
    <div className="dictation-history-browser-body" data-has-selection={affected.length > 0}>
      <div className="dictation-history-index">
        <label className="dictation-history-search"><Search aria-hidden="true" />
          <input ref={searchInput} type="search" value={query} placeholder={t("Search dictation history")} aria-label={t("Search dictation history")}
            onChange={event => { changePage(0); setQuery(event.target.value) }} />
        </label>
        <div className="dictation-history-index-card">
          <div className="dictation-history-results" aria-busy={!current}>
            {error ? <p className="dictation-history-empty" role="alert">Could not read dictation history.</p> : entries.length ?
              <ul ref={listRef} className="dictation-history-list" aria-label={t("Dictations")}>
                {entries.map((item, index) => {
                  const timestamp = item.time * 1000
                  const date = historyDate.format(timestamp)
                  const day = days[index]
                  const active = selectedIDs.has(item.id)
                  return <li key={item.id} data-connect-before={active && selectedIDs.has(entries[index - 1]?.id ?? "") && day === days[index - 1]}
                    data-connect-after={active && selectedIDs.has(entries[index + 1]?.id ?? "") && day === days[index + 1]}>
                    {day !== days[index - 1] && <div className="dictation-history-day" aria-hidden="true">
                      {day === todayKey ? t("Today") : day === yesterdayKey ? t("Yesterday") : historyDay.format(timestamp)}
                    </div>}
                    <div className="dictation-history-row">
                      <button type="button" data-history-id={item.id} aria-pressed={active}
                        tabIndex={item.id === tabStop ? 0 : -1} onFocus={() => setFocused(item.id)}
                        onPointerDown={event => beginRange(event, item.id)} onPointerMove={extendRange} onPointerUp={endRange}
                        onLostPointerCapture={() => { pointerRange.current = null }}
                        onKeyDown={event => navigate(event, index)} onClick={event => choose(item.id, event)}>
                        <span dir="auto">{item.text.replace(/\s+/g, " ")}</span>
                        <time title={date} aria-label={date} dateTime={new Date(timestamp).toISOString()}>{relativeTime(item.time)}</time>
                        <small dir="auto">{item.kind === "raw" ? "Raw dictation · " : item.kind === "printed" ? "Printed dictation · " : ""}
                          {item.app || "Unknown application"}</small>
                      </button>
                      <Button className="dictation-history-row-copy" type="button" variant="ghost" size="icon-xs"
                        tabIndex={-1} aria-label={t("Copy dictation")} data-copied={copied === item.id} title={copied === item.id ? t("Copied") : t("Copy")}
                        disabled={pending} onClick={() => void copy([item.id], item.id)}>
                        {copied === item.id ? <Check /> : <Copy />}
                      </Button>
                    </div>
                  </li>
                })}
              </ul>
            : <div className="dictation-history-empty" role="status">{results ? <>
              <strong>{query.trim() ? "No dictations found." : "No saved dictations yet."}</strong>
              {query.trim() && <p>Try a different word or app name.</p>}
            </> : "Loading history…"}</div>}
          </div>
          {!!results?.total &&
            <div className="dictation-history-index-footer">
              <small>{results.total} {results.total === 1 ? t("dictation") : t("dictations")} · {results.words} {t("words")}
                {results.seconds > 0 && ` · ${results.seconds >= 60 ? audioMinutes.format(results.seconds / 60) : audioSeconds.format(results.seconds)}`}
              </small>
              <Button variant="ghost" size="xs" disabled={pending} onClick={event => {
                deleteTrigger.current = event.currentTarget
                setConfirm("all")
              }}>{t("Clear history…")}</Button>
            </div>}
          {!!results?.total && results.total > pageSize && <div className="dictation-pages">
            <Button size="icon-sm" variant="ghost" aria-label="Newer results" disabled={offset === 0} onClick={() => changePage(offset - pageSize)}><ChevronLeft /></Button>
            <span>{Math.floor(offset / pageSize) + 1} of {Math.ceil(results.total / pageSize)}</span>
            <Button size="icon-sm" variant="ghost" aria-label="Older results" disabled={offset + pageSize >= results.total} onClick={() => changePage(offset + pageSize)}><ChevronRight /></Button>
          </div>}
        </div>
      </div>
      {affected.length > 0 && <article className="dictation-history-detail" aria-label="Selected dictation">
        <header className="dictation-history-detail-header">
          <div>{entry ? <><h2>{entry.kind === "raw" ? "Raw dictation" : entry.kind === "printed" ? "Printed dictation" : t("Dictation")}</h2><time
            dateTime={new Date(entry.time * 1000).toISOString()}>{historyDate.format(entry.time * 1000)}</time></>
            : <h2>{selected.length} dictations selected</h2>}</div>
          <div className="dictation-row-actions">
            <Button variant="outline" size="icon-sm" aria-label="Copy selected dictation" title={copied === detailCopyKey ? t("Copied") : t("Copy")}
              disabled={pending} onClick={() => void copy(affected, detailCopyKey)}>
              {copied === detailCopyKey ? <Check /> : <Copy />}
            </Button>
            <Button variant="outline" size="icon-sm" aria-label="Delete selected dictation" title={t("Delete")} disabled={pending}
              onClick={event => {
                deleteTrigger.current = event.currentTarget
                setConfirm({ ids: affected })
              }}><Trash2 /></Button>
          </div>
        </header>
        {entry && <div className="dictation-history-detail-body" role="region" aria-label="Transcript and details" tabIndex={0} key={entry.id}>
          <p className="dictation-history-text" dir="auto">{entry.text}</p>
          {entry.raw && entry.raw !== entry.text && <details className="dictation-history-original">
            <summary>Original transcript</summary><p dir="auto">{entry.raw}</p>
          </details>}
          <dl className="dictation-history-meta">
            <div><dt>{t("Application")}</dt><dd dir="auto">{entry.app || "Unknown application"}</dd></div>
            {entry.mode && <div><dt>Output profile</dt><dd dir="auto">{profileName(entry.mode)}</dd></div>}
            <HistoryProcessing processing={entry.processing} />
            <div><dt>{t("Speech model")}</dt><dd>Rumble2 Flash</dd></div>
            <HistoryTiming {...entry} />
          </dl>
        </div>}
      </article>}
    </div>
    <AlertDialog.Root open={!!confirm} onOpenChange={open => { if (!open) setConfirm(undefined) }}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="dictation-tour-overlay" />
        <AlertDialog.Content className="dictation-tour dictation-history-confirm"
          onCloseAutoFocus={event => {
            event.preventDefault()
            const target = deleteTrigger.current
            if (target?.isConnected && !target.disabled) target.focus()
            else searchInput.current?.focus()
          }}>
          <AlertDialog.Title>{confirm === "all" ? "Clear dictation history?" : "Delete selected dictations?"}</AlertDialog.Title>
          <AlertDialog.Description>{t("These dictations will be removed from this device.")}</AlertDialog.Description>
          <div className="dictation-settings-actions">
            <AlertDialog.Cancel asChild><Button variant="outline">{t("Cancel")}</Button></AlertDialog.Cancel>
            <Button variant="destructive" disabled={pending} onClick={() => void remove()}>{t("Delete")}</Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </div>
}
