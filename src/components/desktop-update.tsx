import { useInterfaceLanguage } from "@/interface-language"
import { ExternalLink, RotateCw } from "lucide-react"
import type { DesktopUpdateChannel } from "@/desktop"
import type { DesktopUpdateControls } from "@/desktop-update"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const channelLabel: Record<DesktopUpdateChannel, string> = { stable: "Stable", dev: "Development" }
const progress = {
  checking: "Looking for new versions…",
  downloading: "Downloading update…",
  staging: "Verifying update…",
} as const

type Props = { controls: DesktopUpdateControls; busy: boolean }

function updateControl(controls: DesktopUpdateControls, busy: boolean, t: ReturnType<typeof useInterfaceLanguage>["t"]) {
  const { update, pending, manual, error, run } = controls
  if (!update) return null
  const { phase, notes, message, version, channel, offer, offerVersion, currentVersion } = update
  const managed = update.updatesDisabled === true
  const unavailable = managed || phase === "unavailable"
  const nativeProgress = phase === "checking" || phase === "downloading" || phase === "staging"
  const checking = nativeProgress ? progress[phase] : pending === "check" ? progress.checking : null
  const restarting = pending === "restart" || phase === "applying" || update.restartInProgress === true
  const staged = !unavailable && (update.staged ?? phase === "ready")
  const ready = restarting || staged
  const restartBlocked = staged && (busy || update.restartBlocked)
  const visibleProgress = phase === "checking" && !manual ? null : checking
  const switchLabel = (phase === "switch" || (phase === "ready" && staged)) && offer && channel ? t("Switch to {channel} {version}", {
    channel: t(channelLabel[channel]), version: offerVersion ?? version ?? "",
  }) : null
  const confirmSwitch = switchLabel && offer ? () => void run({ action: "confirmSwitch", offer }) : null
  const label = restarting ? t("Restarting…") : ready ? t("Restart to Update")
    : visibleProgress ? t(visibleProgress) : switchLabel ?? t(phase === "download" ? "Download Update" : "Look for new versions")
  const result = managed ? "Updates managed by your organization"
    : restarting ? ""
    : restartBlocked ? "Finish your current work to restart Jard."
    : error || update.restartError || (unavailable ? "Updates are disabled for this installation."
    : checking ? (ready ? checking : "")
    : phase === "error" ? (manual ? message || "Could not check for updates." : "")
    : phase === "current" && manual ? "Jard is up to date."
    : message || "")
  const available = version && staged ? t("Jard {version} is ready to install.", { version }) : undefined
  return {
    label, result: result ? t(result) : "", notes: staged ? notes?.trim() : "", available, currentVersion, unavailable, managed, ready,
    compact: !ready && !switchLabel && phase !== "download",
    checking: !!checking && !ready, restarting, switchLabel, confirmSwitch,
    disabled: unavailable || !!pending || (!!checking && !ready) || restarting || restartBlocked,
    apply: ready ? () => void run({ action: "restart" }) : confirmSwitch ??
      (() => void run({ action: phase === "download" ? "download" : "check" })),
  }
}

function ReleaseNotes({ controls, notes, sidebar = false }: { controls: DesktopUpdateControls; notes: string; sidebar?: boolean }) {
  const { t } = useInterfaceLanguage()
  const { update, pending, run } = controls
  const version = update?.version
  const channel = update?.stageChannel
  const external = update?.releaseNotesAvailable && version && channel
  const changes = sidebar ? [...new Set(notes.split(/\r?\n/)
    .map(line => line.replace(/[\p{Cc}]/gu, " ").trim())
    .filter(line => line && !/^#{1,6}\s/.test(line))
    .map(line => line.replace(/^(?:[-*+]|\d+[.)])\s+/, "").trim())
    .filter(Boolean))].slice(0, 3) : []
  return <div className={sidebar ? "sidebar-release-notes" : "update-settings-feedback"}>
    <details key={`${channel}/${version}/${notes}`}>
      <summary>{t("Release notes")}</summary>
      <p>{notes}</p>
      {external && <button type="button" className="update-release-link"
        disabled={!!pending || update?.restartInProgress || update?.phase === "applying"}
        aria-label={t("Open release page")} onClick={() => void run({ action: "releaseNotes", version, channel })}>
        <ExternalLink aria-hidden="true" />{t("Open release page")}
      </button>}
    </details>
    {changes.length > 0 && <ul aria-label={t("Release notes")}>
      {changes.map(change => <li key={change}>{change}</li>)}
    </ul>}
  </div>
}

