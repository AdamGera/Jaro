import { useInterfaceLanguage } from "@/interface-language"
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { AlertDialog, Dialog, Tooltip } from "radix-ui"
import { ArrowLeft, ChevronDown, FileAudio2, House, Mic, PanelLeft, Search, Settings, Trash2, UsersRound } from "lucide-react"
import type { DesktopBridge, DictationDestination, WindowNavigation } from "@/desktop"
import { readSession, type SavedSession } from "@/audio"
import { useSessionList, type SessionSummary } from "@/session-list"
import { SessionSearch } from "./session-search"
import { MeetingsWorkspace, type MeetingWorkspaceOptions, type PortableMeetingControls } from "./meetings-workspace"
import { PortableCallBar, type PortableCallBarProps } from "./portable-call-controls"
import { revealSetting, SettingsSearch } from "./settings-search"
import { WhatsNewSettings, type HighlightDestination } from "./whats-new-settings"
import { Button } from "./ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs"
import "@/sidebar.css"

const compactWindow = "(max-width: 800px)"
const scrollKeys = new Set(["PageUp", "PageDown", "Home", "End"])
const editingControl = "input, select, textarea, [contenteditable]:not([contenteditable=false]), [role=combobox], [role=listbox], [role=menu], [role=slider]"
const modalOverlay = '[role="dialog"][data-state="open"], [aria-modal="true"], [role="alertdialog"]'
const date = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" })
const settingsPages = [
  { id: "general", label: "General" },
  { id: "dictation", label: "Dictation" },
  { id: "meetings", label: "Meetings" },
  { id: "annotate", label: "Annotate" },
] as const
const highlightSettings = {
  sounds: { page: "dictation", anchor: "#dictation-sound-styles" },
  microphone: { page: "dictation", anchor: "#dictation-microphone" },
  updates: { page: "general", anchor: "#desktop-app-updates" },
} as const
type SettingsPage = (typeof settingsPages)[number]["id"]
type Pane = "settings" | "dictation" | "meetings"

function scrollablePane(target: Element | null, frame: HTMLElement) {
  for (let element = target; element && element !== frame; element = element.parentElement) {
    if (element instanceof HTMLElement && element.clientHeight > 0 && element.scrollHeight > element.clientHeight + 1
      && /^(auto|scroll)$/.test(getComputedStyle(element).overflowY)) return element
  }
}

