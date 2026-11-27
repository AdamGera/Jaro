import { useInterfaceLanguage } from "@/interface-language"
import { useState } from "react"
import { AlertDialog } from "radix-ui"
import { Power } from "lucide-react"
import { desktopBridge } from "@/desktop"
import { Button } from "./ui/button"
import { ActionTooltip } from "./ui/tooltip"

export function QuitButton({ recording, recoverable = false, onBeforeQuit, onQuit }: {
  recording: boolean; recoverable?: boolean; onBeforeQuit?: () => Promise<unknown>; onQuit: () => void
}) {
  const { t } = useInterfaceLanguage()
  const native = desktopBridge()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  async function quit() {
    setBusy(true)
    setError("")
    try {
      await onBeforeQuit?.().catch(() => {})
      if (native) await native.request("stop")
      const response = await fetch("/api/quit", { method: "POST", headers: { "X-Jard-Action": "quit" } })
      if (response.status === 405 || response.status === 404) {
        // An already-running older server can serve the new UI before its next restart.
        if (!import.meta.env.JARD_WELCOME_PAGE) throw new Error("Quit the older server using its Quit command in Finder.")
        window.location.href = "jaro://quit?page=" + encodeURIComponent(import.meta.env.JARD_WELCOME_PAGE)
        for (let attempt = 0; attempt < 12; attempt++) {
          await new Promise(resolve => setTimeout(resolve, 500))
          try {
            await fetch("/api/status", { cache: "no-store", signal: AbortSignal.timeout(1000) })
          } catch (reason) {
            if (reason instanceof TypeError) { onQuit(); return }
          }
        }
        throw new Error("Jaro is still running. Allow the browser to open Jaro Launcher, then try again.")
      }
      if (response.status === 409) throw new Error(t("Finish the current recording or local work before quitting Jaro."))
      if (!response.ok) throw new Error("Jaro could not quit. Try again.")
      onQuit()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not reach Jaro.")
      setBusy(false)
    }
  }

  return <AlertDialog.Root onOpenChange={() => setError("")}>
    <ActionTooltip label={t("Quit Jaro Everywhere")}>
      <AlertDialog.Trigger asChild>
        <Button size="icon-sm" variant="ghost" aria-label="Quit Jaro Everywhere"><Power /></Button>
      </AlertDialog.Trigger>
    </ActionTooltip>
    <AlertDialog.Portal>
      <AlertDialog.Overlay className="note-language-scrim" />
      <AlertDialog.Content className="note-language-confirm" onEscapeKeyDown={event => { if (busy) event.preventDefault() }}>
        <AlertDialog.Title>Quit Jaro Everywhere?</AlertDialog.Title>
        <AlertDialog.Description>
          {recording && (native ? "The Mac app will save your recording first. "
            : recoverable ? t("Finish your recording before quitting. ") : "Quitting will discard this browser recording. ")}
          {recoverable ? "This stops Jaro’s local engine." :
            `This stops the shared engine and active jobs for all clients. To leave other clients working, close this ${native ? t("window") : "tab"}.`}
        </AlertDialog.Description>
        {error && <p role="alert">{error}</p>}
        <div className="note-language-actions">
          <AlertDialog.Cancel asChild><Button size="sm" variant="outline" disabled={busy}>{t("Cancel")}</Button></AlertDialog.Cancel>
          <Button size="sm" disabled={busy} onClick={() => void quit()}>{busy ? "Quitting…" : "Quit Everywhere"}</Button>
        </div>
      </AlertDialog.Content>
    </AlertDialog.Portal>
  </AlertDialog.Root>
}