export function DesktopUpdateSettings({ controls, busy }: Props) {
  const { t } = useInterfaceLanguage()
  const action = updateControl(controls, busy, t)
  const { update, pending, run } = controls
  if (!action || !update) return null
  const { channels } = update
  return <SettingGroup id="desktop-updates-title" title={t("Updates")}>
    <SettingRow icon={RotateCw} title={t("App updates")} label={false} anchor="desktop-app-updates"
      find="Look for new versions or restart Jard to install an update"
      detail={[action.currentVersion && t("Installed version {version}", { version: action.currentVersion }), action.available].filter(Boolean).join(" · ")}>
      {!action.managed && <Button size="sm" variant={action.ready ? "secondary" : "outline"} disabled={action.disabled} onClick={action.apply}>{action.label}</Button>}
    </SettingRow>
    {!action.unavailable && update.channel && channels && channels.length > 1 &&
      <SettingRow icon={RotateCw} title={t("Update channel")} find="Stable or Development releases"
        detail={update.channelManaged ? "Your administrator manages this setting." :
          update.channel !== update.installedChannel && update.installedChannel ? `Installed channel is ${channelLabel[update.installedChannel]}.` : undefined}>
        {labels => (
          <select {...labels} value={update.channel} disabled={!!pending || update.channelManaged || action.restarting}
            onChange={event => void run({ action: "channel", channel: event.target.value as DesktopUpdateChannel })}>
            {channels.map(channel => <option value={channel} key={channel}>{t(channelLabel[channel])}</option>)}
          </select>
        )}
      </SettingRow>}
    {action.ready && action.confirmSwitch && <div className="update-settings-feedback">
      <Button size="sm" variant="outline" disabled={!!pending || action.restarting} onClick={action.confirmSwitch}>
        {action.switchLabel}
      </Button>
    </div>}
    {action.result && <p className="update-settings-feedback">{action.result}</p>}
    {action.available && action.notes && <ReleaseNotes controls={controls} notes={action.notes} />}
  </SettingGroup>
}

export function DesktopUpdateControl({ controls, busy }: Props) {
  const { t } = useInterfaceLanguage()
  const action = updateControl(controls, busy, t)
  if (!action) return null
  if (action.managed) return <div className="sidebar-update"><small>{action.result}</small></div>
  if (action.unavailable) return null
  const compact = action.compact
  const feedback = action.result || (action.checking && controls.manual ? action.label : "")
  return <div className="sidebar-update">
    <button className={compact ? "sidebar-update-icon" : action.ready ? "sidebar-home update-ready" : "sidebar-home sidebar-update-action"}
      type="button" title={action.label} aria-label={compact ? action.label : undefined}
      disabled={action.disabled} onClick={action.apply}>
      <RotateCw aria-hidden="true" className={action.checking || action.restarting ? "motion-safe:animate-spin" : undefined} />
      {!compact && <span>{action.label}</span>}
    </button>
    {feedback && <small>{feedback}</small>}
    {action.notes && controls.update?.stageChannel === "dev" && <ReleaseNotes controls={controls} notes={action.notes} sidebar />}
  </div>
}

export function DesktopUpdateFeedback({ controls, busy }: Props) {
  const { t } = useInterfaceLanguage()
  const action = updateControl(controls, busy, t)
  return <span role="status" className="sr-only">
    {controls.manual || controls.pending || controls.error || action?.restarting || controls.update?.restartError
      ? action?.result || action?.label : action?.available}
  </span>
}
