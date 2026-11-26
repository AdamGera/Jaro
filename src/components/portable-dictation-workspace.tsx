import { useCallback, useEffect, useId, useRef, useState } from "react"
import { Activity, Clipboard, Keyboard, Languages, Mic, Play, Sparkles, Terminal, Volume2 } from "lucide-react"
import { useInterfaceLanguage } from "@/interface-language"
import { changePortableOnboarding, hasPortableOnboarding } from "@/portable-onboarding"
import { dictationSoundStyles, type DictationHistory, type DictationOutputPreview, type DictationOutputProfiles,
  type DictationPreferences, type ProfileApplication, type DictationVocabularyEntry } from "@/system-dictation"
import { Button } from "./ui/button"
import type { Language } from "./language-picker"
import { RuleEditor, SettingDisclosure, SettingGroup, SettingRow } from "./settings-rows"
import { DictationVocabulary, type SaveDictationVocabulary } from "./dictation-vocabulary"
import { OutputProfileEditor } from "./dictation-output-profiles"
import { DictationHistoryBrowser, DictationHistoryHeader, HistoryRetention } from "./system-dictation"

export const hasPortableDictation = document.documentElement.hasAttribute("data-jaro-portable-dictation")

type DictationStatus = {
  enabled: boolean
  shortcutRequested: boolean
  platform: "x11" | "wayland" | "windows"
  shortcut: string
  defaultShortcut?: string
  rawShortcut?: string
  rawShortcutError?: string
  portalRawShortcut?: string
  portalRawRequested?: boolean
  recoveryShortcut?: string
  cancelShortcut?: string
  annotationShortcut?: string
  cancelShortcutConflict?: boolean
  canCancel?: boolean
  canStart?: boolean
  queueFull?: boolean
  pendingJobs?: number
  microphoneDenied?: boolean
  shortcutMode?: DictationPreferences["mode"]
  authorizing: boolean
  configurable: boolean
  keyboardAllowed?: boolean
  pasteAuthorizing?: boolean
  savedVirtualPaste?: boolean
  commandPrefix?: string
  commandMode?: boolean
  gnomeShortcutAvailable?: boolean
  gnomeShortcuts?: boolean
  gnomeAnnotation?: boolean
  gnomeShortcutsNeedUpdate?: boolean
  phase: "idle" | "starting" | "recording" | "transcribing" | "delivering" | "pasting"
  message: string
  text: string
  terminal: boolean
  language: string
  vocabularyEntries: DictationVocabularyEntry[]
  replacements: string
  outputProfiles: string
  indicatorPosition: "top" | "bottom" | "off"
  indicatorAvailable: boolean
  sound: boolean
  soundStyle: string
  soundVolume: number
  soundAvailable: boolean
  otherAudio?: DictationPreferences["audioWhileDictating"]
  otherAudioPercent?: number
  otherAudioModes?: DictationPreferences["audioWhileDictating"][]
  noiseCleanup: boolean
  boostMicrophone: boolean
  cleanupStatus: "notInstalled" | "downloading" | "error" | "ready"
  retention: DictationPreferences["retention"]
  historyRevision: number
  historyAvailable: boolean
}

const endpoint = "/api/portable/dictation"
const desktopCommands = [
  ["toggle", "Toggle dictation"], ["start", "Start dictation"], ["stop", "Finish dictation"],
  ["toggle --raw", "Toggle dictation without processing"], ["start --raw", "Start dictation without processing"],
  ["cancel", "Cancel dictation"], ["paste-last", "Paste last dictation"],
] as const
const windowsShortcutModes = {
  hold: { title: "Hold", help: "Hold Left Alt to dictate. Release to paste into the same field." },
  tap: { title: "Tap to start and stop", help: "Tap Left Alt to start dictation. Tap it again to finish." },
  holdDouble: { title: "Hold or double-tap", help: "Hold Left Alt to dictate, or double-tap it to speak hands-free." },
  doubleTap: { title: "Double-tap only", help: "Double-tap Left Alt to start dictation. Tap it once to finish." },
} as const
const linuxShortcutModes = {
  tap: { title: "Tap to start and stop", help: "Press the shortcut to start dictation. Press it again to finish." },
  hold: { title: "Hold", help: "Hold the shortcut to dictate. Release it to finish." },
  holdDouble: { title: "Hold or double-tap", help: "Hold the shortcut to dictate, or double-tap it to speak hands-free." },
  doubleTap: { title: "Double-tap only", help: "Press the shortcut twice to start. Press it once to finish." },
} as const

