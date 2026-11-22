import { useInterfaceLanguage } from "@/interface-language"
import { Component, lazy, Suspense, useCallback, useDeferredValue, useMemo, useEffect, useRef, useState, type ReactNode, type RefObject } from "react"
import { AudioLines, CalendarDays, ChartNoAxesColumnIncreasing, Check, ChevronUp, Clock3, Copy, Download, FileText, LayoutDashboard, ListTree, Pencil, RotateCw, StickyNote, UsersRound } from "lucide-react"
import { isSettledMeeting, type MeetingBrief, type MeetingExport, type SavedSession, type Transcript } from "@/audio"
import { clock } from "@/speaker-activity"
import { useOverview, type EditableOverview } from "@/overview"
import { AudioPlayer, usePlayback } from "./audio-player"
import { OverviewLanguage, languageName } from "./overview-language"
import { transcriptMatches } from "@/transcript-search"
import { TranscriptFind } from "./transcript-find"
import { LiveTranscriptScroll } from "./live-transcript-scroll"
import { meetingSegments } from "./transcript-turns"
import { desktopBridge } from "@/desktop"
import { SessionImages } from "./session-images"
import { Button } from "./ui/button"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./ui/tabs"

const SpeakerAnalytics = lazy(() => import("./speaker-analytics").then(module => ({ default: module.SpeakerAnalytics })))
const TimedTranscript = lazy(() => import("./timed-transcript").then(module => ({ default: module.TimedTranscript })))
const SessionChat = lazy(() => import("./session-conversation"))
const PersonalNotesEditor = lazy(() => import("./personal-notes").then(module => ({ default: module.PersonalNotesEditor })))
const OverviewEditor = lazy(() => import("./overview-editor"))
const NoteMarkdown = lazy(() => import("./note-markdown").then(module => ({ default: module.NoteMarkdown })))

type Props = {
  result: Transcript; session?: SavedSession; source: Blob | string | null
  tab: string; onTab: (tab: string) => void
  jump?: number
  working: boolean; recording: boolean; liveMeeting: boolean; followLive: boolean; emptyText: string; progress: ReactNode
  copied: boolean; onCopy: () => void
  dictationEnabled: boolean; onDictationBusy: (busy: boolean) => void
  onMeetingRenamed: (id: string, title: string, exported: MeetingExport | null) => void
}
const date = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" })

class NotesErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (this.state.failed) return <div className="notes-load-state" role="alert">
      <p>My Notes couldn’t open. You can still use the other tabs.</p>
      <Button size="sm" variant="outline" onClick={() => window.location.reload()}>Reload workspace</Button>
    </div>
    return this.props.children
  }
}

function annotationTime(seconds: number) {
  const tenths = Math.floor(seconds * 10 + .0001)
  return `${clock(Math.floor(tenths / 10))}.${tenths % 10}`
}

const missingAnnotationVideo = {
  noFrames: "The screen recording produced no video frames.",
  permissionDenied: "macOS declined Screen Recording access.",
  screenInterrupted: "The video source stopped before it saved a usable picture.",
} as const

