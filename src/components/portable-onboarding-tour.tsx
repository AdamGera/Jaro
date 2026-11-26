import { useEffect, useRef, useState } from "react"
import { ArrowLeft, ArrowRight, Check, Clipboard, Keyboard, Mic } from "lucide-react"
import { Dialog } from "radix-ui"
import { useInterfaceLanguage } from "@/interface-language"
import { changePortableOnboarding } from "@/portable-onboarding"
import { portableLogin, PortableLaunchAtLogin } from "./launch-at-login"
import { usePortableDictation } from "./portable-dictation-workspace"
import { PortableModelSetup, type ModelSetup } from "./portable-model-setup"
import { Button } from "./ui/button"
import "./system-dictation.css"

const steps = ["welcome", "access", "practice", "ready"] as const
const shortcutInstructions = {
  hold: "Hold the shortcut while you speak, then release it.",
  tap: "Press the shortcut to start and again to finish.",
  holdDouble: "Hold the shortcut to speak, or double-tap it to start hands-free.",
  doubleTap: "Double-tap the shortcut to start, then press it once to finish.",
}

export function PortableOnboardingTour({ id, modelReady, modelFailed, modelSetup, onModelStarted, onFinished }: {
  id: string; modelReady: boolean; modelFailed: boolean; modelSetup?: ModelSetup; onModelStarted: () => void; onFinished: () => void
}) {
  const { t } = useInterfaceLanguage()
  const { status, pending, feedback, configure } = usePortableDictation(true)
  const [step, setStep] = useState<(typeof steps)[number]>("welcome")
  const [text, setText] = useState("")
  const [pasted, setPasted] = useState(false)
  const [skippedPasteMessage, setSkippedPasteMessage] = useState<string | null>(null)
  const [changing, setChanging] = useState(false)
  const [failure, setFailure] = useState("")
  const title = useRef<HTMLHeadingElement>(null)
  const field = useRef<HTMLTextAreaElement>(null)
  const wayland = status?.platform === "wayland"
  const windows = status?.platform === "windows"
  const shortcut = status?.shortcut || status?.portalRawShortcut || ""
  const hasShortcut = Boolean(status?.enabled && (shortcut || status.commandMode))
  const manualPaste = wayland && !status?.keyboardAllowed
  const ready = modelReady && hasShortcut && !status?.microphoneDenied && (!manualPaste || skippedPasteMessage !== null)
  const recordingBusy = changing || pending || Boolean(status?.pendingJobs)
    || (status != null && status.phase !== "idle")
  const busy = recordingBusy || Boolean(status?.authorizing || status?.pasteAuthorizing)
  const scene = !ready && (step === "practice" || step === "ready") ? "access" : step
  const current = steps.indexOf(scene)
  const heading = scene === "welcome" ? "Speak where you write." : scene === "practice" ? "Try the shortcut"
    : scene === "ready" ? "Dictation is ready" : "Set up dictation"
  const wordsLanded = Boolean(text.trim() && pasted)
  const shortcutPrompt = status?.commandMode ? shortcutInstructions.tap : shortcutInstructions[status?.shortcutMode || "tap"]
  const practiceMessage = status?.phase === "starting" ? "Starting microphone…" :
    status?.phase === "recording" ? "Listening…" : status?.phase === "transcribing" ? "Transcribing dictation…" :
    status?.phase === "delivering" ? "Delivering dictation…" : wordsLanded ? "Your words landed." :
    (status?.message !== skippedPasteMessage && status?.message) || "Wait for Listening, then speak."

  useEffect(() => {
    const frame = requestAnimationFrame(() => (scene === "practice" ? field.current : title.current)?.focus())
    return () => cancelAnimationFrame(frame)
  }, [scene])

  function go(next: (typeof steps)[number]) {
    setFailure("")
    if (next === "welcome" || next === "practice") {
      setText("")
      setPasted(false)
    }
    setStep(next)
  }

  async function change(action: "finish" | "microphone") {
    setChanging(true)
    setFailure("")
    try {
      await changePortableOnboarding(action, id)
      if (action === "finish") onFinished()
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Could not change dictation setup.")
    } finally {
      setChanging(false)
    }
  }

  async function copyCommand() {
    try {
      await navigator.clipboard.writeText(`${status?.commandPrefix} toggle`)
    } catch {
      setFailure("Could not copy the shortcut command.")
    }
  }

  async function copyPractice() {
    if (!status?.text) return
    try {
      await navigator.clipboard.writeText(status.text)
      setFailure("")
      field.current?.focus()
    } catch {
      setFailure("Could not copy the dictation.")
    }
  }

  return <Dialog.Root open modal>
    <Dialog.Portal>
      <Dialog.Overlay className="dictation-tour-overlay" />
      <Dialog.Content className="dictation-tour" onEscapeKeyDown={event => event.preventDefault()}
        onPointerDownOutside={event => event.preventDefault()}>
        <div className="dictation-tour-top">
          <div className="dictation-tour-mark"><img src="/jard-icon.png" width="26" height="26" alt="" />{t("Jaro Dictation")}</div>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => void change("finish")}>{t("Set up later")}</Button>
        </div>
        <div key={scene} className="dictation-tour-scene" data-step={scene}>
          <Dialog.Title ref={title} tabIndex={-1}>{t(heading)}</Dialog.Title>
          {scene === "welcome" && <>
            <Dialog.Description>{t("Press a shortcut, speak, and watch your words land at the cursor. Your voice stays on this computer.")}</Dialog.Description>
            <div className="dictation-demo" aria-hidden="true">
              <div className="dictation-demo-note"><div><span/><span/><span/></div>
                <strong>{t("Project notes")}</strong><p>{t("Let’s review the first draft together tomorrow.")}<i /></p>
              </div>
              <div className="dictation-demo-tools"><kbd>{shortcut || t("Dictation shortcut")}</kbd><ArrowRight size={16} /><Mic size={16} /></div>
            </div>
          </>}
          {scene === "access" && <>
            <Dialog.Description>{t("Prepare the local speech model and let Jaro type in the active field.")}</Dialog.Description>
            {ready && <p className="dictation-access-ready" role="status"><Check size={17} />{t("Your shortcut is ready to try.")}</p>}
            {modelReady && !status && <p className="dictation-tour-hint" role="status">{t("Checking desktop shortcuts…")}</p>}
            {!modelReady && <div className="portable-tour-checks">
              <strong>Rumble2 Flash</strong>
              {modelSetup && !modelSetup.additional ? <PortableModelSetup setup={modelSetup} inTour onStarted={onModelStarted} />
                : <p role={modelFailed ? "alert" : "status"}>{t(modelFailed ? "The local speech model could not start." : "Checking local models…")}</p>}
            </div>}
            {modelReady && status?.microphoneDenied && <div className="dictation-permission portable-tour-checks">
              <span><Mic size={17} />{t("Allow desktop apps to access Microphone in Windows Settings.")}</span>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void change("microphone")}>{t("Open microphone settings")}</Button>
            </div>}
            {modelReady && status && !status.microphoneDenied && !hasShortcut && <div className="portable-tour-checks">
              <div className="dictation-permission">
                <span><Keyboard size={17} />{t(status.authorizing ? "Waiting for desktop shortcut approval…" : status.configurable
                  ? "Choose a key for Jaro dictation in your desktop’s shortcut settings." : "Enable a shortcut to dictate in other apps.")}</span>
                {wayland && status.authorizing
                  ? <Button size="sm" variant="outline" disabled={recordingBusy} onClick={() => void configure("/cancel-approval")}>{t("Cancel approval")}</Button>
                  : <Button size="sm" variant="outline" disabled={busy} onClick={() => void configure(status.configurable ? "/configure" : "/enable")}>
                    {t(status.configurable ? "Configure shortcuts" : "Enable shortcut")}</Button>}
              </div>
              {wayland && !status.configurable && !status.annotationShortcut && status.gnomeShortcutAvailable && <Button size="sm" variant="outline" disabled={busy}
                onClick={() => void configure("/add-gnome-shortcuts")}>{t("Add GNOME shortcuts")}</Button>}
              {wayland && !status.configurable && !status.annotationShortcut && status.commandPrefix && <Button size="sm" variant="ghost" disabled={busy}
                onClick={() => void configure("/enable-commands")}>{t("Use desktop shortcut commands")}</Button>}
            </div>}
            {modelReady && hasShortcut && manualPaste && <div className="portable-tour-checks">
              <div className="dictation-permission">
                <span><Clipboard size={17} />{t(status?.pasteAuthorizing ? "Approve keyboard and clipboard access in the desktop dialog."
                  : "Allow keyboard and clipboard access to paste your words.")}</span>
                <Button size="sm" variant="outline" disabled={status?.pasteAuthorizing ? recordingBusy : busy}
                  onClick={() => void configure(status?.pasteAuthorizing ? "/cancel-approval" : "/allow-paste")}>
                  {t(status?.pasteAuthorizing ? "Cancel approval" : "Allow paste")}</Button>
              </div>
              {skippedPasteMessage === null && <Button size="sm" variant="ghost" disabled={busy} onClick={() => {
                setSkippedPasteMessage(status?.message || "")
                go("practice")
              }}>{t("Continue without automatic paste")}</Button>}
            </div>}
            {!ready && status?.message && !feedback && <p className="dictation-tour-hint" role="status">{t(status.message)}</p>}
            {status?.commandMode && !status.gnomeShortcuts && <div className="portable-tour-checks">
              <p>{t("Assign this command to a key in your desktop’s shortcut settings.")}</p>
              <Button size="sm" variant="outline" onClick={() => void copyCommand()}>{t("Copy shortcut command")}</Button>
            </div>}
          </>}
          {scene === "practice" && <>
            <Dialog.Description>{shortcut && <><kbd>{shortcut}</kbd>{" "}</>}{t(shortcutPrompt)}</Dialog.Description>
            <div className="dictation-practice">
              <label htmlFor="portable-dictation-practice">{t("Try saying “Here’s what I want to work on today.”")}</label>
              <textarea ref={field} id="portable-dictation-practice" dir="auto" rows={3} value={text}
                onChange={event => setText(event.target.value)} onPaste={() => setPasted(true)}
                placeholder={t("Your words will appear here…")} />
              <div className="dictation-practice-state" role="status">
                {wordsLanded && <Check size={15} />}
                <span>{t(practiceMessage)}</span>
                {text && <Button size="sm" variant="ghost" disabled={busy} onClick={() => { setText(""); setPasted(false); field.current?.focus() }}>{t("Try again")}</Button>}
              </div>
            </div>
            {manualPaste && <div className="dictation-permission portable-tour-checks">
              <span>{t("After dictating, copy your words and paste them into the practice field.")}</span>
              <Button size="sm" variant="outline" disabled={busy || !status?.text}
                onClick={() => void copyPractice()}>{t("Copy last dictation")}</Button>
            </div>}
            {windows && status?.message && !wordsLanded && !busy && <Button size="sm" variant="ghost"
              onClick={() => void change("microphone")}>{t("Open microphone settings")}</Button>}
          </>}
          {scene === "ready" && <>
            <Dialog.Description>{t(manualPaste ? "Use Copy last in Dictation to copy your words. You can allow automatic paste later."
              : windows ? "Jaro stays available from the system tray." : "Jaro can stay running in the background so your shortcut is ready.")}</Dialog.Description>
            <div className="dictation-ready-mark"><Check size={36} strokeWidth={1.6} /></div>
            <div className="dictation-ready-key">{shortcut && <kbd>{shortcut}</kbd>}<span>{t(shortcutPrompt)}</span></div>
            {portableLogin && <PortableLaunchAtLogin />}
          </>}
          {(failure || feedback) && <p role="alert" className="dictation-error">{t(failure || feedback)}</p>}
        </div>
        <div className="dictation-tour-footer">
          <Button size="sm" variant="ghost" disabled={current === 0 || busy} onClick={() => go(steps[current - 1])}><ArrowLeft />{t("Back")}</Button>
          <div className="dictation-progress" aria-label={t("Step {step} of {total}", { step: current + 1, total: steps.length })}>
            {steps.map((item, index) => <span key={item} data-active={index <= current} />)}
          </div>
          <Button disabled={busy || (scene === "access" && !ready)} onClick={() => {
            if (scene === "ready") void change("finish")
            else if (scene === "welcome") go(ready ? "practice" : "access")
            else if (scene === "access") go(step === "ready" ? "ready" : "practice")
            else go("ready")
          }}>
            {t(scene === "welcome" ? "Get started" : scene === "ready" ? "Start using Jaro" : scene === "practice" && !wordsLanded ? "Skip practice" : "Continue")}<ArrowRight />
          </Button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