async function request<T = DictationStatus>(path = "", options?: RequestInit): Promise<T> {
  const response = await fetch(endpoint + path, { cache: "no-store", ...options })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not access system dictation.")
  return result
}

function loadHistory(query: string, offset: number) {
  const params = new URLSearchParams({ query, offset: String(offset) })
  return request<DictationHistory>("/history?" + params)
}

function loadApplications() {
  return request<ProfileApplication[]>("/output-profiles/applications")
}

function chooseApplication() {
  return request<ProfileApplication | null>("/output-profiles/choose-application", { method: "POST" })
}

function profilesRequest<T>(path: string, body: object) {
  return request<T>("/output-profiles" + path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  })
}

async function describeProfiles(profiles: string) {
  return (await profilesRequest<DictationOutputProfiles>("/describe", { profiles })).profiles
}

function previewProfiles({ text, profiles, app, site }: { text: string; profiles: string; app: string; site: string }) {
  return profilesRequest<DictationOutputPreview>("/preview", { text, profiles, app, site })
}

export function usePortableDictation(visible: boolean) {
  const [status, setStatus] = useState<DictationStatus>()
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState("")

  useEffect(() => {
    if (!visible) return
    const controller = new AbortController()
    let timer: number
    async function refresh() {
      try {
        const value = await request("", { signal: controller.signal })
        if (!controller.signal.aborted) setStatus(value)
      } catch {
        if (!controller.signal.aborted) setFeedback("Could not check system dictation.")
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(refresh, 350)
      }
    }
    void refresh()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [visible])

  async function configure(path: string, body?: object, reportError: (message: string) => void = setFeedback) {
    setPending(true)
    setFeedback("")
    try {
      const options: RequestInit = { method: "POST" }
      if (body) {
        options.headers = { "Content-Type": "application/json" }
        options.body = JSON.stringify(body)
      }
      setStatus(await request(path, options))
      return true
    } catch (error) {
      reportError(error instanceof Error ? error.message : "Could not change system dictation.")
      return false
    } finally {
      setPending(false)
    }
  }

  const saveVocabulary = useCallback<SaveDictationVocabulary>(async (base, entries) => {
    const updated = await request("/vocabulary", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base, entries }),
    })
    setStatus(updated)
    return updated.vocabularyEntries
  }, [])

  const saveProfiles = useCallback(async (profiles: string, base: string) => {
    setStatus(await profilesRequest<DictationStatus>("", { profiles, base }))
  }, [])

  return { status, pending, feedback, setFeedback, configure, saveVocabulary, saveProfiles }
}

type MicrophoneOptions = {
  selected: string
  inputs: { id: string; label: string; default: boolean }[]
  available: boolean
}

