import { useEffect, useState } from "react"
import { Languages, Monitor } from "lucide-react"
import type { DesktopBridge, DesktopDockIcon } from "@/desktop"
import { SettingGroup, SettingRow } from "./settings-rows"
import { useInterfaceLanguage } from "@/interface-language"

export function DesktopApplicationSettings({ native }: { native: DesktopBridge }) {
  const { t, language, locale, selectLanguage, pending: switchingLanguage, error: languageError } = useInterfaceLanguage()
  const [dock, setDock] = useState<DesktopDockIcon>()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")

  useEffect(() => {
    let active = true
    let received = false
    const receive = (event: WindowEventMap["native-sdk:jard-dock-icon"]) => {
      received = true
      setDock(event.detail)
    }
    window.addEventListener("native-sdk:jard-dock-icon", receive)
    void native.request("dockIcon", { action: "status" }).then(value => {
      if (active && !received) setDock(value)
    }).catch(() => { if (active && !received) setError("Could not load the Dock icon setting.") })
    return () => { active = false; window.removeEventListener("native-sdk:jard-dock-icon", receive) }
  }, [native])

  async function change(enabled: boolean) {
    setPending(true)
    setError("")
    try {
      setDock(await native.request("dockIcon", { action: "set", enabled }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not change the Dock icon setting.")
    } finally {
      setPending(false)
    }
  }

  const menuUnavailable = dock?.enabled === false && !dock.menuBarAvailable
  const choices = Object.entries(language?.choices ?? {}).sort((left, right) => left[1].localeCompare(right[1], locale))
  return <SettingGroup id="desktop-application-title" title={t("Application")}>
    <SettingRow icon={Monitor} title={t("Show Dock icon")} find={t("Keep Jard in the Dock or reopen it from the menu bar")}
      detail={menuUnavailable ? t("Jard stays in the Dock while the menu bar item is unavailable.")
        : t("When off, reopen Jard from the menu bar after closing the window.")}>
      {labels => (
        <input {...labels} type="checkbox" role="switch" checked={dock?.enabled ?? true}
          disabled={pending || !dock} onChange={event => void change(event.target.checked)} />
      )}
    </SettingRow>
    <SettingRow icon={Languages} title={t("Interface language")} find={t("Change the language of Jard’s controls and menus")}>
      {labels => (
        <select {...labels} value={language?.selected ?? "system"} disabled={!language || switchingLanguage}
          onChange={event => void selectLanguage(event.target.value)}>
          <option value="system">{t("System")}</option>
          {choices.map(([code, label]) => <option key={code} value={code} lang={code}>{label}</option>)}
        </select>
      )}
    </SettingRow>
    {(error || languageError) && <p className="update-settings-feedback" role="alert">{t(error || languageError)}</p>}
  </SettingGroup>
}
