import { useState } from "react"
import { Download } from "lucide-react"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"
import { Progress } from "./ui/progress"

export type ModelSetup = {
  phase: "checking" | "needed" | "downloading" | "download-error" | "starting" | "load-error" | "unavailable"
  total?: number
  completed?: number
  additional?: boolean
  repair?: boolean
}

export function PortableModelSetup({ setup, onStarted }: { setup: ModelSetup; onStarted: () => void }) {
  const { t, locale } = useInterfaceLanguage()
  const [requestPending, setRequestPending] = useState(false)
  const [requestFailed, setRequestFailed] = useState(false)
  const { phase, total, completed = 0, additional, repair } = setup
  const desktop = document.documentElement.hasAttribute("data-jaro-desktop-client")
  const percent = total ? Math.floor(completed / total * 100) : 0
  const size = total ? new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(total / 1_000_000_000) : ""
  const action = phase === "needed" ? "Download models"
    : phase === "download-error" ? (repair ? "Resume repair" : "Resume download")
    : phase === "load-error" ? "Repair models" : null
  const progressLabel = repair ? "Checking and repairing local models… {percent}%"
    : percent === 100 ? "Verifying the downloaded models…" : "Downloading local models… {percent}%"

  async function start() {
    setRequestPending(true)
    setRequestFailed(false)
    try {
      const response = await fetch("/api/portable/models", { method: "POST" })
      if (!response.ok) throw new Error()
      onStarted()
    } catch {
      setRequestFailed(true)
    } finally {
      setRequestPending(false)
    }
  }

  const feedback = <>
    {phase === "downloading" && <div className="portable-model-progress">
      <p role="status">{t(progressLabel, { percent: String(percent) })}</p>
      <Progress value={percent} aria-label={t(repair ? "Model repair" : "Model download")} />
    </div>}
    {phase === "starting" && <p role="status">{t("Starting local models…")}</p>}
    {phase === "download-error" && <p role="alert">{t(repair ? "Repair stopped. Check your connection and available disk space, then resume."
      : "The download stopped. Check your connection and available disk space, then resume.")}</p>}
    {phase === "load-error" && <p role="alert">{t("Jaro couldn’t start the installed models. Repair checks the files and replaces damaged copies.")}</p>}
    {phase === "unavailable" && <p role="alert">{t("The model installer is unavailable. Quit Jaro and reopen it to try again.")}</p>}
    {action && !desktop && <p>{t("Open Jaro to set up local models.")}</p>}
    {requestFailed && <p role="alert">{t("Couldn’t start setup. Try again.")}</p>}
  </>
  const button = action && desktop && <Button size={additional ? "sm" : "default"}
    disabled={requestPending} onClick={() => void start()}>{t(action)}</Button>

  if (additional) return <SettingGroup id="additional-local-models" title={t("Local models")}>
    <SettingRow icon={Download} title={t("Speaker labels, word timing, and writing")} label={false}>
      {button}
    </SettingRow>
    {(phase !== "needed" || !desktop || requestFailed) && <div className="update-settings-feedback">{feedback}</div>}
  </SettingGroup>

  return <section className="transcript-panel portable-model-setup" aria-labelledby="portable-model-title">
    <span className="empty-icon" aria-hidden="true"><Download /></span>
    <h1 id="portable-model-title">{t("Set up Jaro’s local models")}</h1>
    <p>{t("Install Rumble2 Flash for transcription, Nemotron for speaker labels, and Qwen for notes and summaries.")}</p>
    {phase === "needed" && size && <p>{t("Download size: {size} GB. After setup, these models work offline.", { size })}</p>}
    {feedback}
    {button}
  </section>
}
