import { desktopBridge } from "./desktop"

const portableSnapshot = document.getElementById("jaro-workspace-drafts")
type WorkspaceValue = object | string
const savedValues = new Map<string, WorkspaceValue>(Object.entries(JSON.parse(portableSnapshot?.textContent ?? "{}")))
const pendingValues = new Map<string, WorkspaceValue | null>()
let sending: Promise<void> | undefined

export async function flushWorkspaceDrafts() {
  while (pendingValues.size) {
    if (!sending) {
      const batch = [...pendingValues].slice(0, 64)
      const body = JSON.stringify({ values: Object.fromEntries(batch) })
      sending = fetch("/api/portable/workspace-drafts", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body, keepalive: new TextEncoder().encode(body).length < 60_000,
      }).then(response => {
        if (!response.ok) throw new Error("Jaro could not keep your workspace drafts or settings. Try restarting again.")
        for (const [key, value] of batch) {
          if (pendingValues.get(key) === value) pendingValues.delete(key)
        }
      }).finally(() => { sending = undefined })
    }
    await sending
  }
}

export function isDesktopWorkspace() {
  return !!desktopBridge() || document.documentElement.hasAttribute("data-jaro-desktop-client")
}

function draftStorage() {
  return isDesktopWorkspace() ? localStorage : sessionStorage
}

export function readWorkspaceDraft(key: string) {
  if (portableSnapshot && savedValues.has(key)) return savedValues.get(key)
  try {
    // Recover session-scoped drafts made by older builds in a window still open.
    return JSON.parse(draftStorage().getItem(key) ?? sessionStorage.getItem(key) ?? "null") as unknown
  } catch { return null }
}

function writePortableWorkspaceValue(key: string, value: WorkspaceValue | null) {
  if (!portableSnapshot) return
  if (value === null) savedValues.delete(key)
  else savedValues.set(key, value)
  pendingValues.set(key, value)
  void flushWorkspaceDrafts().catch(() => {})
}

export function readWorkspacePreference(key: "jard.recordingMode" | "jard.sidebar") {
  if (portableSnapshot) {
    const value = savedValues.get(key)
    return typeof value === "string" ? value : null
  }
  try { return localStorage.getItem(key) } catch { return null }
}

export function writeWorkspacePreference(key: "jard.recordingMode" | "jard.sidebar", value: string) {
  writePortableWorkspaceValue(key, value)
  try { localStorage.setItem(key, value) } catch { /* Use this choice for the current window. */ }
}

export function rememberWorkspaceDraft(key: string, value: object | null) {
  writePortableWorkspaceValue(key, value)
  try {
    if (value) draftStorage().setItem(key, JSON.stringify(value))
    else draftStorage().removeItem(key)
    if (isDesktopWorkspace()) sessionStorage.removeItem(key)
    return true
  } catch { return false }
}

export function discardSessionDrafts(id: string) {
  const overview = `jard.overviewDraft.${id}.`
  const notes = `jard.personalNotes.${id}`
  for (const key of savedValues.keys()) {
    if (key === notes || key.startsWith(overview)) rememberWorkspaceDraft(key, null)
  }
  try {
    for (const storage of [draftStorage(), sessionStorage]) {
      storage.removeItem(notes)
      for (const key of Object.keys(storage)) if (key.startsWith(overview)) storage.removeItem(key)
    }
  } catch { /* The saved session is already gone. */ }
}
