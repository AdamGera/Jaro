import { useInterfaceLanguage } from "@/interface-language"
import { useEffect, useState } from "react"
import { Activity, AudioLines, Bell, BookOpen, CalendarDays, Clock, FileText, Folder, HardDrive, History, Monitor, Pencil, Phone, SlidersHorizontal, Sparkles, Square, Video } from "lucide-react"
import { AlertDialog } from "radix-ui"
import { meetingAudioWarning, meetingTimeFormat, useDesktopMeeting } from "@/desktop-meeting"
import type { DesktopBridge, DesktopMeetingAction, DesktopMeetingCalendar, MeetingIndicatorCorner, MeetingRecordingPolicy } from "@/desktop"
import { useNoiseCleanup } from "@/system-dictation"
import { MeetingAudioAccess } from "./meeting-audio-access"
import type { PortableCallControls } from "@/portable-calls"
import { PortableCallSettings } from "./portable-call-controls"
import type { PortableMeetingControls } from "./meetings-workspace"
import { SettingDisclosure, SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

export function PortableMeetingSettings({ controls, calls }: { controls: PortableMeetingControls; calls: PortableCallControls }) {
  const { t } = useInterfaceLanguage()
  type VoiceInput = { noiseCleanup: boolean; boostMicrophone: boolean; cleanupStatus?: "notInstalled" | "downloading" | "ready" | "error" }
  const [voice, setVoice] = useState<VoiceInput>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const endpoint = "/api/portable/meeting-voice-input"

  useEffect(() => {
    const controller = new AbortController()
    const read = () => void fetch(endpoint, { cache: "no-store", signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error()
      const current = await response.json()
      if (!controller.signal.aborted) setVoice(current)
    }).catch(() => {
      if (!controller.signal.aborted) setError("Could not read the meeting voice settings.")
    })
    read()
    const timer = voice?.cleanupStatus === "downloading" ? window.setInterval(read, 1500) : undefined
    return () => {
      if (timer !== undefined) window.clearInterval(timer)
      controller.abort()
    }
  }, [endpoint, voice?.cleanupStatus])

  async function change(setting: "noiseCleanup" | "boostMicrophone", enabled: boolean) {
    setPending(true)
    setError("")
    try {
      const response = await fetch(endpoint, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ setting, enabled }) })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not change the meeting voice setting.")
      setVoice(result)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not change the meeting voice setting.")
    } finally { setPending(false) }
  }

  const recording = controls.phase === "recording"
  return <section className="dictation-settings dictation-preferences meetings-settings" data-setting-group="Meetings">
    <SettingGroup id="meeting-recording-title" title={t("Recording")}>
      <SettingRow icon={Video} title={t("Meeting audio")} label={false}
        detail={t("Record your microphone and computer audio across playback devices. Find the transcript in Sessions.")}>
        <Button size="sm" variant="outline" disabled={!recording && !controls.canStart}
          onClick={recording ? controls.stop : controls.start}>
          {recording ? <Square /> : <AudioLines />}
          {controls.phase === "transcribing" ? t("Saving…") : controls.phase === "starting" ? t("Starting…") :
            recording ? t("Stop & Save") : t("Record Meeting")}
        </Button>
      </SettingRow>
      {controls.feedback && <p className="meeting-status" role="status">{controls.feedback}</p>}
    </SettingGroup>
    <SettingGroup id="meeting-voice-title" title={t("Voice input")}>
      <SettingRow icon={Sparkles} title={t("Noise cleanup (StuPASE)")}
        detail={voice?.noiseCleanup && voice.cleanupStatus === "downloading" ? t("Downloading 2.3 GB. Meetings can still be recorded.")
          : voice?.noiseCleanup && voice.cleanupStatus === "error" ? t("Download failed. Turn this setting off and on to retry.")
          : voice?.noiseCleanup && voice.cleanupStatus === "ready" ? t("Cleans the microphone audio locally for the final transcript of new meetings.")
          : t("Cleans microphone audio for the final transcript. Downloads 2.3 GB the first time.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={voice?.noiseCleanup ?? false} disabled={!voice || pending}
          onChange={event => void change("noiseCleanup", event.target.checked)} />}
      </SettingRow>
      <SettingRow icon={Activity} title={t("Boost microphone (+6 dB)")}
        detail={t("Raises quiet microphone audio and limits loud peaks for the final transcript of new meetings.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={voice?.boostMicrophone ?? false} disabled={!voice || pending}
          onChange={event => void change("boostMicrophone", event.target.checked)} />}
      </SettingRow>
      {error && <p className="meeting-status" role="alert">{t(error)}</p>}
    </SettingGroup>
    <SettingGroup id="meeting-after-title" title={t("After a meeting")}>
      <MeetingWritingPreference portable />
      <RetentionPreference kind="meeting" portable />
      <RetentionPreference kind="audio" portable />
    </SettingGroup>
    <PortableCallSettings calls={calls} />
  </section>
}

export function DesktopMeetingControls({ native, busy, languages }: { native: DesktopBridge; busy: boolean; languages: string[] }) {
  const { t } = useInterfaceLanguage()
  const { meeting, pending, feedback, act } = useDesktopMeeting(native)
  const cleanup = useNoiseCleanup(native, !meeting?.managed && meeting?.noiseCleanup === true)

  if (!meeting?.phase) return null
  if (meeting.managed) return <section className="dictation-settings dictation-preferences meetings-settings" data-setting-group="Meetings">
    <SettingGroup id="meetings-title" title={t("Meetings")}>
      <p className="meeting-status" role="status">{t("Meetings are managed by your organization.")}</p>
    </SettingGroup>
  </section>
  const recording = meeting.phase === "recording" || meeting.phase === "callEnded"
  const audioWarning = meetingAudioWarning(meeting)
  return <section className="dictation-settings dictation-preferences meetings-settings" aria-labelledby="meetings-title" data-setting-group="Meetings">
    <h2 id="meetings-title" className="sr-only">{t("Meetings")}</h2>
    <SettingGroup id="meeting-recording-title" title={t("Recording")}>
      <SettingRow icon={Video} title={t("Meeting audio")} label={false} find="Record Meeting or stop and save"
        detail={meeting.busy && meeting.title ? <span dir="auto">{meeting.title}</span>
          : t("Capture your microphone and Mac audio. Find the transcript in Sessions.")}>
        <Button size="sm" variant="outline" disabled={pending || !!meeting.permission || meeting.recoveryAvailable || (meeting.busy ? !recording : busy)}
          onClick={() => void act(recording ? { action: "stop" } : { action: "start", languages })}>
          {recording ? <Square /> : <AudioLines />}
          {meeting.phase === "saving" ? t("Saving…") : meeting.phase === "starting" ? t("Starting…") : recording ? t("Stop & Save") : t("Record Meeting")}
        </Button>
      </SettingRow>
      <MeetingAudioAccess native={native} busy={pending || busy || meeting.busy} />
      {recording && !meeting.microphoneUnavailable && meeting.microphone && <p className="meeting-status">
        {t("Microphone")} · <bdi>{meeting.microphone}</bdi>
      </p>}
      {(feedback || meeting.message) && <p className="meeting-status" role="status">{feedback || meeting.message}</p>}
      {audioWarning && <p className="meeting-status" role="alert">{t(audioWarning)}</p>}
      {meeting.permission && <SettingRow icon={Monitor} title={t("Recording permission")} label={false}>
        <div className="dictation-row-actions">
          <Button size="sm" variant="outline" disabled={pending} onClick={() => void act({ action: "recordingPrivacy" })}>
            {t(meeting.permission === "mic" ? "Open Microphone Settings" : "Open Screen & System Audio Settings")}
          </Button>
          <Button size="sm" variant="outline" disabled={pending || busy} onClick={() => void act({ action: "retryPermission" })}>{t("Try Again")}</Button>
          <Button size="sm" variant="ghost" disabled={pending} onClick={() => void act({ action: "dismissPermission" })}>{t("Not Now")}</Button>
        </div>
      </SettingRow>}
      {!!meeting.recoveryAvailable && !meeting.busy && <SettingRow icon={History} title={t("Interrupted meeting")} label={false}>
        <div className="dictation-row-actions">
          <Button size="sm" variant="outline" disabled={pending || busy} onClick={() => void act({ action: "recoverMeeting" })}>{t("Recover Meeting")}</Button>
          <Button size="sm" variant="ghost" disabled={pending} onClick={() => void act({ action: "dismissRecovery" })}>{t("Not Now")}</Button>
        </div>
      </SettingRow>}
      {(recording || !!meeting.hasText) && <SettingRow icon={FileText} title={t("Meeting transcript")} label={false}
        detail={recording ? t("Paste new text with {shortcut}", { shortcut: "⌥⌃V" }) : undefined}>
        <div className="dictation-row-actions dictation-media-actions">
          {recording && <Button size="sm" variant="outline" disabled={pending}
            onClick={() => void act({ action: "showLiveTranscript" })}>{t("View live transcript")}</Button>}
          {meeting.phase === "callEnded" && <Button size="sm" variant="outline" disabled={pending}
            onClick={() => void act({ action: "keepRecording" })}>{t("Keep Recording")}</Button>}
          <Button size="sm" variant="outline" disabled={pending || meeting.copying || (!recording && !meeting.hasNewText)} onClick={() => void act({ action: "copyNew" })}>{t("Copy New")}</Button>
          <Button size="sm" variant="outline" disabled={pending || meeting.copying} onClick={() => void act({ action: "copyAll" })}>{t("Copy All")}</Button>
        </div>
      </SettingRow>}
    </SettingGroup>
    <SettingGroup id="meeting-voice-title" title={t("Voice input")}>
      <SettingRow icon={Sparkles} title={t("Noise cleanup (StuPASE)")}
        detail={cleanup === "downloading" ? t("Downloading 2.3 GB. Meetings can still be recorded.")
          : cleanup === "error" ? t("Download failed. Turn this setting off and on to retry.")
          : cleanup === "ready" ? t("Cleans the microphone audio locally for the final transcript of new meetings.")
          : t("Cleans microphone audio for the final transcript. Downloads 2.3 GB the first time.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={meeting.noiseCleanup ?? false} disabled={pending}
          onChange={event => void act({ action: "noiseCleanup", enabled: event.target.checked })} />}
      </SettingRow>
      <SettingRow icon={Activity} title={t("Boost microphone (+6 dB)")}
        detail={t("Raises quiet microphone audio and limits loud peaks for the final transcript of new meetings.")}>
        {labels => <input {...labels} type="checkbox" role="switch" checked={meeting.boostMicrophone ?? false} disabled={pending}
          onChange={event => void act({ action: "boostMicrophone", enabled: event.target.checked })} />}
      </SettingRow>
    </SettingGroup>
    <SettingGroup id="meeting-after-title" title={t("After a meeting")}>
      <MeetingWritingPreference />
      <RetentionPreference kind="meeting" />
      <RetentionPreference kind="audio" />
    </SettingGroup>
    <SettingGroup id="meeting-export-title" title={t("Markdown export")}>
      <MeetingExportPreference native={native} />
    </SettingGroup>
    {meeting.calendar && <MeetingCalendar calendar={meeting.calendar} act={act} pending={pending} busy={busy || meeting.busy} />}
    <SettingGroup id="meeting-detection-title" title={t("Detected calls")}>
      <SettingRow icon={Phone} title="Offer to record detected calls" find="Automatically detect meetings">
        {labels => (
          <input {...labels} type="checkbox" role="switch" checked={meeting.detection} disabled={pending}
            onChange={event => void act({ action: "detection", enabled: event.target.checked })} />
        )}
      </SettingRow>
      <SettingRow icon={Clock} title="Stop automatically when a call ends" detail="Automatically started recordings keep going for 30 seconds after a call ends.">
        {labels => (
          <input {...labels} type="checkbox" role="switch" checked={meeting.autoStop ?? false} disabled={pending}
            onChange={event => void act({ action: "autoStop", enabled: event.target.checked })} />
        )}
      </SettingRow>
      {meeting.providerPolicies.map(({ provider, label, scope, policy, canAlwaysRecord }) => <SettingRow key={provider}
        icon={Phone} title={t(label)} context={scope} find={scope}>
        {labels => (
          <select {...labels} value={policy} disabled={pending}
            onChange={event => void act({ action: "setPolicy", provider, policy: event.target.value as MeetingRecordingPolicy })}>
            <option value="ask">{t("Ask")}</option>
            {!!canAlwaysRecord && <option value="auto">{t("Always record")}</option>}
            <option value="never">{t("Never offer")}</option>
          </select>
        )}
      </SettingRow>)}
    </SettingGroup>
    <section className="dictation-preference-section dictation-advanced" aria-labelledby="meeting-advanced-title" data-setting-group={t("Advanced")}>
      <h3 id="meeting-advanced-title">{t("Advanced")}</h3>
      <SettingDisclosure icon={SlidersHorizontal} title={t("Indicator and recovery")}>
        <SettingRow icon={Monitor} title={t("Show recording indicator")} detail="Use the floating controls to copy transcript text or stop recording.">
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={meeting.showIndicator ?? true} disabled={pending}
              onChange={event => void act({ action: "indicator", enabled: event.target.checked })} />
          )}
        </SettingRow>
        <SettingRow icon={Monitor} title="Indicator corner">
          {labels => (
            <select {...labels} value={meeting.indicatorCorner ?? "topRight"} disabled={pending}
              onChange={event => void act({ action: "indicatorCorner", corner: event.target.value as MeetingIndicatorCorner })}>
              <option value="topLeft">Top left</option><option value="topRight">Top right</option>
              <option value="bottomLeft">Bottom left</option><option value="bottomRight">Bottom right</option>
            </select>
          )}
        </SettingRow>
        {!meeting.recoveryAvailable && <SettingRow icon={History} title="Interrupted meetings" label={false} find="Check for interrupted meetings">
          <Button size="sm" variant="outline" disabled={pending || busy || meeting.busy}
            onClick={() => void act({ action: "refreshRecovery" })}>Check for interrupted meetings</Button>
        </SettingRow>}
      </SettingDisclosure>
    </section>
  </section>
}

