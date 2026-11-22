import { useCallback, useEffect, useState } from "react"
import type { DesktopBridge } from "./desktop"

export type DictationStep = "welcome" | "access" | "practice" | "ready" | "done" | "later"
export type DictationShortcutSide = "left" | "either" | "right"
export const dictationSoundStyles = [
  { id: "classic", title: "Classic", done: false },
  { id: "signature", title: "Signature", done: true },
  { id: "click", title: "Click", done: true },
  { id: "soft-tone", title: "Soft tone", done: true },
  { id: "wood", title: "Wood", done: true },
  { id: "analog", title: "Warm synth", done: true },
  { id: "thock", title: "Thock", done: false },
] as const
export type DictationSoundCue = "sample" | "start" | "stop" | "done" | "error"
export type DictationSoundAnimation = { id: number; cue: DictationSoundCue; style: DictationPreferences["soundSet"]; startedAt: number }
export type DictationPreferences = {
  enabled: boolean
  onboarding: DictationStep
  mode: "holdDouble" | "hold" | "tap" | "doubleTap"
  sound: boolean
  placement: "top" | "bottom" | "cursor" | "off"
  output: "paste" | "copy" | "type"
  replacements: string
  appRules: string
  fillerWords: boolean
  spokenFormatting: boolean
  retention: 0 | 1 | 7 | 30
  languages: string[]
  inputDevice: string
  avoidBluetooth: boolean
  vocabulary: string
  livePreview: boolean
  keepMicrophoneReady: boolean
  outputProfiles: string
  rewriteInstructions: string
  soundSet: typeof dictationSoundStyles[number]["id"] | "soft" | "glass"
  soundVolume: number
  audioWhileDictating: "none" | "mute" | "lower" | "pause"
  lowerAudioPercent: number
}
export type DictationVocabularyEntry = { term: string; soundsLike: string[] }
export type DictationStatus = DictationPreferences & {
  vocabularyEntries?: DictationVocabularyEntry[]
  shortcutLabel: string
  shortcutSide: DictationShortcutSide | ""
  rawLabel: string
  pasteLabel: string
  cancelLabel: string
  annotateLabel?: string
  annotationEnabled?: boolean
  annotationShowOverlays?: boolean
  annotationListener?: boolean
  annotationRecorderAvailable?: boolean
  annotationShortcutConflict?: boolean
  shortcutTarget: "" | "shortcut" | "raw" | "paste" | "cancel" | "annotate"
  microphone: "granted" | "request" | "denied" | "restricted"
  accessibility: boolean
  accessibilityRequested: boolean
  accessibilityStale: boolean
  listenerNeedsRestart: boolean
  mediaAccessMessage: string
  browserAccessTarget?: string
  browserAccessMessage?: string
  inputMonitoring: boolean
  listener: boolean
  pendingJobs: number
  soundPreview?: boolean
  soundPreviewRevision?: number
  soundPreviewAnimation?: DictationSoundAnimation
  outputScriptName: string
  phase: "idle" | "starting" | "listening" | "processing" | "inserted" | "saved" | "cancelled" | "empty" | "error"
  message: string
  lastFailure: string
  warmInput?: { phase: "fallback"; microphone: string } | { phase: "unavailable"; message: string }
  preferencesWarning?: string
  preferencesRevision?: number
  preview: string
  choosingShortcut: boolean
  hasLast: boolean
  historyRevision: string
}
export type DictationHistoryEntry = {
  id: string
  time: number
  text: string
  raw: string
  app: string
  kind?: "dictation" | "raw" | "printed"
  mode?: string
  processing?: {
    latencyMs: number
    model?: string
    fallbacks?: ("rewrite" | "transformations")[]
  }
  seconds?: number
  inferenceMs?: number
  totalMs?: number
}
export type DictationHistory = { entries: DictationHistoryEntry[]; total: number; words: number; seconds: number }
export type DictationOutputProfile = {
  target: string
  steps: string[]
  vocabulary: string[]
  instruction: string
  deadline: number
  corrections: string
}
export type DictationOutputProfiles = { profiles: DictationOutputProfile[] }

