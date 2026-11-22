import { useEffect, useState } from "react"
import { Clipboard, Keyboard, Terminal } from "lucide-react"
import { useInterfaceLanguage } from "@/interface-language"
import type { DictationHistory, DictationPreferences } from "@/system-dictation"
import { Button } from "./ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs"
import { SettingGroup, SettingRow } from "./settings-rows"
import { DictationHistoryBrowser, HistoryRetention } from "./system-dictation"

export const hasPortableDictation = document.documentElement.hasAttribute("data-jaro-portable-dictation")

type DictationStatus = {
  enabled: boolean
  platform: "x11" | "wayland" | "windows"
  shortcut: string
  recoveryShortcut?: string
  authorizing: boolean
  configurable: boolean
  keyboardAllowed?: boolean
  pasteAuthorizing?: boolean
  commandPrefix?: string
  commandMode?: boolean
  phase: "idle" | "starting" | "recording" | "transcribing" | "pasting"
  message: string
  text: string
  terminal: boolean
  retention: DictationPreferences["retention"]
  historyRevision: number
  historyAvailable: boolean
}

const endpoint = "/api/portable/dictation"
const desktopCommands = [
  ["toggle", "Toggle dictation"], ["start", "Start dictation"], ["stop", "Finish dictation"],
  ["cancel", "Cancel dictation"], ["paste-last", "Paste last dictation"],
] as const

async function request<T = DictationStatus>(path = "", options?: RequestInit): Promise<T> {
  const response = await fetch(endpoint + path, { cache: "no-store", ...options })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not access system dictation.")
  return result
}

function loadHistory(query: string, offset: number) {
  const params = new URLSearchParams({ query, offset: String(offset) })
  return request<DictationHistory>("/history?" + params)
}

