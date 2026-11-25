import { useEffect, useState } from "react"
import type { DesktopBridge, DesktopMeeting, DesktopMeetingAction } from "@/desktop"

export function meetingAudioWarning(meeting?: DesktopMeeting) {
  if (meeting?.phase !== "recording" && meeting?.phase !== "callEnded") return ""
  if (meeting.systemAudioPermissionDenied) return "Mac audio access was denied."
  if (meeting.microphoneUnavailable && meeting.systemAudioUnavailable) return "Microphone and Mac audio are reconnecting."
  if (meeting.microphoneUnavailable) return "Microphone is reconnecting."
  if (meeting.systemAudioUnavailable && meeting.microphoneSilent) return "Mac audio is reconnecting. The microphone is silent."
  if (meeting.systemAudioUnavailable) return "Mac audio is reconnecting."
  if (meeting.microphoneSilent) return "Microphone silent. Mac audio is still recording."
  return ""
}

export async function copySavedMeetingTranscript(id: string, native?: DesktopBridge) {
  if (native) {
    await native.request("copyMeetingTranscript", { id })
    return
  }
  async function readText() {
    const response = await fetch(`/api/sessions/${encodeURIComponent(id)}/transcript-clipboard`, { cache: "no-store" })
    if (!response.ok) {
      const body = await response.json().catch(() => null)
      throw new Error(typeof body?.detail === "string" ? body.detail : "Could not copy the meeting transcript.")
    }
    return response.text()
  }
  if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    // WebKit requires the clipboard operation to start during the click.
    const contents = readText().then(text => new Blob([text], { type: "text/plain" }))
    contents.catch(() => {})
    await navigator.clipboard.write([new ClipboardItem({ "text/plain": contents })])
  } else {
    await navigator.clipboard.writeText(await readText())
  }
}

export function meetingTimeFormat(locale: string, weekday?: "short") {
  const english = locale === "en"
  return new Intl.DateTimeFormat(locale, {
    weekday, hour: english ? "numeric" : "2-digit", minute: "2-digit", hourCycle: english ? "h12" : "h23",
  })
}

export function useDesktopMeeting(native?: DesktopBridge) {
  const [meeting, setMeeting] = useState<DesktopMeeting>()
  const [pending, setPending] = useState(false)
  const [feedback, setFeedback] = useState("")

  useEffect(() => {
    if (!native) return
    let active = true
    let received = false
    function receive(event: WindowEventMap["native-sdk:jard-meetings"]) {
      received = true
      setMeeting(event.detail)
    }
    window.addEventListener("native-sdk:jard-meetings", receive)
    void native.request("meetings", { action: "status" }).then(result => {
      if (active && !received) setMeeting(result)
    }).catch(() => {
      if (active && !received) setFeedback("Could not read the meeting status.")
    })
    return () => {
      active = false
      window.removeEventListener("native-sdk:jard-meetings", receive)
    }
  }, [native])

  async function act(request: DesktopMeetingAction) {
    if (!native) return
    setFeedback("")
    setPending(true)
    try {
      const updated = await native.request("meetings", request)
      setMeeting(updated)
      if (request.action === "copyAll" || request.action === "copyNew") {
        setFeedback(updated.copyPartial ? "Copied the transcript available so far." : "Copied meeting transcript.")
      }
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "Could not update the meeting.")
    } finally {
      setPending(false)
    }
  }

  return { meeting, pending, feedback, act }
}