function VoiceInput({ visible, disabled, status, languages, configure, saveVocabulary }: {
  visible: boolean
  disabled: boolean
  status: DictationStatus | undefined
  languages: Language[]
  configure: (path: string, body: object) => Promise<boolean>
  saveVocabulary: SaveDictationVocabulary
}) {
  const { t } = useInterfaceLanguage()
  const heading = useId()
  const [options, setOptions] = useState<MicrophoneOptions>()
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState("")

  useEffect(() => {
    if (!visible || saving) return
    const controller = new AbortController()
    let timer: number
    async function refresh() {
      try {
        const value = await request<MicrophoneOptions>("/microphone", { signal: controller.signal })
        if (!controller.signal.aborted) setOptions(value)
      } catch {
        if (!controller.signal.aborted) setOptions(current => ({
          selected: current?.selected ?? "", inputs: [], available: false,
        }))
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(refresh, 5000)
      }
    }
    void refresh()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [visible, saving])

  async function select(selected: string) {
    setSaving(true)
    setFeedback("")
    try {
      await request("/microphone", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ selected }),
      })
      setOptions(current => current && { ...current, selected })
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "Could not save the dictation microphone.")
    } finally {
      setSaving(false)
    }
  }

  const defaultInput = options?.inputs.find(input => input.default)?.label
  const missing = options?.selected && !options.inputs.some(input => input.id === options.selected)
  const detail = options?.available === false ? "Could not check available microphones."
    : missing ? "Jaro uses the system microphone until the selected one is available."
    : "Choose the microphone for system-wide dictation."
  const cleanupDetail = status?.cleanupStatus === "downloading" ? "Downloading 2.3 GB. Dictation is available during setup."
    : status?.cleanupStatus === "error" ? "Noise cleanup is unavailable. Turn this setting off and on to retry."
    : status?.cleanupStatus === "ready" ? "Cleans up background noise locally before transcription."
    : "Downloads 2.3 GB the first time you turn it on."

  return <SettingGroup id={heading} title={t("Voice input")}>
    <SettingRow icon={Languages} title={t("Language")}>
      {labels => <select {...labels} value={status?.language ?? ""} disabled={disabled || saving || languages.length === 0}
        onChange={event => void configure("/language", { language: event.target.value })}>
        <option value="">{t("Detect automatically")}</option>
        {languages.map(option => <option key={option.code} value={option.code}>{option.name}</option>)}
      </select>}
    </SettingRow>
    <DictationVocabulary saved={status?.vocabularyEntries} saveVocabulary={saveVocabulary} />
    <SettingRow icon={Mic} title={t("Microphone")} detail={t(detail)}>
      {labels => <select {...labels} value={options?.selected ?? ""} disabled={disabled || saving || !options}
        onChange={event => void select(event.target.value)}>
        <option value="">{t("System default")}{defaultInput && " · " + defaultInput}</option>
        {missing && <option value={options?.selected} disabled>{t("Selected microphone (unavailable)")}</option>}
        {options?.inputs.map(input => <option key={input.id} value={input.id}>{input.label}</option>)}
      </select>}
    </SettingRow>
    <SettingDisclosure icon={Activity} title={t("Audio processing")}>
      <SettingRow icon={Sparkles} title={t("Noise cleanup (StuPASE)")} detail={t(cleanupDetail)}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={status?.noiseCleanup ?? false} disabled={disabled}
          onChange={event => void configure("/voice-input", { setting: "noiseCleanup", enabled: event.target.checked })} />}
      </SettingRow>
      <SettingRow icon={Activity} title={t("Boost microphone (+6 dB)")} detail={t("Raises quiet audio and limits loud peaks before transcription.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={status?.boostMicrophone ?? false} disabled={disabled}
          onChange={event => void configure("/voice-input", { setting: "boostMicrophone", enabled: event.target.checked })} />}
      </SettingRow>
    </SettingDisclosure>
    {feedback && <p role="status" className="update-settings-feedback">{t(feedback)}</p>}
  </SettingGroup>
}

export function PortableShortcut({ shortcut, defaultShortcut, disabled, title = "Dictation shortcut",
  editLabel = "Edit dictation shortcut", detail, bindingError, save }: {
  shortcut: string; defaultShortcut: string; disabled: boolean; title?: string; editLabel?: string
  detail?: string; bindingError?: string
  save: (shortcut: string, reportError: (message: string) => void) => Promise<boolean>
}) {
  const { t } = useInterfaceLanguage()
  const [draft, setDraft] = useState<string>()
  const [error, setError] = useState("")
  const input = useRef<HTMLInputElement>(null)
  const examples = defaultShortcut === "Left Alt"
    ? "Enter Left Alt or a chord such as Ctrl+Alt+F9."
    : "Enter a chord such as Ctrl+Shift+F9 or Ctrl+PageUp."
  const edit = useRef<HTMLButtonElement>(null)
  const optional = defaultShortcut === ""
  const canSave = !disabled && draft !== undefined && (optional || !!draft.trim()) && draft.trim() !== shortcut
  function close() {
    setDraft(undefined)
    setError("")
    requestAnimationFrame(() => edit.current?.focus())
  }
  return <SettingRow icon={Keyboard} title={t(title)} label={false}
    detail={draft !== undefined ? t(examples) : bindingError ? t(bindingError) : detail}>
    {draft === undefined ? <Button ref={edit} type="button" size="sm" variant="outline" disabled={disabled} onClick={() => {
      setDraft(shortcut)
      requestAnimationFrame(() => { input.current?.focus(); input.current?.select() })
    }} aria-label={t(editLabel)}>{shortcut ? <kbd>{shortcut}</kbd> : t("Off")}</Button> :
      <form className="dictation-row-actions portable-shortcut-form" onSubmit={async event => {
        event.preventDefault()
        setError("")
        if (canSave && await save(draft.trim(), setError)) close()
      }}>
        <input ref={input} type="text" dir="ltr" aria-label={t("New shortcut")} value={draft} maxLength={64}
          spellCheck={false} autoComplete="off" disabled={disabled}
          onChange={event => { setDraft(event.target.value); setError("") }}
          onKeyDown={event => {
            if (event.key === "Escape") { event.preventDefault(); close() }
          }} />
        <Button type="button" variant="ghost" size="sm" disabled={disabled || draft === defaultShortcut}
          onClick={() => { setDraft(defaultShortcut); setError("") }}>{t(optional ? "Clear" : "Reset")}</Button>
        <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={close}>{t("Cancel")}</Button>
        <Button type="submit" size="sm" variant="outline" disabled={!canSave}>{t("Save")}</Button>
        {error && <p className="dictation-error" role="alert">{t(error)}</p>}
      </form>}
  </SettingRow>
}