function AnnotationPanel({ id, annotation, movie, videoExpired, unavailable, video, onPlay, onListen }: {
  id: string; annotation: NonNullable<SavedSession["annotation"]>; movie: string | null
  videoExpired: boolean; unavailable: boolean; video: RefObject<HTMLVideoElement | null>; onPlay: () => void; onListen?: () => void
}) {
  const { t } = useInterfaceLanguage()
  const [busy, setBusy] = useState(false)
  const [canSeek, setCanSeek] = useState(false)
  const [error, setError] = useState("")
  const [shareStatus, setShareStatus] = useState("")
  const native = desktopBridge()

  async function save(kind: "annotation-manifest" | "annotation-contact-sheet") {
    if (!native) return
    setShareStatus("")
    setError("")
    setBusy(true)
    try { await native.request("export", { id, kind }) }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Could not export the annotation.") }
    finally { setBusy(false) }
  }

  async function shareAnnotation(action: "copyAnnotation" | "pasteAnnotation") {
    if (!native) return
    setError("")
    setShareStatus("")
    setBusy(true)
    try {
      if (action === "copyAnnotation") {
        const { includedImage } = await native.request(action, { id })
        setShareStatus(includedImage ? "Copied context and contact sheet for rich-text paste." : "Copied annotation context.")
      } else {
        const { destination } = await native.request(action, { id })
        setShareStatus(`Sent annotation to ${destination}.`)
      }
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not share the annotation.") }
    finally { setBusy(false) }
  }

  const assets = [
    { kind: "annotation-contact-sheet", url: annotation.contactSheetUrl, name: "contact-sheet.png", label: "Save contact sheet" },
    { kind: "annotation-manifest", url: annotation.manifestUrl, name: "annotation.json", label: "Save strokes" },
  ] as const

  return <div className="annotation-review">
    {movie && <video ref={video} src={movie} controls playsInline preload="metadata" aria-label="Annotation recording"
      onPlay={onPlay} onLoadedMetadata={() => setCanSeek(true)} />}
    {annotation.missingVideo && <p className="annotation-interruption">{t(missingAnnotationVideo[annotation.missingVideo])}</p>}
    {onListen && <div><Button variant="outline" size="sm" onClick={onListen}>{t("Open narration player")}</Button></div>}
    {videoExpired && <p className="annotation-interruption">{t(annotation.missingVideo
      ? "The narration audio moved to Trash. The transcript and strokes are still available."
      : "The video moved to Trash. Narration and annotation details are still available.")}</p>}
    {unavailable && <p role="status" className="annotation-interruption">{t("Reconnect the Annotations folder to play or export the recording and images.")}</p>}
    {!!annotation.frames.length && movie && <div className="annotation-frame-links" aria-label="Contact sheet timestamps" role="group">
      {annotation.frames.map(position => {
        const time = annotationTime(position)
        return <Button key={position} type="button" size="sm" variant="outline" disabled={!canSeek}
          aria-label={`Jump to ${time}`} onClick={() => { if (video.current) video.current.currentTime = position }}>
          {time}
        </Button>
      })}
    </div>}
    <div className="annotation-review-tools">
      <p>{annotation.strokeCount} {annotation.strokeCount === 1 ? "stroke" : "strokes"}</p>
      {native && <>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void shareAnnotation("copyAnnotation")}><Copy />Copy for Agent</Button>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void shareAnnotation("pasteAnnotation")}>Paste into Last App</Button>
      </>}
      {assets.map(({ kind, url, name, label }) => url && (native
        ? <Button key={kind} variant="outline" size="sm" disabled={busy} onClick={() => void save(kind)}><Download />{label}</Button>
        : <Button key={kind} asChild variant="outline" size="sm"><a href={url} download={name}><Download />{label}</a></Button>))}
    </div>
    {shareStatus && <p role="status">{shareStatus}</p>}
    {error && <p role="alert">{error}</p>}
  </div>
}

function useMeetingBrief(id?: string, initial?: MeetingBrief | null) {
  const [loaded, setLoaded] = useState<{ id: string; brief: MeetingBrief | null }>()
  const [write, setWrite] = useState<{ id: string; error?: string }>()
  const request = useRef<AbortController>(null)
  const brief = id && loaded?.id === id ? loaded.brief : initial
  const action = id && write?.id === id ? write : undefined
  const queued = brief?.status === "queued"

  useEffect(() => () => request.current?.abort(), [id])
  useEffect(() => {
    if (!id || !queued) return
    const key = id
    const abort = new AbortController()
    let timer: number
    async function poll() {
      try {
        const response = await fetch("/api/sessions/" + key + "/meeting-brief", { cache: "no-store", signal: abort.signal })
        if (response.status === 404) return
        if (response.ok) {
          const value: MeetingBrief | null = await response.json()
          if (abort.signal.aborted) return
          setLoaded({ id: key, brief: value })
          if (value?.status !== "queued") {
            window.dispatchEvent(new Event("jard-meeting-brief"))
            return
          }
        }
      } catch { /* Try again after a short connection interruption. */ }
      if (!abort.signal.aborted) timer = window.setTimeout(poll, 1200)
    }
    timer = window.setTimeout(poll, 1000)
    return () => { abort.abort(); window.clearTimeout(timer) }
  }, [id, queued])

  async function summarize() {
    if (!id || queued || (action && !action.error)) return
    const abort = new AbortController()
    request.current = abort
    setWrite({ id })
    try {
      const response = await fetch("/api/sessions/" + id + "/meeting-brief", { method: "POST", signal: abort.signal })
      const value = await response.json()
      if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "Could not queue the meeting summary.")
      if (!abort.signal.aborted) {
        setLoaded({ id, brief: value })
        setWrite(undefined)
        window.dispatchEvent(new Event("jard-meeting-brief"))
      }
    } catch (error) {
      if (!abort.signal.aborted) setWrite({ id, error: error instanceof Error ? error.message : "Could not queue the meeting summary." })
    }
  }

  return { brief, summarize, pending: queued || !!(action && !action.error), error: action?.error || brief?.error }
}

