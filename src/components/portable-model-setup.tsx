import { useState } from "react"
import { Download } from "lucide-react"
import { useInterfaceLanguage } from "@/interface-language"
import { Button } from "./ui/button"
import { Progress } from "./ui/progress"

export type ModelSetup = {
  phase: "checking" | "needed" | "downloading" | "download-error" | "starting" | "load-error" | "unavailable"
  total?: number
  completed?: number
}

export function PortableModelSetup({ setup }: { setup: ModelSetup }) {
  const { t, locale } = useInterfaceLanguage()
  const [requestPending, setRequestPending] = useState(false)
  const [requestFailed, setRequestFailed] = useState(false)
  const { phase, total, completed = 0 } = setup
  const percent = total ? Math.floor(completed / total * 100) : 0
  const size = total ? new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(total / 1_000_000_000) : ""
  const action = phase === "needed" ? "Download models"
    : phase === "download-error" ? "Resume download"
    : phase === "load-error" ? "Retry startup" : null

  async function start() {
    setRequestPending(true)
    setRequestFailed(false)
    try {
      const response = await fetch("/api/portable/models", { method: "POST" })
      if (!response.ok) throw new Error()
    } catch {
      setRequestFailed(true)
    } finally {
      setRequestPending(false)
    }
  }

  return <section className="transcript-panel portable-model-setup" aria-labelledby="portable-model-title">
    <span className="empty-icon" aria-hidden="true"><Download /></span>
    <h1 id="portable-model-title">{t("Set up Jaro’s local models")}</h1>
    <p>{t("Install Rumble2 Flash for transcription, Nemotron for speaker labels, and Qwen for notes and summaries.")}</p>
    {phase === "needed" && size && <p>{t("Download size: {size} GB. After setup, these models work offline.", { size })}</p>}
    {phase === "downloading" && <div className="portable-model-progress">
      <p role="status">{percent === 100 ? t("Verifying the downloaded models…") : t("Downloading local models… {percent}%", { percent: String(percent) })}</p>
      <Progress value={percent} aria-label={t("Model download")} />
    </div>}
    {phase === "starting" && <p role="status">{t("Starting local models…")}</p>}
    {phase === "download-error" && <p role="alert">{t("The download stopped. Check your connection and available disk space, then resume.")}</p>}
    {phase === "load-error" && <p role="alert">{t("The models are installed, but Jaro couldn’t start them. Try again.")}</p>}
    {phase === "unavailable" && <p role="alert">{t("The model installer is unavailable. Quit Jaro and reopen it to try again.")}</p>}
    {action && <>
      {requestFailed && <p role="alert">{t("Couldn’t start setup. Try again.")}</p>}
      <Button disabled={requestPending} onClick={() => void start()}>{t(action)}</Button>
    </>}
  </section>
}