function scrollWorkspace(event: KeyboardEvent, frame: HTMLElement, pointer: [number, number] | null) {
  if (event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
  const target = event.target
  if (!(target instanceof Element) || (target !== document.body && !frame.contains(target))
    || target.closest(editingControl) || document.querySelector(modalOverlay)) return

  const focusedPane = frame.contains(target) ? scrollablePane(target, frame) : undefined
  const hovered = !focusedPane && pointer ? document.elementFromPoint(...pointer) : null
  const pane = focusedPane ?? (hovered && frame.contains(hovered) ? scrollablePane(hovered, frame) : undefined)
  if (!pane) return
  event.preventDefault()
  if (event.key === "Home") pane.scrollTop = 0
  else if (event.key === "End") pane.scrollTop = pane.scrollHeight
  else pane.scrollTop += (event.key === "PageDown" ? 1 : -1) * Math.round(pane.clientHeight * 0.9)
}

type Props = {
  busy: boolean
  currentId?: string
  onHome: () => void
  onSelect: (session: SavedSession) => void
  onDelete: (id: string) => void
  settings?: Partial<Record<Exclude<SettingsPage, "dictation">, ReactNode>> & { dictation?: (visible: boolean) => ReactNode }
  native?: DesktopBridge
  meetingOptions?: MeetingWorkspaceOptions
  portableMeeting?: PortableMeetingControls
  callBar?: PortableCallBarProps
  dictation?: (destination: DictationDestination, navigationId?: string) => ReactNode
  update?: ReactNode
  openUpdateSettings?: string
  installedVersion?: string
  children: ReactNode
}

export function SessionLibrary({ busy, currentId, onHome, onSelect, onDelete, settings, native, meetingOptions, portableMeeting, callBar, dictation, update, openUpdateSettings, installedVersion, children }: Props) {
  const { t } = useInterfaceLanguage()
  const [compact, setCompact] = useState(() => matchMedia(compactWindow).matches)
  const [pinned, setPinned] = useState(() => {
    try { return localStorage.getItem("jard.sidebar") !== "collapsed" } catch { return true }
  })
  const [overlay, setOverlay] = useState(false)
  const expanded = compact ? overlay : pinned
  const [showSessions, setShowSessions] = useState(true)
  const [searchOpen, setSearchOpen] = useState(false)
  const [pane, setPane] = useState<Pane>()
  const [meetingQuery, setMeetingQuery] = useState("")
  const [settingsPage, setSettingsPage] = useState("general")
  const [windowNavigation, setWindowNavigation] = useState<WindowNavigation>()
  const [dictationRoute, setDictationRoute] = useState<{ pane: DictationDestination; id: string }>()
  const paneTitle = useRef<HTMLHeadingElement>(null)
  const settingsScope = useRef<HTMLDivElement>(null)
  const settingsButton = useRef<HTMLButtonElement>(null)
  const dictationButton = useRef<HTMLButtonElement>(null)
  const meetingsButton = useRef<HTMLButtonElement>(null)
  const [selectionError, setSelectionError] = useState("")
  const [deletionNotice, setDeletionNotice] = useState("")
  const [deleting, setDeleting] = useState<SessionSummary | null>(null)
  const [deletePending, setDeletePending] = useState(false)
  const [deleteError, setDeleteError] = useState("")
  const list = useSessionList(expanded && showSessions, "", currentId ?? "")
  const request = useRef<AbortController | null>(null)
  const searchButton = useRef<HTMLButtonElement>(null)
  const toggleButton = useRef<HTMLButtonElement>(null)
  const overlayToggleButton = useRef<HTMLButtonElement>(null)
  const deleteButton = useRef<HTMLButtonElement>(null)
  const workspaceFrame = useRef<HTMLDivElement>(null)
  const scrollPointer = useRef<[number, number] | null>(null)
  const handledUpdateSettings = useRef<string>(undefined)
  const handledNavigation = useRef<string>(undefined)

  useEffect(() => {
    const media = matchMedia(compactWindow)
    function resize() { setCompact(media.matches); setOverlay(false) }
    media.addEventListener("change", resize)
    return () => media.removeEventListener("change", resize)
  }, [])
  useEffect(() => () => request.current?.abort(), [])
  useEffect(() => {
    if (busy) request.current?.abort()
  }, [busy])

  function toggle() {
    if (compact) { setOverlay(!overlay); return }
    setPinned(!pinned)
    try { localStorage.setItem("jard.sidebar", pinned ? "collapsed" : "expanded") } catch { /* optional preference */ }
  }
  function search() {
    if (busy || deleting) return
    setOverlay(false)
    setSelectionError("")
    setSearchOpen(true)
  }
  useEffect(() => {
    function keydown(event: KeyboardEvent) {
      if (scrollKeys.has(event.key)) {
        if (workspaceFrame.current) scrollWorkspace(event, workspaceFrame.current, scrollPointer.current)
        return
      }
      if (pane || event.defaultPrevented || !(event.metaKey || event.ctrlKey) || event.altKey || event.repeat || event.isComposing) return
      if (event.key.toLowerCase() === "k" && !event.shiftKey) {
        if (busy || deleting || document.querySelector('[role="alertdialog"]')) return
        event.preventDefault()
        setOverlay(false)
        setSelectionError("")
        setSearchOpen(true)
      }
    }
    window.addEventListener("keydown", keydown)
    return () => window.removeEventListener("keydown", keydown)
  }, [busy, deleting, pane])

  const openPane = useCallback((next?: Pane) => {
    request.current?.abort()
    setSelectionError("")
    setOverlay(false)
    setPane(next)
  }, [setSelectionError, setOverlay, setPane])
  function openHighlight(destination: HighlightDestination) {
    if (destination === "meetings") {
      openPane("meetings")
      return
    }
    const { page, anchor } = highlightSettings[destination]
    setSettingsPage(page)
    requestAnimationFrame(() => {
      const scope = settingsScope.current
      const row = scope?.querySelector<HTMLElement>(anchor)
      if (!scope) return
      if (!row) {
        paneTitle.current?.focus({ preventScroll: true })
        return
      }
      if (row.closest("[hidden]")) return
      const control = revealSetting(row, scope)
      if (destination === "sounds" && control.getAttribute("aria-haspopup") === "dialog") control.click()
    })
  }
  useEffect(() => {
    if (!openUpdateSettings || busy || deleting || handledUpdateSettings.current === openUpdateSettings) return
    const frame = requestAnimationFrame(() => {
      handledUpdateSettings.current = openUpdateSettings
      try {
        if (sessionStorage.getItem("jard.updateSettingsRequest") === openUpdateSettings) return
        sessionStorage.setItem("jard.updateSettingsRequest", openUpdateSettings)
      } catch { /* The component still handles the current request once. */ }
      setSearchOpen(false)
      setSettingsPage("general")
      openPane("settings")
    })
    return () => cancelAnimationFrame(frame)
  }, [openUpdateSettings, busy, deleting, openPane])
  useEffect(() => {
    if (!native) return
    let active = true
    let receivedEvent = false
    function receive(event: WindowEventMap["native-sdk:jard-window-navigation"]) {
      receivedEvent = true
      setWindowNavigation(event.detail)
    }
    window.addEventListener("native-sdk:jard-window-navigation", receive)
    void native.request("windowNavigation").then(snapshot => {
      if (active && !receivedEvent && snapshot?.navigation) setWindowNavigation(snapshot.navigation)
    }).catch(() => { /* Older desktop hosts have no window navigation. */ })
    return () => {
      active = false
      window.removeEventListener("native-sdk:jard-window-navigation", receive)
    }
  }, [native])
  useEffect(() => {
    if (!windowNavigation || (busy && windowNavigation.pane !== "live-meeting" && windowNavigation.pane !== "meeting-library") || deleting ||
        handledNavigation.current === windowNavigation.id) return
    const frame = requestAnimationFrame(() => {
      handledNavigation.current = windowNavigation.id
      try {
        if (sessionStorage.getItem("jard.windowNavigation") === windowNavigation.id) return
        sessionStorage.setItem("jard.windowNavigation", windowNavigation.id)
      } catch { /* The current workspace still handles the request once. */ }
      setSearchOpen(false)
      const { id, pane: destination } = windowNavigation
      if (destination === "home" || destination === "live-meeting") {
        openPane()
        if (destination === "live-meeting") requestAnimationFrame(() => {
          document.querySelector<HTMLElement>("[data-live-transcript]")?.focus({ preventScroll: true })
        })
      } else if (destination === "dictation" || destination === "history" || destination === "output") {
        setDictationRoute({ pane: destination, id })
        openPane("dictation")
      } else if (destination === "meeting-library") {
        openPane("meetings")
      } else {
        setSettingsPage(destination === "settings" ? "general" : destination)
        openPane("settings")
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [windowNavigation, busy, deleting, openPane])
  useEffect(() => {
    if (!pane) return
    paneTitle.current?.focus({ preventScroll: true })
    window.scrollTo(0, 0)
    const close = () => setPane(undefined)
    window.addEventListener("popstate", close)
    return () => window.removeEventListener("popstate", close)
  }, [pane])

  function home() {
    if (busy) return
    request.current?.abort()
    setOverlay(false)
    setSelectionError("")
    setPane(undefined)
    onHome()
  }
  async function select(id: string) {
    if (busy) return
    request.current?.abort()
    setSelectionError("")
    const abort = new AbortController()
    request.current = abort
    try {
      const session = await readSession(id, abort.signal)
      if (abort.signal.aborted) return
      setOverlay(false)
      setPane(undefined)
      onSelect(session)
    } catch (error) {
      if (!abort.signal.aborted) setSelectionError(error instanceof Error ? error.message : "Could not open session.")
    }
  }
  function confirmTrash(row: SessionSummary, trigger: HTMLButtonElement) {
    deleteButton.current = trigger
    setDeleteError("")
    setDeleting(row)
  }
  async function remove() {
    if (!deleting || deletePending) return
    setDeletePending(true)
    try {
      const response = await fetch("/api/sessions/" + deleting.id, { method: "DELETE" })
      if (!response.ok && response.status !== 404) throw new Error((await response.json()).detail)
      setSelectionError("")
      setDeletionNotice(response.status === 202 ? t("Meeting archived. Jaro will retry moving it to the Trash.") : "")
      onDelete(deleting.id)
      deleteButton.current = null
      setDeleting(null)
      if (deleting.meeting) window.dispatchEvent(new Event("jard-meeting-retention"))
      else list.refresh()
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : "Could not delete session.")
    } finally { setDeletePending(false) }
  }

  function tools(inOverlay = false) {
    const sidebarLabel = expanded ? "Hide sidebar" : "Show sidebar"
    return <div className="sidebar-tools">
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <button ref={inOverlay ? overlayToggleButton : toggleButton} type="button" onClick={toggle}
            aria-expanded={expanded} aria-controls="workspace-sidebar" aria-label={sidebarLabel}>
            <PanelLeft aria-hidden="true" />
          </button>
        </Tooltip.Trigger>
        <Tooltip.Portal><Tooltip.Content side="bottom" sideOffset={6} className="sidebar-tip">
          {sidebarLabel}
        </Tooltip.Content></Tooltip.Portal>
      </Tooltip.Root>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <button ref={inOverlay ? undefined : searchButton} type="button" className="sidebar-search-trigger"
            aria-label={t("Search sessions")} aria-keyshortcuts="Meta+K Control+K" disabled={busy} onClick={search}>
            <Search aria-hidden="true" />
          </button>
        </Tooltip.Trigger>
        <Tooltip.Portal><Tooltip.Content side="bottom" sideOffset={6} className="sidebar-tip">
          {t("Search")} <kbd>{/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ K" : "Ctrl K"}</kbd>
        </Tooltip.Content></Tooltip.Portal>
      </Tooltip.Root>
    </div>
  }
  const navigation = <nav id="workspace-sidebar" aria-label="Workspace" className="sidebar-navigation">
    <button className="sidebar-home" disabled={busy} onClick={home} aria-current={!pane && !currentId ? "page" : undefined}>
      <House aria-hidden="true" />{t("Home")}
    </button>
    {dictation && <button type="button" ref={dictationButton} className="sidebar-home" disabled={busy}
      aria-current={pane === "dictation" ? "page" : undefined} onClick={() => {
        if (pane !== "dictation") setDictationRoute(undefined)
        openPane("dictation")
      }}>
      <Mic aria-hidden="true" />{t("Dictation")}
    </button>}
    {(meetingOptions || portableMeeting) && <button type="button" ref={meetingsButton} className="sidebar-home"
      aria-current={pane === "meetings" ? "page" : undefined} onClick={() => openPane("meetings")}>
      <UsersRound aria-hidden="true" />{t("Meetings")}
    </button>}
    <button className="sidebar-section" aria-expanded={showSessions} aria-controls="sidebar-sessions"
      onClick={() => setShowSessions(!showSessions)}>
      {t("Sessions")}<ChevronDown aria-hidden="true" />
    </button>
    <div className="sidebar-list" id="sidebar-sessions" hidden={!showSessions}>
      {list.error ? <button className="sidebar-retry" onClick={list.refresh}>Couldn’t load sessions. Retry</button>
        : !list.rows.length && <p>{list.loading ? t("Loading sessions…") : t("No saved sessions yet.")}</p>}
      {list.rows.map(row => <div key={row.id} className="sidebar-row" data-current={!pane && row.id === currentId}>
        <button className="library-open" disabled={busy || row.status === "processing"}
          aria-current={!pane && row.id === currentId ? "page" : undefined} title={row.name + " · " + date.format(new Date(row.created))}
          onClick={() => void select(row.id)}>
          {row.meeting ? <UsersRound aria-label="Meeting" /> : <FileAudio2 aria-hidden="true" />}<span dir="auto">{row.name}</span>
          {row.status !== "complete" && <small>{row.status === "processing" ? t("Processing") : t("Interrupted")}</small>}
        </button>
        <button className="sidebar-delete" aria-label={"Delete " + row.name} title="Delete session"
          disabled={busy || row.status === "processing" || row.finalizationPending}
          onClick={event => confirmTrash(row, event.currentTarget)}>
          <Trash2 aria-hidden="true" />
        </button>
      </div>)}
    </div>
    {(settings || update) && <div className="sidebar-footer">
      {settings && <button type="button" ref={settingsButton} className="sidebar-home sidebar-settings" disabled={busy}
        aria-current={pane === "settings" ? "page" : undefined} onClick={() => openPane("settings")}>
        <Settings aria-hidden="true" />{t("Settings")}
      </button>}
      {update}
    </div>}
  </nav>

  const visibleSettings = settingsPages.filter(({ id }) => settings?.[id])
  return <Tooltip.Provider delayDuration={150} skipDelayDuration={300}><div ref={workspaceFrame} className="workspace-frame"
    data-sidebar={expanded && !compact} onPointerMove={event => {
      if (event.pointerType !== "touch") scrollPointer.current = [event.clientX, event.clientY]
    }} onPointerLeave={() => { scrollPointer.current = null }}>
    {!compact && <aside className="desktop-sidebar" inert={!expanded} aria-hidden={!expanded}>{navigation}</aside>}
    <div className="main-navigation-tools">{tools()}</div>
    <Dialog.Root open={compact && overlay} onOpenChange={setOverlay}>
      <Dialog.Portal>
        <Dialog.Overlay className="sidebar-backdrop" />
        <Dialog.Content className="compact-sidebar" aria-describedby={undefined}
          onEscapeKeyDown={event => {
            if (!deleting) return
            event.preventDefault()
            if (!deletePending) setDeleting(null)
          }}
          onInteractOutside={event => { if (deleting) event.preventDefault() }}
          onCloseAutoFocus={event => { event.preventDefault(); if (pane && !searchOpen) paneTitle.current?.focus(); else if (!searchOpen) toggleButton.current?.focus() }}>
          <Dialog.Title className="sr-only">Workspace sidebar</Dialog.Title>
          {tools(true)}{navigation}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
    <div className="workspace-content" hidden={!!pane}>{children}</div>
    {callBar && <PortableCallBar {...callBar} onView={call => { openPane(undefined); callBar.onView(call) }} />}
    {pane && <main className="workspace-settings" data-pane={pane}>
      <button type="button" className="settings-back" onClick={() => {
        setPane(undefined)
        const button = pane === "dictation" ? dictationButton : pane === "meetings" ? meetingsButton : settingsButton
        const target = expanded && !compact ? button.current : toggleButton.current
        target?.focus()
      }}><ArrowLeft aria-hidden="true" />{t("Back")}</button>
      <div className="settings-pane-title">
        <h1 tabIndex={-1} ref={paneTitle}>{pane === "dictation" ? t("Dictation") : pane === "meetings" ? t("Meetings") : t("Settings")}</h1>
        {pane === "settings" && <SettingsSearch scope={settingsScope} onReveal={row => {
          const page = row.closest<HTMLElement>("[data-settings-page]")?.dataset.settingsPage
          if (page) setSettingsPage(page)
        }} />}
      </div>
      {pane === "dictation" && dictation?.(dictationRoute?.pane ?? "dictation", dictationRoute?.id)}
      {pane === "meetings" && (meetingOptions || portableMeeting) &&
        <MeetingsWorkspace native={native} options={meetingOptions} portable={portableMeeting}
          onShowLive={() => { portableMeeting?.showLive(); openPane(undefined) }}
        busy={busy} currentId={currentId} query={meetingQuery} onQuery={setMeetingQuery} onOpen={id => void select(id)}
        onTrash={confirmTrash} onSettings={() => { setSettingsPage("meetings"); openPane("settings") }} />}
      {pane === "settings" && settings && <Tabs value={settingsPage} onValueChange={setSettingsPage} className="workspace-settings-tabs">
        <TabsList variant="line" aria-label={t("Settings")}>
          {visibleSettings.map(({ id, label }) => <TabsTrigger key={id} value={id}>{t(label)}</TabsTrigger>)}
          {native && <TabsTrigger value="whats-new">{t("What's new")}</TabsTrigger>}
        </TabsList>
        <div ref={settingsScope}>
          {visibleSettings.map(({ id, label }) => <TabsContent key={id} value={id} forceMount hidden={settingsPage !== id}
            data-settings-page={id} data-setting-group={id === "general" ? t(label) : undefined}>
            {id === "dictation" ? settings.dictation?.(settingsPage === id) : settings[id]}
          </TabsContent>)}
          <TabsContent value="whats-new">
            <WhatsNewSettings version={installedVersion} onOpen={openHighlight} />
          </TabsContent>
        </div>
      </Tabs>}
    </main>}
    {selectionError && <div className="navigation-error" role="alert">{selectionError}
      <button onClick={() => setSelectionError("")}>Dismiss</button></div>}
    {!selectionError && deletionNotice && <div className="navigation-error" role="status">{deletionNotice}
      <button onClick={() => setDeletionNotice("")}>{t("Dismiss")}</button></div>}
    <SessionSearch open={searchOpen && !busy} onOpenChange={open => {
      setSearchOpen(open)
      if (!open) requestAnimationFrame(() => searchButton.current?.focus())
    }} onSelect={id => void select(id)} onNew={home} />
    <AlertDialog.Root open={!!deleting} onOpenChange={open => { if (!open && !deletePending) setDeleting(null) }}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="note-language-scrim" />
        <AlertDialog.Content className="note-language-confirm" onEscapeKeyDown={event => { if (deletePending) event.preventDefault() }}
          onCloseAutoFocus={event => {
            event.preventDefault()
            const trigger = deleteButton.current
            const next = trigger?.isConnected ? trigger : overlayToggleButton.current ?? toggleButton.current
            next?.focus()
          }}>
          <AlertDialog.Title>{deleting?.meeting ? t("Move “{name}” to Trash?", { name: deleting.name }) : `Delete “${deleting?.name}”?`}</AlertDialog.Title>
          <AlertDialog.Description>{deleting?.meeting
            ? t("Jaro moves the meeting and any unchanged automatic Markdown exports to the Trash. Edited exports and manually saved copies stay where they are.")
            : "This permanently removes the recording, transcript, overviews, speaker data, and chat."}</AlertDialog.Description>
          {deleteError && <p role="alert">{deleteError}</p>}
          <div className="note-language-actions">
            <AlertDialog.Cancel asChild><Button variant="ghost" size="sm" disabled={deletePending}>{t("Cancel")}</Button></AlertDialog.Cancel>
            <Button variant="destructive" size="sm" disabled={deletePending} onClick={() => void remove()}>
              {deleting?.meeting ? t("Move to Trash") : "Delete permanently"}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </div></Tooltip.Provider>
}
