import { useCallback, useEffect, useRef, useState } from "react"
import { Power } from "lucide-react"
import type { DesktopBridge, DesktopLoginAction, DesktopLoginItem } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingGroup, SettingRow, type SettingControlLabels } from "./settings-rows"
import { Button } from "./ui/button"

type LoginRequest = (action: DesktopLoginAction) => Promise<DesktopLoginItem>

export function LaunchAtLogin({ native, onboarding = false }: { native: DesktopBridge; onboarding?: boolean }) {
  const request = useCallback<LoginRequest>(action => native.request("loginItem", action), [native])
  return <LoginSwitch request={request} onboarding={onboarding} />
}

export const portableLogin = document.documentElement.hasAttribute("data-jaro-portable-login")

async function requestPortableLogin(action: DesktopLoginAction): Promise<DesktopLoginItem> {
  const endpoint = "/api/portable/login-item"
  const response = await fetch(action.action === "openSettings" ? `${endpoint}/open-settings` : endpoint, {
    cache: "no-store",
    ...(action.action === "set" ? {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: action.enabled }),
    } : action.action === "openSettings" ? { method: "POST" } : {}),
  })
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}))
    throw new Error(typeof failure.detail === "string" ? failure.detail : "Could not access Launch at Login.")
  }
  return await response.json() as DesktopLoginItem
}

export function PortableApplicationSettings() {
  const { t } = useInterfaceLanguage()
  return <SettingGroup id="desktop-application-title" title={t("Application")}>
    <LoginSwitch request={requestPortableLogin} description={t("Start Jaro in the background when you sign in.")} portable />
  </SettingGroup>
}

function LoginSwitch({ request, onboarding, description, portable }: {
  request: LoginRequest; onboarding?: boolean; description?: string; portable?: boolean
}) {
  const { t } = useInterfaceLanguage()
  const [status, setStatus] = useState<DesktopLoginItem>()
  const [requested, setRequested] = useState<boolean>()
  const [actionError, setActionError] = useState("")
  const revision = useRef(0)
  const changing = useRef(false)
  const mounted = useRef(false)

  useEffect(() => {
    mounted.current = true
    let active = true
    let reading = false
    async function refresh() {
      if (changing.current || reading || document.hidden) return
      reading = true
      const version = revision.current
      try {
        const result = await request({ action: "status" })
        if (active && version === revision.current) setStatus(result)
      } catch {
        if (active && version === revision.current) setStatus(previous => previous ?? { state: "unknown" })
      } finally {
        reading = false
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 1500)
    window.addEventListener("focus", refresh)
    document.addEventListener("visibilitychange", refresh)
    return () => {
      active = mounted.current = false
      window.clearInterval(timer)
      window.removeEventListener("focus", refresh)
      document.removeEventListener("visibilitychange", refresh)
    }
  }, [request])

  async function change(action: DesktopLoginAction) {
    if (changing.current) return
    const version = ++revision.current
    changing.current = true
    setRequested(action.action === "set" ? action.enabled : undefined)
    setActionError("")
    try {
      const result = await request(action)
      if (mounted.current && version === revision.current) {
        setStatus(result)
        setActionError(result.message ?? "")
      }
    } catch (error) {
      if (mounted.current && version === revision.current)
        setActionError(error instanceof Error ? error.message : "Could not change Launch at Login.")
    } finally {
      if (mounted.current && version === revision.current) {
        changing.current = false
        setRequested(undefined)
      }
    }
  }

  const title = t(onboarding ? "Launch Jard at login" : "Launch at login")
  const error = actionError || (status?.state === "unknown" ? status.message : undefined)
  if (status?.state === "enabled" || status?.state === "disabled") {
    const input = (labels?: SettingControlLabels) => <input {...labels} type="checkbox" role={onboarding ? undefined : "switch"}
      checked={requested ?? (status.state === "enabled")} disabled={requested !== undefined}
      onChange={event => void change({ action: "set", enabled: event.target.checked })} />
    if (onboarding) return <>
      <label className="dictation-login">{input()}{title}</label>
      {error && <p className="dictation-error" role="alert">{t(error)}</p>}
    </>
    return <SettingRow icon={Power} title={title} detail={error ? <span className="dictation-error" role="alert">{t(error)}</span> : description}>
      {input}
    </SettingRow>
  }

  const control = status?.state === "approval"
    ? <Button type="button" size="sm" variant="outline" onClick={() => void change({ action: "openSettings" })}>
      {t(portable ? "Open Startup Settings" : "Open Login Items")}
    </Button>
    : <span className="dictation-login-state" role="status">{t(status ? "Unavailable" : "Checking…")}</span>
  const detail = error ? <span className="dictation-error" role="alert">{t(error)}</span>
    : status?.state === "approval" ? t(portable ? "Turn on Jaro in Windows Startup settings." : "Allow Jaro in System Settings.") : undefined
  if (onboarding) return <div className="dictation-login">
    <span>{title}{detail && <small className="dictation-login-detail">{detail}</small>}</span>{control}
  </div>
  return <SettingRow icon={Power} title={title} detail={detail} label={false}>{control}</SettingRow>
}
