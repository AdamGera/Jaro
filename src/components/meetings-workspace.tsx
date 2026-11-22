import { useState } from "react"
import { AudioLines, Check, Copy, Search, Square, Trash2 } from "lucide-react"
import { isSettledMeeting } from "@/audio"
import type { DesktopBridge } from "@/desktop"
import { copySavedMeetingTranscript, meetingAudioWarning, meetingTimeFormat, useDesktopMeeting } from "@/desktop-meeting"
import { useInterfaceLanguage } from "@/interface-language"
import { useSessionList, type SessionSummary } from "@/session-list"
import { clock } from "@/speaker-activity"
import { Button } from "./ui/button"

export type MeetingWorkspaceOptions = { languages: string[]; elapsed: number; busy: boolean }

function midnight(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

function meetingStart(row: SessionSummary) {
  return row.meetingStartedAt ?? row.created
}

export function MeetingsWorkspace({ native, options, busy, currentId, query, onQuery, onOpen, onTrash, onSettings }: {
  native: DesktopBridge; options: MeetingWorkspaceOptions; busy: boolean; currentId?: string
  query: string; onQuery: (query: string) => void; onOpen: (id: string) => void
  onTrash: (row: SessionSummary, trigger: HTMLButtonElement) => void; onSettings: () => void
}) {
  const { t, locale } = useInterfaceLanguage()
  const { meeting, act, pending, feedback } = useDesktopMeeting(native)
  const recording = meeting?.phase === "recording" || meeting?.phase === "callEnded"
  const audioWarning = meetingAudioWarning(meeting)
  const saving = meeting?.phase === "starting" || meeting?.phase === "saving"
  const list = useSessionList(true, query, [currentId, meeting?.session, meeting?.phase].join(":"), true)
  const [copying, setCopying] = useState("")
  const [copied, setCopied] = useState("")
  const [copyError, setCopyError] = useState("")
  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium" })
  const timeFormat = meetingTimeFormat(locale)
  const now = new Date()
  const today = midnight(now)
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime()
  const groups = new Map<number, SessionSummary[]>()
  const byStart = [...list.rows].sort((left, right) => Date.parse(meetingStart(right)) - Date.parse(meetingStart(left)))
  for (const row of byStart) {
    const day = midnight(new Date(meetingStart(row)))
    const group = groups.get(day)
    if (group) group.push(row)
    else groups.set(day, [row])
  }

  async function copyTranscript(row: SessionSummary) {
    setCopying(row.id)
    setCopied("")
    setCopyError("")
    try {
      await copySavedMeetingTranscript(row.id, native)
      setCopied(row.id)
    } catch (error) {
      setCopyError(error instanceof Error ? error.message : t("Could not copy the transcript."))
    } finally { setCopying("") }
  }

  return <div className="meetings-workspace">
    <div className="meeting-workspace-controls">
      {meeting?.managed ? <p role="status">{t("Meetings are managed by your organization.")}</p> : <>
        {recording && <span className="meeting-live-time" aria-label={t("Meeting recording time")}>
          <span aria-hidden="true" /><bdi>{clock(options.elapsed)}</bdi>
        </span>}
        {recording && <Button variant="outline" size="sm" disabled={pending}
          onClick={() => void act({ action: "showLiveTranscript" })}>{t("View live transcript")}</Button>}
        <Button size="sm" variant={recording ? "outline" : "default"}
          disabled={!meeting || pending || saving || (!recording && (busy || options.busy || meeting.busy || !!meeting.permission || !!meeting.recoveryAvailable))}
          onClick={() => void act(recording ? { action: "stop" } : { action: "start", languages: options.languages })}>
          {recording ? <Square aria-hidden="true" /> : <AudioLines aria-hidden="true" />}
          {meeting?.phase === "saving" ? t("Saving…") : meeting?.phase === "starting" ? t("Starting…") :
            recording ? t("Stop & Save") : t("Record Meeting")}
        </Button>
      </>}
    </div>
    {(feedback || meeting?.message) && <p className="meeting-status" role="status">{feedback || meeting?.message}</p>}
    {audioWarning && <p className="meeting-status" role="alert">{t(audioWarning)}</p>}
    {meeting && (!!meeting.permission || !!meeting.recoveryAvailable) && !meeting.busy && <Button size="sm" variant="outline" onClick={onSettings}>
      {t("Open Meeting Settings")}
    </Button>}
    <label className="meeting-workspace-search">
      <Search aria-hidden="true" />
      <input type="search" aria-label={t("Search meetings")} placeholder={t("Search meetings…")} value={query}
        onChange={event => onQuery(event.target.value)} />
    </label>
    {copyError && <p className="meeting-workspace-error" role="alert">{copyError}</p>}
    {copied && <span role="status" className="sr-only">{t("Copied meeting transcript.")}</span>}
    <div className="meeting-workspace-list" aria-busy={list.loading}>
      {list.error ? <p role="alert">{t(list.error)}{" "}
        <Button size="sm" variant="ghost" onClick={list.refresh}>{t("Retry")}</Button></p> :
        list.loading ? <p role="status">{t("Loading meetings…")}</p> :
        !list.rows.length ? <p role="status">{query.trim() ? t("No meetings match.") : t("No meetings yet.")}</p> : null}
      {Array.from(groups, ([day, rows]) => <section key={day}>
        <h2>{day === today ? t("Today") : day === yesterday ? t("Yesterday") : dateFormat.format(day)}</h2>
        <ul>{rows.map(row => {
          const settled = isSettledMeeting(row)
          return <li key={row.id} className="meeting-workspace-row" data-current={currentId === row.id}>
            <button className="meeting-workspace-open" type="button" disabled={busy || row.status === "processing"}
              onClick={() => onOpen(row.id)}>
              <strong dir="auto">{row.name}</strong>
              <small>{[row.meetingProvider, row.status === "processing" || row.finalizationPending ? t("Processing") :
                row.status === "interrupted" ? t("Interrupted") : "", row.duration == null ? "" : clock(row.duration)]
                .filter(Boolean).join(" · ")}</small>
            </button>
            <time dateTime={meetingStart(row)}>{timeFormat.format(new Date(meetingStart(row)))}</time>
            <Button size="icon-sm" variant="ghost" disabled={!settled || busy || !!copying} onClick={() => void copyTranscript(row)}
              aria-label={copied === row.id ? t("Copied") : t("Copy transcript")} title={t("Copy transcript")}>
              {copied === row.id ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            </Button>
            <Button size="icon-sm" variant="ghost" disabled={!settled || busy} onClick={event => onTrash(row, event.currentTarget)}
              aria-label={t("Move meeting to Trash")} title={t("Move to Trash")}><Trash2 aria-hidden="true" /></Button>
          </li>
        })}</ul>
      </section>)}
    </div>
  </div>
}