function ShortcutMode({ platform, mode, disabled, onChange }: {
  platform: DictationStatus["platform"]
  mode: DictationPreferences["mode"]
  disabled: boolean
  onChange: (mode: DictationPreferences["mode"]) => void
}) {
  const { t } = useInterfaceLanguage()
  const modes = platform === "windows" ? windowsShortcutModes : linuxShortcutModes
  return <SettingRow icon={Keyboard} title={t("Shortcut mode")} detail={t(modes[mode].help)}>
    {labels => <select {...labels} value={mode} disabled={disabled}
      onChange={event => onChange(event.target.value as DictationPreferences["mode"])}>
      {Object.entries(modes).map(([value, { title }]) => <option key={value} value={value}>{t(title)}</option>)}
    </select>}
  </SettingRow>
}

function hasShortcutMode(status: DictationStatus | undefined): status is DictationStatus & { shortcutMode: DictationPreferences["mode"] } {
  return !!status?.shortcutMode && (status.platform !== "wayland"
    || (!!(status.shortcut || status.portalRawShortcut) && !status.commandMode))
}

function FeedbackSettings({ status, disabled, configure }: {
  status?: DictationStatus
  disabled: boolean
  configure: (path: string, body: object) => Promise<boolean>
}) {
  const { t } = useInterfaceLanguage()
  const heading = useId()
  if (!status?.indicatorPosition) return null
  const busy = disabled || status.phase !== "idle"
  const audioModes = status.otherAudioModes ?? []
  const audio = status.otherAudio && audioModes.includes(status.otherAudio) ? status.otherAudio : "none"
  const audioLabels = { none: "Do nothing", mute: "Mute other audio", lower: "Lower other audio", pause: "Pause media" }
  function saveVolume(input: HTMLInputElement) {
    if (input.valueAsNumber !== status?.soundVolume) void configure("/sound", { volume: input.valueAsNumber })
  }

  return <SettingGroup id={heading} title={t("Recording controls")}>
    <SettingRow icon={Activity} title={t("Recording indicator")} find="Dictation HUD position"
      detail={t(status.indicatorAvailable ? "Shows when Jaro is listening and processing your dictation."
        : "The floating indicator is unavailable on this desktop.")}>
      {labels => <select {...labels} value={status.indicatorPosition} disabled={busy}
        onChange={event => void configure("/indicator", { position: event.target.value })}>
        <option value="top">{t("Top")}</option>
        <option value="bottom">{t("Bottom")}</option>
        <option value="off">{t("Off")}</option>
      </select>}
    </SettingRow>
    <SettingRow icon={Volume2} title={t("Play dictation sounds")}
      detail={status.soundAvailable ? undefined : t("Sound playback is unavailable on this desktop.")}>
      {labels => <input {...labels} type="checkbox" role="switch" checked={status.sound ?? false} disabled={busy}
        onChange={event => void configure("/sound", { enabled: event.target.checked })} />}
    </SettingRow>
    <SettingRow icon={Volume2} title={t("Sound style")} find="Sound preview">
      {labels => <div className="dictation-row-actions">
        <select {...labels} value={status.soundStyle ?? "classic"} disabled={busy}
          onChange={event => void configure("/sound", { style: event.target.value })}>
          {dictationSoundStyles.map(({ id, title }) => <option value={id} key={id}>{t(title)}</option>)}
        </select>
        <Button type="button" size="icon-sm" variant="ghost" aria-label={t("Preview sound")} title={t("Preview sound")}
          disabled={busy || !status.soundAvailable} onClick={() => void configure("/sound/preview", {})}><Play aria-hidden="true" /></Button>
      </div>}
    </SettingRow>
    <SettingRow icon={Volume2} title={t("Sound volume")}>
      {labels => <input {...labels} key={status.soundVolume} defaultValue={status.soundVolume ?? 70} type="range" min={0} max={100}
        disabled={busy || !status.sound}
        onPointerUp={event => saveVolume(event.currentTarget)} onKeyUp={event => saveVolume(event.currentTarget)} />}
    </SettingRow>
    {audioModes.length > 1 && <SettingRow icon={Volume2} title={t("While dictating")}>
      {labels => <select {...labels} value={audio} disabled={busy}
        onChange={event => void configure("/other-audio", { behavior: event.target.value })}>
        {audioModes.map(mode => <option key={mode} value={mode}>{t(audioLabels[mode])}</option>)}
      </select>}
    </SettingRow>}
    {audio === "lower" && <SettingRow icon={Volume2} title={t("Playback volume while dictating")}
      detail={t("Percentage of the current playback volume.")}>
      {labels => <input {...labels} key={status.otherAudioPercent} type="number" min={0} max={100}
        defaultValue={status.otherAudioPercent ?? 20} disabled={busy}
        onBlur={event => {
          const input = event.currentTarget
          if (!input.validity.valid || !Number.isFinite(input.valueAsNumber)) input.valueAsNumber = status.otherAudioPercent ?? 20
          else if (input.valueAsNumber !== status.otherAudioPercent) void configure("/other-audio", { percent: input.valueAsNumber })
        }}
        onKeyDown={event => { if (event.key === "Enter") event.currentTarget.blur() }} />}
    </SettingRow>}
  </SettingGroup>
}

