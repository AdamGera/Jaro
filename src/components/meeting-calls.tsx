import { useState } from "react"
import { Clock, Globe, Phone, SlidersHorizontal } from "lucide-react"
import { Dialog } from "radix-ui"
import type { DesktopMeeting, MeetingCallAction, MeetingRecordingPolicy } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const policyTitles = {
  ask: "Ask",
  auto: "Always record",
  never: "Never offer",
} as const

type CallSettings = Pick<DesktopMeeting, "detection" | "defaultPolicy" | "autoStop" | "providerPolicies">
export function MeetingCalls({ meeting, pending, act, feedback, stopDetail, requestedStop, warning }: {
  meeting: CallSettings; pending: boolean; act: (request: MeetingCallAction) => Promise<void>; feedback: string; stopDetail: string; warning?: string
  requestedStop?: { enabled: boolean; change: (enabled: boolean) => Promise<void> }
}) {
  const { t, locale } = useInterfaceLanguage()
  const [edited, setEdited] = useState(false)
  const defaultPolicy = meeting.defaultPolicy ?? "ask"
  const formatDate = new Intl.DateTimeFormat(locale, { dateStyle: "medium" })

  function change(action: MeetingCallAction) {
    setEdited(true)
    void act(action)
  }

  return <SettingGroup id="meeting-detection-title" title={t("Detected calls")}>
    <Dialog.Root onOpenChange={() => setEdited(false)}>
      <SettingRow icon={Phone} title={t("Calls")} label={false} anchor="meeting-call-settings"
        find="Automatically detect meetings, offer to record detected calls, stop automatically when a call ends, app and website rules"
        detail={meeting.detection ? t(policyTitles[defaultPolicy]) : t("Call detection is off.")}>
        <Dialog.Trigger asChild>
          <Button size="sm" variant="outline">{t("Manage calls")}</Button>
        </Dialog.Trigger>
      </SettingRow>
      <Dialog.Portal>
        <Dialog.Overlay className="note-language-scrim" />
        <Dialog.Content className="note-language-confirm meeting-calls-dialog dictation-preferences">
          <header className="meeting-calls-header">
            <div>
              <Dialog.Title>{t("Calls")}</Dialog.Title>
              <Dialog.Description>{t("Choose what Jaro does when it detects a call.")}</Dialog.Description>
            </div>
            <Dialog.Close asChild><Button variant="outline" size="sm">{t("Done")}</Button></Dialog.Close>
          </header>
          <div className="meeting-calls-body">
            <SettingGroup id="meeting-calls-defaults" title={t("When a call starts")}>
              <SettingRow icon={Phone} title={t("Detect calls")} find={false}>
                {labels => <input {...labels} type="checkbox" role="switch" checked={meeting.detection} disabled={pending}
                  onChange={event => change({ action: "detection", enabled: event.target.checked })} />}
              </SettingRow>
              <SettingRow icon={SlidersHorizontal} title={t("Default call behavior")} find={false}
                detail={defaultPolicy === "auto" ? t("Jaro asks before recording when it can’t identify the website.") : undefined}>
                {labels => <select {...labels} value={defaultPolicy} disabled={pending}
                  onChange={event => change({ action: "setDefaultPolicy", policy: event.target.value as MeetingRecordingPolicy })}>
                  {Object.entries(policyTitles).map(([value, title]) => <option key={value} value={value}>{t(title)}</option>)}
                </select>}
              </SettingRow>
              <SettingRow icon={Clock} title={t("Stop automatically when a call ends")} find={false}
                detail={t(stopDetail)}>
                {labels => <input {...labels} type="checkbox" role="switch" checked={meeting.autoStop ?? false} disabled={pending}
                  onChange={event => change({ action: "autoStop", enabled: event.target.checked })} />}
              </SettingRow>
              {requestedStop && <SettingRow icon={Clock} title={t("Stop calls I start recording")} find={false}
                detail={t("For accepted offers and meetings started with one recognized call app on the microphone. You have 30 seconds to keep recording after the call ends.")}>
                {labels => <input {...labels} type="checkbox" role="switch" checked={requestedStop.enabled} disabled={pending}
                  onChange={event => { setEdited(true); void requestedStop.change(event.target.checked) }} />}
              </SettingRow>}
            </SettingGroup>
            {warning && <p className="meeting-calls-feedback" role="status">{t(warning)}</p>}
            {edited && feedback && <p className="meeting-calls-feedback" role="status">{t(feedback)}</p>}
            <SettingGroup id="meeting-calls-sources" title={t("Apps and websites")}>
              {meeting.providerPolicies.map(({ provider, label, scope, policy, canAlwaysRecord, inheritsPolicy, lastSeen, website }) => {
                const isWebsite = website || provider.startsWith("site:")
                const observed = lastSeen ? new Date(lastSeen * 1000) : undefined
                const date = observed && Number.isFinite(observed.getTime()) ? formatDate.format(observed) : undefined
                return <SettingRow key={provider} icon={isWebsite ? Globe : Phone} title={t(label)}
                  context={scope || undefined} find={false}
                  detail={date ? t("Last seen {date}", { date }) : undefined}>
                  {labels => <select {...labels} value={inheritsPolicy ? "inherit" : policy} disabled={pending}
                    onChange={event => change({ action: "setPolicy", provider,
                      policy: event.target.value as MeetingRecordingPolicy | "inherit" })}>
                    <option value="inherit">{inheritsPolicy && !isWebsite ? t("Inherited · {policy}", { policy: t(policyTitles[policy]) }) : t("Use inherited rules")}</option>
                    <option value="ask">{t(policyTitles.ask)}</option>
                    {canAlwaysRecord && <option value="auto">{t(policyTitles.auto)}</option>}
                    <option value="never">{t(policyTitles.never)}</option>
                  </select>}
                </SettingRow>
              })}
            </SettingGroup>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </SettingGroup>
}