function MeetingCalendar({ calendar, act, pending, busy }: {
  calendar: DesktopMeetingCalendar; act: (action: DesktopMeetingAction) => Promise<void>; pending: boolean; busy: boolean
}) {
  const { t, locale } = useInterfaceLanguage()
  const eventTime = meetingTimeFormat(locale, "short")
  return <SettingGroup id="meeting-calendar-title" title={t("Calendar")}>
    <SettingRow icon={CalendarDays} title="Mac calendar" label={false} detail={calendar.connected ? "Connected" :
      calendar.access === "denied" ? "Allow full calendar access in System Settings." : "Connect your calendar for upcoming meetings and reminders."}>
      <Button size="sm" variant="outline" disabled={pending} onClick={() => void act({ action: calendar.connected ?
        "calendarDisconnect" : calendar.access === "denied" ? "calendarSettings" : "calendarConnect" })}>
        {calendar.connected ? "Disconnect" : calendar.access === "denied" ? "Open Calendar Settings" : t("Connect Calendar")}
      </Button>
    </SettingRow>
    {!!calendar.connected && <>
      <SettingRow icon={Bell} title={t("Remind me before meetings")}>
        {labels => (
          <input {...labels} type="checkbox" role="switch" checked={calendar.reminders} disabled={pending}
            onChange={event => void act({ action: "calendarReminders", enabled: event.target.checked })} />
        )}
      </SettingRow>
      {!!calendar.reminders && <SettingRow icon={Clock} title="Reminder time">
        {labels => (
          <select {...labels} value={calendar.lead} disabled={pending} onChange={event => void act({ action: "calendarLead", minutes: Number(event.target.value) })}>
            <option value={0}>{t("At start")}</option><option value={1}>1 minute before</option><option value={2}>2 minutes before</option><option value={5}>5 minutes before</option>
          </select>
        )}
      </SettingRow>}
      {calendar.calendars.length > 0 && <SettingDisclosure icon={CalendarDays} title={`Calendars (${calendar.calendars.filter(item => item.included).length} of ${calendar.calendars.length})`}>
        {calendar.calendars.map(item => <SettingRow icon={CalendarDays} title={item.title} context={item.source} key={item.id} find={false}>
          {labels => (
            <input {...labels} type="checkbox" role="switch" checked={item.included} disabled={pending}
              onChange={event => void act({ action: "calendarInclude", id: item.id, enabled: event.target.checked })} />
          )}
        </SettingRow>)}
      </SettingDisclosure>}
      <section className="meeting-upcoming" aria-labelledby="meeting-upcoming-title">
        <h4 id="meeting-upcoming-title">Upcoming meetings</h4>
        {calendar.upcoming.length ? <ul>{calendar.upcoming.map(event => <li key={event.id} className="meeting-calendar-event">
          <div><strong dir="auto">{event.title}</strong>
            <small>{eventTime.format(event.start * 1000)}{event.provider && ` · ${event.provider}`}</small></div>
          <div className="dictation-row-actions">
            {!!event.canJoin && <Button size="sm" variant="outline" disabled={pending}
              onClick={() => void act({ action: "calendarEvent", id: event.id, choice: "join" })}>{t("Join")}</Button>}
            <Button size="sm" variant="ghost" disabled={pending || busy}
              onClick={() => void act({ action: "calendarEvent", id: event.id, choice: event.canJoin ? "joinRecord" : "record" })}>
              {event.canJoin ? t("Join & Record") : t("Record")}
            </Button>
          </div>
        </li>)}</ul> : <p className="meeting-calendar-empty">{t("No upcoming meetings.")}</p>}
      </section>
    </>}
  </SettingGroup>
}

