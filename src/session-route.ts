import { desktopBridge } from "./desktop"
import type { SavedSession } from "./audio"

const sessionId = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
const tabs = new Set(["overview", "timeline", "transcript", "notes", "analytics", "annotation"])

export function sessionTab(session: SavedSession, wanted: string) {
  if (session.status !== "complete") return session.annotation && wanted !== "transcript" ? "overview" : "transcript"
  return wanted === "annotation" ? "overview" : wanted
}

const desktopLocation = "jard.lastSession"

// Restore the last document for Finder launches. An explicit URL always wins, and
// regular browser tabs continue to use their own URL and history independently.
if (desktopBridge()) {
  const url = new URL(window.location.href)
  const changed = url.searchParams.has("jard-launch")
  url.searchParams.delete("jard-launch")
  try {
    if (!url.searchParams.has("session")) {
      const saved = JSON.parse(localStorage.getItem(desktopLocation) ?? "null") as { id: string; tab: string } | null
      if (saved && sessionId.test(saved.id) && tabs.has(saved.tab)) {
        url.searchParams.set("session", saved.id)
        url.searchParams.set("tab", saved.tab)
      }
    }
  } catch { /* An old or unavailable preference opens the recorder. */ }
  if (changed || url.href !== window.location.href) history.replaceState(null, "", url)
}

export function readSessionRoute() {
  const params = new URLSearchParams(window.location.search)
  const id = params.get("session")
  const tab = params.get("tab") ?? "overview"
  const raw = params.get("at")?.trim()
  const offset = raw ? Number(raw) : NaN
  const at = tab === "transcript" && Number.isFinite(offset) && offset >= 0 && offset <= 10801 ? offset : undefined
  return { id: id?.toLowerCase(), invalid: id !== null && !sessionId.test(id), tab: tabs.has(tab) ? tab : "overview", at }
}

export function writeSessionRoute(id: string | null, tab = "overview", replace = false, at?: number) {
  const url = new URL(window.location.href)
  if (id) { url.searchParams.set("session", id); url.searchParams.set("tab", tab) }
  else { url.searchParams.delete("session"); url.searchParams.delete("tab") }
  if (id && tab === "transcript" && at !== undefined) url.searchParams.set("at", String(at))
  else url.searchParams.delete("at")
  if (desktopBridge()) {
    try {
      if (id) localStorage.setItem(desktopLocation, JSON.stringify({ id, tab }))
      else localStorage.removeItem(desktopLocation)
    } catch { /* Session URLs remain usable without preferences. */ }
  }
  if (url.href === window.location.href) return
  if (replace) history.replaceState(null, "", url)
  else history.pushState(null, "", url)
}
