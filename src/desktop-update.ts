import { useEffect, useRef, useState } from "react"
import type { DesktopBridge, DesktopUpdate, DesktopUpdateAction } from "@/desktop"

export function useDesktopUpdate(native: DesktopBridge | undefined) {
  const [update, setUpdate] = useState<DesktopUpdate>()
  const [pending, setPending] = useState<DesktopUpdateAction["action"]>()
  const [manual, setManual] = useState(false)
  const [error, setError] = useState("")
  const requestPending = useRef(false)
  const revision = useRef(0)
  const latestMenuRequest = useRef<string>(undefined)

  useEffect(() => {
    if (!native) return
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
    void native.request("update", { action: "status" }).then(status => {
      if (active && initial === revision.current) setUpdate(status)
    }).catch(() => { if (active && initial === revision.current) setUpdate({ phase: "unavailable" }) })
    return () => { active = false; window.removeEventListener("native-sdk:jard-update", receive) }
  }, [native])

  async function run(action: DesktopUpdateAction) {
    if (!native || requestPending.current) return
    requestPending.current = true
    const initial = ++revision.current
    setPending(action.action)
    setManual(action.action === "check")
    setError("")
    try {
      const result = await native.request("update", action)
      if (initial === revision.current) setUpdate(result)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not complete the update request.")
    } finally {
      requestPending.current = false
      setPending(undefined)
    }
  }

  return { update, pending, manual: pending === "check" || (update?.manualCheck ?? manual), error, run }
}

export type DesktopUpdateControls = ReturnType<typeof useDesktopUpdate>