const automaticNotesEndpoint = "/api/meetings/automatic-notes"

function MeetingExportPreference({ native }: { native: DesktopBridge }) {
  const { t } = useInterfaceLanguage()
  const [folder, setFolder] = useState<string | null>()
  const [separateNotes, setSeparateNotes] = useState(false)
  const [descriptiveNames, setDescriptiveNames] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    let active = true
    void native.request("meetingExports", { action: "status" }).then(result => {
      if (active) {
        setFolder(result.folder)
        setSeparateNotes(result.separateNotes ?? false)
        setDescriptiveNames(result.descriptiveNames ?? false)
      }
    }).catch(() => {
      if (active) setError("Could not load the meeting export folder.")
    })
    return () => { active = false }
  }, [native])

  async function change(request: { action: "choose" | "disable" | "reveal" } | { action: "separateNotes" | "descriptiveNames"; enabled: boolean }) {
    setPending(true)
    setError("")
    try {
      const next = await native.request("meetingExports", request)
      setFolder(next.folder)
      if (next.separateNotes !== undefined) setSeparateNotes(next.separateNotes)
      if (next.descriptiveNames !== undefined) setDescriptiveNames(next.descriptiveNames)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update meeting exports.")
    } finally {
      setPending(false)
    }
  }

  return <>
    <SettingRow icon={Folder} title={t("Meeting export folder")} label={false} find="Automatically save meeting Markdown"
      detail={folder ? <span dir="auto" title={folder}>{folder}</span> : "Save a Markdown copy of each finished meeting."}>
      <div className="dictation-row-actions">
        {folder && <Button size="sm" variant="ghost" disabled={pending} onClick={() => void change({ action: "reveal" })}>Reveal Folder</Button>}
        {folder && <Button size="sm" variant="ghost" disabled={pending} onClick={() => void change({ action: "disable" })}>Turn Off</Button>}
        <Button size="sm" variant="outline" disabled={pending} onClick={() => void change({ action: "choose" })}>
          {folder ? "Change Folder…" : "Choose Folder…"}
        </Button>
      </div>
    </SettingRow>
    <SettingRow icon={Pencil} title={t("Descriptive meeting filenames")} detail="Update exported filenames when a meeting’s title changes. Keep the date and time.">
      {labels => (
        <input {...labels} type="checkbox" role="switch" checked={descriptiveNames} disabled={folder === undefined || pending}
          onChange={event => void change({ action: "descriptiveNames", enabled: event.target.checked })} />
      )}
    </SettingRow>
    <SettingRow icon={BookOpen} title={t("Separate notes file")} detail="Save a separate Markdown notes file with a link to the transcript when a summary is ready.">
      {labels => (
        <input {...labels} type="checkbox" role="switch" checked={separateNotes} disabled={folder === undefined || pending}
          onChange={event => void change({ action: "separateNotes", enabled: event.target.checked })} />
      )}
    </SettingRow>
    {error && <p className="dictation-error meeting-status" role="alert">{error}</p>}
  </>
}

