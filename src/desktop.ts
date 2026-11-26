import type { SpeakerSegment } from "./audio"
import type { DictationAction, ProfileApplication, DictationHistoryStats, DictationHistory, DictationOutputPreview, DictationOutputProfiles, DictationScriptReview, DictationStatus } from "./system-dictation"

export type RecordingMode = "both" | "mic" | "system"
export type CaptureSource = Exclude<RecordingMode, "both">
export type PermissionStatus = "granted" | "denied" | "restricted" | "unavailable"
export type RecordingPermissions = Partial<Record<CaptureSource, PermissionStatus>>

export function captureSources(mode: RecordingMode): CaptureSource[] {
  return mode === "both" ? ["mic", "system"] : [mode]
}

export const RECORDING_MODES: Record<RecordingMode, string> = {
  both: "Microphone + Mac audio",
  mic: "Microphone only",
  system: "Mac audio only",
}

export type DesktopAnnotationSource = "display" | "window" | "area" | "phone"
export type DesktopScreenshotSource = Exclude<DesktopAnnotationSource, "phone">
export type DesktopAnnotationAudio = { microphone: boolean; computerAudio: boolean }
export type DesktopAnnotationAction =
  | { action: "status" | "stop" | "cancel" | "reveal" }
  | { action: "start"; languages?: string[]; source?: DesktopAnnotationSource }
  | ({ action: "setAudio" } & DesktopAnnotationAudio)

export type DesktopAnnotation = {
  phase: "idle" | "starting" | "picking" | "selecting" | "recording" | "saving" | "cancelling" | "finished" | "cancelled" | "error"
  message: string
  session: string
  recoverable: boolean
  busy: boolean
  managed?: boolean
  enabled?: boolean
  audio?: DesktopAnnotationAudio
}

export type DesktopAnnotationAccess = {
  authorization: "checking" | "off" | "unavailable" | "restart" | "granted"
  canRepair: boolean
  busy: boolean
}

export type AnnotationAgent = "codex" | "claude" | "opencode" | "t3"
export type AnnotationAgentSession = { id: string; title: string; directory: string }
export type AnnotationRouteScope = { kind: "app" | "site" | "terminal"; key: string; label: string }
export type AnnotationDestinationRule = {
  id: string; scope: AnnotationRouteScope; title: string; target: string; channel: string; automatic?: boolean
}
export type SavedAnnotationRule = {
  id: string; kind: AnnotationRouteScope["kind"]; source: string; title: string; target: string; channel: string; automatic: boolean
}
export type AnnotationRuleDestinationChange =
  ({ action: "add"; kind: AnnotationRouteScope["kind"]; source: string } | { action: "retarget"; id: string })
  & { target: string; service: string; project?: true }
export type AnnotationRulesAction = { action: "list" } | { action: "remove"; id: string }
  | { action: "setAutomatic"; id: string; enabled: boolean } | AnnotationRuleDestinationChange
export type AnnotationDestinations = {
  service: string | null
  sessions: AnnotationAgentSession[]
  warning?: string
  scopes?: AnnotationRouteScope[]
  rule?: (AnnotationDestinationRule & { available: boolean }) | null
  suggestion?: { sessionId: string; reason: string } | null
}
export type AnnotationRouteStatus = {
  capture: string
  phase: "idle" | "transcribing" | "waiting" | "offer" | "countdown" | "held" | "preparing" | "sending" | "sent" | "cancelled" | "failed" | "unconfirmed"
  title?: string
  message?: string
  seconds?: number
  receiptStatus?: AnnotationDelivery["status"]
}
export type AnnotationDelivery = {
  deliveryId: string
  annotation: string
  service: string
  session: AnnotationAgentSession
  message: string | null
  status: "queued" | "withdrawn" | "received" | "missing" | "unconfirmed" | "withdrawal-unconfirmed" | "refused"
    | "corrected" | "correction-unconfirmed" | "reverted" | "rollback-files" | "rollback-staged"
    | "rollback-unconfirmed" | "rollback-commit-unconfirmed"
  includedSheet: boolean
  createdAt: number
  undoUntil: number
  canUndo: boolean
  correctionMessage: string | null
  canCorrect: boolean
  rememberedRule?: { status: "pending" | "saved" | "changed" | "restored" | "removed"; label: string }
}

