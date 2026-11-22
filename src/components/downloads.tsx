import { useInterfaceLanguage } from "@/interface-language"
import { useState } from "react"
import { Check, Download, FileAudio, FileJson, Mic } from "lucide-react"
import { AlertDialog, DropdownMenu } from "radix-ui"

import { desktopBridge } from "@/desktop"
import { copySavedMeetingTranscript } from "@/desktop-meeting"
import { isSettledMeeting, type SavedSession, type Transcript } from "@/audio"
import { Button } from "@/components/ui/button"

export type DownloadItem = {
  id: string
  name: string
  languageHints: readonly string[]
  audio?: Blob
  audioUrl?: string | null
  audioName?: string
  recording?: boolean
  transcript?: Transcript
  saved?: SavedSession
}

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = name
  document.body.append(link)
  link.click()
  link.remove()
  // Safari can consume a download URL asynchronously after the click.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

function downloadAudio(audio: Blob, name: string) {
  const format = audio.type.split(";")[0]
  const extension: Record<string, string> = {
    "audio/aac": "aac",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "video/mp4": "mp4",
    "video/webm": "webm",
  }
  save(audio, `${name}.${extension[format] ?? "audio"}`)
}

function downloadTranscript(item: DownloadItem) {
  const json = JSON.stringify({
    ...(item.saved && {
      id: item.id, created: item.saved.created, audioUrl: item.saved.audioUrl, audioPath: item.saved.audioPath, recording: item.saved.recording,
      live: item.saved.live, diarization: item.saved.diarization, model: item.saved.model,
      modelRevision: item.saved.modelRevision, speakerModel: item.saved.speakerModel, speakerModelRevision: item.saved.speakerModelRevision,
    }),
    source: item.audioName || item.name,
    languageHints: item.languageHints,
    ...item.transcript,
  }, null, 2)
  const stem = item.recording ? item.name : item.name.replace(/\.[^.]+$/, "") || item.name
  save(new Blob([json + "\n"], { type: "application/json" }), `${stem}.json`)
}

const meetingFormats = [["md", "Transcript (.md)"], ["notes", "Notes (.md)"], ["srt", "Subtitles (.srt)"], ["vtt", "Subtitles (.vtt)"]] as const

