import { ChevronDown, ChevronUp, Search, X } from "lucide-react"
import { useEffect, useRef } from "react"
import { useInterfaceLanguage } from "@/interface-language"
import { Button } from "./ui/button"
import { ActionTooltip } from "./ui/tooltip"

type Props = {
  open: boolean; onOpen: (open: boolean) => void
  query: string; onQuery: (query: string) => void
  count: number; index: number; step: (distance: number) => void
}

const findShortcut = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ F" : "Ctrl F"

export function TranscriptFind({ open, onOpen, query, onQuery, count, index, step }: Props) {
  const { t } = useInterfaceLanguage()
  const input = useRef<HTMLInputElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (open) input.current?.focus({ preventScroll: true })
  }, [open])
  function close() {
    onOpen(false)
    requestAnimationFrame(() => trigger.current?.focus({ preventScroll: true }))
  }
  return open ? <div className="transcript-find" role="search" aria-label={t("Find in transcript")}
    onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close() }
    }}>
    <Search aria-hidden="true" />
    <input ref={input} type="text" dir="auto" spellCheck={false} autoComplete="off"
      placeholder={t("Find in transcript")} aria-label={t("Search transcript")} value={query}
      onChange={event => onQuery(event.target.value)}
      onKeyDown={event => {
        if (event.key === "Enter" && !event.nativeEvent.isComposing) {
          event.preventDefault()
          step(event.shiftKey ? -1 : 1)
        }
      }} />
    {query.trim() && <span role="status" aria-label={count ? `Match ${index + 1} of ${count}` : "No matches"}>
      {count ? `${index + 1}/${count}` : "0"}</span>}
    <ActionTooltip label={t("Previous match")}>
      <Button size="icon-xs" variant="ghost" disabled={count < 2} aria-label={t("Previous match")} onClick={() => step(-1)}><ChevronUp /></Button>
    </ActionTooltip>
    <ActionTooltip label={t("Next match")}>
      <Button size="icon-xs" variant="ghost" disabled={count < 2} aria-label={t("Next match")} onClick={() => step(1)}><ChevronDown /></Button>
    </ActionTooltip>
    <ActionTooltip label={t("Close search")}>
      <Button size="icon-xs" variant="ghost" aria-label={t("Close search")} onClick={close}><X /></Button>
    </ActionTooltip>
  </div> : <ActionTooltip label={t("Search transcript")} shortcut={findShortcut}>
    <Button ref={trigger} size="icon-sm" variant="ghost" className="find-trigger" aria-expanded={false}
      aria-label={t("Search transcript")} onClick={() => onOpen(true)}><Search /></Button>
  </ActionTooltip>
}