export function PortableDictationWorkspace({ destination }: { destination: "dictation" | "history" | "output" }) {
  const { t } = useInterfaceLanguage()
  const [tab, setTab] = useState(destination === "history" ? "history" : "general")
  const [status, setStatus] = useState<DictationStatus>()
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState("")
  const [copied, setCopied] = useState<{ text: string; message: string }>()

  useEffect(() => {
    const controller = new AbortController()
    let timer: number
    async function refresh() {
      try {
        const value = await request("", { signal: controller.signal })
        if (!controller.signal.aborted) setStatus(value)
      } catch {
        if (!controller.signal.aborted) setFeedback("Could not check system dictation.")
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(refresh, 350)
      }
    }
    void refresh()
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [])

  async function configure(path: string, body?: object) {
    setPending(true)
    setFeedback("")
    try {
      const options: RequestInit = { method: "POST" }
      if (body) {
        options.headers = { "Content-Type": "application/json" }
        options.body = JSON.stringify(body)
      }
      setStatus(await request(path, options))
      return true
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "Could not change system dictation.")
      return false
    } finally {
      setPending(false)
    }
  }

  async function copyHistory(_ids: string[], text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setFeedback("")
      return true
    } catch {
      setFeedback("Could not copy the dictation.")
      return false
    }
  }

  async function copy() {
    if (!status?.text) return
    try {
      await navigator.clipboard.writeText(status.text)
      setCopied({ text: status.text, message: "Copied last dictation." })
    } catch {
      setCopied({ text: status.text, message: "Could not copy the dictation." })
    }
  }

  async function copyCommand(action: (typeof desktopCommands)[number][0]) {
    if (!status?.commandPrefix) return
    try {
      await navigator.clipboard.writeText(`${status.commandPrefix} ${action}`)
      setFeedback("Copied shortcut command.")
    } catch {
      setFeedback("Could not copy the shortcut command.")
    }
  }

  const windows = status?.platform === "windows"
  const activity = status?.phase === "starting" ? "Starting microphone…" :
    status?.phase === "recording" ? "Recording dictation…" :
    status?.phase === "transcribing" ? "Transcribing dictation…" :
    status?.phase === "pasting" ? "Pasting last dictation…" :
    status?.pasteAuthorizing ? "Approve keyboard and clipboard access in the desktop dialog." :
    status?.authorizing ? (windows ? "Starting the keyboard shortcut…" : "Approve the shortcut in the desktop dialog.") : status?.message
  const message = status?.phase === "idle" ? feedback || activity : activity || feedback
  const wayland = status?.platform === "wayland"
  const shortcutConnected = status?.enabled || status?.configurable

  return <Tabs value={tab} onValueChange={setTab} className="dictation-workspace-tabs">
    <div className="dictation-workspace-header">
      <TabsList aria-label={t("Dictation tabs")} variant="line">
        <TabsTrigger value="general">{t("General")}</TabsTrigger>
        <TabsTrigger value="history">{t("History")}</TabsTrigger>
      </TabsList>
      {tab === "history" && status && <div className="dictation-workspace-actions">
        <HistoryRetention value={status.retention} pending={pending || !status.historyAvailable}
          onChange={retention => void configure("/history", { action: "retention", retention })} />
      </div>}
    </div>
    <TabsContent value="general"><div className="dictation-preferences">
    <SettingGroup id="linux-dictation-title" title={t("System dictation")}>
      {!status?.commandMode && <SettingRow icon={Keyboard} title={status?.shortcut || t("Global shortcut")} label={false}
        detail={t(windows ? "Hold Left Alt to dictate. Release to paste into the same field." :
          wayland ? status.keyboardAllowed ? "Finish with the shortcut in the field where you want the text." :
          "Press to start speaking, then press again to finish. Use Copy last to paste." :
          "Press to start speaking, then press again to paste into the same window.")}>
        {status?.configurable && <Button size="sm" variant="ghost" disabled={pending}
          onClick={() => void configure("/configure")}>{t("Configure shortcut")}</Button>}
        <Button size="sm" variant="outline" disabled={!status || pending}
          onClick={() => void configure(shortcutConnected || status?.authorizing ? "/disable" : "/enable")}>
          {t(status?.authorizing ? "Cancel approval" : shortcutConnected ? "Disable shortcut" : "Enable shortcut")}
        </Button>
      </SettingRow>}
      {status?.enabled && status.recoveryShortcut && <SettingRow icon={Keyboard} title={t("Paste last dictation")}
        detail={t("Reinsert the last dictation at the current cursor.")} label={false}>
        <kbd>{status.recoveryShortcut}</kbd>
      </SettingRow>}
      {wayland && status.commandPrefix && (!shortcutConnected || status.commandMode) && <SettingRow icon={Terminal}
        title={t("Desktop shortcut commands")} label={false}
        detail={t(status.commandMode ? "Assign these commands in your desktop’s keyboard settings." :
          "Use your desktop’s keyboard settings to bind a shortcut, even without the shortcut portal.")}>
        <Button size="sm" variant="outline" disabled={pending || status.authorizing || status.phase !== "idle"}
          onClick={() => void configure(status.commandMode ? "/disable" : "/enable-commands")}>
          {t(status.commandMode ? "Disable commands" : "Enable commands")}
        </Button>
      </SettingRow>}
      {wayland && status.enabled && <SettingRow icon={Clipboard} title={t("Automatic paste")} label={false}
        detail={t("Allow keyboard and clipboard access to paste into the active field when you finish with the shortcut.")}>
        <Button size="sm" variant="outline" disabled={pending || status.phase !== "idle"}
          onClick={() => void configure(status.keyboardAllowed || status.pasteAuthorizing ? "/disable-paste" : "/allow-paste")}>
          {t(status.pasteAuthorizing ? "Cancel approval" : status.keyboardAllowed ? "Disable paste" : "Allow paste")}
        </Button>
      </SettingRow>}
      {wayland && <SettingRow icon={Clipboard} title={t("Manual dictation")} label={false}>
        <Button size="sm" variant="outline" disabled={pending || status.authorizing || status.pasteAuthorizing || status.phase === "starting" || status.phase === "transcribing"}
          onClick={() => void configure("/toggle")}>
          {t(status.phase === "recording" ? "Finish dictation" : "Start dictation")}
        </Button>
      </SettingRow>}
      {status?.enabled && (!wayland || status.keyboardAllowed) && <SettingRow icon={Clipboard} title={t("Paste shortcut")}
        detail={t("Select Ctrl+Shift+V for terminals that use it.")}>
        {labels => <select {...labels} disabled={pending} value={status.terminal ? "terminal" : "standard"}
          onChange={event => void configure("/paste-shortcut?terminal=" + (event.target.value === "terminal"))}>
          <option value="standard">Ctrl+V</option>
          <option value="terminal">Ctrl+Shift+V</option>
        </select>}
      </SettingRow>}
      {message && <p role="status" className="update-settings-feedback">{t(message)}</p>}
    </SettingGroup>
    {status?.commandMode && <SettingGroup id="linux-dictation-commands" title={t("Commands to bind")}>
      {desktopCommands.map(([action, label]) => <SettingRow key={action} icon={Keyboard} title={t(label)} label={false}>
        <Button size="sm" variant="outline" aria-label={t("Copy command") + " · " + t(label)}
          onClick={() => void copyCommand(action)}>{t("Copy command")}</Button>
      </SettingRow>)}
    </SettingGroup>}
    {status?.text && <SettingGroup id="linux-dictation-last" title={t("Last dictation")}>
      <SettingRow icon={Clipboard} title={t("Copy to paste into your app")} label={false}>
        <Button size="sm" variant="outline" onClick={() => void copy()}>{t("Copy last")}</Button>
      </SettingRow>
      <p className="update-settings-feedback whitespace-pre-wrap break-words" dir="auto">{status.text}</p>
      {copied && copied.text === status.text && <p role="status" className="update-settings-feedback">{t(copied.message)}</p>}
    </SettingGroup>}
    </div></TabsContent>
    <TabsContent value="history" tabIndex={-1}>
      <section className="dictation-history-tab" aria-label={t("Dictation history")}>
        {status?.retention === 0 && <p className="update-settings-feedback">{t("History is off. New dictations will not be saved.")}</p>}
        <DictationHistoryBrowser loadHistory={loadHistory} copyHistory={copyHistory} pending={pending}
          deleteHistory={ids => configure("/history", ids ? { action: "delete", ids } : { action: "clear" })}
          historyRevision={String(status?.historyRevision)} />
        {feedback && <p className="dictation-error" role="alert">{t(feedback)}</p>}
      </section>
    </TabsContent>
  </Tabs>
}
