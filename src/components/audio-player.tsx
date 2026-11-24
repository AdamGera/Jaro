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
  const detached = useRef<{ element: HTMLAudioElement; time: number; playing: boolean } | null>(null)
  const [mount, setMount] = useState(0)
  const attachAudio = useCallback((node: HTMLAudioElement | null) => {
    const previous = audio.current
    if (previous === node) return
    if (previous && !node) {
      detached.current = { element: previous, time: previous.ended ? 0 : previous.currentTime, playing: !previous.paused }
    } else if (node && detached.current?.element === node) {
      detached.current = null
    }
    audio.current = node
    setMount(count => count + 1)
  }, [])
  const wave = useRef<HTMLSpanElement>(null)
  const meter = useRef<ReturnType<typeof createPlaybackWaveform> | null>(null)
  const [time, setTime] = useState(0)
  const [playback, setPlayback] = useState<{ mount: number; source: Blob | string; playing: boolean; advancing: boolean } | null>(null)
  const [loaded, setLoaded] = useState<{ mount: number; source: Blob | string; duration: number } | null>(null)
  const [failure, setFailure] = useState<{ mount: number; source: Blob | string; message: string } | null>(null)
  const switching = useRef<{ source: string | Blob; time: number; playing: boolean } | null>(null)
  const isCurrent = (state: { mount: number; source: Blob | string } | null) =>
    mount > 0 && !!source && state?.mount === mount && state.source === source
  const ready = isCurrent(loaded)
  const mediaDuration = ready && loaded ? loaded.duration : 0
  const playing = isCurrent(playback) && !!playback?.playing
  const advancing = isCurrent(playback) && !!playback?.advancing
  const error = isCurrent(failure) && failure ? failure.message : ""
  const duration = mediaDuration || transcriptDuration || 0

  const prepareMeter = useCallback(async (el: HTMLAudioElement) => {
    if (!wave.current || typeof AudioContext === "undefined") return
    try {
      if (meter.current?.element !== el) {
        meter.current?.close()
        meter.current = createPlaybackWaveform(el)
      }
      await meter.current.connect()
    } catch { /* Playback still works if audio analysis is unavailable. */ }
  }, [])
  useEffect(() => () => {
    meter.current?.close()
    meter.current = null
  }, [])

  useEffect(() => {
    const el = audio.current
    if (!mount || !el || !source) return
    const currentSource = source
    let url = typeof currentSource === "string" ? currentSource : URL.createObjectURL(currentSource)
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
      setPlayback({ mount, source: currentSource, playing: false, advancing: false })
    }
    const prepare = async () => {
      if (preparing) {
        setFailure({ mount, source: currentSource, message: "This audio could not be played." })
        return
      }
      preparing = true
      setLoaded(current => current?.mount === mount && current.source === currentSource ? null : current)
      try {
        const params = new URLSearchParams()
        if (typeof currentSource === "string") {
          const address = new URL(currentSource, location.href)
          params.set("saved", address.pathname.split("/")[3])
          const track = address.searchParams.get("track")
          if (track) params.set("track", track)
        }
        const response = await fetch(`/api/playback${params.size ? `?${params}` : ""}`, {
          method: "POST", body: typeof currentSource === "string" ? undefined : currentSource, headers: { "Content-Type": "application/octet-stream" }, signal: abort.signal,
        })
        if (!response.ok) throw new Error()
        const playable = await response.blob()
        if (abort.signal.aborted) return
        release()
        url = URL.createObjectURL(playable)
        el.src = url
      } catch {
        if (!abort.signal.aborted) setFailure({ mount, source: currentSource, message: "This audio could not be played." })
      }
    }
    el.addEventListener("loadedmetadata", () => {
      if (!Number.isFinite(el.duration) || el.duration <= 0) { void prepare(); return }
      setLoaded({ mount, source: currentSource, duration: el.duration })
      if (switching.current?.source === currentSource) {
        const saved = switching.current
        switching.current = null
        if (saved.time > 0) el.currentTime = seekableTime(saved.time, el.duration)
        sync()
        if (saved.playing) void el.play().catch(() => {})
      } else {
        sync()
      }
    }, options)
    el.addEventListener("error", prepare, options)
    el.addEventListener("play", () => setPlayback(current => ({
      mount, source: currentSource, playing: true, advancing: current?.mount === mount && current.source === currentSource ? current.advancing : false,
    })), options)
    for (const event of ["waiting", "seeking"]) el.addEventListener(event, () => {
      cancelAnimationFrame(frame)
      setPlayback(current => ({
        mount, source: currentSource, playing: current?.mount === mount && current.source === currentSource ? current.playing : !el.paused, advancing: false,
      }))
    }, options)
    el.addEventListener("playing", () => {
      if (!started) {
        started = true
        onStart()
        void prepareMeter(el)
      }
      cancelAnimationFrame(frame)
      setFailure(current => current?.mount === mount && current.source === currentSource ? null : current)
      setPlayback({ mount, source: currentSource, playing: true, advancing: true })
      tick()
    }, options)
    for (const event of ["pause", "ended"]) el.addEventListener(event, pause, options)
    el.addEventListener("seeked", () => {
      sync()
      if (!el.paused) {
        cancelAnimationFrame(frame)
        setPlayback({ mount, source: currentSource, playing: true, advancing: true })
        tick()
      }
    }, options)
    el.addEventListener("timeupdate", sync, options)
    el.src = url
    return () => {
      const saved = detached.current?.element === el ? detached.current : null
      if (saved) detached.current = null
      if (audio.current && audio.current !== el && !switching.current && saved && (saved.playing || saved.time > 0)) {
        switching.current = { source: currentSource, time: saved.time, playing: saved.playing }
      }
      abort.abort()
      cancelAnimationFrame(frame)
      el.pause()
      el.removeAttribute("src")
      el.load()
      release()
      if (audio.current !== el && meter.current?.element === el) {
        meter.current.close()
        meter.current = null
      }
    }
  }, [mount, source, onStart, prepareMeter])

  const play = useCallback(() => {
    const el = audio.current
    if (!el || !source) return
    const continuing = !el.paused
    const start = () => {
      if (el !== audio.current) return
      void el.play().then(() => {
        if (continuing && !el.paused && el === audio.current) onStart()
      }).catch((reason: Error) => {
        if (reason.name !== "AbortError") setFailure({ mount, source, message: "Playback could not start." })
      })
    }
    // Connect Web Audio before starting playback; attaching MediaElementAudioSourceNode
    // after AVFoundation begins playing without a seek can silence WebKit's analyser output.
    if (!wave.current || typeof AudioContext === "undefined") {
      start()
      return
    }
    void prepareMeter(el).then(start)
  }, [mount, source, onStart, prepareMeter])

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
  }

  return { audio, attachAudio, wave, time, playing, advancing, duration, ready, error, seek, toggle, prepareSwitch }
}

export function AudioPlayer({ player }: { player: ReturnType<typeof usePlayback> }) {
  const { t } = useInterfaceLanguage()
  const { attachAudio, time, duration, ready, playing, error, seek, toggle } = player
  return <div className="audio-player" role="group" aria-label="Audio player">
    <audio ref={attachAudio} preload="metadata" />
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