function PortableTextOutput({ status, disabled, configure, saveProfiles }: {
  status?: DictationStatus; disabled: boolean
  configure: (path: string, body?: object, reportError?: (message: string) => void) => Promise<boolean>
  saveProfiles: (value: string, base: string) => Promise<void>
}) {
  const { t } = useInterfaceLanguage()
  const heading = useId()
  const portalControls = status?.portalRawRequested !== undefined && !status.commandMode ? status : undefined
  const rawBusy = disabled || portalControls?.authorizing || portalControls?.phase !== "idle"
  const rawCanRetry = portalControls?.portalRawRequested && !portalControls.portalRawShortcut
    && (portalControls.enabled || portalControls.configurable || !!portalControls.annotationShortcut)
  return <SettingGroup id={heading} title={t("Output")}>
    <RuleEditor label={t("Word replacements")} help={t("Enter one correction per line, such as jaro audio => Jaro Audio.")}
      value={status?.replacements ?? ""} disabled={disabled} maxLength={4000} save={text => configure("/replacements", { text })} />
    {status?.rawShortcut !== undefined && <PortableShortcut shortcut={status.rawShortcut} defaultShortcut=""
      title="Dictate without processing" editLabel="Edit raw dictation shortcut"
      detail={t(status.enabled ? "Use this shortcut to paste speech as heard, without Output rules."
        : "Enable the global shortcut to use this binding.")}
      bindingError={status.rawShortcutError} disabled={disabled || status.phase !== "idle"}
      save={(shortcut, reportError) => configure("/raw-shortcut", { shortcut }, reportError)} />}
    {portalControls && <SettingRow icon={Keyboard} title={t("Dictate without processing")} label={false}
      detail={portalControls.portalRawShortcut || t(portalControls.authorizing ? "Waiting for desktop shortcut approval."
        : rawCanRetry ? "The desktop did not assign a raw dictation shortcut."
        : "Use a separate desktop shortcut to paste speech without Output rules.")}>
      <div className="dictation-row-actions">
        {rawCanRetry && <Button type="button" size="sm" variant="ghost" disabled={rawBusy}
          onClick={() => void configure("/raw-shortcut", { enabled: true })}>{t("Retry")}</Button>}
        {portalControls.portalRawShortcut && portalControls.configurable && <Button type="button" size="sm" variant="ghost"
          disabled={rawBusy} onClick={() => void configure("/configure")}>{t("Configure shortcuts")}</Button>}
        <input aria-label={t("Dictate without processing")} type="checkbox" role="switch"
          disabled={rawBusy} checked={portalControls.portalRawRequested}
          onChange={event => void configure("/raw-shortcut", { enabled: event.target.checked })} />
      </div>
    </SettingRow>}
    {status && <OutputProfileEditor value={status.outputProfiles ?? ""} pending={disabled}
      chooseApplication={document.documentElement.hasAttribute("data-jaro-application-picker") ? chooseApplication : undefined}
      loadApplications={loadApplications} describe={describeProfiles} previewOutput={previewProfiles} save={saveProfiles} websiteRules wayland={status.platform === "wayland"} />}
  </SettingGroup>
}

