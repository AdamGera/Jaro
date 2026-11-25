import { useEffect, useState, type ReactNode } from "react"
import { ListMinus } from "lucide-react"
import type { SavedSession } from "@/audio"
import { clock, speakerLabel } from "@/speaker-activity"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

export function TranscriptEditingPreference() {
  const { t } = useInterfaceLanguage()
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  useEffect(() => {
    const controller = new AbortController()
    void fetch("/api/meeting-transcript-edits", { signal: controller.signal, cache: "no-store" })
      .then(async response => {
        if (!response.ok) throw Error()
        setEnabled((await response.json()).enabled)
      }).catch(() => { if (!controller.signal.aborted) setError("Could not read transcript settings.") })
    return () => controller.abort()
  }, [])
  async function change(value: boolean) {
    setPending(true)
    setError("")
    try {
      const response = await fetch("/api/meeting-transcript-edits", { method: "PUT",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: value }) })
      if (!response.ok) throw Error()
      setEnabled(value)
      window.dispatchEvent(new Event("jaro-transcript-edit-setting"))
    } catch { setError("Could not save the transcript setting.") }
    finally { setPending(false) }
  }
  return <SettingRow icon={ListMinus} title={t("Delete transcript lines")}
    detail={t(error || "Choose and remove lines from a finished meeting transcript.")}>
    {labels => <input {...labels} type="checkbox" role="switch" checked={!!enabled}
      disabled={enabled === null || pending} onChange={event => void change(event.target.checked)} />}
  </SettingRow>
}

export function MeetingTranscriptEdit({ session, onEdited, children }: {
  session: SavedSession; onEdited: (session: SavedSession) => void; children: ReactNode
}) {
  const { t } = useInterfaceLanguage()
  const [selecting, select] = useState(false)
  const [selection, setSelection] = useState<number[]>([])
  const [confirming, confirm] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  async function remove() {
    setPending(true)
    setError("")
    try {
      const response = await fetch(`/api/sessions/${session.id}/delete-transcript-lines`, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: session.transcriptEditRevision, lines: selection }) })
      const value = await response.json()
      if (!response.ok) throw Error(typeof value.detail === "string" ? value.detail : t("Could not delete the selected lines."))
      onEdited(value)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("Could not delete the selected lines."))
      confirm(false)
    } finally { setPending(false) }
  }
  if (!selecting) return <>
    <Button size="sm" variant="ghost" onClick={() => select(true)}><ListMinus />{t("Select lines")}</Button>
    {children}
  </>
  return <section aria-label={t("Select transcript lines")} className="meeting-transcript-edit">
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" variant="outline" disabled={pending || !selection.length} onClick={() => confirming ? void remove() : confirm(true)}>
        {t(confirming ? "Confirm deletion" : "Delete selected")} ({selection.length})
      </Button>
      <Button size="sm" variant="ghost" disabled={pending} onClick={() => {
        select(false); setSelection([]); confirm(false); setError("")
      }}>{t("Cancel")}</Button>
    </div>
    {confirming && <p role="status">{t("Delete these lines and refresh the generated notes? The recording and My Notes will be kept.")}</p>}
    {error && <p role="alert">{error}</p>}
    {session.meetingTurns?.map((turn, index) => <label key={index} className="flex items-baseline gap-3 py-2">
      <input type="checkbox" disabled={pending || confirming} checked={selection.includes(index)}
        onChange={event => setSelection(event.target.checked ? [...selection, index] : selection.filter(i => i !== index))} />
      <time className="text-xs" dir="ltr">{clock(turn.start ?? 0)}</time>
      <span dir="auto">{turn.speaker && <strong>{speakerLabel(turn.speaker, session.transcript.speakerNames)} · </strong>}{turn.text}</span>
    </label>)}
  </section>
}