function MeetingWritingPreference({ portable = false }: { portable?: boolean }) {
  const [enabled, setEnabled] = useState<boolean>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    void fetch(automaticNotesEndpoint, { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error()
      const result = await response.json()
      if (!controller.signal.aborted) setEnabled(result.enabled)
    }).catch(() => { if (!controller.signal.aborted) setError("Could not load automatic meeting notes.") })
    return () => controller.abort()
  }, [])

  async function change(next: boolean) {
    setPending(true)
    setError("")
    try {
      const response = await fetch(automaticNotesEndpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      })
      if (!response.ok) throw new Error()
      const result = await response.json()
      setEnabled(result.enabled)
    } catch {
      setError("Could not update automatic meeting notes.")
    } finally {
      setPending(false)
    }
  }

  return <>
    <SettingRow icon={Sparkles} title="Automatic titles and summaries" detail={portable
      ? "Write a title and short summary after the meeting with local Qwen."
      : "Write a live title and short meeting summary with local Qwen."}>
      {labels => (
        <input {...labels} type="checkbox" role="switch" checked={enabled ?? false} disabled={enabled === undefined || pending}
          onChange={event => void change(event.target.checked)} />
      )}
    </SettingRow>
    {error && <p className="dictation-error meeting-status" role="alert">{error}</p>}
  </>
}

