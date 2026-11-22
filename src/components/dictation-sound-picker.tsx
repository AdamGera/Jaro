import { useEffect, useRef, useState } from "react"
import { Check, ChevronRight, Play, Square, Volume2, X } from "lucide-react"
import { Dialog } from "radix-ui"
import { useInterfaceLanguage } from "@/interface-language"
import { dictationSoundStyles, type DictationPreferences, type DictationSoundCue, type SystemDictationControls } from "@/system-dictation"
import { DictationSoundPreview, preloadDictationSoundPreview } from "./dictation-sound-preview"
import { SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const earlierStyles = [
  { id: "soft", title: "Soft", done: false },
  { id: "glass", title: "Glass", done: false },
] as const
const styles = [...dictationSoundStyles, ...earlierStyles] as const
const cues = [
  { id: "start", title: "Start" },
  { id: "stop", title: "Stop" },
  { id: "done", title: "Done" },
  { id: "error", title: "Error" },
] as const

export function DictationSoundPicker({ controls, visible }: { controls: SystemDictationControls; visible: boolean }) {
  const { t } = useInterfaceLanguage()
  const { status, run, pending, error } = controls
  const [open, setOpen] = useState(false)
  const [previewStyle, setPreviewStyle] = useState<DictationPreferences["soundSet"]>()
  const openSession = useRef<object | null>(null)

  useEffect(() => () => { openSession.current = null }, [])
  useEffect(() => {
    if (visible || !openSession.current) return
    openSession.current = null
    setOpen(false)
    void run({ action: "stopSoundPreview" })
  }, [visible, run])

  if (!status) return null
  const selected = styles.find(style => style.id === status.soundSet) ?? styles[0]
  const previewed = styles.find(style => style.id === previewStyle) ?? selected
  const animation = status.soundPreviewAnimation

  function changeOpen(next: boolean) {
    const wasOpen = !!openSession.current
    openSession.current = next ? {} : null
    setOpen(next)
    if (next) {
      setPreviewStyle(undefined)
      preloadDictationSoundPreview()
    } else if (wasOpen) void run({ action: "stopSoundPreview" })
  }

  function preview(style: DictationPreferences["soundSet"], cue: DictationSoundCue) {
    setPreviewStyle(style)
    return run({ action: "previewSound", cue, style })
  }

  async function choose(style: DictationPreferences["soundSet"]) {
    const session = openSession.current
    if (style !== selected.id && !await run({ action: "set", key: "soundSet", value: style })) return
    if (session && openSession.current === session) await preview(style, "sample")
  }

  function styleRow(style: typeof styles[number]) {
    const playing = style.id === animation?.style
    return <li key={style.id} className="dictation-sound-option">
      <button type="button" className="dictation-sound-choice" aria-pressed={style.id === selected.id} disabled={pending}
        onClick={() => void choose(style.id)}>
        <span>{t(style.title)}</span>{style.id === selected.id && <Check aria-hidden="true" />}
      </button>
      <Button type="button" size="icon-sm" variant="ghost" disabled={pending}
        aria-label={playing ? `${t("Stop preview")} · ${t(style.title)}` : `${t("Preview")} · ${t(style.title)}`}
        title={t(playing ? "Stop preview" : "Sample dictation")}
        onClick={() => void (playing ? run({ action: "stopSoundPreview" }) : preview(style.id, "sample"))}>
        {playing ? <Square aria-hidden="true" /> : <Play aria-hidden="true" />}
      </Button>
    </li>
  }

  return <Dialog.Root open={open && visible} onOpenChange={changeOpen}>
    <SettingRow label={false} icon={Volume2} title={t("Sound style")} find="Sound preview" anchor="dictation-sound-styles">
      <Dialog.Trigger asChild>
        <Button type="button" variant="outline" size="sm" aria-label={t("Sound style")}>
          {t(selected.title)}<ChevronRight aria-hidden="true" />
        </Button>
      </Dialog.Trigger>
    </SettingRow>
    <Dialog.Portal>
      <Dialog.Overlay className="dictation-tour-overlay" />
      <Dialog.Content className="dictation-tour dictation-sound-dialog">
        <div className="dictation-sound-heading">
          <Dialog.Title>{t("Sound style")}</Dialog.Title>
          <Dialog.Close asChild>
            <Button type="button" variant="ghost" size="icon-sm" aria-label={t("Close")}><X aria-hidden="true" /></Button>
          </Dialog.Close>
        </div>
        <Dialog.Description>{t("Choose the sounds Jaro plays when you dictate.")}</Dialog.Description>
        <div className="dictation-sound-stage">
          {status.placement === "off" ? <span>{t("Recording indicator hidden")}</span> : animation &&
            <DictationSoundPreview key={animation.id} animation={animation} placement={status.placement} />}
        </div>
        <div className="dictation-sound-cues" role="group" aria-label={t("Sound preview")}>
          <span>{t(previewed.title)}</span>
          {cues.map(cue => <Button key={cue.id} type="button" size="sm" variant="outline"
            disabled={pending || (cue.id === "done" && !previewed.done)}
            onClick={() => void preview(previewed.id, cue.id)}>{t(cue.title)}</Button>)}
        </div>
        {error && <p role="alert" className="dictation-error">{error}</p>}
        <ul className="dictation-sound-options" aria-label={t("Sound style")}>{dictationSoundStyles.map(styleRow)}</ul>
        <ul className="dictation-sound-options" aria-label={t("Earlier styles")}>
          <li className="dictation-sound-earlier">{t("Earlier styles")}</li>
          {earlierStyles.map(styleRow)}
        </ul>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
