import { useInterfaceLanguage } from "@/interface-language"
import { useEffect, useRef, useState } from "react"
import { AudioLines, Check, CircleHelp, Mic } from "lucide-react"
import { Dialog } from "radix-ui"
import { captureSources, type CaptureSource, type DesktopBridge, type RecordingMode } from "@/desktop"
import type { RecordingAccess } from "@/use-recording-access"
import { SettingDisclosure, SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"
import "./recording-permissions.css"

const names = { mic: "Microphone", system: "Mac audio" }
const panes = { mic: "Microphone", system: "Screen & System Audio Recording" }
const statuses = { granted: "Allowed", restricted: "Restricted by macOS", denied: "Access needed", unavailable: "Access needed" }
const resetMicrophone = "/usr/bin/tccutil reset Microphone local.jardaudio.desktop"

type AccessProps = { native: DesktopBridge; access: RecordingAccess; refusedSource: CaptureSource | null }
type DialogProps = AccessProps & {
  mode: RecordingMode; inspection: boolean; starting: boolean; onClose: () => void; onRetry: () => void
}

export function RecordingAccessSettings(props: AccessProps) {
  return <AccessControls {...props} mode="both" view="settings" />
}

export function RecordingAccessDialog({ inspection, ...props }: DialogProps) {
  return <AccessControls {...props} view={inspection ? "inspection" : "record"} />
}

type ControlsProps = AccessProps & {
  mode: RecordingMode; view: "inspection" | "record" | "settings"
  starting?: boolean; onClose?: () => void; onRetry?: () => void
}

function AccessControls({ native, mode, access, refusedSource, view, starting = false, onClose, onRetry }: ControlsProps) {
  const { t } = useInterfaceLanguage()
  const { permissions, check: read } = access
  const settings = view === "settings"
  const sources = captureSources(mode)
  const granted = sources.every(source => permissions[source] === "granted")
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState("")
  const active = useRef(true)
  const changing = useRef(false)
  const cancel = useRef<HTMLButtonElement>(null)
  const busy = starting || pending

  async function check() {
    if (starting || changing.current) return
    changing.current = true
    setPending(true)
    try {
      const result = await read(mode)
      if (active.current) {
        setMessage(sources.every(s => result[s] === "granted") ?
          (refusedSource ? "System Settings reports access is enabled. Quit and reopen Jard if macOS still refuses capture." : "Access is ready.") :
          (result.system === "unavailable" ? "Choose Verify Mac audio access to check with macOS." : "Check the macOS settings for the sources marked above."))
      }
    } catch { if (active.current) setMessage("Could not check access. Try again.") }
    finally { changing.current = false; if (active.current) setPending(false) }
  }
  useEffect(() => {
    active.current = true
    // Opening Settings checks both grants independently of the Home source selector.
    if (settings || refusedSource) void read(mode).catch(() => {
      if (active.current) setMessage("Could not check access. Try again.")
    })
    return () => { active.current = false }
  }, [read, mode, refusedSource, settings])

  async function request(source: CaptureSource) {
    if (busy || changing.current) return
    changing.current = true
    setPending(true)
    try {
      const status = await access.request(source)
      if (active.current) {
        setMessage(status === "granted" ? names[source] + " access is ready." : "If macOS skips the prompt, use the privacy settings for this source.")
      }
    } catch { if (active.current) setMessage("Could not request access. Try again.") }
    finally { changing.current = false; if (active.current) setPending(false) }
  }

  function openSettings(source: CaptureSource) {
    void native.request("openPrivacy", { source }).catch(() => setMessage("Open System Settings → Privacy & Security → " + panes[source] + "."))
  }

  const accessNotice = (refusedSource || sources.some(source => permissions[source] === "denied"))
    ? "If Jard is already on in System Settings, quit Jard with ⌘Q, reopen it from Applications, then retry."
    : ""
  const sourceRows = sources.map(source => {
    const status = permissions[source]
    const refused = refusedSource === source
    const allowed = !refused && status === "granted"
    const needsSettings = refused || status === "denied" || status === "restricted"
    const statusLabel = refused ? "Capture refused" : source === "system" && status === "unavailable" ?
      "Unconfirmed" : status ? statuses[status] : access.checked ? "Could not check" : "Checking…"
    const Icon = source === "mic" ? Mic : AudioLines
    const detail = t(source === "mic" ? "Your voice from the selected input" : "Sound from calls and other apps")
    const actions = <>
      {allowed ? <span className="recording-access-allowed"><Check aria-hidden="true" />{t("Allowed")}</span> : <small>{t(statusLabel)}</small>}
      {!allowed && !needsSettings && <Button variant="outline" size="sm" disabled={busy}
        onClick={() => void request(source)}>{t(source === "system" ? "Verify Mac audio access" : "Request access")}</Button>}
      {(settings || needsSettings) && <Button variant="outline" size="sm" disabled={busy}
        aria-label={"Open " + panes[source] + " settings"} onClick={() => openSettings(source)}>{t("Open Settings")}</Button>}
    </>
    if (settings) return <SettingRow key={source} icon={Icon} title={t(names[source])} detail={detail} label={false}
      find={t(source === "mic" ? "Manage microphone recording permissions" : "Manage Mac audio recording permissions")}>
      <div className="dictation-row-actions recording-access-setting-actions">{actions}</div>
    </SettingRow>
    return <div key={source} className="recording-access-row">
      <Icon aria-hidden="true" /><div><strong>{t(names[source])}</strong><small>{detail}</small></div>
      <div className="recording-access-action">{actions}</div>
    </div>
  })
  const helpTitle = t("Jard isn’t listed in System Settings")
  const help = <>
    <p>Open Jaro from Finder and press Record to register its request. If macOS asks you to quit and reopen Jaro, do that before checking again.</p>
    {sources.includes("mic") && <p>Under Privacy &amp; Security → Microphone, macOS lists apps after they request access. Turn on Jaro. A restricted Mac may need an administrator.</p>}
    {permissions.mic === "denied" && <div className="recording-access-reset">
      <p>If Jard is still missing from Microphone after reopening, copy this command, quit Jard, then run it in Terminal. It clears only Jard’s microphone decision. Reopen Jard and press Record for a fresh macOS prompt.</p>
      <code>{resetMicrophone}</code>
      <button type="button" onClick={() => void navigator.clipboard.writeText(resetMicrophone)
        .then(() => setMessage("Microphone recovery command copied."))
        .catch(() => setMessage("Select and copy the command shown above."))}>Copy recovery command</button>
    </div>}
    {sources.includes("system") && <><p>Under Privacy &amp; Security → Screen &amp; System Audio Recording, turn on Jard. If it is missing, use the add (+) button when available and select the app shown in Finder.</p>
      <div className="recording-access-links">
        <button type="button" disabled={busy} onClick={() => void native.request("revealApp").catch(() => setMessage("Locate Jaro in Finder."))}>Show this app in Finder</button>
        {(permissions.system === "denied" || refusedSource === "system") && <button type="button" disabled={busy} onClick={() => void request("system")}>Request Mac audio access again</button>}
      </div></>}
  </>
  const feedback = starting ? "Respond to the macOS permission request." : message

  if (settings) return <div className="recording-settings">
    <SettingGroup id="recording-access-title" title={t("Recording access")}>
      {sourceRows}
      {accessNotice && <p className="recording-access-settings-note">{t(accessNotice)}</p>}
      <SettingDisclosure icon={CircleHelp} title={helpTitle}>
        <div className="recording-access-settings-help">{help}</div>
      </SettingDisclosure>
      <div className="recording-access-settings-footer">
        <p role="status" className="recording-access-status">{t(feedback)}</p>
        <Button variant="outline" size="sm" disabled={busy} onClick={() => void check()}>{t("Check access again")}</Button>
      </div>
    </SettingGroup>
  </div>

  const inspection = view === "inspection"
  return <Dialog.Root open onOpenChange={open => { if (!open && !busy) onClose?.() }}>
    <Dialog.Portal>
      <Dialog.Overlay className="session-search-overlay" />
      <Dialog.Content className="recording-access-dialog"
        onOpenAutoFocus={event => { event.preventDefault(); cancel.current?.focus() }}
        onCloseAutoFocus={event => {
          event.preventDefault()
          const trigger = (inspection ? document.querySelector<HTMLButtonElement>(".native-permission-link") : null)
            ?? document.querySelector<HTMLButtonElement>(".record-button")
          trigger?.focus()
        }}>
        <Dialog.Title>{inspection ? "Recording access" : "Allow recording on this Mac"}</Dialog.Title>
        <Dialog.Description>{refusedSource ?
          "macOS refused " + (refusedSource === "system" ? "Mac audio capture" : "microphone capture") + ". Check Jaro in Privacy & Security, then retry." :
          (inspection ? "Check macOS access for your selected audio sources." : "Check access for the audio sources you selected.")}</Dialog.Description>
        {accessNotice && <p>{t(accessNotice)}</p>}
        <div className="recording-access-sources">{sourceRows}</div>
        {(!granted || refusedSource) && <details className="recording-access-help">
          <summary>{helpTitle}</summary>
          {help}
        </details>}
        <p role="status" className="recording-access-status">{t(feedback)}</p>
        <div className="recording-access-footer"><Button ref={cancel} variant="ghost" size="sm" disabled={busy} onClick={onClose}>{t("Cancel")}</Button>
          <Button size="sm" disabled={busy} onClick={() => granted ? (inspection ? onClose?.() : onRetry?.()) : void check()}>{granted ? (inspection ? "Done" : "Retry recording") : "Check access again"}</Button></div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
