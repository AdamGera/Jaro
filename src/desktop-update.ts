import { useCallback, useEffect, useEffectEvent, useRef, useState } from "react"
import type { DesktopBridge, DesktopUpdate, DesktopUpdateAction } from "@/desktop"
import { flushWorkspaceDrafts } from "@/workspace-drafts"

export const portableUpdates = document.documentElement.hasAttribute("data-jaro-portable-updates")

async function requestUpdate(native: DesktopBridge | undefined, client: string, action: DesktopUpdateAction) {
  if (native) return native.request("update", action)
  const status = action.action === "status"
  const path = `${status ? "" : "/" + action.action}?client=${client}`
  const response = await fetch("/api/portable/updates" + path, { method: status ? "GET" : "POST", cache: "no-store" })
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}))
    throw new Error(typeof failure.detail === "string" ? failure.detail : "Could not complete the update request.")
  }
  return await response.json() as DesktopUpdate
}

export function useDesktopUpdate(native: DesktopBridge | undefined, client: string) {
  const [update, setUpdate] = useState<DesktopUpdate>()
  const [pending, setPending] = useState<DesktopUpdateAction["action"]>()
  const [manual, setManual] = useState(false)
  const [error, setError] = useState("")
  const requestPending = useRef(false)
  const revision = useRef(0)
  const latestMenuRequest = useRef<string>(undefined)
  const latestTrayRequest = useRef<string>(undefined)
  const restartDialog = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    if (!native && !portableUpdates) return
    let active = true
    const initial = revision.current
    const receive = (event: WindowEventMap["native-sdk:jard-update"]) => {
      const request = event.detail.openUpdateSettings
      if (event.detail.restartInProgress || (request && request !== latestMenuRequest.current)) setError("")
      latestMenuRequest.current = request
      revision.current++
      setUpdate(event.detail)
    }
    window.addEventListener("native-sdk:jard-update", receive)
    void requestUpdate(native, client, { action: "status" }).then(status => {
      if (active && initial === revision.current) setUpdate(status)
    }).catch(() => { if (active && initial === revision.current) setUpdate({ phase: "unavailable" }) })
    return () => { active = false; window.removeEventListener("native-sdk:jard-update", receive) }
  }, [native, client])

  async function run(action: DesktopUpdateAction, trayToken?: string) {
    if ((!native && !portableUpdates) || requestPending.current || restartDialog.current?.open) return
    requestPending.current = true
    const initial = ++revision.current
    setPending(action.action)
    setManual(action.action === "check")
    setError("")
    let restartGuard: HTMLDialogElement | null = null
    let applying = false
    try {
      if (trayToken) {
        const query = new URLSearchParams({ token: trayToken })
        const claimed = await fetch(`/api/portable/updates/claim-tray-restart?${query}`, { method: "POST" })
        if (claimed.status === 409) return
        if (!claimed.ok) throw new Error("Could not start the tray update.")
      }
      if (!native && action.action === "restart") {
        // Make the workspace and its portals inert before taking the final draft checkpoint.
        restartGuard = restartDialog.current
        restartGuard?.showModal()
        await flushWorkspaceDrafts()
      }
      const result = await requestUpdate(native, client, action)
      applying = result.phase === "applying"
      if (initial === revision.current) setUpdate(result)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not complete the update request.")
    } finally {
      if (!applying) restartGuard?.close()
      requestPending.current = false
      setPending(undefined)
    }
  }

  const restartFromTray = useEffectEvent((token: string) => void run({ action: "restart" }, token))
  const trayRestart = update?.trayRestart
  useEffect(() => {
    if (!native && portableUpdates && trayRestart && !pending && !requestPending.current &&
        latestTrayRequest.current !== trayRestart) {
      latestTrayRequest.current = trayRestart
      restartFromTray(trayRestart)
    }
  }, [native, trayRestart, pending])

  const acknowledgeSettings = useCallback((token: string) => {
    if (native || !portableUpdates) return
    const query = new URLSearchParams({ token })
    void fetch(`/api/portable/updates/settings-opened?${query}`, { method: "POST", keepalive: true }).catch(() => {})
  }, [native])

  return { update, pending, manual: pending === "check" || (update?.manualCheck ?? manual), error, run, acknowledgeSettings, restartDialog }
}

export type DesktopUpdateControls = ReturnType<typeof useDesktopUpdate>
