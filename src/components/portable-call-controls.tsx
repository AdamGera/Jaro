import { AudioLines, Ellipsis, Phone, Square } from "lucide-react"
import { useInterfaceLanguage } from "@/interface-language"
import type { DetectedCall, PortableCallControls } from "@/portable-calls"
import { clock } from "@/speaker-activity"
import { MeetingCalls } from "./meeting-calls"
import { Button } from "./ui/button"

export function PortableCallSettings({ calls }: { calls: PortableCallControls }) {
  const { status, pending, error } = calls
  if (!status) return null
  const { enabled, autoStop, defaultPolicy = "ask", policies } = status
  const providerPolicies = status.providers.map(app => {
    const canAlwaysRecord = app.canAlwaysRecord ?? !app.browser
    const policy = policies[app.id] ?? defaultPolicy
    return {
      provider: app.id, label: app.name, website: app.website, lastSeen: app.lastSeen ?? undefined,
      policy: !canAlwaysRecord && policy === "auto" ? "ask" as const : policy,
      canAlwaysRecord, inheritsPolicy: !Object.hasOwn(policies, app.id),
    }
  })
  return <MeetingCalls meeting={{ detection: enabled, autoStop, defaultPolicy, providerPolicies }} pending={pending} feedback={error} warning={status.settingsWarning}
    stopDetail="Automatically started recordings save 30 seconds after the call app stops using both microphone and playback."
    requestedStop={{ enabled: !!status.stopRequestedCalls, change: enabled => calls.update({ action: "stopRequestedCalls", enabled }) }}
    act={calls.update} />
}

export type PortableCallBarProps = {
  calls: PortableCallControls; languages: string[]; viewing: boolean; canView: boolean; onView: (call: DetectedCall) => void
}

export function PortableCallBar({ calls, languages, viewing, canView, onView }: PortableCallBarProps) {
  const { t } = useInterfaceLanguage()
  const { status, pending, error } = calls
  const offer = status?.offer
  const active = status?.active
  if (!offer && !active && !error && !status?.message) return null
  return <aside className="portable-call-bar" aria-label={t("Detected call")} onKeyDown={event => {
    if (event.key === "Escape" && offer?.choosing && !pending) {
      event.stopPropagation()
      void calls.answer(offer.token, "back")
    }
  }}>
    {offer && <>
      <div className="portable-call-description" role="status"><Phone aria-hidden="true" />
        <span>{!offer.choosing && t("Call detected")} <strong>{offer.name}</strong>
          {offer.choosing && <small>{t("Change anytime in Settings")}</small>}
        </span>
      </div>
      <div className="dictation-row-actions">
        <Button variant="ghost" size={offer.choosing ? "sm" : "icon-sm"} disabled={pending}
          aria-label={t(offer.choosing ? "Cancel" : "Recording options")}
          title={offer.choosing ? undefined : t("Recording options")}
          aria-expanded={offer.choosing} onClick={() => void calls.answer(offer.token, offer.choosing ? "back" : "more")}>
          {offer.choosing ? t("Cancel") : <Ellipsis aria-hidden="true" />}
        </Button>
        {offer.choosing ? <>
          <Button variant="outline" size="sm" disabled={pending}
            onClick={() => void calls.answer(offer.token, "never")}>{t("Never offer")}</Button>
          {offer.canAlways && <Button size="sm" disabled={pending}
            onClick={() => void calls.answer(offer.token, "always", languages)}>{t("Always record")}</Button>}
        </> : <>
          <Button variant="ghost" size="sm" disabled={pending}
            onClick={() => void calls.answer(offer.token, "dismiss")}>{t("Not now")}</Button>
          <Button size="sm" disabled={pending}
            onClick={() => void calls.answer(offer.token, "record", languages)}>{t("Record")}</Button>
        </>}
      </div>
    </>}
    {active && <>
      <div className="portable-call-description">
        <AudioLines aria-hidden="true" />
        <span><strong>{active.name}</strong> · <bdi>{clock(active.elapsed)}</bdi>
          <small>{active.stopSeconds !== null && active.stopSeconds !== undefined ?
            `${t(active.roomChanged ? "Call changed" : "Call ended")} · ${t("Stops in")} ${active.stopSeconds}s` :
            active.automatic ? t("Started automatically") : t("Recording")}</small>
        </span>
      </div>
      <div className="dictation-row-actions">
        {!!active.stopSeconds && <Button variant="ghost" size="sm" disabled={pending}
          onClick={() => void calls.recording(active.id, "keep")}>{t("Keep recording")}</Button>}
        {!viewing && <Button variant="ghost" size="sm" disabled={!canView || pending || active.saving}
          onClick={() => onView(active)}>{t("View live transcript")}</Button>}
        <Button variant="outline" size="sm" disabled={pending || active.saving}
          onClick={() => void calls.recording(active.id, "stop")}>
          <Square aria-hidden="true" />{active.saving ? t("Saving…") : t("Stop & Save")}
        </Button>
      </div>
    </>}
    {(error || status?.message) && <span role="alert">{t(error || status?.message || "")}</span>}
  </aside>
}
