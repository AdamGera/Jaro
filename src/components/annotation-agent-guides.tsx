import { useEffect, useState } from "react"
import { BookOpen, Copy, Terminal } from "lucide-react"
import type { AnnotationAgentSkillRequest, AnnotationAgentSkillResult, AnnotationSkillStatus, DesktopBridge } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingDisclosure, SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const names = { claude: "Claude Code", codex: "Codex", opencode: "OpenCode", pi: "Pi" }
const labels = { installed: "Installed", missing: "Guide available", managed: "Managed elsewhere", different: "A different guide is installed" }
const loadFailure = "Could not read the annotation guides."

async function requestGuide(native: DesktopBridge | undefined, command: AnnotationAgentSkillRequest): Promise<AnnotationAgentSkillResult> {
  if (native) return native.request("annotationAgentSkill", command)
  const response = await fetch("/api/portable/annotation-agent-skill", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(command),
  })
  const value = await response.json()
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : loadFailure)
  return value
}

export function AnnotationAgentGuides({ native, busy }: { native?: DesktopBridge; busy: boolean }) {
  const { t } = useInterfaceLanguage()
  const [open, setOpen] = useState(false)
  const [agents, setAgents] = useState<AnnotationSkillStatus[]>([])
  const [guide, setGuide] = useState("")
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState("")
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!open) return
    let current = true
    void Promise.all([requestGuide(native, { action: "status" }), requestGuide(native, { action: "guide" })])
      .then(([status, content]) => {
        if (!current) return
        if (!("agents" in status) || !("markdown" in content)) throw new Error(loadFailure)
        setAgents(status.agents)
        setGuide(content.markdown)
      }).catch(error => { if (current) setFeedback(error instanceof Error ? error.message : loadFailure) })
    return () => { current = false }
  }, [native, open, reload])

  async function install(row: AnnotationSkillStatus) {
    setPending(true)
    setFeedback("")
    try {
      const result = await requestGuide(native, { action: row.state === "different" ? "update" : "install", agent: row.agent })
      if (!("agents" in result)) throw new Error("Could not install the guide.")
      setAgents(result.agents)
      setFeedback("Guide installed. Open a new agent chat to use it.")
    } catch (error) { setFeedback(error instanceof Error ? error.message : "Could not install the guide.") }
    finally { setPending(false) }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(guide)
      setFeedback("Annotation guide copied.")
    } catch { setFeedback("Jaro couldn’t copy to the clipboard.") }
  }

  return <SettingGroup id="annotation-agent-guides" title={t("Agents")}>
    <SettingDisclosure icon={BookOpen} title={t("Capture review guides")} onToggle={setOpen}>
      <SettingRow icon={Copy} title={t("Jaro annotation guide")} label={false}
        detail={t("Give your agent instructions to inspect narration, drawings, and video frames.")}>
        <Button size="sm" variant="outline" disabled={!guide} onClick={() => void copy()}>{t("Copy guide")}</Button>
      </SettingRow>
      {agents.map(row => <SettingRow key={row.agent} icon={Terminal} title={names[row.agent]} label={false}
        detail={<span title={row.path}>{t(row.found ? labels[row.state] : "Agent not configured")}</span>}>
        {row.found && (row.state === "missing" || row.state === "different") &&
          <Button size="sm" variant="outline" disabled={busy || pending}
            onClick={() => void install(row)}>{t(row.state === "different" ? "Replace guide" : "Install guide")}</Button>}
      </SettingRow>)}
      {feedback && <div className="update-settings-feedback" role="status">{t(feedback)}</div>}
      {!agents.length && feedback && <Button size="sm" variant="ghost" onClick={() => {
        setFeedback("")
        setReload(value => value + 1)
      }}>{t("Try again")}</Button>}
    </SettingDisclosure>
  </SettingGroup>
}
