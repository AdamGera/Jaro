import { useEffect, useRef, useState } from "react"
import { Check, ScanLine } from "lucide-react"
import type { DesktopAnnotationAccess, DesktopBridge } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const details = {
  checking: "Checking ongoing access to record your Mac screen.",
  off: "Allow ongoing access in System Settings. You can still choose a source with the macOS picker.",
  unavailable: "Jaro couldn’t check ongoing access. You can still choose a source with the macOS picker.",
  restart: "macOS granted access. Reopen Jaro to use it.",
  granted: "macOS allows Jaro to record your screen.",
}

export function AnnotationAccess({ native, busy }: { native: DesktopBridge; busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const surface = useRef<HTMLDivElement>(null)
  const mounted = useRef(false)
  const acting = useRef(false)
  const revision = useRef(0)
  const [status, setStatus] = useState<DesktopAnnotationAccess>()
  const [pending, setPending] = useState(false)
  const [failure, setFailure] = useState("")

  useEffect(() => {
    const element = surface.current
    if (!element) return
    mounted.current = true
    let active = true
    let reading = false
    let shown = false
    async function refresh() {
      if (!shown || document.hidden || reading || acting.current) return
      reading = true
      const version = revision.current
      try {
        const next = await native.request("annotationAccess", { action: "status" })
        if (active && version === revision.current) {
          setStatus(next)
          if (next.authorization === "granted") setFailure("")
        }
      } catch {
        if (active && version === revision.current)
          setStatus(value => value ?? { authorization: "unavailable", canRepair: false, busy: false })
      } finally { reading = false }
    }
    const observer = new IntersectionObserver(([entry]) => {
      shown = entry.isIntersecting
      if (shown) void refresh()
    })
    observer.observe(element)
    const timer = window.setInterval(refresh, 1200)
    window.addEventListener("focus", refresh)
    document.addEventListener("visibilitychange", refresh)
    return () => {
      active = mounted.current = false
      observer.disconnect()
      window.clearInterval(timer)
      window.removeEventListener("focus", refresh)
      document.removeEventListener("visibilitychange", refresh)
    }
  }, [native])

  async function change(action: "allow" | "repair" | "restart") {
    if (acting.current) return
    acting.current = true
    ++revision.current
    setPending(true)
    setFailure("")
    try {
      const next = await native.request("annotationAccess", { action })
      if (mounted.current && action !== "restart") setStatus(next)
    } catch (error) {
      if (mounted.current) setFailure(error instanceof Error ? error.message : "Could not update Screen Recording access.")
    } finally {
      acting.current = false
      if (mounted.current) setPending(false)
    }
  }

  const authorization = status?.authorization ?? "checking"
  const disabled = busy || pending || !!status?.busy
  return <div ref={surface}>
    <SettingGroup id="annotation-permissions" title={t("Permissions")}>
      <SettingRow icon={ScanLine} title={t("Screen Recording")} label={false}
        find={t("Screen Recording ongoing permission allow restart repair")}
        detail={<>{t(details[authorization])}{failure && <span className="annotation-access-error" role="alert">{t(failure)}</span>}</>}>
        {authorization === "checking" ? <span className="dictation-login-state" role="status">{t("Checking access…")}</span> :
          authorization === "granted" ? <span className="dictation-granted" role="status"><Check size={14} aria-hidden="true" />{t("Allowed")}</span> :
          authorization === "restart" ? <Button size="sm" variant="outline" disabled={disabled}
            onClick={() => void change("restart")}>{t(pending ? "Restarting…" : "Restart Jaro")}</Button> :
          <div className="dictation-row-actions">
            <Button size="sm" variant="outline" disabled={disabled} onClick={() => void change("allow")}>{t("Open System Settings")}</Button>
            {!!status?.canRepair && <Button size="sm" variant="ghost" disabled={disabled}
              onClick={() => void change("repair")}>{t("Reset Screen Recording access")}</Button>}
          </div>}
      </SettingRow>
    </SettingGroup>
  </div>
}