export function PortableDictationSettings({ visible, disabled, languages }: {
  visible: boolean; disabled: boolean; languages: Language[]
}) {
  const { t } = useInterfaceLanguage()
  const { status, pending, feedback, setFeedback, configure, saveVocabulary, saveProfiles } = usePortableDictation(visible)
  const [copied, setCopied] = useState<{ text: string; message: string }>()

  async function copy() {
    if (!status?.text) return
    try {
      await navigator.clipboard.writeText(status.text)
      setCopied({ text: status.text, message: "Copied last dictation." })
    } catch {
      setCopied({ text: status.text, message: "Could not copy the dictation." })
    }
  }

  async function copyCommand(action: (typeof desktopCommands)[number][0]) {
    if (!status?.commandPrefix) return
    try {
      await navigator.clipboard.writeText(`${status.commandPrefix} ${action}`)
      setFeedback("Copied shortcut command.")
    } catch {
      setFeedback("Could not copy the shortcut command.")
    }
  }

  async function replaySetup() {
    try {
      await changePortableOnboarding("restart")
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "Could not replay dictation setup.")
    }
  }

  const windows = status?.platform === "windows"
  const activity = status?.queueFull ? "Finish the queued dictations before starting another." :
    status?.phase === "starting" ? "Starting microphone…" :
    status?.phase === "recording" ? "Recording dictation…" :
    status?.phase === "transcribing" ? "Transcribing dictation…" :
    status?.phase === "delivering" ? "Delivering dictation…" :
    status?.phase === "pasting" ? "Pasting last dictation…" :
    status?.pasteAuthorizing ? "Approve keyboard and clipboard access in the desktop dialog." :
    status?.authorizing ? (windows ? "Starting the keyboard shortcut…" : "Approve the shortcut in the desktop dialog.") : status?.message
  const message = status?.phase === "idle" ? feedback || activity : activity || feedback
  const wayland = status?.platform === "wayland"
  const shortcutConnected = status?.enabled || status?.configurable || !!status?.annotationShortcut

  return <div className="dictation-preferences" data-setting-group={t("Dictation")}>
    <SettingGroup id="linux-dictation-title" title={t("System dictation")}>
      {hasPortableOnboarding && <SettingRow icon={Sparkles} title={t("Dictation setup")} label={false}>
        <Button size="sm" variant="outline" disabled={pending || status?.phase !== "idle" || Boolean(status.pendingJobs)}
          onClick={() => void replaySetup()}>{t("Replay setup")}</Button>
      </SettingRow>}
      {status?.defaultShortcut && <PortableShortcut shortcut={status.shortcut} defaultShortcut={status.defaultShortcut}
        disabled={pending || status.phase !== "idle"} save={(shortcut, reportError) => configure("/shortcut", { shortcut }, reportError)} />}
      {!status?.commandMode && <SettingRow icon={Keyboard} title={status?.defaultShortcut
        ? t("Global shortcut") : status?.shortcut || t("Global shortcut")} label={false}
        detail={windows ? undefined : t(
          wayland ? status.keyboardAllowed ? "Finish with the shortcut in the field where you want the text." :
          "Use Copy last to paste until you allow automatic paste." :
          "The shortcut inserts the transcript into the field where you started.")}>
        {status?.configurable && <Button size="sm" variant="ghost" disabled={pending || status.phase !== "idle"}
          onClick={() => void configure("/configure")}>{t("Configure shortcuts")}</Button>}
        {status?.shortcutRequested && !shortcutConnected && !status.authorizing &&
          <Button size="sm" variant="ghost" disabled={pending}
            onClick={() => void configure("/disable")}>{t("Disable on launch")}</Button>}
        <Button size="sm" variant="outline" disabled={!status || pending}
          onClick={() => void configure(wayland && status?.authorizing ? "/cancel-approval" : shortcutConnected || status?.authorizing ? "/disable" : "/enable")}>
          {t(status?.authorizing ? "Cancel approval" : shortcutConnected ? "Disable shortcut" : "Enable shortcut")}
        </Button>
      </SettingRow>}
      {hasShortcutMode(status) && <ShortcutMode platform={status.platform} mode={status.shortcutMode}
        disabled={pending || status.authorizing || status.phase !== "idle"}
        onChange={mode => void configure("/shortcut-mode", { mode })} />}
      {status?.enabled && status.recoveryShortcut && <SettingRow icon={Keyboard} title={t("Paste last dictation")}
        detail={t(wayland && !status.keyboardAllowed ? "Allow automatic paste to use this shortcut." :
          windows ? "Tap to paste. Hold to preview recent dictations." : "Reinsert the last dictation at the current cursor.")} label={false}>
        <kbd>{status.recoveryShortcut}</kbd>
      </SettingRow>}
      {status?.cancelShortcut && <SettingRow icon={Keyboard} title={t("Cancel dictation shortcut")}
        detail={t(wayland ? "Discard the current take before it is pasted." : "Discard a hands-free take.")} label={false}>
        <kbd>{status.cancelShortcut}</kbd>
      </SettingRow>}
      {status?.annotationShortcut && <SettingRow icon={Keyboard} title={t("Annotate shortcut")}
        detail={t("Tap to choose a display or save. Hold to draw on supported displays.")} label={false}>
        <kbd>{status.annotationShortcut}</kbd>
      </SettingRow>}
      {status?.gnomeAnnotation && <SettingRow icon={Keyboard} title={t("Annotate shortcut")} label={false} children={null}
        detail={t("Tap to choose a display or save. Assign the key in GNOME Settings → Keyboard → Custom Shortcuts.")} />}
      {wayland && status.commandPrefix && (!shortcutConnected || status.commandMode) && <SettingRow icon={Terminal}
        title={t("Desktop shortcut commands")} label={false}
        detail={t(status.commandMode ? "Assign these commands in your desktop’s keyboard settings." :
          "Use your desktop’s keyboard settings to bind a shortcut, even without the shortcut portal.")}>
        <Button size="sm" variant="outline" disabled={pending || status.authorizing || status.phase !== "idle"}
          onClick={() => void configure(status.commandMode ? "/disable" : "/enable-commands")}>
          {t(status.commandMode ? "Disable commands" : "Enable commands")}
        </Button>
      </SettingRow>}
      {wayland && status.gnomeShortcutAvailable && (!shortcutConnected || status.commandMode) && <SettingRow
        icon={Keyboard} title={t("GNOME keyboard shortcuts")} label={false}
        detail={t(status.gnomeShortcuts
          ? "Change these keys in GNOME Settings → Keyboard → Custom Shortcuts."
          : "Use Ctrl+Alt+Shift+D to dictate, Ctrl+Alt+Shift+X to cancel, and Ctrl+Alt+V to paste the last dictation.")}>
        <div className="dictation-row-actions">
          {status.gnomeShortcutsNeedUpdate && <Button size="sm" variant="outline"
            disabled={pending || status.authorizing || status.pasteAuthorizing || status.phase !== "idle"}
            onClick={() => void configure("/add-gnome-shortcuts")}>{t("Update GNOME shortcuts")}</Button>}
          <Button size="sm" variant="outline" disabled={pending || status.authorizing || status.pasteAuthorizing || status.phase !== "idle"}
            onClick={() => void configure(status.gnomeShortcuts ? "/remove-gnome-shortcuts" : "/add-gnome-shortcuts")}>
            {t(status.gnomeShortcuts ? "Remove GNOME shortcuts" : "Add GNOME shortcuts")}
          </Button>
        </div>
      </SettingRow>}
      {wayland && status.enabled && <SettingRow icon={Clipboard} title={t("Automatic paste")} label={false}
        detail={t("Allow keyboard and clipboard access to paste into the active field. Supported desktops can remember this access.")}>
        <div className="dictation-row-actions">
          {status.savedVirtualPaste && !status.keyboardAllowed && !status.pasteAuthorizing &&
            <Button size="sm" variant="outline" disabled={pending || status.phase !== "idle"}
              onClick={() => void configure("/disable-paste")}>{t("Forget saved access")}</Button>}
          <Button size="sm" variant="outline" disabled={pending || status.phase !== "idle"}
            onClick={() => void configure(status.pasteAuthorizing ? "/cancel-approval" : status.keyboardAllowed ? "/disable-paste" : "/allow-paste")}>
            {t(status.pasteAuthorizing ? "Cancel approval" : status.keyboardAllowed ? "Disable paste" : "Allow paste")}
          </Button>
        </div>
      </SettingRow>}
      {wayland && <SettingRow icon={Clipboard} title={t("Manual dictation")} label={false}>
        <Button size="sm" variant="outline" disabled={pending || status.authorizing || status.pasteAuthorizing ||
          (status.phase !== "recording" && !(status.canStart ?? status.phase === "idle"))}
          onClick={() => void configure("/toggle")}>
          {t(status.phase === "recording" ? "Finish dictation" : "Start dictation")}
        </Button>
      </SettingRow>}
      {status?.canCancel && <SettingRow icon={Mic} title={t("Current dictation")} label={false}
        detail={status.cancelShortcutConflict ? t("Another app is using Escape.") : undefined}>
        <Button size="sm" variant="outline" disabled={pending}
          onClick={() => void configure("/cancel")}>{t("Cancel dictation")}</Button>
      </SettingRow>}
      {status?.enabled && (!wayland || status.keyboardAllowed) && <SettingRow icon={Clipboard} title={t("Paste shortcut")}
        detail={t("Select Ctrl+Shift+V for terminals that use it.")}>
        {labels => <select {...labels} disabled={pending} value={status.terminal ? "terminal" : "standard"}
          onChange={event => void configure("/paste-shortcut?terminal=" + (event.target.value === "terminal"))}>
          <option value="standard">Ctrl+V</option>
          <option value="terminal">Ctrl+Shift+V</option>
        </select>}
      </SettingRow>}
      {message && <p role="status" className="update-settings-feedback">{t(message)}</p>}
    </SettingGroup>
    <VoiceInput visible={visible} disabled={disabled || pending || status?.phase !== "idle"}
      status={status} languages={languages} configure={configure} saveVocabulary={saveVocabulary} />
    <FeedbackSettings status={status} disabled={disabled || pending} configure={configure} />
    {status?.commandMode && <SettingGroup id="linux-dictation-commands" title={t("Commands to bind")}>
      {desktopCommands.map(([action, label]) => <SettingRow key={action} icon={Keyboard} title={t(label)} label={false}>
        <Button size="sm" variant="outline" aria-label={t("Copy command") + " · " + t(label)}
          onClick={() => void copyCommand(action)}>{t("Copy command")}</Button>
      </SettingRow>)}
    </SettingGroup>}
    {status?.text && <SettingGroup id="linux-dictation-last" title={t("Last dictation")}>
      <SettingRow icon={Clipboard} title={t("Copy to paste into your app")} label={false}>
        <Button size="sm" variant="outline" onClick={() => void copy()}>{t("Copy last")}</Button>
      </SettingRow>
      <p className="update-settings-feedback whitespace-pre-wrap break-words" dir="auto">{status.text}</p>
      {copied && copied.text === status.text && <p role="status" className="update-settings-feedback">{t(copied.message)}</p>}
    </SettingGroup>}
    <SettingDisclosure icon={Clipboard} title={t("Output and vocabulary corrections")}>
      <PortableTextOutput status={status} disabled={disabled || pending || !status}
        configure={configure} saveProfiles={saveProfiles} />
    </SettingDisclosure>
  </div>
}