export function Downloads({ item, compact = false, onAudioRemoved }: {
  item: DownloadItem | null; compact?: boolean; onAudioRemoved?: (id: string, removedAt: string) => void
}) {
  const { t } = useInterfaceLanguage()
  const [exportError, setExportError] = useState("")
  const [confirm, setConfirm] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [transcriptCopy, setTranscriptCopy] = useState<"pending" | "copied">()
  if (!item) return null
  const bridge = item.saved ? desktopBridge() : undefined
  const sessionId = item.id
  const settledMeeting = isSettledMeeting(item.saved)
  const revealRecording = settledMeeting && !!item.audioUrl && !!bridge
  const removeRecording = settledMeeting && item.saved?.status === "complete" && !!item.audioUrl && !!onAudioRemoved
  async function exportSaved(kind: "audio" | "json" | typeof meetingFormats[number][0]) {
    setExportError("")
    try {
      await bridge!.request("export", { id: sessionId, kind })
    } catch (reason) {
      setExportError(reason instanceof Error ? reason.message : "Could not save the export.")
    }
  }

  async function removeAudio() {
    setRemoving(true)
    setExportError("")
    try {
      const response = await fetch(`/api/sessions/${sessionId}/audio`, { method: "DELETE" })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || "Could not remove the meeting audio.")
      onAudioRemoved?.(sessionId, result.audioRemovedAt)
      setConfirm(false)
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Could not remove the meeting audio.")
      setConfirm(false)
    } finally { setRemoving(false) }
  }

  async function revealFiles() {
    if (!bridge) return
    setExportError("")
    try {
      await bridge.request("meetingExports", { action: "revealFiles", id: sessionId })
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Could not reveal the recording files.")
    }
  }

  async function copyMeeting() {
    setExportError("")
    setTranscriptCopy("pending")
    try {
      await copySavedMeetingTranscript(sessionId, bridge)
      setTranscriptCopy("copied")
    } catch (error) {
      setTranscriptCopy(undefined)
      setExportError(error instanceof Error ? error.message : t("Could not copy the transcript."))
    }
  }

  return <section className={compact ? "compact-downloads" : "downloads"} aria-label="Session downloads">
    <div className={compact ? undefined : "current-file"}>
      {!compact && <><span className="download-source" aria-hidden="true">{item.recording ? <Mic /> : <FileAudio />}</span>
      <span className="download-name" title={item.audioName || item.name} dir="auto">{item.audioName || item.name}</span></>}
      <div className="download-actions">
        {bridge && item.audioUrl ? <Button variant="ghost" size="icon-sm" aria-label={`Download audio for ${item.name}`}
          title="Download audio" onClick={() => void exportSaved("audio")}><FileAudio /></Button> : item.audioUrl ? <Button asChild variant="ghost" size="icon-sm">
          <a href={item.audioUrl} download={item.audioName || item.name} aria-label={`Download audio for ${item.name}`} title="Download audio"><FileAudio /></a>
        </Button> : item.audio && <Button variant="ghost" size="icon-sm" aria-label={`Download audio for ${item.name}`}
          title="Download audio" onClick={() => downloadAudio(item.audio!, item.name)}><FileAudio /></Button>}
        {item.transcript && <Button variant="ghost" size="icon-sm" aria-label={`Download JSON transcript for ${item.name}`}
          title="Download transcript as JSON" onClick={() => bridge ? void exportSaved("json") : downloadTranscript(item)}><FileJson /></Button>}
        {item.saved?.meeting && <DropdownMenu.Root onOpenChange={open => {
          if (open && transcriptCopy === "copied") setTranscriptCopy(undefined)
        }}>
          <DropdownMenu.Trigger asChild><Button variant="ghost" size="icon-sm" title="Meeting exports" aria-label="Meeting exports">
            {transcriptCopy === "copied" ? <Check aria-hidden="true" /> : <Download aria-hidden="true" />}
          </Button></DropdownMenu.Trigger>
          <DropdownMenu.Portal><DropdownMenu.Content align="end" className="meeting-export-menu" sideOffset={5}>
            {settledMeeting && <DropdownMenu.Item disabled={transcriptCopy === "pending"}
              onSelect={() => { void copyMeeting() }}>{t("Copy transcript")}</DropdownMenu.Item>}
            {meetingFormats.map(([kind, label]) => bridge ?
              <DropdownMenu.Item key={kind} onSelect={() => { void exportSaved(kind) }}>{label}</DropdownMenu.Item> :
              <DropdownMenu.Item key={kind} asChild><a download href={`/api/sessions/${sessionId}/export?kind=${kind}`}>{label}</a></DropdownMenu.Item>)}
            {(revealRecording || removeRecording) && <>
              <DropdownMenu.Separator />
              {revealRecording && <DropdownMenu.Item onSelect={() => void revealFiles()}>{t("Reveal files")}</DropdownMenu.Item>}
              {removeRecording && <DropdownMenu.Item onSelect={() => setConfirm(true)}>Move meeting audio to Trash…</DropdownMenu.Item>}
            </>}
          </DropdownMenu.Content></DropdownMenu.Portal>
        </DropdownMenu.Root>}
      </div>
    </div>
    {transcriptCopy === "copied" && <span role="status" className="sr-only">{t("Copied meeting transcript.")}</span>}
    {exportError && <p role="alert" className="feedback">{exportError}</p>}
    <AlertDialog.Root open={confirm} onOpenChange={open => { if (!removing) setConfirm(open) }}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="note-language-scrim" />
        <AlertDialog.Content className="note-language-confirm">
          <AlertDialog.Title>Move this meeting’s audio to Trash?</AlertDialog.Title>
          <AlertDialog.Description>The recording and source tracks move to the macOS Trash. The transcript and notes stay in Sessions.</AlertDialog.Description>
          <div className="note-language-actions">
            <AlertDialog.Cancel asChild><Button variant="outline" disabled={removing}>{t("Cancel")}</Button></AlertDialog.Cancel>
            <Button variant="destructive" disabled={removing} onClick={() => void removeAudio()}>{t("Move to Trash")}</Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </section>
}
