import { useInterfaceLanguage } from "@/interface-language"
import { useCallback, useEffect, useEffectEvent, useRef, useState, type ChangeEvent } from "react"
import { AudioLines, ChevronDown, FolderOpen, Mic, Plus, Square, UsersRound } from "lucide-react"

import { captureSources, desktopBridge, RECORDING_MODES, savedRecordingMode, type CaptureSource, type DesktopCaptureEvent, type RecordingDraft, type RecordingMode } from "./desktop"
import { useRecordingAccess } from "./use-recording-access"
import { DesktopAnnotationControls } from "@/components/desktop-annotation"
import { DesktopMeetingControls, PortableMeetingSettings } from "@/components/desktop-meetings"
import type { PortableMeetingControls } from "@/components/meetings-workspace"
import { DesktopUpdateControl, DesktopUpdateFeedback, DesktopUpdateSettings } from "@/components/desktop-update"
import { DesktopApplicationSettings } from "@/components/desktop-application-settings"
import { portableLogin, PortableApplicationSettings } from "@/components/launch-at-login"
import { AgentSetupSettings } from "@/components/agent-setup-settings"
import { hasPortableDictation, PortableDictationSettings, PortableDictationWorkspace } from "@/components/portable-dictation-workspace"
import { PortableModelSetup, type ModelSetup } from "@/components/portable-model-setup"
import { PortableOnboardingTour } from "@/components/portable-onboarding-tour"
import { hasPortableOnboarding } from "@/portable-onboarding"
import { hasPortableScreenRecording, hasDesktopAnnotationSettings, PortableScreenRecording, DesktopAnnotationSettings } from "@/components/portable-screen-recording"
import { portableUpdates, useDesktopUpdate } from "@/desktop-update"
import { useSystemDictation } from "./system-dictation"
import { discardPortableRecording, finishPortableRecording, followPortableRecording, hasPortableAudio, portableRecordingPreview, portableRecordingDrafts, openPortableRecording, setPortableRecordingPreview, type PortableRecording, type SourceLevels } from "./portable-capture"
import { usePortableCalls, type DetectedCall } from "./portable-calls"
import { DictationVocabulary, type VocabularyRequest } from "@/components/dictation-vocabulary"
import { SystemDictationSettings, SystemDictationTour, SystemDictationWorkspace } from "@/components/system-dictation"
import { RecordingAccessDialog, RecordingAccessSettings } from "@/components/recording-permissions"
import { discardSessionDrafts, readWorkspacePreference, writeWorkspacePreference } from "./workspace-drafts"
import { readSessionRoute, sessionTab, writeSessionRoute } from "./session-route"
import { clock, transcriptForClipboard } from "./speaker-activity"
import { copySavedMeetingTranscript } from "./desktop-meeting"
import { QuitButton } from "@/components/quit-button"
import { SessionLibrary } from "@/components/session-library"
import { ResultsPanel } from "@/components/results-panel"
import { Downloads, type DownloadItem } from "@/components/downloads"
import { LanguagePicker, type Language } from "@/components/language-picker"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { ActionTooltip } from "@/components/ui/tooltip"
import {
  EMPTY_TRANSCRIPT,
  isSettledMeeting,
  livePreview,
  MAX_SECONDS,
  MissingSessionError,
  readSession,
  openMicrophone,
  transcribeMedia,
  type AudioFrames,
  type Microphone,
  type SavedSession,
} from "./audio"

type ModelStatus = "loading" | "ready" | "error"
type Phase = "idle" | "starting" | "recording" | "transcribing"
type ProcessingAudio = { seconds: number; duration: number | null }
type Session = {
  microphone: Microphone | PortableRecording
  languages: string[]
  diarization: boolean
  meeting: boolean
  id?: string
  notesDraftId?: string
  preview?: ReturnType<typeof livePreview>
  previewUpdate?: Promise<void>
}

function message(error: unknown) {
  if (error instanceof DOMException && (error.name === "NotAllowedError" || error.name === "SecurityError")) {
    return "Microphone access was denied. Allow it in your browser or open an audio file."
  }
  if (error instanceof DOMException && (error.name === "NotFoundError" || error.name === "NotReadableError")) {
    return "The microphone is unavailable. Check your input device or open an audio file."
  }
  return error instanceof Error ? error.message : "Something went wrong. Try again."
}

export default function App() {
  const [quitting, setQuitting] = useState(false)
  return quitting
    ? <main className="quit-screen" role="status"><h1>Jaro is shutting down.</h1><p>{desktopBridge() ? "You can close this window." : "You can close this tab."}</p></main>
    : <Workspace onQuit={() => setQuitting(true)} />
}