export function PortableDictationWorkspace({ onSettings }: { onSettings: () => void }) {
  const { t } = useInterfaceLanguage()
  const { status, pending, feedback, setFeedback, configure } = usePortableDictation(true)
  const [playingID, setPlayingID] = useState<string>()
  const playback = useRef<{ id: string; player: HTMLAudioElement }>(null)

  const stopAudio = useCallback((id?: string) => {
    const current = playback.current
    if (!current || (id !== undefined && current.id !== id)) return
    playback.current = null
    current.player.onended = current.player.onerror = null
    current.player.pause()
    current.player.removeAttribute("src")
    current.player.load()
    setPlayingID(undefined)
  }, [])

  useEffect(() => () => stopAudio(), [stopAudio])

  useEffect(() => {
    if (status?.phase && status.phase !== "idle") stopAudio()
  }, [status?.phase, stopAudio])

  async function toggleAudio(id: string) {
    setFeedback("")
    const previous = playback.current
    if (previous) {
      stopAudio(previous.id)
      if (previous.id === id) return true
    }
    const player = new Audio()
    playback.current = { id, player }
    setPlayingID(id)
    function failed() {
      if (playback.current?.player !== player) return
      stopAudio(id)
      setFeedback("Could not play the dictation audio.")
    }
    player.onended = () => stopAudio(id)
    player.onerror = failed
    player.src = endpoint + "/history/" + encodeURIComponent(id) + "/audio"
    try {
      await player.play()
      return true
    } catch {
      failed()
      return false
    }
  }

  async function copyHistory(_ids: string[], text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setFeedback("")
      return true
    } catch {
      setFeedback("Could not copy the dictation.")
      return false
    }
  }

  return <>
    <DictationHistoryHeader onSettings={onSettings}>
      {status && <HistoryRetention value={status.retention} pending={pending || !status.historyAvailable}
        onChange={retention => void configure("/history", { action: "retention", retention })} />}
    </DictationHistoryHeader>
    <section className="dictation-history-tab" aria-label={t("Dictation history")}>
      {status?.retention === 0 && <p className="update-settings-feedback">{t("History is off. New dictations will not be saved.")}</p>}
      <DictationHistoryBrowser loadHistory={loadHistory} copyHistory={copyHistory} pending={pending}
        deleteHistory={ids => configure("/history", ids ? { action: "delete", ids } : { action: "clear" })}
        audio={{ playingID, stop: stopAudio, toggle: toggleAudio }}
        historyRevision={String(status?.historyRevision)} />
      {feedback && <p className="dictation-error" role="alert">{t(feedback)}</p>}
    </section>
  </>
}