export type DesktopUpdateChannel = "stable" | "dev"
export type DesktopDockIcon = { enabled: boolean; menuBarAvailable: boolean }
export type DesktopLoginAction = { action: "status" | "openSettings" } | { action: "set"; enabled: boolean }
export type DesktopLoginItem = { state: "enabled" | "disabled" | "approval" | "unknown"; message?: string }
export type DesktopInterfaceLanguage = { selected: string; resolved: string; choices: Record<string, string>; messages: Record<string, string> }
export type DesktopUpdateAction = { action: "status" | "restart" | "download" | "check" }
  | { action: "channel"; channel: DesktopUpdateChannel }
  | { action: "confirmSwitch"; offer: string }
  | { action: "releaseNotes"; version: string; channel: DesktopUpdateChannel }
export type DesktopUpdate = {
  phase: "idle" | "unavailable" | "checking" | "current" | "downloading" | "staging" | "ready" | "download" | "applying" | "error" | "switch"
  version?: string
  currentVersion?: string
  notes?: string
  message?: string
  installedChannel?: DesktopUpdateChannel
  channel?: DesktopUpdateChannel
  channels?: DesktopUpdateChannel[]
  channelManaged?: boolean
  updatesDisabled?: boolean
  restartBlocked?: boolean
  restartBlockReason?: string
  restartInProgress?: boolean
  restartError?: string
  staged?: boolean
  stageChannel?: DesktopUpdateChannel
  releaseNotesAvailable?: boolean
  offer?: string
  offerVersion?: string
  manualCheck?: boolean
  openUpdateSettings?: string
  trayRestart?: string
}

export type DesktopMeetingCalendar = {
  access: "granted" | "notDetermined" | "denied"
  connected: boolean; reminders: boolean; lead: number
  calendars: { id: string; title: string; source: string; included: boolean }[]
  upcoming: { id: string; title: string; start: number; end: number; provider: string; canJoin: boolean }[]
}

export type DesktopMeetingAudioAccess = {
  authorization: "granted" | "request" | "denied" | "unknown"
  screen: boolean
  canRepair: boolean
  busy: boolean
}

export type MeetingRecordingPolicy = "ask" | "auto" | "never"

export type DesktopMeeting = {
  phase: "idle" | "offered" | "reminder" | "starting" | "recording" | "callEnded" | "saving" | "finished" | "error" | "permission" | "interrupted"
  message: string
  detection: boolean
  managed?: boolean
  busy: boolean
  provider: string
  title?: string
  session: string
  hasText: boolean
  hasNewText: boolean
  copying?: boolean
  copyPartial?: boolean
  autoStop?: boolean
  noiseCleanup?: boolean
  boostMicrophone?: boolean
  autoStopSeconds?: number
  calendar?: DesktopMeetingCalendar
  showIndicator?: boolean
  indicatorCorner?: MeetingIndicatorCorner
  permission?: CaptureSource | ""
  recoveryAvailable?: boolean
  microphoneSilent?: boolean
  microphoneUnavailable?: boolean
  systemAudioUnavailable?: boolean
  systemAudioPermissionDenied?: boolean
  microphone?: string
  defaultPolicy?: MeetingRecordingPolicy
  providerPolicies: {
    provider: string; label: string; scope?: string; policy: MeetingRecordingPolicy
    canAlwaysRecord: boolean; inheritsPolicy?: boolean; lastSeen?: number; website?: boolean
  }[]
}

export type MeetingIndicatorCorner = "topLeft" | "topRight" | "bottomLeft" | "bottomRight"

export type MeetingCallAction =
  | { action: "detection"; enabled: boolean }
  | { action: "autoStop"; enabled: boolean }
  | { action: "setDefaultPolicy"; policy: MeetingRecordingPolicy }
  | { action: "setPolicy"; provider: string; policy: MeetingRecordingPolicy | "inherit" }

