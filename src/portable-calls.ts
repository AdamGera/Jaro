import { useEffect, useRef, useState } from "react"

type Policy = "ask" | "never" | "auto"
type OfferAction = "record" | "dismiss" | "more" | "back" | "always" | "never"
type Preferences = { enabled: boolean; autoStop: boolean; policies: Record<string, Policy> }
export type DetectedCall = {
  id: string; name: string; automatic: boolean; elapsed: number; saving: boolean;
  sampleRate: number; stopSeconds: number | null; roomChanged?: boolean; languages: string[];
}
type CallStatus = Preferences & {
  providers: { id: string; name: string; browser: boolean }[]
  offer: { token: string; name: string; choosing: boolean; canAlways: boolean } | null
  active: DetectedCall | null
  completed: string | null
  message: string
}

const endpoint = "/api/portable/calls"

async function request(path: string, options?: RequestInit): Promise<CallStatus> {
  const response = await fetch(endpoint + path, { cache: "no-store", ...options })
  const result = await response.json().catch(() => null)
  if (!response.ok || !Array.isArray(result?.providers)) {
    throw new Error(typeof result?.detail === "string" ? result.detail : "Could not update detected calls.")
  }
  return result
}

export function usePortableCalls(available: boolean) {
  const [status, setStatus] = useState<CallStatus>()
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)
  const revision = useRef(0)
  const changing = useRef(false)

  useEffect(() => {
    if (!available) return
    const controller = new AbortController()
    let timer: number
    async function poll() {
      const version = revision.current
      try {
        if (!changing.current) {
          const value = await request("", { signal: controller.signal })
          if (!controller.signal.aborted && version === revision.current) {
            setStatus(previous => JSON.stringify(previous) === JSON.stringify(value) ? previous : value)
          }
        }
      } catch { /* The engine may restart. Read its state on the next poll. */ }
      if (!controller.signal.aborted) timer = window.setTimeout(poll, 1000)
    }
    void poll()
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [available])

  async function act(path: string, options: RequestInit) {
    if (changing.current) return
    changing.current = true
    revision.current++
    setPending(true)
    setError("")
    try {
      setStatus(await request(path, options))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not update detected calls.")
    } finally {
      changing.current = false
      setPending(false)
    }
  }

  return {
    status, error, pending,
    update(preferences: Preferences) {
      return act("/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(preferences) })
    },
    answer(token: string, action: OfferAction, languages: string[] = []) {
      const query = new URLSearchParams()
      for (const language of languages) query.append("language", language)
      return act(`/offer/${encodeURIComponent(token)}/${action}?${query}`, { method: "POST" })
    },
    recording(id: string, action: "stop" | "keep") {
      return act(`/recording/${encodeURIComponent(id)}/${action}`, { method: "POST" })
    },
  }
}

export type PortableCallControls = ReturnType<typeof usePortableCalls>
