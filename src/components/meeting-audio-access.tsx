import { useEffect, useRef, useState } from "react"
import { AudioLines, Check } from "lucide-react"
import type { DesktopBridge, DesktopMeetingAudioAccess } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

export function MeetingAudioAccess({ native, busy }: { native: DesktopBridge; busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const [status, setStatus] = useState<DesktopMeetingAudioAccess>()
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const revision = useRef(0)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    let active = true
    let reading = false
    async function refresh() {
      if (reading) return
      reading = true
      const version = revision.current
      try {
        const next = await native.request("meetingAudioAccess", { action: "status" })
        if (active && version === revision.current) setStatus(next)
      } catch {
        // The settings tab can appear while the desktop bridge is starting.
      } finally { reading = false }
    }
    void refresh()
    const timer = window.setInterval(refresh, 750)
    window.addEventListener("focus", refresh)
    return () => {
      active = mounted.current = false
      window.clearInterval(timer)
      window.removeEventListener("focus", refresh)
    }
  }, [native])

  async function change(action: "allow" | "repair") {
    const version = ++revision.current
    setError("")
    setPending(true)
    try {
      const next = await native.request("meetingAudioAccess", { action })
      if (mounted.current && version === revision.current) setStatus(next)
    } catch (failure) {
      if (mounted.current && version === revision.current)
        setError(failure instanceof Error ? failure.message : "Could not update Mac audio access.")
    } finally {
      if (mounted.current && version === revision.current) {
        revision.current++
        setPending(false)
      }
    }
  }

  const allowed = status?.authorization === "granted"
  const requesting = pending || !!status?.busy
  const detail = allowed ? "Jaro can record Mac audio on its own." : status?.screen
    ? "Screen Recording is allowed as a fallback. You can also allow audio-only access."
    : status?.authorization === "denied" || status?.authorization === "unknown"
      ? "In Privacy & Security, turn on Jaro in the System Audio Recording Only list."
      : "Allows meetings to capture the other participants’ audio."
  return <SettingRow icon={AudioLines} title={t("System Audio Recording")} label={false} find="Mac audio permission or audio-only access"
    detail={<>{t(detail)}{error && !allowed && <span className="meeting-audio-error dictation-error" role="alert">{t(error)}</span>}</>}>
    {allowed ? <span className="dictation-granted meeting-audio-allowed" role="status"><Check size={14} aria-hidden="true" />{t("Allowed")}</span> :
      !status ? <span className="dictation-login-state" role="status">{t("Checking…")}</span> :
        <div className="dictation-row-actions dictation-media-actions">
          <Button size="sm" variant="outline" disabled={busy || requesting} onClick={() => void change("allow")}>
            {requesting && <i className="thinking-dot meeting-audio-wait" aria-hidden="true" />}
            {t(requesting ? "Waiting for macOS…" : status.authorization === "request" ? "Allow Mac audio" : "Open System Settings")}
          </Button>
          {!!status.canRepair && <Button size="sm" variant="ghost" disabled={busy || requesting} onClick={() => void change("repair")}>
            {t("Reset Mac audio access")}
          </Button>}
        </div>}
  </SettingRow>
}