export type DesktopMeetingAction =
  | MeetingCallAction
  | { action: "status" | "stop" | "copyNew" | "copyAll" }
  | { action: "start"; languages: string[] }
  | { action: "indicator"; enabled: boolean }
  | { action: "indicatorCorner"; corner: MeetingIndicatorCorner }
  | { action: "retryPermission" | "recordingPrivacy" | "dismissPermission" | "recoverMeeting" | "dismissRecovery" | "refreshRecovery" }
  | { action: "showLiveTranscript" }
  | { action: "keepRecording" | "calendarConnect" | "calendarDisconnect" | "calendarSettings" }
  | { action: "calendarReminders"; enabled: boolean }
  | { action: "noiseCleanup" | "boostMicrophone"; enabled: boolean }
  | { action: "calendarLead"; minutes: number }
  | { action: "calendarInclude"; enabled: boolean; id: string }
  | { action: "calendarEvent"; id: string; choice: "join" | "record" | "joinRecord" | "dismiss" }

type WindowChrome = { controlsEnd: number; controlsCenterY: number; fullscreen: boolean }

function applyWindowChrome(chrome: WindowChrome) {
  if (!chrome || !Number.isFinite(chrome.controlsEnd) || !Number.isFinite(chrome.controlsCenterY)) return
  const style = document.documentElement.style
  style.setProperty("--window-controls-end", Math.max(8, chrome.controlsEnd) + "px")
  style.setProperty("--window-controls-center-y", Math.max(15, chrome.controlsCenterY) + "px")
}

type CaptureOptions = { diarization: boolean; languages: string[] }
export type RecordingDraft = { id: string; seconds: number; meeting?: boolean; screen?: boolean; videoOnly?: boolean }
type ActiveCapture = { dictation?: string; meeting?: string; warm?: boolean; mode: RecordingMode; live: boolean; seconds?: number; text?: string; tentative?: string; segments?: SpeakerSegment[] }
export type DictationDestination = "dictation" | "history" | "output"
export type WindowNavigation = {
  id: string
  pane: DictationDestination | "settings" | "general" | "meetings" | "annotate" | "home" | "live-meeting" | "meeting-library" | "vocabulary" | "add-term"
}

export type AnnotationSkillAgent = "claude" | "codex" | "opencode" | "pi"
export type AnnotationAgentSkillRequest = { action: "guide" | "status" }
  | { action: "install" | "update"; agent: AnnotationSkillAgent }
export type AnnotationSkillStatus = {
  agent: AnnotationSkillAgent; path: string; found: boolean; state: "installed" | "missing" | "managed" | "different"
}
export type AnnotationAgentSkillResult = { agents: AnnotationSkillStatus[] } | { markdown: string }

