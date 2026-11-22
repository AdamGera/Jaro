import { useEffect, useRef, useState } from "react"
import type { DesktopBridge } from "@/desktop"
import { useInterfaceLanguage } from "@/interface-language"

export function MicrophoneLevel({ native, enabled }: { native: DesktopBridge; enabled: boolean }) {
  const { t } = useInterfaceLanguage()
  const meter = useRef<HTMLSpanElement>(null)
  const [step, setStep] = useState<number | null>(null)

  useEffect(() => {
    if (!enabled || !meter.current) return
    let visible = false
    let revision = 0
    let timer: number
    let level = 0

    function updateVisibility() {
      window.clearTimeout(timer)
      revision++
      level = 0
      setStep(null)
      if (visible && !document.hidden) void read(revision)
    }

    async function read(request: number) {
      let peak: number | null = null
      try {
        peak = (await native.request("microphoneLevel"))?.peak ?? null
      } catch { /* The input may be unavailable while the recorder is closing. */ }
      if (request !== revision) return
      level = peak === null ? 0 : Math.max(Math.min(peak * 3, 1), level - 0.12)
      const next = Math.round(level * 6)
      setStep(peak === null ? null : next)
      timer = window.setTimeout(() => void read(request), next ? 33 : 250)
    }

    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting
      updateVisibility()
    })
    observer.observe(meter.current)
    document.addEventListener("visibilitychange", updateVisibility)
    return () => {
      revision++
      window.clearTimeout(timer)
      observer.disconnect()
      document.removeEventListener("visibilitychange", updateVisibility)
    }
  }, [enabled, native])

  const value = enabled ? step : null
  return <span ref={meter} className="dictation-microphone-level" role="meter" aria-label={t("Microphone level")}
    aria-valuemin={0} aria-valuemax={6} aria-valuenow={value ?? 0}
    aria-valuetext={value === null ? t("No audio") : undefined} data-receiving={value !== null}>
    {[0.6, 1, 0.75].map(weight => <span key={weight} aria-hidden="true"
      style={{ height: Math.round(2 + weight * (4 + (value ?? 0) * 1.4)) }} />)}
  </span>
}