function Workspace({ onQuit }: { onQuit: () => void }) {
  const { t } = useInterfaceLanguage()
  const [dictating, setDictating] = useState(false)
  const [screenBusy, setScreenBusy] = useState(false)
  const [status, setStatus] = useState<ModelStatus>("loading")
  const [modelSetup, setModelSetup] = useState<ModelSetup>()
  const [modelStatusRequest, setModelStatusRequest] = useState(0)
  const [phase, setPhase] = useState<Phase>("idle")
  const [languageOptions, setLanguageOptions] = useState<Language[]>([])
  const [selectedLanguages, setSelectedLanguages] = useState<Language[]>([])
  const [diarization, setDiarization] = useState(true)
  const [speakerLabelsAvailable, setSpeakerLabelsAvailable] = useState(false)
  const [live, setLive] = useState(true)
  const [previewPaused, setPreviewPaused] = useState(false)
  const [tab, setTab] = useState("transcript")
  const tabRef = useRef(tab)
  const setActiveTab = useCallback((next: string) => {
    tabRef.current = next
    setTab(next)
  }, [])
  const keepNotesTab = useCallback((fallback: string) => tabRef.current === "notes" ? "notes" : fallback, [])
  const [captureId, setCaptureId] = useState<string | null>(null)
  const captureIdRef = useRef<string | null>(null)
  const [captureFlowKey, setCaptureFlowKey] = useState<string | null>(null)
  const liveNotesFlush = useRef<(() => Promise<boolean>) | null>(null)
  const savedNotesDrafts = useRef<Set<string>>(new Set())
  const setActiveCapture = useCallback((id: string | null) => {
    captureIdRef.current = id
    setCaptureId(id)
    setCaptureFlowKey(id)
  }, [])
  const recordSavedDraft = useCallback((id: string) => {
    savedNotesDrafts.current.add(id)
  }, [])
  const [result, setResult] = useState(EMPTY_TRANSCRIPT)
  const [feedback, setFeedback] = useState("")
  const [processed, setProcessed] = useState<ProcessingAudio | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [level, setLevel] = useState(0)
  const native = desktopBridge()
  const calls = usePortableCalls(hasPortableAudio && !native)
  const detectedRecording = calls.status?.active
  const workspaceActions = useRef<HTMLElement>(null)
  const [sessionToolbar, setSessionToolbar] = useState<HTMLDivElement | null>(null)
  useEffect(() => {
    const toolbar = workspaceActions.current
    if (!native || !toolbar) return
    let reportedInset: number | undefined
    const report = () => {
      const { left, width } = toolbar.getBoundingClientRect()
      const inset = width ? Math.ceil(window.innerWidth - left) : 0
      if (inset === reportedInset) return
      reportedInset = inset
      void native.request("workspaceToolbar", { inset }).catch(() => {})
    }
    const observer = new ResizeObserver(report)
    observer.observe(toolbar)
    window.addEventListener("resize", report)
    report()
    return () => { observer.disconnect(); window.removeEventListener("resize", report) }
  }, [native])
  const [client] = useState(() => crypto.randomUUID())
  const [portableOnboarding, setPortableOnboarding] = useState<string | null>(null)
  const desktopUpdate = useDesktopUpdate(native, client)
  const recordingAccess = useRecordingAccess(native)
  const systemDictation = useSystemDictation(native)
  const [vocabularyRequest, setVocabularyRequest] = useState<VocabularyRequest>()
  const openVocabulary = useCallback((addTerm: boolean) => setVocabularyRequest({ addTerm }), [])
  const [permissionGuide, setPermissionGuide] = useState<"inspection" | "record" | null>(null)
  const [refusedSource, setRefusedSource] = useState<CaptureSource | null>(null)
  const [mode, setMode] = useState<RecordingMode>(() => {
    if (!hasPortableAudio) return savedRecordingMode()
    const saved = readWorkspacePreference("jard.recordingMode")
    return saved === "system" || saved === "both" ? saved : "mic"
  })
  const portableMeeting = hasPortableAudio && mode === "both"
  const [meetingTake, setMeetingTake] = useState(false)
  const [meetingFeedback, setMeetingFeedback] = useState("")
  const captureMode = meetingTake ? "both" : mode
  const selectedSources = captureSources(mode)
  const selectedRefusal = refusedSource && selectedSources.includes(refusedSource) ? refusedSource : null
  const needsRecordingAccess = !!selectedRefusal || (recordingAccess.checked &&
    selectedSources.some(source => recordingAccess.permissions[source] !== "granted"))
  const [microphoneName, setMicrophoneName] = useState("Mac default microphone")
  const [systemLevel, setSystemLevel] = useState(0)
  const [choosingFile, setChoosingFile] = useState(false)
  const [recoveredAudio, setRecoveredAudio] = useState<RecordingDraft[]>([])
  const [discarding, setDiscarding] = useState<string | null>(null)
  const nativeCapture = useRef(false)
  const nativeStopping = useRef(false)
  const nativeMeeting = useRef(false)
  const [liveMeeting, setLiveMeeting] = useState<{ shared: boolean } | null>(null)
  const [meetingStopPending, setMeetingStopPending] = useState(false)
  const nativeSession = useRef<AbortController | null>(null)
  const deviceRevision = useRef(0)
  const liveRevision = useRef(0)
  const [copied, setCopied] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const [current, setCurrent] = useState<DownloadItem | null>(null)
  const [restoring, setRestoring] = useState(() => !!readSessionRoute().id)
  const [jump, setJump] = useState<number>()
  const restoreRequest = useRef<AbortController | null>(null)
  const pendingNavigation = useRef(false)
  const session = useRef<Session | null>(null)
  const copyTimer = useRef<number>(undefined)

  const completedCall = calls.status?.completed
  useEffect(() => {
    if (completedCall) window.dispatchEvent(new Event("jaro-portable-meeting-saved"))
  }, [completedCall])

  useEffect(() => {
    if (!hasPortableAudio) return
    const controller = new AbortController()
    let timer: number
    async function load() {
      try {
        const { drafts, recovering } = await portableRecordingDrafts(controller.signal)
        if (controller.signal.aborted) return
        setRecoveredAudio(drafts)
        if (recovering) timer = window.setTimeout(load, 1800)
      } catch {
        // A model status change or new attempt will refresh the recovery list.
      }
    }
    void load()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [status])

  useEffect(() => {
    if (native) return
    const endpoint = `/api/clients/${client}`
    let connection: EventSource | undefined
    const connect = () => {
      connection?.close()
      connection = new EventSource(endpoint + "/stream")
      if (portableUpdates) connection.addEventListener("update", event => {
        window.dispatchEvent(new CustomEvent("native-sdk:jard-update", { detail: JSON.parse(event.data) }))
      })
      if (hasPortableOnboarding) connection.addEventListener("onboarding", event => {
        const { id } = JSON.parse(event.data) as { id: string | null }
        setPortableOnboarding(id)
      })
    }
    const disconnect = () => {
      connection?.close()
      void fetch(endpoint, { method: "DELETE", keepalive: true }).catch(() => {})
    }
    connect()
    window.addEventListener("pageshow", connect)
    window.addEventListener("pagehide", disconnect)
    return () => {
      window.removeEventListener("pageshow", connect)
      window.removeEventListener("pagehide", disconnect)
      disconnect()
    }
  }, [native, client])

  useEffect(() => {
    const controller = new AbortController()
    let revision: string | undefined
    let checking = false
    async function check() {
      if (document.hidden || checking) return
      checking = true
      try {
        const response = await fetch("/api/sessions/revision", { cache: "no-store", signal: controller.signal })
        if (!response.ok) return
        const { revision: next } = await response.json() as { revision?: string }
        if (controller.signal.aborted || typeof next !== "string" || next === revision) return
        revision = next
        window.dispatchEvent(new Event("jaro-sessions-changed"))
      } catch { /* Try again while the workspace is visible. */ }
      finally { checking = false }
    }
    void check()
    const timer = window.setInterval(check, 1500)
    document.addEventListener("visibilitychange", check)
    return () => {
      controller.abort()
      window.clearInterval(timer)
      document.removeEventListener("visibilitychange", check)
    }
  }, [])

  useEffect(() => {
    const abort = new AbortController()
    let timer: number
    let confirmed = false
    async function check() {
      try {
        const response = await fetch("/api/status", { cache: "no-store", signal: abort.signal })
        if (!response.ok) throw new Error("Jaro is unavailable.")
        const update: { status: ModelStatus; languages?: Language[]; diarization?: boolean; modelSetup?: ModelSetup } = await response.json()
        if (abort.signal.aborted) return
        if (native && !confirmed) {
          confirmed = true
          void native.request("workspaceReady").catch(() => {})
        }
        if (update.status === "ready" && update.languages) setLanguageOptions(update.languages)
        setSpeakerLabelsAvailable(update.diarization === true)
        setStatus(update.status)
        setModelSetup(update.modelSetup)
        const modelPhase = update.modelSetup?.phase
        if (update.status !== "loading" && modelPhase !== "checking" && modelPhase !== "downloading" && modelPhase !== "starting") return
      } catch {
        if (abort.signal.aborted) return
        setStatus("error")
      }
      timer = window.setTimeout(check, 650)
    }
    void check()
    return () => {
      abort.abort()
      window.clearTimeout(timer)
    }
  }, [native, modelStatusRequest])

  useEffect(() => {
    function releaseMic() {
      session.current?.preview?.stop()
      session.current?.microphone.close()
    }
    window.addEventListener("pagehide", releaseMic)
    return () => {
      window.clearTimeout(copyTimer.current)
      window.removeEventListener("pagehide", releaseMic)
      releaseMic()
    }
  }, [])

  const recording = phase === "recording"
  const busy = phase === "starting" || phase === "transcribing"
  const desktopIdle = phase === "idle" && !restoring && !choosingFile && !dictating
  const desktopReady = status === "ready" && desktopIdle
  const canRecord = desktopReady && !detectedRecording
  const canStart = canRecord && !screenBusy
  const preparingModel = status === "loading"
  const waitingForFinalization = current?.saved?.status === "interrupted" && !!current.saved.finalizationPending && !current.saved.meetingsManaged
  const processingSavedAudio = phase === "idle" && (current?.saved?.status === "processing" || waitingForFinalization)
  const working = busy || restoring || preparingModel || processingSavedAudio

  function showSession(saved: SavedSession, wanted: string, at?: number, preserveLive = false) {
    const tab = sessionTab(saved, wanted)
    setCurrent({ ...saved, saved })
    setResult(previous => preserveLive && saved.status === "processing" && !saved.transcript.text && previous.text
      ? { ...previous, tentative: undefined, transcriptSource: "rolling" } : saved.transcript)
    setActiveTab(tab)
    setJump(at)
    setCopied(false)
    setFeedback(saved.transcript.interruption ?? "")
    return tab
  }

  function clearSession(feedback = "", preserveCapture = false) {
    setCurrent(null)
    setResult(EMPTY_TRANSCRIPT)
    setActiveTab("transcript")
    setJump(undefined)
    setCopied(false)
    setFeedback(feedback)
    if (!preserveCapture) setActiveCapture(null)
  }

  function openSaved(saved: SavedSession, reopening = true, preserveLive = false) {
    restoreRequest.current?.abort()
    setRestoring(false)
    if (reopening) setActiveCapture(null)
    const wanted = saved.annotation || reopening ? "overview" : keepNotesTab("transcript")
    const tab = showSession(saved, wanted, undefined, preserveLive)
    writeSessionRoute(saved.id, tab)
  }

  async function openAcceptedSession(id: string, interruption?: string, preserveLive = false) {
    nativeCapture.current = false
    nativeStopping.current = false
    nativeSession.current?.abort()
    const controller = new AbortController()
    nativeSession.current = controller
    setLevel(0)
    setSystemLevel(0)
    const wanted = keepNotesTab("transcript")
    if (!pendingNavigation.current) writeSessionRoute(id, wanted)
    try {
      const saved = await readSession(id, controller.signal)
      if (!controller.signal.aborted && !pendingNavigation.current) {
        openSaved(saved, false, preserveLive)
        if (interruption) setFeedback("Saved the audio captured before the recording was interrupted.")
      }
    } catch (error) {
      if (!controller.signal.aborted) setFeedback(message(error))
    } finally {
      if (!controller.signal.aborted) setPhase("idle")
    }
  }

  const receiveCapture = useEffectEvent((event: DesktopCaptureEvent) => {
    if (event.dictation) return
    deviceRevision.current++
    switch (event.event) {
      case "started": {
        if (refusedSource && captureSources(event.mode).includes(refusedSource)) setRefusedSource(null)
        nativeCapture.current = true
        nativeStopping.current = false
        nativeMeeting.current = !!event.meeting
        setLiveMeeting(event.meeting ? { shared: !!event.warm } : null)
        setMeetingStopPending(false)
        setMode(event.mode)
        if (event.live !== undefined) setLive(event.live)
        setMicrophoneName(event.microphone)
        if (!pendingNavigation.current) writeSessionRoute(null)
        clearSession("", true)
        setActiveCapture(event.session ?? crypto.randomUUID())
        setPreviewPaused(false)
        setElapsed(0)
        setLevel(0)
        setSystemLevel(0)
        setPhase("recording")
        break
      }
      case "levels":
        if (!nativeCapture.current || nativeStopping.current) return
        setElapsed(Math.floor(event.seconds))
        setLevel(Math.max(0, Math.min(1, event.mic)))
        setSystemLevel(Math.max(0, Math.min(1, event.system)))
        if (event.microphone) setMicrophoneName(event.microphone)
        break
      case "sourceChanged":
        if (event.microphone) setMicrophoneName(event.microphone)
        setFeedback(event.message)
        break
      case "preview":
        if (nativeCapture.current && live && !nativeStopping.current) {
          setResult({ ...EMPTY_TRANSCRIPT, text: event.text, tentative: event.tentative || undefined, segments: event.segments ?? [] })
        }
        break
      case "finished":
        nativeStopping.current = true
        void liveNotesFlush.current?.().catch(() => {})
        setResult(previous => previous.tentative ? { ...previous, tentative: undefined } : previous)
        setPhase("transcribing")
        if (event.reason && event.reason !== "Stopped") setFeedback(event.reason)
        break
      case "accepted":
        setRecoveredAudio(drafts => drafts.filter(draft => draft.id !== event.id))
        void openAcceptedSession(event.id, event.interruption, nativeCapture.current)
        break
      case "warning":
        setFeedback(event.message)
        break
      case "error":
        nativeCapture.current = false
        nativeStopping.current = false
        setPhase("idle")
        if (captureIdRef.current && !savedNotesDrafts.current.has(captureIdRef.current)) {
          setActiveCapture(null)
        }
        if (event.permission) {
          recordingAccess.refused(event.permission)
          setRefusedSource(event.permission)
          if (!event.meeting) setPermissionGuide("record")
          setFeedback(event.meeting ? event.message : "")
        } else setFeedback(event.message)
        if (event.draft) {
          const draft = { id: event.draft, seconds: elapsed, meeting: !!event.meeting }
          setRecoveredAudio(existing => existing.some(item => item.id === draft.id) ? existing : [draft, ...existing])
        }
        break
    }
  })

  useEffect(() => {
    if (!native) return
    let disposed = false
    const receive = (e: WindowEventMap["jard-capture"]) => receiveCapture(e.detail)
    const receiveMeeting = (e: WindowEventMap["native-sdk:jard-meetings"]) => {
      if (!nativeCapture.current || !nativeMeeting.current) return
      if (e.detail.phase === "saving") setMeetingStopPending(true)
      else if (e.detail.phase === "recording" || e.detail.phase === "callEnded") setMeetingStopPending(false)
    }
    const sync = async () => {
      const revision = deviceRevision.current
      try {
        const devices = await native.request("devices")
        if (disposed || deviceRevision.current !== revision) return
        if (devices.microphone) setMicrophoneName(devices.microphone)
        setRecoveredAudio(devices.drafts ?? [])
        if (devices.capture && !devices.capture.dictation && !nativeStopping.current) {
          nativeCapture.current = true
          nativeMeeting.current = !!devices.capture.meeting
          setLiveMeeting(devices.capture.meeting ? { shared: !!devices.capture.warm } : null)
          if (!nativeMeeting.current) setMeetingStopPending(false)
          if (devices.capture.session && !captureIdRef.current) setActiveCapture(devices.capture.session)
          setMode(devices.capture.mode)
          setLive(devices.capture.live)
          setElapsed(Math.floor(devices.capture.seconds ?? 0))
          if (devices.capture.text || devices.capture.tentative) {
            setResult({
              ...EMPTY_TRANSCRIPT,
              text: devices.capture.text ?? "",
              tentative: devices.capture.tentative || undefined,
              segments: devices.capture.segments ?? [],
            })
          }
          setPhase("recording")
        }
      } catch (error) {
        if (!disposed && deviceRevision.current === revision) setFeedback(message(error))
      }
    }
    window.addEventListener("jard-capture", receive)
    window.addEventListener("native-sdk:jard-meetings", receiveMeeting)
    window.addEventListener("focus", sync)
    void sync()
    return () => {
      disposed = true
      window.removeEventListener("jard-capture", receive)
      window.removeEventListener("native-sdk:jard-meetings", receiveMeeting)
      window.removeEventListener("focus", sync)
      nativeSession.current?.abort()
    }
  }, [native, setActiveCapture])

  const restore = useEffectEvent(async () => {
    restoreRequest.current?.abort()
    if (phase !== "idle") { pendingNavigation.current = true; return }
    pendingNavigation.current = false
    const route = readSessionRoute()
    if (!route.id || route.invalid) {
      setRestoring(false)
      clearSession(route.invalid ? "This session link is invalid." : "")
      writeSessionRoute(null, "overview", true)
      return
    }
    if (current?.saved?.id === route.id) {
      const tab = sessionTab(current.saved, route.tab)
      setActiveTab(tab)
      setJump(route.at)
      setRestoring(false)
      writeSessionRoute(route.id, tab, true, route.at)
      return
    }
    const controller = new AbortController()
    restoreRequest.current = controller
    clearSession()
    setRestoring(true)
    try {
      const saved = await readSession(route.id, controller.signal)
      if (controller.signal.aborted) return
      writeSessionRoute(saved.id, showSession(saved, route.tab, route.at), true, route.at)
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof MissingSessionError) {
        discardSessionDrafts(route.id)
        writeSessionRoute(null, "overview", true)
      }
      setFeedback(error instanceof MissingSessionError ? error.message : "Could not open this session. Reload to try again.")
    } finally {
      if (!controller.signal.aborted) setRestoring(false)
    }
  })

  useEffect(() => {
    const navigate = () => { void restore() }
    navigate()
    window.addEventListener("popstate", navigate)
    return () => { restoreRequest.current?.abort(); window.removeEventListener("popstate", navigate) }
  }, [])
  useEffect(() => {
    if (phase === "idle" && pendingNavigation.current) void restore()
  }, [phase])

  const selectedId = current?.saved?.id
  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    async function refreshEditPermission() {
      try {
        const response = await fetch(`/api/sessions/${selectedId}`, { signal: controller.signal, cache: "no-store" })
        if (!response.ok) return
        const saved: SavedSession = await response.json()
        if (!controller.signal.aborted) setCurrent(item => item && item.saved && item.saved.id === selectedId
          ? { ...item, saved: { ...item.saved, transcriptEditRevision: saved.transcriptEditRevision } } : item)
      } catch { /* Reopening the session retries the permission lookup. */ }
    }
    window.addEventListener("jaro-transcript-edit-setting", refreshEditPermission)
    return () => { controller.abort(); window.removeEventListener("jaro-transcript-edit-setting", refreshEditPermission) }
  }, [selectedId])
  const selectedAnnotation = !!current?.saved?.annotation
  const selectedCustomStorage = !!current?.saved?.annotationCustomStorage
  useEffect(() => {
    if (!selectedId || phase !== "idle") return
    const controller = new AbortController()
    async function checkSelection() {
      try {
        const response = await fetch("/api/sessions", { signal: controller.signal, cache: "no-store" })
        if (!response.ok) return
        const rows: Pick<SavedSession, "id" | "audioRemovedAt" | "meetingRetentionDays">[] = await response.json()
        if (controller.signal.aborted || readSessionRoute().id !== selectedId) return
        const selected = rows.find(row => row.id === selectedId)
        if (!selected) {
          clearSession("Session no longer exists.")
          writeSessionRoute(null, "overview", true)
        } else if (selectedCustomStorage) {
          const latest = await readSession(selectedId!, controller.signal)
          if (controller.signal.aborted || readSessionRoute().id !== selectedId) return
          setCurrent(previous => {
            if (!previous?.saved || previous.saved.id !== selectedId ||
                (previous.saved.annotationStorageUnavailable === latest.annotationStorageUnavailable &&
                 previous.saved.audioRemovedAt === latest.audioRemovedAt)) return previous
            return { ...previous, audioUrl: latest.audioUrl, saved: { ...previous.saved,
              audioRemovedAt: latest.audioRemovedAt, audioUrl: latest.audioUrl, audioPath: latest.audioPath,
              annotation: latest.annotation, annotationStorageUnavailable: latest.annotationStorageUnavailable } }
          })
        } else {
          if (selected.audioRemovedAt) sessionMediaRemoved(selected.id, selected.audioRemovedAt)
          setCurrent(previous => {
            if (!previous?.saved || previous.saved.id !== selectedId || previous.saved.meetingRetentionDays === selected.meetingRetentionDays) return previous
            return { ...previous, saved: { ...previous.saved, meetingRetentionDays: selected.meetingRetentionDays } }
          })
        }
      } catch { /* Existing content stays available across a transient disconnect. */ }
    }
    window.addEventListener("focus", checkSelection)
    window.addEventListener("jaro-sessions-changed", checkSelection)
    window.addEventListener("jard-meeting-retention", checkSelection)
    window.addEventListener("jard-annotation-retention", checkSelection)
    const poll = selectedAnnotation ? window.setInterval(checkSelection, 30_000) : undefined
    return () => {
      controller.abort()
      window.clearInterval(poll)
      window.removeEventListener("focus", checkSelection)
      window.removeEventListener("jaro-sessions-changed", checkSelection)
      window.removeEventListener("jard-meeting-retention", checkSelection)
      window.removeEventListener("jard-annotation-retention", checkSelection)
    }
  }, [selectedId, selectedAnnotation, selectedCustomStorage, phase])

  // A refresh can rejoin an offline transcription after its audio has been accepted.
  // While a recording is processing, refresh its readable transcript whenever coverage advances.
  const processingId = current && (current.saved?.status === "processing" || waitingForFinalization) ? current.id : null
  const processingMeeting = !!current?.saved?.meeting
  useEffect(() => {
    if (!processingId || phase !== "idle") return
    const abort = new AbortController()
    let timer: number
    let processed: number | undefined
    let lastState: SavedSession["status"] | undefined
    async function poll() {
      try {
        const response = await fetch("/api/sessions", { signal: abort.signal, cache: "no-store" })
        if (response.ok) {
          const rows: (Pick<SavedSession, "id" | "status" | "finalizationPending"> & { processed: number })[] = await response.json()
          const found = rows.find(row => row.id === processingId)
          if (!found || (found.status !== "processing" && !found.finalizationPending)) {
            if (abort.signal.aborted || readSessionRoute().id !== processingId) return
            if (found) {
              const saved = await readSession(found.id, abort.signal)
              const route = readSessionRoute()
              if (!abort.signal.aborted && route.id === saved.id) showSession(saved, saved.annotation ? route.tab : tabRef.current === "notes" ? "notes" : "transcript")
            } else { clearSession("Session no longer exists."); writeSessionRoute(null, "overview", true) }
            return
          }
          if ((found.processed !== processed || found.status !== lastState) && readSessionRoute().id === processingId) {
            const saved = await readSession(found.id, abort.signal)
            if (!abort.signal.aborted && readSessionRoute().id === saved.id) {
              if (saved.status !== "processing" && !saved.finalizationPending) { showSession(saved, tabRef.current === "notes" ? "notes" : "transcript"); return }
              setCurrent({ ...saved, saved })
              setResult(previous => saved.transcript.text ? saved.transcript : previous)
              setFeedback(saved.transcript.interruption ?? "")
              processed = found.processed
              lastState = found.status
            }
          }
        }
      } catch { if (abort.signal.aborted) return }
      if (!abort.signal.aborted) timer = window.setTimeout(poll, 2500)
    }
    timer = window.setTimeout(poll, 1000)
    return () => { abort.abort(); window.clearTimeout(timer) }
  }, [phase, processingId])

  function changeTab(tab: string) {
    setActiveTab(tab)
    setJump(undefined)
    if (current && (viewingSession || current.saved?.status === "processing")) writeSessionRoute(current.id, tab)
  }

  function sessionMediaRemoved(id: string, audioRemovedAt: string) {
    setCurrent(item => item?.id === id && item.saved && item.saved.audioRemovedAt !== audioRemovedAt ? {
      ...item, audioUrl: null, saved: { ...item.saved, audioUrl: null, audioPath: null, recordingTracks: undefined, audioRemovedAt },
    } : item)
  }

  async function finishAudio(
    audio: Blob, languages: readonly string[],
    options: { name: string; recording?: boolean; diarization: boolean; session?: string; live?: boolean; notesDraft?: string },
  ) {
    const saved = await transcribeMedia(audio, languages, (seconds, duration) => setProcessed({ seconds, duration }), options,
      id => { if (!pendingNavigation.current) writeSessionRoute(id, keepNotesTab("transcript")) })
    if (pendingNavigation.current) return
    openSaved(saved, false)
    if (saved.status === "complete" && !saved.transcript.text) setFeedback("No speech detected. Try another recording or audio file.")
  }

  async function startRecording(meeting = false, detected?: DetectedCall) {
    if (detected ? !desktopReady || screenBusy || detected.id !== detectedRecording?.id : !canStart) return
    setFeedback("")
    if (meeting) {
      setMeetingFeedback("")
      setMeetingTake(true)
    }
    setPreviewPaused(false)
    setActiveTab("transcript")
    setPhase("starting")
    const languages = detected?.languages ?? selectedLanguages.map(({ code }) => code)
    const withSpeakers = (diarization || portableMeeting || meeting) && speakerLabelsAvailable
    const id = withSpeakers ? crypto.randomUUID() : undefined
    try {
      if (native) {
        const statuses = await recordingAccess.check(mode)
        const sources = captureSources(mode)
        if (sources.some(source => statuses[source] !== "granted")) {
          setPermissionGuide("record")
          for (const source of sources) {
            if (statuses[source] !== "unavailable") continue
            statuses[source] = await recordingAccess.request(source)
          }
          if (sources.some(source => statuses[source] !== "granted")) {
            setPhase("idle")
            return
          }
        }
        setPermissionGuide(null)
        nativeCapture.current = true
        nativeStopping.current = false
        await native.request("record", { mode, live, diarization: withSpeakers, languages })
        return
      }
      const inputMode = meeting ? "both" : mode
      const systemAudio = hasPortableAudio && inputMode === "system"
      const onFrame = (audio: AudioFrames, samples: Float32Array, levels?: SourceLevels) => {
        const seconds = audio.length / audio.sampleRate
        setElapsed(Math.floor(seconds))
        if (levels) {
          setLevel(levels.microphone)
          setSystemLevel(levels.computerAudio)
        } else {
          let energy = 0
          for (const sample of samples) energy += sample * sample
          const nextLevel = Math.min(1, Math.sqrt(energy / samples.length) * 5)
          if (systemAudio) setSystemLevel(nextLevel)
          else setLevel(nextLevel)
        }
        if (seconds >= MAX_SECONDS && !detected) {
          void stopRecording()
          return
        }
        session.current?.preview?.update(audio)
      }
      const onEnded = () => {
        if (!detected) {
          void stopRecording()
          return
        }
        const active = session.current
        if (!active || !("captureId" in active.microphone) || active.microphone.captureId !== detected.id) return
        session.current = null
        active.preview?.stop()
        active.microphone.close()
        setMeetingTake(false)
        setPhase("idle")
        void readSession(detected.id).then(saved => {
          if (!session.current && !readSessionRoute().id) openSaved(saved, false, true)
        }).catch(() => {})
      }
      const microphone = detected
        ? followPortableRecording(detected.id, detected.sampleRate, inputMode, onFrame, onEnded)
        : hasPortableAudio
        ? await openPortableRecording(inputMode, onFrame, onEnded, { meeting, languages, live, diarization: withSpeakers })
        : await openMicrophone(onFrame, onEnded)
      const onPreviewDelayed = () => setPreviewPaused(true)
      const preview = live || detected
        ? "captureId" in microphone
          ? portableRecordingPreview(microphone.captureId, transcript => { setPreviewPaused(false); setResult(transcript) }, onPreviewDelayed)
          : livePreview(languages, id, setResult, onPreviewDelayed)
        : undefined
      if (!pendingNavigation.current) writeSessionRoute(null)
      const notesDraftId = "captureId" in microphone ? microphone.captureId : crypto.randomUUID()
      session.current = { microphone, languages, preview, diarization: withSpeakers, id, meeting, notesDraftId }
      setActiveCapture(notesDraftId)
      if (detected) setLive(true)
      setElapsed(detected?.elapsed ?? 0)
      setLevel(0)
      setSystemLevel(0)
      setCopied(false)
      setCurrent(null)
      setResult(EMPTY_TRANSCRIPT)
      setPhase("recording")
    } catch (error) {
      nativeCapture.current = false
      setPermissionGuide(null)
      setFeedback(message(error))
      if (meeting) {
        setMeetingFeedback(message(error))
        setMeetingTake(false)
      }
      setPhase("idle")
    }
  }

  function toggleLive() {
    const revision = ++liveRevision.current
    if (native && nativeCapture.current) {
      void native.request("live", { enabled: !live }).catch(error => {
        if (revision === liveRevision.current) { setLive(live); setFeedback(message(error)) }
      })
    }
    const active = session.current
    if (active && !active.meeting && "captureId" in active.microphone) {
      const id = active.microphone.captureId
      active.previewUpdate = (active.previewUpdate ?? Promise.resolve())
        .then(() => session.current === active ? setPortableRecordingPreview(id, !live) : undefined)
        .catch(error => {
          if (session.current === active && revision === liveRevision.current) {
            setPreviewPaused(!live)
            setFeedback(message(error))
          }
        })
    }
    if (active && live) active.preview?.stop()
    if (live) setResult(previous => previous.tentative ? { ...previous, tentative: undefined } : previous)
    if (active && !live) {
      const onPreviewDelayed = () => setPreviewPaused(true)
      active.preview = "captureId" in active.microphone
        ? portableRecordingPreview(active.microphone.captureId, transcript => { setPreviewPaused(false); setResult(transcript) }, onPreviewDelayed)
        : livePreview(active.languages, active.id, setResult, onPreviewDelayed, active.microphone.length, result)
    }
    setPreviewPaused(false)
    setLive(!live)
  }

  async function stopRecording() {
    if (native) {
      if (!nativeCapture.current || nativeStopping.current) return
      nativeStopping.current = true
      setProcessed(null)
      setPhase("transcribing")
      try {
        void liveNotesFlush.current?.().catch(() => {})
        await native.request("stop")
      } catch (error) {
        nativeStopping.current = false
        setPhase(nativeCapture.current ? "recording" : "idle")
        setFeedback(message(error))
      }
      return
    }
    const active = session.current
    if (!active) return
    session.current = null
    setProcessed(null)
    setPhase("transcribing")
    try {
      active.preview?.stop()
      const notesSave = liveNotesFlush.current?.().catch(() => {})
      const timestamp = new Date().toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" })
      const name = `${t(active.meeting ? "Meeting" : "Recording")} ${timestamp}`
      if ("captureId" in active.microphone) {
        const id = active.microphone.captureId
        active.microphone.close()
        const saved = await finishPortableRecording(id, {
          name, languages: active.languages, diarization: active.diarization, live: !!active.preview, session: active.id,
        })
        setRecoveredAudio(drafts => drafts.filter(draft => draft.id !== id))
        await openAcceptedSession(saved.id, undefined, true)
        return
      }
      const audio = await active.microphone.stop()
      await notesSave
      const item: DownloadItem = {
        id: crypto.randomUUID(),
        name,
        languageHints: active.languages, recording: true, audio: audio.size ? audio : undefined,
      }
      if (item.audio) setCurrent(item)
      const notesDraft = active.notesDraftId && savedNotesDrafts.current.has(active.notesDraftId)
        ? active.notesDraftId : undefined
      await finishAudio(audio, active.languages, {
        name: item.name, recording: true, live: !!active.preview, diarization: active.diarization, session: active.id,
        notesDraft,
      })
    } catch (error) {
      setFeedback(message(error))
      if (active.meeting) setMeetingFeedback(message(error))
      if (hasPortableAudio) void portableRecordingDrafts().then(({ drafts }) => setRecoveredAudio(drafts)).catch(() => {})
    } finally {
      if (active.meeting) setMeetingTake(false)
      setPhase("idle")
    }
  }

  async function chooseAudio() {
    if (!native) { fileInput.current?.click(); return }
    if (!canStart) return
    setChoosingFile(true)
    setFeedback("")
    try {
      const selected = await native.request("openAudio", {
        diarization: diarization && speakerLabelsAvailable,
        languages: selectedLanguages.map(language => language.code),
      })
      if (selected?.id) await openAcceptedSession(selected.id)
    } catch (error) {
      setFeedback(message(error))
    } finally {
      setChoosingFile(false)
    }
  }

  function canRecover(draft: RecordingDraft) {
    return canStart || (draft.screen && draft.videoOnly && desktopIdle && !detectedRecording && !screenBusy)
  }

  async function handleRecovery(draft: RecordingDraft, discard: boolean) {
    if ((!native && !hasPortableAudio && !draft.screen) || !canRecover(draft)) return
    const { id } = draft
    setChoosingFile(true)
    setFeedback("")
    try {
      if (hasPortableAudio || draft.screen) {
        if (discard) await discardPortableRecording(id)
        else {
          const saved = await finishPortableRecording(id, {
            diarization: diarization && speakerLabelsAvailable, languages: selectedLanguages.map(l => l.code),
          })
          await openAcceptedSession(saved.id)
        }
      } else if (native) {
        if (discard) await native.request("discard", { id })
        else {
          const saved = await native.request("recover", {
            id, diarization: diarization && speakerLabelsAvailable, languages: selectedLanguages.map(l => l.code),
          })
          await openAcceptedSession(saved.id)
        }
      }
      if (discard) discardSessionDrafts(id)
      setRecoveredAudio(drafts => drafts.filter(d => d.id !== id))
      setDiscarding(null)
    } catch (error) {
      setFeedback(message(error))
    } finally {
      setChoosingFile(false)
    }
  }

  function changeMode(value: RecordingMode) {
    setPermissionGuide(null)
    setMode(value)
    writeWorkspacePreference("jard.recordingMode", value)
  }

  async function openFile(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget
    const file = input.files?.[0]
    input.value = ""
    if (!file || !canStart) return
    setFeedback("")
    setActiveTab("transcript")
    setActiveCapture(null)
    setProcessed(null)
    setPhase("transcribing")
    const languages = selectedLanguages.map(({ code }) => code)
    try {
      setCopied(false)
      await finishAudio(file, languages, { name: file.name, diarization: diarization && speakerLabelsAvailable })
    } catch (error) {
      setFeedback(message(error))
    } finally {
      setPhase("idle")
    }
  }

  async function copyTranscript() {
    const meetingId = phase === "idle" && current?.saved?.meeting ? current.id : undefined
    try {
      if (meetingId) await copySavedMeetingTranscript(meetingId, native)
      else await navigator.clipboard.writeText(transcriptForClipboard(result))
      setCopied(true)
      window.clearTimeout(copyTimer.current)
      copyTimer.current = window.setTimeout(() => setCopied(false), 1600)
    } catch (error) {
      setFeedback(meetingId ? message(error) : "Clipboard access is unavailable. You can select and copy the text.")
    }
  }

  const recordingActivity = live && !previewPaused ? "Live preview" : "Transcribes when you stop"
  const emptyText = recording
    ? live && !previewPaused ? "Listening for speech…" : "Your transcript will appear when you stop."
    : "Record or open audio."
  const progressLabel = restoring ? "Opening session…" : processingSavedAudio ?
    (waitingForFinalization ? "Waiting to resume transcription…" :
      result.transcriptSource === "rolling" ? (processingMeeting ? "Finalizing meeting transcript…" : "Finalizing transcript…") : "Transcribing audio…") : preparingModel ? "Loading local models…"
    : phase === "starting" ? "Waiting for microphone…"
      : processed ? "Transcribing audio…" : "Preparing audio…"
  const progressValue = phase === "transcribing" && processed?.duration && processed.seconds > 0
    ? Math.min(100, processed.seconds / processed.duration * 100)
    : null
  const processingIndicator = working && (
    <div className="processing-indicator">
      <div className="processing-caption">
        <span role="status">{progressLabel}</span>
        {processed && phase === "transcribing" && (
          <span className="processing-time" aria-hidden="true">
            {clock(processed.seconds)}{processed.duration ? ` / ${clock(processed.duration)}` : ""}
          </span>
        )}
      </div>
      <Progress className="processing-track" value={progressValue} aria-label={progressLabel} />
    </div>
  )

  const viewingSession = phase === "idle" && (current?.saved?.status === "complete" || Boolean(current?.saved?.annotation) ||
    isSettledMeeting(current?.saved))
  function newSession() {
    restoreRequest.current?.abort()
    setRestoring(false)
    clearSession()
    writeSessionRoute(null)
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>(".record-button")?.focus())
  }

  const updateBusy = phase !== "idle" || dictating || choosingFile || screenBusy || !!detectedRecording
    || modelSetup?.phase === "downloading" || modelSetup?.phase === "starting"
  const meetingControls: PortableMeetingControls | undefined = hasPortableAudio && !native ? {
    phase: meetingTake ? phase : "idle", elapsed, canStart, feedback: meetingFeedback,
    start: () => void startRecording(true), stop: () => void stopRecording(),
    showLive: () => { if (!live) toggleLive(); setActiveTab("transcript") },
  } : undefined
  const showScreenRecovery = recoveredAudio.some(draft => draft.screen)
  const recoveryPanel = !recording && recoveredAudio.length > 0 && <section
    className={showScreenRecovery ? "native-recovery screen-recovery" : "native-recovery"} aria-label="Interrupted recordings">
    {recoveredAudio.map(draft => <div className="native-recovery-row" key={draft.id}>
      {discarding === draft.id ? <>
        <span>Permanently discard this {clock(draft.seconds)} {t("recording?")}</span>
        <Button size="sm" variant="ghost" disabled={choosingFile} onClick={() => setDiscarding(null)}>{t("Cancel")}</Button>
        <Button size="sm" variant="destructive" disabled={!canRecover(draft)} onClick={() => void handleRecovery(draft, true)}>Discard recording</Button>
      </> : <>
        <span>{draft.screen ? "Interrupted screen recording" : draft.meeting ? "Interrupted meeting" : "Interrupted recording"} · {clock(draft.seconds)}</span>
        <Button size="sm" variant="outline" disabled={!canRecover(draft)} onClick={() => void handleRecovery(draft, false)}>Recover</Button>
        <Button size="sm" variant="ghost" disabled={!canRecover(draft)} onClick={() => setDiscarding(draft.id)}>{t("Discard…")}</Button>
      </>}
    </div>)}
  </section>
  return (
    <>
    {!native && portableUpdates && <dialog ref={desktopUpdate.restartDialog} className="workspace-restart-dialog"
      aria-label={t("Restarting…")} onCancel={event => event.preventDefault()} onKeyDown={event => event.stopPropagation()}>
      {t("Restarting…")}
    </dialog>}
    {native && <DictationVocabulary external request={vocabularyRequest} saved={systemDictation.status?.vocabularyEntries}
      saveVocabulary={systemDictation.saveVocabulary} />}
    <SessionLibrary onOpenVocabulary={openVocabulary} update={(native || portableUpdates) && <DesktopUpdateControl controls={desktopUpdate} busy={updateBusy} />}
      openUpdateSettings={desktopUpdate.update?.openUpdateSettings}
      onUpdateSettingsOpened={desktopUpdate.acknowledgeSettings}
      installedVersion={desktopUpdate.update?.currentVersion}
      native={native}
      meetingOptions={native ? { languages: selectedLanguages.map(language => language.code), elapsed, busy: updateBusy || status !== "ready" } : undefined}
      portableMeeting={meetingControls}
      callBar={meetingControls ? {
        calls, languages: selectedLanguages.map(language => language.code), viewing: meetingTake,
        canView: desktopReady && !screenBusy, onView: call => void startRecording(true, call),
      } : undefined}
      dictation={native ? ((onSettings, id) => <SystemDictationWorkspace key={id} onSettings={onSettings}
        controls={systemDictation} native={native} />) :
        hasPortableDictation ? ((onSettings, id) => <PortableDictationWorkspace key={id} onSettings={onSettings} />) : undefined}
      settings={native ? {
        general: <div className="dictation-preferences">
          <DesktopUpdateSettings controls={desktopUpdate} busy={updateBusy} />
          <DesktopApplicationSettings native={native} />
          <RecordingAccessSettings native={native} access={recordingAccess} refusedSource={refusedSource} />
          <AgentSetupSettings native={native} />
        </div>,
        dictation: (visible, outputRequest) => <SystemDictationSettings openVocabulary={openVocabulary} controls={systemDictation}
          native={native} languages={languageOptions} visible={visible} outputRequest={outputRequest} />,
        meetings: <DesktopMeetingControls native={native} languages={selectedLanguages.map(language => language.code)} busy={updateBusy} />,
        annotate: <DesktopAnnotationControls native={native} languages={selectedLanguages.map(language => language.code)}
          controls={systemDictation} busy={updateBusy}
          allowConcurrentCapture={phase === "recording" && !!liveMeeting?.shared && !meetingStopPending && !choosingFile && !dictating} />,
      } : portableUpdates || portableLogin || hasPortableAudio || hasDesktopAnnotationSettings || hasPortableDictation ? {
        general: <div className="dictation-preferences">
          {portableUpdates && <DesktopUpdateSettings controls={desktopUpdate} busy={updateBusy} />}
          {portableLogin && <PortableApplicationSettings />}
          {modelSetup?.additional && <PortableModelSetup setup={modelSetup} onStarted={() => setModelStatusRequest(value => value + 1)} />}
        </div>,
        dictation: hasPortableDictation ? visible => <PortableDictationSettings visible={visible}
          disabled={updateBusy} languages={languageOptions} /> : undefined,
        meetings: meetingControls ? <PortableMeetingSettings controls={meetingControls} calls={calls} /> : undefined,
        annotate: hasDesktopAnnotationSettings ? <DesktopAnnotationSettings busy={updateBusy} /> : undefined,
      } : undefined}
      busy={phase !== "idle" || restoring || dictating || screenBusy} currentId={current?.id}
      onHome={newSession} onSelect={openSaved} onDelete={id => {
        discardSessionDrafts(id)
        if (current?.id !== id && readSessionRoute().id !== id) return
        restoreRequest.current?.abort()
        setRestoring(false)
        clearSession()
        writeSessionRoute(null, "overview", true)
      }}>
    {(native || portableUpdates) && <DesktopUpdateFeedback controls={desktopUpdate} busy={updateBusy} />}
    <SystemDictationTour native={native} controls={systemDictation} modelReady={status === "ready"} modelFailed={status === "error"} />
    {portableOnboarding && <PortableOnboardingTour key={portableOnboarding} id={portableOnboarding}
      modelReady={status === "ready"} modelFailed={status === "error"} modelSetup={modelSetup}
      onModelStarted={() => setModelStatusRequest(value => value + 1)}
      onFinished={() => setPortableOnboarding(current => current === portableOnboarding ? null : current)} />}
    <div className="soundfolio-shell" data-session={viewingSession}>
      <header className="soundfolio-header">
        <div className="wordmark">
          <picture>
            <source media="(prefers-color-scheme: dark)" srcSet="/jard-dark.png" />
            <img className="wordmark-icon" src="/jard-light.png" width="40" height="40" alt="" />
          </picture>
          <span className="wordmark-text">Jaro</span>
        </div>
        <nav ref={workspaceActions} className="workspace-actions" aria-label="Session actions">
          <div ref={setSessionToolbar} className="session-toolbar-slot" />
          {viewingSession && <Downloads key={current?.id} item={current} compact onAudioRemoved={sessionMediaRemoved} />}
          {hasPortableScreenRecording && <PortableScreenRecording disabled={!canRecord} hiddenWhenIdle={viewingSession} onBusy={setScreenBusy}
            languageCodes={selectedLanguages.map(language => language.code)} onSave={openAcceptedSession} />}
          <ActionTooltip label={t("New session")}>
            <Button size="sm" variant="ghost" disabled={phase !== "idle" || restoring || dictating || screenBusy} onClick={newSession} aria-label={t("New session")}><Plus /><span>{t("New session")}</span></Button>
          </ActionTooltip>
          <QuitButton recording={recording || phase === "starting" || dictating || screenBusy || !!detectedRecording} recoverable={hasPortableAudio}
            onBeforeQuit={() => liveNotesFlush.current?.() ?? Promise.resolve()} onQuit={onQuit} />
        </nav>
      </header>

      <main className="workspace" data-session={viewingSession}>
        {!viewingSession && showScreenRecovery && recoveryPanel}
        {modelSetup && !modelSetup.additional && modelSetup.phase !== "checking" && !viewingSession ? <>
          <PortableModelSetup setup={modelSetup} onStarted={() => setModelStatusRequest(value => value + 1)} />
          {feedback && <p className="feedback" role="alert">{feedback}</p>}
        </> : <>
        <ResultsPanel key={captureFlowKey ?? (phase === "idle" && current ? current.id : "capture")}
          session={phase === "idle" ? current?.saved : undefined} result={result} source={phase === "idle" && current ? current.audioUrl ?? current.audio ?? null : null}
          captureId={captureId ?? undefined}
          notesKey={captureFlowKey ?? current?.id}
          notesLanguages={selectedLanguages.map(language => language.code)}
          notesFlushRef={liveNotesFlush}
          onNotesDraftSaved={recordSavedDraft}
          tab={tab} onTab={changeTab} working={working} recording={recording}
          followLive={recording && live && !previewPaused && !meetingStopPending} jump={jump} toolbar={sessionToolbar}
          dictationEnabled={status === "ready" && phase === "idle" && !choosingFile}
          onDictationBusy={setDictating}
          onMeetingEdited={saved => {
            if (readSessionRoute().id === saved.id) showSession(saved, "transcript")
            window.dispatchEvent(new Event("jard-meeting-brief"))
          }}
          onMeetingRenamed={(id, title, meetingExport) => {
            setCurrent(item => item?.saved?.id === id ? {
              ...item, name: title, saved: { ...item.saved, name: title, meetingTitle: title, meetingExport },
            } : item)
            window.dispatchEvent(new Event("jard-meeting-brief"))
          }}
          emptyText={emptyText} progress={processingIndicator} copied={copied} onCopy={() => void copyTranscript()} />

        {viewingSession && feedback && !(current?.saved?.annotation && feedback === result.interruption) &&
          <p className="session-feedback feedback" role="alert">{feedback}</p>}
        {!viewingSession && <><div className="controls">
          <div className="control-dock" role="group" aria-label="Audio controls">
            <Button
              className="record-button"
              data-recording={recording}
              disabled={!recording && !canStart}
              onClick={() => void (recording ? stopRecording() : startRecording())}
            >
              {recording ? <Square fill="currentColor" /> : <Mic />}
              {recording ? t("Stop") : t("Record")}
            </Button>
            <Button
              variant="ghost"
              className="dock-button"
              disabled={!canStart}
              onClick={() => void chooseAudio()}
            >
              <FolderOpen />
              Open audio
            </Button>
            <span className="dock-divider" aria-hidden="true" />
            <Button
              variant="ghost"
              className="dock-button live-button"
              disabled={!recording && !canStart}
              aria-pressed={live}
              onClick={toggleLive}
            >
              <span className="live-dot" aria-hidden="true" />
              Live
            </Button>
            <input ref={fileInput} type="file" accept="audio/*,.mp3,.wav,.m4a" onChange={(event) => void openFile(event)} hidden />
          </div>
          {recording && <div className="activity" role="status">
            <span className="capture-status">
              {((!native && !hasPortableAudio) || captureMode !== "system") && <span className="capture-meter">
                {(native || captureMode === "both") && <span>Mic</span>}<meter min={0} max={1} value={level} aria-label="Microphone level" />
              </span>}
              {(native || hasPortableAudio) && captureMode !== "mic" && <span className="capture-meter">
                <span>{hasPortableAudio ? "Computer audio" : "Mac audio"}</span>
                <meter min={0} max={1} value={systemLevel} aria-label={hasPortableAudio ? "Computer audio level" : "Mac audio level"} />
              </span>}
              <span className="timer" aria-hidden="true">{clock(elapsed)}</span>
            </span>
            <span>{recordingActivity}</span>
          </div>}
          {(feedback || status === "error") && (
            <p className="feedback" role="alert">
              {feedback || "Jaro could not start. Check the launcher, then reload this page."}
            </p>
          )}
          <div className="transcription-options">
            <LanguagePicker
              languages={languageOptions}
              selected={selectedLanguages}
              disabled={!canStart || languageOptions.length === 0}
              onChange={setSelectedLanguages}
            />
            <Button
              variant="outline"
              size="sm"
              className="speaker-toggle"
              aria-pressed={(diarization || portableMeeting) && speakerLabelsAvailable}
              disabled={!canStart || !speakerLabelsAvailable || portableMeeting}
              onClick={() => setDiarization(!diarization)}
              title={!speakerLabelsAvailable ? "Speaker labeling is unavailable" : portableMeeting ? "Meetings label speakers automatically" : "Label who spoke"}
            >
              <UsersRound />
              Speakers
            </Button>
          </div>
          {(native || hasPortableAudio) && <div className="native-audio-source">
            <label className="native-source-picker">
              {hasPortableAudio && mode !== "mic" ? <AudioLines aria-hidden="true" /> : <Mic aria-hidden="true" />}
              <select aria-label="Recording source" value={mode} disabled={phase !== "idle" || choosingFile}
                onChange={event => changeMode(event.target.value as RecordingMode)}>
                {hasPortableAudio ? <>
                  <option value="mic">{t("Microphone only")}</option>
                  <option value="system">{t("Computer audio only")}</option>
                  <option value="both">{t("Microphone + computer audio")}</option>
                </> : Object.entries(RECORDING_MODES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
              <ChevronDown aria-hidden="true" />
            </label>
            {native && mode !== "system" && <span className="native-input-name" title="Current input from macOS Sound settings">
              {microphoneName}
            </span>}
            {native && needsRecordingAccess && <button className="native-permission-link" type="button" disabled={phase !== "idle" || choosingFile}
              onClick={() => setPermissionGuide("inspection")}>Recording access</button>}
          </div>}
          {!showScreenRecovery && recoveryPanel}
        </div>
        <Downloads item={current} /></>}
        </>}
      </main>
    </div>
    {native && permissionGuide && <RecordingAccessDialog native={native} mode={mode} access={recordingAccess} inspection={permissionGuide === "inspection"}
      refusedSource={selectedRefusal} starting={phase === "starting"}
      onClose={() => setPermissionGuide(null)} onRetry={() => void startRecording()} />}
    </SessionLibrary>
    </>
  )
}