function RetentionPreference({ kind, portable = false }: { kind: "meeting" | "audio"; portable?: boolean }) {
  const { t } = useInterfaceLanguage()
  const audio = kind === "audio"
  const endpoint = audio ? "/api/meetings/audio-retention" : "/api/meetings/retention"
  const [days, setDays] = useState<number | null>()
  const [choice, setChoice] = useState<number>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const duration = choice === 1 ? "24 hours" : `${choice} days`
  const trash = portable ? "the system Trash" : "the macOS Trash"

  useEffect(() => {
    const controller = new AbortController()
    void fetch(endpoint, { signal: controller.signal }).then(async response => {
      if (!response.ok) return
      const result = await response.json()
      if (!controller.signal.aborted) setDays(result.days)
    }).catch(() => {})
    return () => controller.abort()
  }, [endpoint])

  async function change(next: number) {
    setPending(true)
    setError("")
    try {
      const response = await fetch(`${endpoint}?days=${next}`, { method: "PUT" })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || "Could not change meeting retention.")
      setDays(result.days)
      window.dispatchEvent(new Event("jard-meeting-retention"))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not change meeting retention.")
    } finally {
      setChoice(undefined)
      setPending(false)
    }
  }

  if (days === undefined) return null
  return <>
    <SettingRow icon={audio ? HardDrive : FileText} title={audio ? "Keep meeting audio" : t("Keep meetings")} find="Meeting retention and expiry"
      detail={audio ? "Move expired audio to Trash. Transcripts and notes remain until the meeting expires."
        : `Expired meetings move to ${trash} with their audio, transcripts, and notes.`}>
      {labels => (
        <select {...labels} value={days ?? (audio ? 0 : "default")} disabled={pending} onChange={event => {
          const next = Number(event.target.value)
          if (next <= 0 || (days != null && days > 0 && next > days)) void change(next)
          else setChoice(next)
        }}>
          {!audio && days === null && <option value="default" disabled>24 hours for new meetings</option>}
          <option value={0}>{audio ? t("Same as meetings") : t("Forever")}</option>
          <option value={1}>{t("24 hours")}</option><option value={7}>{t("7 days")}</option><option value={30}>{t("30 days")}</option>
          {audio && <option value={-1}>{t("Forever")}</option>}
        </select>
      )}
    </SettingRow>
    {error && <p className="dictation-error meeting-status" role="alert">{error}</p>}
    <AlertDialog.Root open={choice !== undefined} onOpenChange={open => { if (!open && !pending) setChoice(undefined) }}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="note-language-scrim" />
        <AlertDialog.Content className="note-language-confirm">
          <AlertDialog.Title>{audio ? "Set meeting audio retention?" : "Set meeting retention?"}</AlertDialog.Title>
          <AlertDialog.Description>{audio
            ? `Audio from completed meetings older than ${duration} moves to ${trash}. Transcripts and notes stay in Sessions while the meeting is kept.`
            : `Completed meetings older than ${duration}, including existing meetings, will move to ${trash} with their audio, transcripts, and notes.`}</AlertDialog.Description>
          <div className="note-language-actions">
            <AlertDialog.Cancel asChild><Button variant="outline" disabled={pending}>{t("Cancel")}</Button></AlertDialog.Cancel>
            <Button variant="destructive" disabled={pending} onClick={() => { if (choice !== undefined) void change(choice) }}>Set retention</Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </>
}