type Requests = {
  workspaceReady: undefined
  microphoneLevel: undefined
  windowNavigation: undefined
  workspaceToolbar: { inset: number }
  annotation: DesktopAnnotationAction
  sessionScreenshot: { source: DesktopScreenshotSource }
  annotationAccess: { action: "status" | "allow" | "repair" | "restart" }
  annotationStorage: { action: "status" | "choose" | "reveal" | "reset" }
  annotationAgentSkill: AnnotationAgentSkillRequest
  copyAnnotation: { id: string }
  pasteAnnotation: { id: string }
  annotationRoutingPolicy: undefined
  annotationRules: AnnotationRulesAction
  annotationDestinations: { query: string; id?: string }
  annotationAgentDestinations: { agent: Exclude<AnnotationAgent, "opencode">; query: string }
  sendSessionToAgent: { agent: AnnotationAgent; target: string; id: string; service?: string; open?: boolean }
  sendAnnotationToAgent: { agent: Exclude<AnnotationAgent, "opencode">; target: string; id: string }
  rememberAnnotationDestination: { id: string; service: string; target: string; scope: AnnotationRouteScope["kind"]; scopeKey?: string; automatic: boolean }
  forgetAnnotationDestination: { id: string; ruleId: string }
  sendAnnotation: { id: string; target: string; service: string }
  annotationDeliveries: { id: string }
  annotationRoute: { id: string; action: "status" | "cancel" }
  undoAnnotationDelivery: { deliveryId: string; restoreFiles?: boolean }
  correctAnnotationDelivery: { deliveryId: string }
  meetings: DesktopMeetingAction
  copyMeetingTranscript: { id: string }
  meetingAudioAccess: { action: "status" | "allow" | "repair" }
  dockIcon: { action: "status" } | { action: "set"; enabled: boolean }
  agentSetup: undefined
  loginItem: DesktopLoginAction
  interfaceLanguage: { action: "status" } | { action: "set"; selected: string }
  update: DesktopUpdateAction
  systemDictation: DictationAction
  dictationCleanup: { action: "status" | "prepare" }
  permission: { source: CaptureSource; prompt: boolean }
  openPrivacy: { source: CaptureSource }
  revealApp: undefined
  devices: undefined
  inputDevices: undefined
  record: CaptureOptions & { mode: RecordingMode; live: boolean; dictation?: string }
  stop: undefined
  dictation: { action: "stop" | "cancel" | "discard" | "transcribe"; id: string; languages?: string[] }
  live: { enabled: boolean }
  openAudio: CaptureOptions
  revealSessionMedia: { id: string }
  recover: CaptureOptions & { id: string }
  discard: { id: string }
  export: { id: string; kind: "audio" | "json" | "md" | "notes" | "srt" | "vtt" | "annotation-manifest" | "annotation-contact-sheet" | "annotation-package" }
    | { id: string; kind: "annotation-clip"; startMs: number; endMs: number }
  meetingExports: { action: "status" | "choose" | "disable" } | { action: "reveal"; id?: string }
    | { action: "revealFiles"; id: string }
    | { action: "separateNotes" | "descriptiveNames"; enabled: boolean }
}
type Responses = {
  workspaceReady: null
  microphoneLevel: { peak: number | null }
  windowNavigation: { navigation?: WindowNavigation }
  workspaceToolbar: null
  annotation: DesktopAnnotation
  sessionScreenshot: { jpeg?: string }
  annotationAccess: DesktopAnnotationAccess
  annotationStorage: { folder: string; custom: boolean; available: boolean }
  annotationAgentSkill: AnnotationAgentSkillResult
  copyAnnotation: { includedImage: boolean }
  pasteAnnotation: { destination: string; includedSheet: boolean }
  annotationRoutingPolicy: { managed: boolean }
  annotationRules: { managed: boolean; rules: SavedAnnotationRule[] }
  annotationDestinations: AnnotationDestinations
  annotationAgentDestinations: { sessions: AnnotationAgentSession[] }
  sendSessionToAgent: { images: "attached" | "references" | "none"; opened: boolean }
  sendAnnotationToAgent: { contactSheet: "attached" | "reference" | "none" }
  rememberAnnotationDestination: AnnotationDestinationRule
  forgetAnnotationDestination: { forgotten: boolean }
  sendAnnotation: AnnotationDelivery & { delivery: "queued" }
  annotationDeliveries: { deliveries: AnnotationDelivery[] }
  annotationRoute: AnnotationRouteStatus
  undoAnnotationDelivery: AnnotationDelivery
  correctAnnotationDelivery: AnnotationDelivery
  meetings: DesktopMeeting
  copyMeetingTranscript: { copied: boolean }
  meetingAudioAccess: DesktopMeetingAudioAccess
  dockIcon: DesktopDockIcon
  agentSetup: { cli: string }
  loginItem: DesktopLoginItem
  interfaceLanguage: DesktopInterfaceLanguage
  update: DesktopUpdate
  systemDictation: { stats: DictationHistoryStats | null } | { application: ProfileApplication | null } | { applications: ProfileApplication[] } | DictationStatus | DictationHistory | DictationOutputPreview | DictationOutputProfiles | DictationScriptReview
  dictationCleanup: { status: "notInstalled" | "downloading" | "ready" | "error" }
  permission: { status: PermissionStatus }
  openPrivacy: unknown
  revealApp: unknown
  devices: DesktopDevices
  inputDevices: DesktopInputDevices
  record: unknown
  stop: unknown
  dictation: { text: string } | null
  live: unknown
  openAudio: { id: string } | null
  revealSessionMedia: null
  recover: { id: string }
  discard: unknown
  export: { saved: boolean; name?: string } | null
  meetingExports: { folder: string | null; separateNotes?: boolean; descriptiveNames?: boolean }
}
export type DesktopInputDevices = {
  microphone: string
  dictationMicrophones?: [system: string, preferNonBluetooth: string]
  inputs?: { id: string; name: string; unavailable?: boolean }[]
}
export type DesktopDevices = DesktopInputDevices & {
  capture?: ActiveCapture | null
  drafts?: RecordingDraft[]
}
export type DesktopBridge = {
  request<A extends keyof Requests>(action: A, ...args: Requests[A] extends undefined ? [] : [Requests[A]]): Promise<Responses[A]>
}