export function profileName(target: string) {
  if (target.toLowerCase() === "default") return "Default"
  const name = target.slice(target.indexOf(":") + 1)
  return name || "Untitled profile"
}
export type DictationOutputPreview = { text: string; steps: string[]; profile: string; vocabulary: string[]; warnings: string[]; skipped?: string[] }
export type DictationLocalTransform = { id: string; name: string; description: string }
export type DictationTransformGrants = { network?: boolean; programs?: boolean; pageContext?: boolean }
export type DictationTransformPermissions = DictationTransformGrants & {
  hosts?: string[]; commands?: string[]; partial?: boolean
}
export type DictationScriptReview = {
  state: "none" | "found" | "unavailable" | "review" | "approved"
  version?: string; token?: string; message?: string; file?: string; transforms?: DictationLocalTransform[]; stale?: boolean
  kind?: "embedded" | "executable"
  policy?: "normal" | "loopback" | "disabled"
  permissions?: DictationTransformPermissions; grants?: DictationTransformGrants
}
export type DictationAction =
  | { action: "status" | "microphone" | "accessibility" | "repairAccessibility" | "inputMonitoring" | "reopen" | "cancelShortcut" | "clearRawShortcut" | "resetShortcut" | "resetCancelShortcut" | "resetPasteShortcut" | "copyLast" | "chooseOutputScript" | "clearOutputScript" | "requestMediaAccess" | "openMediaAutomationSettings" }
  | { action: "cancelAnnotationShortcut" | "resetAnnotationShortcut" }
  | { action: "setAnnotationEnabled"; enabled: boolean }
  | { action: "setAnnotationOverlays"; show: boolean }
  | { action: "requestBrowserAccess" | "openBrowserAutomationSettings" }
  | { action: "previewSound"; cue: DictationSoundCue; style?: DictationPreferences["soundSet"] }
  | { action: "stopSoundPreview" }
  | { action: "copyOutputAgentPrompt" | "revealOutputSettings" }
  | { action: "inspectOutputScript" | "revealOutputScript" | "createOutputConfig" }
  | { action: "approveOutputScript"; token: string; grants?: DictationTransformGrants }
  | { action: "editVocabulary"; base: DictationVocabularyEntry[]; entries: DictationVocabularyEntry[] }
  | { [K in keyof DictationPreferences]: { action: "set"; key: K; value: DictationPreferences[K] } }[keyof DictationPreferences]
  | { action: "chooseShortcut"; target?: "shortcut" | "raw" | "paste" | "cancel" | "annotate" }
  | { action: "setShortcutSide"; value: DictationShortcutSide }
  | { action: "bulkHistory"; operation: "copy" | "delete"; ids: string[] }
  | { action: "clearHistory" }
  | { action: "previewOutput"; text: string; profiles: string; app: string; site: string }
  | { action: "tryOutput"; text: string }
  | { action: "describeOutputProfiles"; profiles: string }
  | { action: "history"; query: string; offset: number }

export function useSystemDictation(native: DesktopBridge | undefined) {
  const [status, setStatus] = useState<DictationStatus>()
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const [settingsVisible, setSettingsVisible] = useState(false)

  const acceptStatus = useCallback((next: DictationStatus) => {
    setStatus(previous => {
      if (next.preferencesRevision !== undefined && previous?.preferencesRevision !== undefined &&
        next.preferencesRevision < previous.preferencesRevision) return previous
      if (next.soundPreviewRevision !== undefined && previous?.soundPreviewRevision !== undefined &&
        next.soundPreviewRevision < previous.soundPreviewRevision) {
        return { ...next, soundPreview: previous.soundPreview, soundPreviewAnimation: previous.soundPreviewAnimation,
          soundPreviewRevision: previous.soundPreviewRevision }
      }
      return next
    })
  }, [])

  const run = useCallback(async (input: Exclude<DictationAction, { action: "history" }>) => {
    if (!native) return false
    setError("")
    setPending(true)
    try {
      const result = await native.request("systemDictation", input)
      if (result && "onboarding" in result) acceptStatus(result)
      return true
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not update dictation. Try again.")
      return false
    } finally {
      setPending(false)
    }
  }, [native, acceptStatus])

  const saveVocabulary = useCallback(async (base: DictationVocabularyEntry[], entries: DictationVocabularyEntry[]) => {
    const result = await native?.request("systemDictation", { action: "editVocabulary", base, entries })
    if (!result || !("onboarding" in result) || !result.vocabularyEntries) throw new Error("Jaro could not save the vocabulary.")
    acceptStatus(result)
    return result.vocabularyEntries
  }, [native, acceptStatus])

  const step = status?.onboarding
  const polling = step === "access" || step === "practice" || step === "ready" || settingsVisible
  useEffect(() => {
    if (!native) return
    let active = true
    const refresh = () => {
      void native.request("systemDictation", { action: "status" }).then(result => {
        if (active && result && "onboarding" in result) acceptStatus(result)
      }).catch(() => {
        // The native workspace can still be opening.
      })
    }
    const initial = window.setTimeout(refresh, 0)
    const interval = polling ? window.setInterval(refresh, 420) : undefined
    window.addEventListener("focus", refresh)
    const replay = (event: Event) => { if (active) acceptStatus((event as CustomEvent<DictationStatus>).detail) }
    window.addEventListener("native-sdk:jard-dictation-setup", replay)
    window.addEventListener("native-sdk:jard-dictation-preferences", replay)
    return () => {
      active = false
      window.clearTimeout(initial)
      window.clearInterval(interval)
      window.removeEventListener("focus", refresh)
      window.removeEventListener("native-sdk:jard-dictation-setup", replay)
      window.removeEventListener("native-sdk:jard-dictation-preferences", replay)
    }
  }, [native, polling, acceptStatus])

  return { status, error, pending, run, saveVocabulary, setSettingsVisible }
}

export type SystemDictationControls = ReturnType<typeof useSystemDictation>
