import { useInterfaceLanguage } from "@/interface-language"
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react"
import { Pause, Play, RotateCcw, RotateCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { createPlaybackWaveform } from "@/playback-waveform"
import { clock } from "@/speaker-activity"

function seekableTime(seconds: number, duration: number) {
  // Seeking to the exact end can request a byte past EOF in WebKit's WAV player.
  return Math.max(0, Math.min(seconds, duration - 0.01))
}

export function usePlayback(source: Blob | string | null, transcriptDuration: number | null, onStart: () => void) {
  const audio = useRef<HTMLAudioElement>(null)
  const wave = useRef<HTMLSpanElement>(null)
  const meter = useRef<ReturnType<typeof createPlaybackWaveform> | null>(null)
  const [time, setTime] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [advancing, setAdvancing] = useState(false)
  const [mediaDuration, setMediaDuration] = useState(0)
  const [ready, setReady] = useState(false)
  const [error, setError] = useState("")
  const switching = useRef<{ source: string | Blob; time: number; playing: boolean } | null>(null)
  const duration = mediaDuration || transcriptDuration || 0

  const prepareMeter = useCallback((el: HTMLAudioElement) => {
    if (!wave.current || typeof AudioContext === "undefined") return
    try {
      if (meter.current?.element !== el) {
        meter.current?.close()
        meter.current = createPlaybackWaveform(el)
      }
      void meter.current.connect().catch(() => {})
    } catch { /* Playback still works if audio analysis is unavailable. */ }
  }, [])
  useEffect(() => () => {
    meter.current?.close()
    meter.current = null
  }, [])

  useEffect(() => {
    const el = audio.current
    if (!el || !source) return
    let url = typeof source === "string" ? source : URL.createObjectURL(source)
    function release() {
      if (url.startsWith("blob:")) URL.revokeObjectURL(url)
    }
    let started = false
    let preparing = false
    const abort = new AbortController()
    const options = { signal: abort.signal }
    let frame = 0
    const sync = () => setTime(el.currentTime)
    let lastFrame = -Infinity
    const tick = (now = performance.now()) => {
      if (now - lastFrame >= 24) {
        lastFrame = now
        sync()
        if (wave.current) meter.current?.draw(wave.current.children)
      }
      if (!el.paused) frame = requestAnimationFrame(tick)
    }
    function pause() {
      started = false
      cancelAnimationFrame(frame)
      sync()
      setAdvancing(false)
      setPlaying(false)
    }
    const prepare = async () => {
      if (preparing) {
        setError("This audio could not be played.")
        return
      }
      preparing = true
      setReady(false)
      try {
        const options = new URLSearchParams()
        if (typeof source === "string") {
          const address = new URL(source, location.href)
          options.set("saved", address.pathname.split("/")[3])
          const track = address.searchParams.get("track")
          if (track) options.set("track", track)
        }
        const response = await fetch(`/api/playback${options.size ? `?${options}` : ""}`, {
          method: "POST", body: typeof source === "string" ? undefined : source, headers: { "Content-Type": "application/octet-stream" }, signal: abort.signal,
        })
        if (!response.ok) throw new Error()
        const playable = await response.blob()
        if (abort.signal.aborted) return
        release()
        url = URL.createObjectURL(playable)
        el.src = url
      } catch {
        if (!abort.signal.aborted) setError("This audio could not be played.")
      }
    }
    el.addEventListener("loadedmetadata", () => {
      if (!Number.isFinite(el.duration) || el.duration <= 0) { void prepare(); return }
      setMediaDuration(el.duration)
      setReady(true)
      if (switching.current?.source === source) {
        const saved = switching.current
        switching.current = null
        if (saved.time > 0) el.currentTime = seekableTime(saved.time, el.duration)
        sync()
        if (saved.playing) void el.play().catch(() => {})
      }
    }, options)
    el.addEventListener("error", prepare, options)
    el.addEventListener("play", () => setPlaying(true), options)
    for (const event of ["waiting", "seeking"]) el.addEventListener(event, () => {
      cancelAnimationFrame(frame)
      setAdvancing(false)
    }, options)
    el.addEventListener("playing", () => {
      if (!started) {
        started = true
        onStart()
        prepareMeter(el)
      }
      cancelAnimationFrame(frame)
      setError("")
      setAdvancing(true)
      tick()
    }, options)
    for (const event of ["pause", "ended"]) el.addEventListener(event, pause, options)
    for (const event of ["seeked", "timeupdate"]) el.addEventListener(event, sync, options)
    el.src = url
    return () => {
      abort.abort()
      cancelAnimationFrame(frame)
      el.pause()
      el.removeAttribute("src")
      el.load()
      release()
    }
  }, [source, onStart, prepareMeter])

  const play = useCallback(() => {
    const el = audio.current
    if (!el) return
    // Resume Web Audio during the same user gesture as media playback.
    prepareMeter(el)
    const continuing = !el.paused
    void el.play().then(() => {
      if (continuing && !el.paused && el === audio.current) onStart()
    }).catch((reason: Error) => {
      if (reason.name !== "AbortError") setError("Playback could not start.")
    })
  }, [onStart, prepareMeter])

  const seek = useCallback((seconds: number, startPlaying = false) => {
    const el = audio.current
    if (!el || !ready) return
    el.currentTime = seekableTime(seconds, duration)
    setTime(el.currentTime)
    if (startPlaying) play()
  }, [duration, ready, play])

  function toggle() {
    const el = audio.current
    if (!el) return
    if (el.paused) play()
    else el.pause()
  }

  function prepareSwitch(next: string | Blob) {
    const el = audio.current
    if (!el || next === source) return
    switching.current = { source: next, time: switching.current?.time ?? (el.ended ? 0 : el.currentTime),
      playing: switching.current?.playing ?? !el.paused }
    setReady(false)
    setPlaying(false)
    setAdvancing(false)
    setError("")
  }

  return { audio, wave, time, playing, advancing, duration, ready, error, seek, toggle, prepareSwitch }
}

export function AudioPlayer({ player }: { player: ReturnType<typeof usePlayback> }) {
  const { t } = useInterfaceLanguage()
  const { audio, time, duration, ready, playing, error, seek, toggle } = player
  return <div className="audio-player" role="group" aria-label="Audio player">
    <audio ref={audio} preload="metadata" />
    <div className="transport">
      <Button variant="ghost" size="icon-sm" disabled={!ready} aria-label="Back 10 seconds" title="Back 10 seconds" onClick={() => seek(time - 10)}><RotateCcw /><span className="skip-seconds" aria-hidden="true">10</span></Button>
      <Button className="play-button" size="icon" disabled={!ready} aria-label={playing ? "Pause" : t("Play")} onClick={toggle}>
        {playing ? <Pause fill="currentColor" /> : <Play fill="currentColor" />}
      </Button>
      <Button variant="ghost" size="icon-sm" disabled={!ready} aria-label="Forward 10 seconds" title="Forward 10 seconds" onClick={() => seek(time + 10)}><RotateCw /><span className="skip-seconds" aria-hidden="true">10</span></Button>
    </div>
    {error ? <span className="player-status" role="alert">{error}</span> : !ready ? <span className="player-status" role="status">Preparing playback…</span> : <>
      <span className="player-time">{clock(time)}</span>
      <input className="player-scrubber" type="range" min="0" max={duration || 1} step="0.01"
        value={Math.min(time, duration)} disabled={!ready} aria-label="Playback position" aria-valuetext={clock(time)}
        style={{ "--played": `${duration ? 100 * time / duration : 0}%` } as CSSProperties}
        onChange={event => seek(event.currentTarget.valueAsNumber)} />
      <span className="player-time">{clock(duration)}</span>
    </>}
  </div>
}
