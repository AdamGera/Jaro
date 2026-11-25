import { useCallback, useEffect, useState } from "react"
import type { SavedSession } from "./audio"

export type SessionSummary = Pick<SavedSession, "id" | "name" | "created" | "status" | "meeting" | "meetingProvider" |
  "audioRemovedAt" | "finalizationPending" | "meetingRetentionDays" | "meetingsManaged"> & {
  duration: number | null
  annotation?: boolean
  meetingStartedAt?: string | null
  pendingOverview?: number
}

export function useSessionList(open: boolean, query: string, refreshKey: string | number = "", meetingsOnly = false) {
  const [result, setResult] = useState<{ query: string; meetingsOnly: boolean; rows: SessionSummary[]; error: string } | null>(null)
  const [version, setVersion] = useState(0)
  const refresh = useCallback(() => setVersion(value => value + 1), [])

  useEffect(() => {
    if (!open) return
    const abort = new AbortController()
    let timer: number
    async function load() {
      try {
        const response = await fetch("/api/sessions?q=" + encodeURIComponent(query.trim()) + (meetingsOnly ? "&meetings=true" : ""), {
          signal: abort.signal, cache: "no-store",
        })
        if (!response.ok) throw new Error()
        const sessions: SessionSummary[] = await response.json()
        if (abort.signal.aborted) return
        setResult({ query, meetingsOnly, rows: sessions, error: "" })
        if (response.headers.get("X-Jard-Meeting-Recovery") === "pending" ||
            sessions.some(row => !row.meetingsManaged && (row.status === "processing" || row.finalizationPending || row.pendingOverview))) {
          timer = window.setTimeout(load, 1800)
        } else if (sessions.some(row => row.meetingRetentionDays || row.annotation)) {
          timer = window.setTimeout(load, 30_000)
        }
      } catch {
        if (!abort.signal.aborted) {
          setResult({ query, meetingsOnly, rows: [], error: "Could not load sessions." })
        }
      }
    }
    function annotationSaved(event: WindowEventMap["native-sdk:jard-annotation"]) {
      if (!meetingsOnly && event.detail.phase === "finished" && event.detail.session) refresh()
    }
    timer = window.setTimeout(load, query.trim() ? 100 : 0)
    window.addEventListener("focus", refresh)
    window.addEventListener("jaro-sessions-changed", refresh)
    window.addEventListener("jard-meeting-retention", refresh)
    window.addEventListener("jard-annotation-retention", refresh)
    window.addEventListener("jard-meeting-brief", refresh)
    window.addEventListener("jaro-portable-meeting-saved", refresh)
    if (!meetingsOnly) window.addEventListener("jaro-portable-annotation-saved", refresh)
    window.addEventListener("native-sdk:jard-annotation", annotationSaved)
    return () => {
      abort.abort()
      window.clearTimeout(timer)
      window.removeEventListener("focus", refresh)
      window.removeEventListener("jaro-sessions-changed", refresh)
      window.removeEventListener("jard-meeting-retention", refresh)
      window.removeEventListener("jard-annotation-retention", refresh)
      window.removeEventListener("jard-meeting-brief", refresh)
      window.removeEventListener("jaro-portable-meeting-saved", refresh)
      window.removeEventListener("jaro-portable-annotation-saved", refresh)
      window.removeEventListener("native-sdk:jard-annotation", annotationSaved)
    }
  }, [open, query, refreshKey, refresh, version, meetingsOnly])

  const current = result?.query === query && result.meetingsOnly === meetingsOnly ? result : null
  return { rows: current?.rows ?? [], loading: !current, error: current?.error ?? "", refresh }
}