export type DesktopCaptureEvent = { dictation?: string; meeting?: string } & (
  | { event: "started"; mode: RecordingMode; microphone: string; live?: boolean; warm?: boolean }
  | { event: "levels"; seconds: number; mic: number; system: number; microphone?: string }
  | { event: "sourceChanged"; microphone: string; message: string }
  | { event: "preview"; text: string; tentative?: string; segments?: SpeakerSegment[] }
  | { event: "finished"; reason?: string }
  | { event: "accepted" | "dictationReady"; id: string; interruption?: string }
  | { event: "warning"; message: string }
  | { event: "error"; message: string; permission?: CaptureSource; draft?: string }
)

declare global {
  interface Window {
    jardDesktop?: DesktopBridge
    zero?: { invoke<R>(command: string, payload: unknown): Promise<R> }
  }
  interface WindowEventMap {
    "jard-capture": CustomEvent<DesktopCaptureEvent>
    "native-sdk:jard-window-navigation": CustomEvent<WindowNavigation>
    "native-sdk:jard-update": CustomEvent<DesktopUpdate>
    "native-sdk:jard-dock-icon": CustomEvent<DesktopDockIcon>
    "native-sdk:jard-interface-language": CustomEvent<DesktopInterfaceLanguage>
    "native-sdk:jard-input-devices": CustomEvent
    "native-sdk:jard-annotation": CustomEvent<DesktopAnnotation>
    "native-sdk:jard-annotation-routing": CustomEvent<AnnotationRouteStatus>
    "native-sdk:jard-annotation-delivery-changed": CustomEvent<{ capture: string }>
    "native-sdk:jard-meetings": CustomEvent<DesktopMeeting>
  }
}

// The portable shell serves the browser workspace until its desktop adapters are available.
if (typeof window.zero?.invoke === "function" && !window.jardDesktop
  && !document.documentElement.hasAttribute("data-jaro-portable-workspace")) {
  const host = window.zero
  window.addEventListener("native-sdk:jard-chrome", event => {
    applyWindowChrome((event as CustomEvent<WindowChrome>).detail)
  })
  void host.invoke<WindowChrome>("jard.chrome", {}).then(applyWindowChrome).catch(() => {
    // The CSS safe area also works while the host is measuring a newly opened window.
  })
  window.jardDesktop = {
    request<A extends keyof Requests>(action: A, ...args: Requests[A] extends undefined ? [] : [Requests[A]]) {
      return host.invoke<Responses[A]>("jard." + action, args[0] ?? {})
    },
  }
  window.addEventListener("native-sdk:jard-capture", event => {
    window.dispatchEvent(new CustomEvent("jard-capture", { detail: (event as CustomEvent<DesktopCaptureEvent>).detail }))
  })
}

if (typeof window.jardDesktop?.request === "function") {
  document.documentElement.dataset.nativeWindow = "true"
}

/** The browser and bundled app use exactly the same React screens. */
export function desktopBridge() {
  return typeof window.jardDesktop?.request === "function" ? window.jardDesktop : undefined
}

export function savedRecordingMode(): RecordingMode {
  try {
    const value = localStorage.getItem("jard.recordingMode")
    if (value === "mic" || value === "system") return value
  } catch { /* A private storage profile can still record. */ }
  return "both"
}