function CopyMeetingText({ text, kind }: { text: string; kind: "overview" | "brief" }) {
  const { t } = useInterfaceLanguage()
  const [outcome, setOutcome] = useState<"copied" | "error">()
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setOutcome("copied")
    } catch {
      setOutcome("error")
    }
  }
  return <>
    <Button type="button" size="sm" variant="ghost" onClick={() => void copy()}>
      {outcome === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      <span aria-live="polite">{outcome === "copied" ? t("Copied") : t(kind === "overview" ? "Copy overview" : "Copy quick brief")}</span>
    </Button>
    {outcome === "error" && <span role="alert">{t(kind === "overview" ? "Could not copy the overview." : "Could not copy the brief.")}</span>}
  </>
}

function MeetingTitle({ id, title, onRenamed }: { id: string; title: string; onRenamed: Props["onMeetingRenamed"] }) {
  const { t } = useInterfaceLanguage()
  const [draft, setDraft] = useState<string>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const trigger = useRef<HTMLButtonElement>(null)

  function close() {
    setDraft(undefined)
    setError("")
    requestAnimationFrame(() => trigger.current?.focus({ preventScroll: true }))
  }

  async function save(title: string) {
    if (!title || saving) return
    setSaving(true)
    setError("")
    try {
      const response = await fetch(`/api/sessions/${id}/meeting-title`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not rename the meeting.")
      onRenamed(id, result.title, result.export)
      close()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not rename the meeting.")
    } finally {
      setSaving(false)
    }
  }

  if (draft === undefined) return <div className="meeting-title-row">
    <h1 dir="auto">{title}</h1>
    <Button ref={trigger} size="icon-sm" variant="ghost" aria-label="Rename meeting" title="Rename meeting"
      onClick={() => setDraft(title)}><Pencil /></Button>
  </div>
  return <form className="meeting-title-form" onSubmit={event => { event.preventDefault(); void save(draft.trim()) }}
    onKeyDown={event => { if (event.key === "Escape" && !saving) { event.preventDefault(); close() } }}>
    <input aria-label="Meeting title" dir="auto" value={draft} onChange={event => setDraft(event.target.value)}
      maxLength={200} disabled={saving} autoFocus />
    <Button type="submit" size="sm" disabled={saving || !draft.trim()}>{t("Save")}</Button>
    <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={close}>{t("Cancel")}</Button>
    {error && <p role="alert" className="annotation-interruption">{error}</p>}
  </form>
}

function ManagedMeetingExport({ id, initial, title }: { id: string; initial: MeetingExport; title: string }) {
  const { t } = useInterfaceLanguage()
  const [loaded, setLoaded] = useState<MeetingExport | null>()
  const [revealError, setRevealError] = useState("")
  const [revealing, setRevealing] = useState(false)
  const native = desktopBridge()
  const value = loaded === undefined ? initial : loaded

  useEffect(() => {
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let polling = initial.status === "pending" || !!initial.refreshing
    async function update() {
      try {
        const response = await fetch(`/api/sessions/${id}/managed-export`, { signal: abort.signal })
        if (response.ok) {
          const result: MeetingExport | null = await response.json()
          if (abort.signal.aborted) return
          setLoaded(result)
          polling = result?.status === "pending" || !!result?.refreshing
        }
      } catch {
        if (abort.signal.aborted) return
      }
      if (!abort.signal.aborted && polling) {
        timer = setTimeout(() => void update(), 1000)
      }
    }
    void update()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [id, initial, title])

  async function reveal() {
    setRevealError("")
    setRevealing(true)
    try {
      await native?.request("meetingExports", { action: "reveal", id })
    } catch (error) {
      setRevealError(error instanceof Error ? error.message : "Could not reveal the exported transcript.")
    } finally {
      setRevealing(false)
    }
  }

  if (!value) return null
  if (value.status === "error") return <p className="annotation-interruption" role="alert">
    Could not save meeting Markdown. {value.error}
  </p>
  if (value.status === "pending") return <span role="status">Saving meeting Markdown…</span>
  return <>
    {native && <Button variant="ghost" size="sm" disabled={revealing} title={value.filename ?? undefined}
      onClick={() => void reveal()}>{t("Reveal Markdown")}</Button>}
    {value.error && <p className="annotation-interruption" role="alert">Could not update meeting Markdown. {value.error}</p>}
    {revealError && <p className="annotation-interruption" role="alert">{revealError}</p>}
  </>
}

export function ResultsPanel({ result, session, source, tab, onTab, jump, working, recording, liveMeeting, followLive, emptyText, progress, copied, onCopy, dictationEnabled, onDictationBusy, onMeetingRenamed }: Props) {
  const { t } = useInterfaceLanguage()
  const complete = session?.status === "complete"
  const reviewable = complete || !!session?.annotation || isSettledMeeting(session)
  const meetingsManaged = !!session?.meetingsManaged
  const briefMeeting = complete && session.meeting && result.text.trim() ? session.id : undefined
  const meetingBrief = useMeetingBrief(meetingsManaged ? undefined : briefMeeting, session?.meetingBrief)
  const overview = useOverview(complete ? session.id : undefined, session?.overview, tab === "overview" || tab === "timeline", meetingsManaged)
  const writing = overview.value
  const version = overview.version
  const overviewText = version && [version.summary, version.markdown].filter(Boolean).join("\n\n")
    .replace(/⟦(\d+(?:\.\d+)?)⟧/g, (_, seconds) => `(${clock(Number(seconds))})`)
  const editRevision = writing?.editRevision
  const [editing, setEditing] = useState<EditableOverview | null>(null)
  const [citation, setCitation] = useState<{ seconds: number } | null>(null)
  const [dock, setDock] = useState<"audio" | "chat" | "expanded" | null>(null)
  const [track, setTrack] = useState<"mix" | "mic" | "system">("mix")
  const tracks = session?.recordingTracks
  const video = useRef<HTMLVideoElement>(null)
  const openPlayer = useCallback(() => { video.current?.pause(); setDock("audio") }, [])
  const player = usePlayback((track === "mix" ? source : tracks?.[track]) ?? source, result.durationSeconds, openPlayer)
  const audioTrigger = useRef<HTMLButtonElement>(null)
  const editTrigger = useRef<HTMLButtonElement>(null)
  const toggleChat = useCallback((open: boolean) => setDock(open ? "chat" : null), [])
  const resizeChat = useCallback(() => setDock(current => current === "expanded" ? "chat" : "expanded"), [])
  const [scrolled, setScrolled] = useState(false)
  const [notesOpened, setNotesOpened] = useState(tab === "notes")
  const [findOpen, setFindOpen] = useState(false)
  const [query, setQuery] = useState("")
  const [matchIndex, setMatchIndex] = useState(0)
  const meeting = !!session?.meeting || liveMeeting
  const originalTurns = session?.meetingTurns
  const transcript = useMemo(() => meeting ? {
    ...result, segments: meetingSegments(originalTurns?.length ? originalTurns : result.segments),
  } : result, [meeting, originalTurns, result])
  const search = useDeferredValue(findOpen && tab === "transcript" ? query : "")
  const matches = useMemo(() => transcriptMatches(transcript, search), [transcript, search])
  const currentMatch = Math.min(matches.length - 1, matchIndex)
  function changeQuery(text: string) { setQuery(text); setMatchIndex(0) }
  function toggleFind(open: boolean) {
    setFindOpen(open)
    if (!open) changeQuery("")
  }
  function stepMatch(delta: number) {
    if (matches.length) setMatchIndex((currentMatch + delta + matches.length) % matches.length)
  }
  const tabList = useRef<HTMLDivElement>(null)
  const changeTab = useCallback((value: string) => {
    setScrolled(false)
    setCitation(null)
    setFindOpen(false)
    setQuery("")
    setMatchIndex(0)
    if (value === "notes") setNotesOpened(true)
    onTab(value)
  }, [onTab])
  useEffect(() => {
    if (!reviewable || tab !== "transcript" || !result.text || recording || working) return
    function find(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "f" || event.altKey || event.shiftKey) return
      if (document.querySelector('[role="dialog"], [role="listbox"]')) return
      event.preventDefault()
      setFindOpen(true)
      const field = document.querySelector<HTMLInputElement>('.transcript-find input')
      field?.focus()
      field?.select()
    }
    document.addEventListener("keydown", find)
    return () => document.removeEventListener("keydown", find)
  }, [reviewable, tab, result.text, recording, working])
  const { seek, ready, wave: waveRef } = player
  const linkedPassage = useMemo(() => jump === undefined ? null : { seconds: jump }, [jump])
  const cite = useCallback((seconds: number) => {
    setDock(null)
    changeTab("transcript")
    setCitation({ seconds })
    seek(seconds, true)
  }, [changeTab, seek])
  useEffect(() => {
    if (dock !== "audio") return
    const dismiss = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return
      setDock(null)
      audioTrigger.current?.focus({ preventScroll: true })
    }
    document.addEventListener("keydown", dismiss)
    return () => document.removeEventListener("keydown", dismiss)
  }, [dock])
  useEffect(() => {
    const strip = tabList.current
    const active = strip?.querySelector<HTMLElement>('[data-state="active"]')
    if (!strip || !active || strip.scrollWidth === strip.clientWidth) return
    strip.scrollLeft += active.getBoundingClientRect().left - strip.getBoundingClientRect().left - (strip.clientWidth - active.offsetWidth) / 2
  }, [tab])
  const writingTab = tab === "overview" || tab === "timeline" ? tab : null
  const showWritingStatus = !writing?.scope || tab === writing.scope
  const draft = showWritingStatus && !writing?.updatingPersonal ? overview.draft : null
  const note = draft?.summary || draft?.markdown ? draft : version
  const timeline = draft?.topics.length ? draft : version
  const showingPrevious = version && (tab === "timeline" ? timeline === version : note === version)
  const chapter = timeline?.topics.findLastIndex(t => t.start != null && t.start <= player.time) ?? -1
  const status = (!overview.errorTab || overview.errorTab === tab ? overview.error : "") || writing?.error
  const pending = writing?.status === "queued" || !writing
  const noteStatus = meetingsManaged ? <p className="note-status" role="status">
    {t("Meeting summaries and overviews are managed by your organization.")}
  </p> : showWritingStatus && (status || pending || writing?.status === "empty") ? <div className="note-state">
    <p className="note-status" role={status ? "alert" : "status"}>
      {!status && pending && <i className="thinking-dot" />}
      {status || (writing?.status === "empty" ? "No speech detected." :
        overview.stageLabel ?? (writing?.scope === "timeline" ? "Writing topic timeline…" : writing?.target && writing.target !== "und" ? `Creating ${languageName(writing.target)} overview…` : "Preparing overview…"))}
      {status && <Button size="xs" variant="ghost" onClick={overview.retry}>{t("Retry")}</Button>}
    </p>
    {showingPrevious && <small>Showing saved {languageName(version.language)} {tab === "timeline" ? "timeline" : "overview"}</small>}
  </div> : null
  const transcriptContent = result.text ? (
    <Suspense fallback={null}>
      <TimedTranscript result={transcript} meeting={meeting} listening={meeting && followLive}
        time={player.time} ready={ready} seek={player.seek}
        citation={citation ?? linkedPassage} matches={matches} activeMatch={currentMatch} />
    </Suspense>
  ) : (
    <div className="empty-state">
      <span className="empty-icon" data-working={working} aria-hidden="true"><AudioLines /></span>
      {working ? progress : <p>{emptyText}</p>}
    </div>
  )
  const transcriptClass = result.text ? "transcript-body has-text" : "transcript-body"
  if (!session || !reviewable) return <section className="transcript-panel capture-panel" aria-label={t("Transcript")}>
    <LiveTranscriptScroll className={transcriptClass} following={followLive} hasText={!!result.text}>
      {transcriptContent}
    </LiveTranscriptScroll>
    {meetingsManaged && session?.finalizationPending && <p className="processing-footer" role="status">
      {t("Meeting transcription is managed by your organization.")}
    </p>}
    {working && result.text && <div className="processing-footer">{progress}</div>}
    {source && <AudioPlayer player={player} />}
  </section>

  const meetingTitle = session.meetingTitle || session.meetingCalendar?.title || meetingBrief.brief?.title ||
    session.provisionalTitle || draft?.title || version?.title || session.name
  return <div className="session-sheet">
    <header className="session-heading">
      {complete && session.meeting ? <MeetingTitle id={session.id} title={meetingTitle} onRenamed={onMeetingRenamed} /> :
        <h1 dir="auto">{meetingTitle}</h1>}
      <div className="session-meta">
        {session.meeting && <span><UsersRound aria-hidden="true" />{session.meetingProvider || "Meeting"}</span>}
        {complete && !!session.meetingRetentionDays && <span>Moves to Trash after {session.meetingRetentionDays === 1 ? t("24 hours") : `${session.meetingRetentionDays} days`}</span>}
        {session.audioRemovedAt && <span>Audio removed</span>}
        <span><CalendarDays aria-hidden="true" />{date.format(new Date(session.created))}</span>
        <span><Clock3 aria-hidden="true" /><bdi>{clock(result.durationSeconds ?? 0)}</bdi></span>
        {session.meetingExport && <ManagedMeetingExport key={session.id} id={session.id} initial={session.meetingExport} title={meetingTitle} />}
        {!meetingsManaged && complete && writing?.status !== "empty" && <OverviewLanguage detected={writing?.autoLanguage ?? ""}
          value={writing?.preference ?? "auto"} disabled={!!editing} onSelect={overview.select} />}
        {!meetingsManaged && writingTab && version && writing && version.language === writing.target && <Button size="sm" variant="ghost" className="regenerate-note"
          aria-label={`Regenerate ${writingTab}`} title={`Regenerate ${writingTab}`}
          disabled={!!editing || writing.status === "queued" || overview.requesting}
          onClick={() => overview.regenerate(writingTab)}><RotateCw aria-hidden="true" /><span>Regenerate</span></Button>}
        {tab === "overview" && session.meeting && overviewText && !editing &&
          <CopyMeetingText key={overviewText} text={overviewText} kind="overview" />}
        {tab === "overview" && version && editRevision && !editing &&
          <Button ref={editTrigger} size="sm" variant="ghost" className="regenerate-note"
            aria-label="Edit overview" title="Edit overview"
            disabled={writing?.status === "queued" || overview.requesting}
            onClick={() => setEditing({ ...version, editRevision })}><Pencil aria-hidden="true" /><span>{t("Edit")}</span></Button>}
      </div>
      {session.annotation && result.interruption && <p className="annotation-interruption" role="alert">{result.interruption}</p>}
      {session.meetingMicrophoneWarning === "digitalSilence" && <p className="annotation-interruption">
        {t("The microphone stopped sending sound during this meeting. Mac audio continued recording.")}
      </p>}
    </header>
    <Tabs className="transcript-panel" data-finding={findOpen && tab === "transcript"} value={tab} onValueChange={changeTab}>
      <div className="panel-heading" data-scrolled={scrolled}>
        <TabsList ref={tabList} aria-label="Recording results">
          {(complete || session.annotation) && <TabsTrigger value="overview"><LayoutDashboard aria-hidden="true" className="size-3.5" />{t("Overview")}</TabsTrigger>}
          {complete && <TabsTrigger value="notes"><StickyNote aria-hidden="true" className="size-3.5" />{t("My Notes")}</TabsTrigger>}
          <TabsTrigger value="transcript"><FileText aria-hidden="true" className="size-3.5" />{t("Transcript")}</TabsTrigger>
          {complete && <TabsTrigger value="timeline"><ListTree aria-hidden="true" className="size-3.5" />{t("Timeline")}</TabsTrigger>}
          {complete && <TabsTrigger value="analytics" disabled={working || recording}>
            <ChartNoAxesColumnIncreasing aria-hidden="true" className="size-3.5" />{t("Speaker analytics")}
          </TabsTrigger>}
        </TabsList>
        {tab === "transcript" && result.text && !recording && !working &&
          <div className="transcript-tools">
          <TranscriptFind open={findOpen} onOpen={toggleFind} query={query} onQuery={changeQuery}
            count={matches.length} index={currentMatch} step={stepMatch} />
          <Button variant="ghost" className="copy-button" onClick={onCopy} aria-label={copied ? t("Copied") : t("Copy transcript")}>
            {copied ? <Check /> : <Copy />}<span>{copied ? t("Copied") : t("Copy")}</span>
          </Button></div>}
      </div>
      <TabsContent value="overview" forceMount={editing ? true : undefined} hidden={tab !== "overview"}
        className="note-body" onScroll={e => setScrolled(e.currentTarget.scrollTop > 2)}>
        {!editing && briefMeeting && (!meetingsManaged || meetingBrief.brief?.summary) && <details className="meeting-quick-brief">
          <summary>{t("Quick brief")}</summary>
          <div className="meeting-quick-brief-body">
            {meetingBrief.brief?.summary && <p dir="auto">{meetingBrief.brief.summary}</p>}
            <div className="meeting-quick-brief-actions">
              {meetingBrief.brief?.summary && <CopyMeetingText key={meetingBrief.brief.summary} text={meetingBrief.brief.summary} kind="brief" />}
              {!meetingsManaged && <Button size="sm" variant="ghost" disabled={meetingBrief.pending}
                onClick={() => void meetingBrief.summarize()}>
                {t(meetingBrief.pending ? "Writing brief…" : meetingBrief.brief?.summary ? "Regenerate brief" : "Write quick brief")}
              </Button>}
            </div>
            {meetingBrief.error && <p role="alert">{meetingBrief.error}</p>}
          </div>
        </details>}
        {noteStatus}
        {editing ? <Suspense fallback={null}>
          <OverviewEditor version={editing} onSave={overview.saveEdit} onClose={() => {
            setEditing(null)
            requestAnimationFrame(() => editTrigger.current?.focus({ preventScroll: true }))
          }} />
        </Suspense> : note && <div className="overview-copy" dir="auto" lang={note.language}>
          {note.summary && <p className="note-lead">{note.summary}</p>}
          {note.markdown && <Suspense fallback={null}>
            <NoteMarkdown text={note.markdown} ready={ready} cite={seconds => seek(seconds, true)} />
          </Suspense>}
        </div>}
        {!editing && <SessionImages key={session.id} session={session}>
          {session.annotation && <AnnotationPanel id={session.id} annotation={session.annotation}
            movie={session.annotation.missingVideo ? null : session.audioUrl}
            onListen={session.annotation.missingVideo && session.audioUrl ? openPlayer : undefined}
            videoExpired={!!session.audioRemovedAt} unavailable={!!session.annotationStorageUnavailable} video={video}
            onPlay={() => player.audio.current?.pause()} />}
        </SessionImages>}
      </TabsContent>
      <TabsContent value="timeline" className="note-body timeline-body" onScroll={e => setScrolled(e.currentTarget.scrollTop > 2)}>
        {noteStatus}
        {timeline && <ol className="topic-list" aria-label="Topic timeline" lang={timeline.language}>
          {timeline.topics.map((topic,i) => <li key={i}>
            <button disabled={!ready || topic.start == null} aria-current={i === chapter ? "time" : undefined}
              aria-label={topic.start == null ? topic.label : `Play ${topic.label} at ${clock(topic.start)}`}
              onClick={() => { if (topic.start != null) player.seek(topic.start, true) }}>
              <time dir="ltr">{topic.start != null ? clock(topic.start) : "…"}</time><span dir="auto"><strong>{topic.label}</strong>
                {topic.detail && <span>{topic.detail}</span>}</span>
            </button>
          </li>)}
        </ol>}
      </TabsContent>
      <TabsContent value="transcript" className={transcriptClass}
        onScroll={e => setScrolled(e.currentTarget.scrollTop > 2)}>
        {transcriptContent}
      </TabsContent>
      {complete && <TabsContent value="notes" forceMount hidden={tab !== "notes"} className="note-body personal-note-body"
        onScroll={e => setScrolled(e.currentTarget.scrollTop > 2)}>
        {(notesOpened || tab === "notes") && <NotesErrorBoundary key={session.id}>
          <Suspense fallback={<p className="notes-load-state" role="status">Opening My Notes…</p>}>
            <PersonalNotesEditor sessionId={session.id} initial={session.personalNotes} languages={session.languageHints}
              dictationEnabled={dictationEnabled} onDictationBusy={onDictationBusy} onSaved={overview.refreshNotes}
              notesChanged={writing?.notesChanged} overviewBusy={!!editing || overview.requesting || writing?.status === "queued"}
              onUpdateOverview={result.text && !meetingsManaged ? () => { overview.updatePersonal(); changeTab("overview") } : undefined} />
          </Suspense>
        </NotesErrorBoundary>}
      </TabsContent>}
      <TabsContent value="analytics" className="analytics-body" onScroll={e => setScrolled(e.currentTarget.scrollTop > 2)}>
        <Suspense fallback={null}><SpeakerAnalytics result={result} time={player.time} ready={ready} seek={player.seek} /></Suspense>
      </TabsContent>
      {working && (result.text || session.annotation) && <div className="processing-footer">{progress}</div>}
    </Tabs>
    <footer className="session-dock" aria-label="Recording tools">
      {source && <>
        <div id="session-audio-player" className="audio-popover" hidden={dock !== "audio"}>
          {(tracks?.mic || tracks?.system) && <label className="session-audio-source">Playback source
            <select value={track} onChange={event => {
              const next = event.target.value as typeof track
              const target = next === "mix" ? source : tracks[next]
              if (target && next !== track) {
                player.prepareSwitch(target)
                setTrack(next)
              }
            }}>
              <option value="mix">{t("Combined audio")}</option>
              {tracks?.mic && <option value="mic">{t("Microphone")}</option>}
              {tracks?.system && <option value="system">{t("Computer audio")}</option>}
            </select>
          </label>}
          <AudioPlayer player={player} />
        </div>
        <Button ref={audioTrigger} className="audio-launcher" variant="outline" aria-expanded={dock === "audio"}
          data-playing={player.advancing} aria-controls="session-audio-player" title={dock === "audio" ? "Close audio player" : "Open audio player"}
          aria-label={dock === "audio" ? "Close audio player" : "Open audio player"} onClick={() => setDock(dock === "audio" ? null : "audio")}>
          <span ref={waveRef} className="playback-wave" aria-hidden="true"><i /><i /><i /><i /><i /></span><ChevronUp aria-hidden="true" />
        </Button>
      </>}
      {complete && result.text && <Suspense fallback={<button className="chat-launcher" disabled>Opening conversation…</button>}>
        <SessionChat id={session.id} open={dock === "chat" || dock === "expanded"} expanded={dock === "expanded"}
          onOpen={toggleChat} onExpand={resizeChat}
          ready={ready} cite={cite} />
      </Suspense>}
    </footer>
  </div>
}
