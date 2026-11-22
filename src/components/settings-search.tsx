import { useInterfaceLanguage } from "@/interface-language"
import { useEffect, useEffectEvent, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react"
import { Search } from "lucide-react"
import { Dialog } from "radix-ui"
import { Button } from "./ui/button"
import { pagedSearchIndex } from "./search-list"
import "@/session-search.css"

type Setting = { row: HTMLElement; title: string; description: string; path: string }
type Props = { scope: RefObject<HTMLElement | null>; localFind?: boolean; onReveal?: (row: HTMLElement) => void }

function controlFor(row: HTMLElement) {
  const control = row.querySelector<HTMLElement>("[data-setting-control]") ??
    row.querySelector<HTMLElement>("input:not([type=hidden]), select, textarea, button, summary")
  return control && !control.matches(":disabled, [aria-disabled=true]") ? control : null
}

export function revealSetting(row: HTMLElement, scope: HTMLElement, control = controlFor(row) ?? row) {
  for (let node = row.parentElement; node && scope.contains(node); node = node.parentElement) {
    if (node instanceof HTMLDetailsElement) node.open = true
  }
  control.scrollIntoView({ block: "center" })
  control.focus({ preventScroll: true })
  return control
}

function indexSettings(scope: HTMLElement): Setting[] {
  return Array.from(scope.querySelectorAll<HTMLElement>("[data-setting-title]")).flatMap(row => {
    if (!controlFor(row)) return []
    const title = row.dataset.settingTitle ?? ""
    const description = row.dataset.settingDescription ?? ""
    const groups: string[] = []
    for (let node = row.parentElement; node && scope.contains(node); node = node.parentElement) {
      if (node.dataset.settingGroup) groups.unshift(node.dataset.settingGroup)
    }
    return [{ row, title, description, path: groups.join(" › ") }]
  })
}

function findSettings(entries: Setting[], query: string) {
  const needle = query.trim().toLowerCase()
  if (!needle) return []
  return entries.flatMap(entry => {
    const title = entry.title.toLowerCase()
    if (title === needle) return [{ entry, rank: 0 }]
    if (title.startsWith(needle)) return [{ entry, rank: 1 }]
    if (title.includes(needle)) return [{ entry, rank: 2 }]
    if (entry.description.toLowerCase().includes(needle) || entry.path.toLowerCase().includes(needle)) return [{ entry, rank: 3 }]
    return []
  }).sort((a, b) => a.rank - b.rank).map(hit => hit.entry)
}

export function SettingsSearch({ scope, localFind = true, onReveal }: Props) {
  const { t } = useInterfaceLanguage()
  const [entries, setEntries] = useState<Setting[] | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const destination = useRef<Setting | null>(null)

  function open() {
    if (!scope.current || scope.current.querySelector("[data-settings-search-blocked]")) return false
    destination.current = null
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setEntries(indexSettings(scope.current))
    return true
  }

  const onKeyDown = useEffectEvent((event: KeyboardEvent) => {
    if (event.defaultPrevented || entries || event.repeat || event.isComposing || event.altKey || event.shiftKey ||
        !(event.metaKey || event.ctrlKey)) return
    const key = event.key.toLowerCase()
    if (key !== "k" && (key !== "f" || !localFind)) return
    if (document.activeElement?.closest("input, select, textarea, [contenteditable]:not([contenteditable=false]), [role=combobox]") ||
        document.querySelector('[role="dialog"][data-state="open"], [aria-modal="true"], [role="alertdialog"]')) return
    if (open()) event.preventDefault()
  })
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => onKeyDown(event)
    window.addEventListener("keydown", keydown)
    return () => window.removeEventListener("keydown", keydown)
  }, [])

  function restoreFocus() {
    const target = previousFocus.current
    if (target?.isConnected && target !== document.body) target.focus({ preventScroll: true })
    else trigger.current?.focus({ preventScroll: true })
  }

  return <Dialog.Root open={entries !== null} onOpenChange={next => { if (!next) setEntries(null) }}>
    <Button ref={trigger} type="button" size="sm" variant="outline" aria-haspopup="dialog" aria-expanded={entries !== null}
      aria-keyshortcuts={localFind ? "Meta+K Meta+F Control+K Control+F" : "Meta+K Control+K"} onClick={open}>
      <Search aria-hidden="true" />{t("Find settings")}
    </Button>
    <Dialog.Portal>
      <Dialog.Overlay className="session-search-overlay" />
      {entries && <SettingResults entries={entries} choose={entry => {
        if (scope.current?.contains(entry.row) && controlFor(entry.row)) {
          destination.current = entry
          onReveal?.(entry.row)
        }
        setEntries(null)
      }} onClose={() => {
        const entry = destination.current
        destination.current = null
        if (!entry) { restoreFocus(); return }
        requestAnimationFrame(() => {
          const container = scope.current
          const control = container?.contains(entry.row) ? controlFor(entry.row) : null
          if (!container || !control) { restoreFocus(); return }
          revealSetting(entry.row, container, control)
        })
      }} />}
    </Dialog.Portal>
  </Dialog.Root>
}

function SettingResults({ entries, choose, onClose }: { entries: Setting[]; choose: (entry: Setting) => void; onClose: () => void }) {
  const { t } = useInterfaceLanguage()
  const [query, setQuery] = useState("")
  const [highlight, setHighlight] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const list = useId()
  const querying = Boolean(query.trim())
  const results = findSettings(entries, query)
  const active = results[highlight]

  useEffect(() => {
    document.getElementById(`${list}-${highlight}`)?.scrollIntoView({ block: "nearest" })
  }, [highlight, list])

  function navigate(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing || !active) return
    if (event.key === "Enter") {
      event.preventDefault()
      choose(active)
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault()
      const step = event.key === "ArrowDown" ? 1 : -1
      setHighlight((highlight + step + results.length) % results.length)
    } else {
      const index = pagedSearchIndex(event, scroll.current, highlight)
      if (index !== undefined) setHighlight(index)
    }
  }

  return <Dialog.Content className="session-search-dialog" aria-describedby={undefined}
    onOpenAutoFocus={event => { event.preventDefault(); input.current?.focus() }}
    onCloseAutoFocus={event => { event.preventDefault(); onClose() }}>
    <Dialog.Title className="sr-only">{t("Find settings")}</Dialog.Title>
    <div className="session-search-input">
      <Search aria-hidden="true" />
      <input ref={input} role="combobox" aria-autocomplete="list" aria-expanded={querying}
        aria-label={t("Find settings")} aria-controls={list} aria-activedescendant={active ? `${list}-${highlight}` : undefined}
        placeholder="Find settings…" autoComplete="off" spellCheck={false} value={query}
        onChange={event => {
          if (scroll.current) scroll.current.scrollTop = 0
          setHighlight(0)
          setQuery(event.target.value)
        }} onKeyDown={navigate} />
      <Dialog.Close className="session-search-escape" aria-label="Close settings search">esc</Dialog.Close>
    </div>
    <div ref={scroll} className="session-search-results" hidden={!querying}>
      <div id={list} role="listbox" aria-label="Matching settings">
        {results.map((entry, index) => <div key={`${entry.path}/${entry.title}`} id={`${list}-${index}`} role="option"
          className="session-search-option" aria-selected={highlight === index} onPointerMove={() => setHighlight(index)}
          onMouseDown={event => event.preventDefault()} onClick={() => choose(entry)}>
          <span className="session-search-name">{entry.title}</span><small>{entry.path}</small>
        </div>)}
      </div>
      {querying && !results.length && <p className="session-search-message" role="status">No matching settings.</p>}
    </div>
  </Dialog.Content>
}
