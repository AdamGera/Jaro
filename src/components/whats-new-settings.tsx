import { Mic, RotateCw, UsersRound, Volume2 } from "lucide-react"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const highlights = [
  {
    id: "sounds",
    icon: Volume2,
    title: "Sound style",
    detail: "Listen to the sounds Jaro plays while you dictate.",
    action: "Open sound styles",
  },
  {
    id: "microphone",
    icon: Mic,
    title: "Microphone",
    detail: "See the active input and fallback as microphones connect or disconnect.",
    action: "Open microphone settings",
  },
  {
    id: "meetings",
    icon: UsersRound,
    title: "Meetings",
    detail: "Identify speakers in the combined recording and keep both original audio tracks.",
    action: "Open Meetings",
  },
  {
    id: "updates",
    icon: RotateCw,
    title: "App updates",
    detail: "Install a verified update when you’re ready to restart.",
    action: "Open update settings",
  },
] as const

export type HighlightDestination = (typeof highlights)[number]["id"]

export function WhatsNewSettings({ version, onOpen }: {
  version?: string
  onOpen: (destination: HighlightDestination) => void
}) {
  const { t } = useInterfaceLanguage()
  return <SettingGroup id="whats-new-highlights" title={version ? t("Installed version {version}", { version }) : t("What's new")}>
    {highlights.map(({ id, icon, title, detail, action }) => <SettingRow key={id} icon={icon} label={false} find={false}
      title={t(title)} detail={t(detail)}>
      <Button type="button" size="sm" variant="outline" onClick={() => onOpen(id)}>{t(action)}</Button>
    </SettingRow>)}
  </SettingGroup>
}
