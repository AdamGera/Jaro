// This script runs only inside a disposable fixture. The host supplies real mouse events.
(async () => {
  const token = new URL(document.currentScript.src).searchParams.get("token")
  const results = {}
  let lastClick
  document.addEventListener("click",event=>{lastClick={target:event.target.outerHTML.slice(0,120),trusted:event.isTrusted}},true)
  const one = selector => document.querySelector(selector)
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
  async function until(read, label) {
    for (let i=0; i<120; i++) {
      const value=read()
      if (value) return value
      await sleep(50)
    }
    throw Error(label)
  }
  async function click(selector, fraction=.5) {
    const target=await until(() => one(selector) && !one(selector).disabled && one(selector), "Missing "+selector)
    target.scrollIntoView({block:"nearest",inline:"nearest"})
    await new Promise(requestAnimationFrame)
    const r=target.getBoundingClientRect()
    const x=r.left+fraction*r.width, y=r.top+r.height/2
    if (!target.contains(document.elementFromPoint(x,y))) throw Error("Obscured "+selector)
    let pressed=false
    const track=e=>{ pressed=e.isTrusted }
    target.addEventListener("click",track,{once:true})
    window.qaPoint=[x,y]
    await until(()=>pressed,"Trusted click never arrived at "+selector)
  }
  const audio=()=>one("audio")
  const viewer=()=>one(".audio-launcher")?.getAttribute("aria-expanded")==="true"
  const levels=()=>[...document.querySelectorAll(".playback-wave i")].map(el=>parseFloat(el.style.getPropertyValue("--level")))
  async function seekAndShow(selector, seconds, label) {
    await click(selector)
    await until(()=>viewer() && !audio().paused && Math.abs(audio().currentTime-seconds)<.75,label+" opens player and seeks to "+seconds)
    results[label]=+audio().currentTime.toFixed(2)
  }
  async function close() {
    if (viewer()) await click(".audio-launcher")
    await until(()=>!viewer(),"Collapse audio viewer")
  }
  // Silence only the hardware destination. The media, analyser and waveform still receive the fixture signal.
  const connect=AudioNode.prototype.connect
  AudioNode.prototype.connect=function(target,...args) {
    if (target instanceof AudioDestinationNode) {
      const silent=this.context.createGain()
      silent.gain.value=0
      connect.call(silent,target)
      return connect.call(this,silent,...args)
    }
    return connect.call(this,target,...args)
  }
  try {
    if (window.jardDesktop) {
      const dictation = await window.jardDesktop.request("systemDictation", { action: "status" }).catch(() => null)
      if (dictation?.onboarding && !["done", "later"].includes(dictation.onboarding)) {
        const later = await until(() => [...document.querySelectorAll(".dictation-tour button")].find(
          button => button.textContent.trim() === "Set up later"), "First-run deferral")
        later.click()
        await until(() => !document.querySelector(".dictation-tour"), "Tour dismissed")
      }
    }
    if (!one(".session-heading h1")) {
      const rows = await fetch("/api/sessions", { cache: "no-store" }).then(response => response.json())
      if (rows[0]?.status === "processing") {
        history.pushState(null, "", `?session=${rows[0].id}&tab=transcript`)
        window.dispatchEvent(new PopStateEvent("popstate"))
      } else {
        await click(".library-open")
      }
    }
    await until(()=>audio()?.readyState>=1,"Audio metadata")
    if (!one(".session-heading h1")) {
      // Transition the open processing session to complete without reloading or switching sessions.
      await fetch("/__parity/complete",{method:"POST",headers:{"x-parity-token":token}})
      await until(()=>one(".session-heading h1") && audio()?.readyState>=1,"Audio ready after completion transition")
      await click(".audio-launcher")
      await click('.play-button[aria-label="Play"]')
      await until(()=>!audio().paused && audio().currentTime>.25 && Math.max(...levels().filter(Number.isFinite),0)>.45,"Immediate playback after completion")
      results.afterCompletion=+audio().currentTime.toFixed(2)
      await close()
    }
    await click('[role="tab"][id$="-trigger-timeline"]')
    await seekAndShow('.topic-list button',0,"chapter")
    const painted=()=>[...document.querySelectorAll(".playback-wave i")].map(el=>el.getBoundingClientRect().height)
    const loud=await until(()=>{
      const samples=levels()
      return samples.length===5 && Math.max(...samples)>.45 && samples
    },"Voice registers on waveform")
    const loudPixels=painted()
    await until(()=>audio().currentTime>2.6 && audio().currentTime<3.8 &&
      Math.max(...levels())<Math.max(...loud)*.4,"Waveform settles over digital silence")
    const silencePixels=painted()
    if (!matchMedia("(prefers-reduced-motion: reduce)").matches &&
        Math.max(...silencePixels)>=Math.max(...loudPixels)*.5) throw Error("Painted bars ignore audio levels")
    results.waveform={voice:loud,silence:levels(),voicePixels:loudPixels,silencePixels}
    await close()
    const closedAt=audio().currentTime
    await until(()=>audio().currentTime>closedAt+.25 && !viewer(),"Manual collapse lasts while audio advances")
    await seekAndShow('.topic-list li:nth-child(2) button',35,"newSeekDuringPlayback")
    await close()
    await click('[role="tab"][id$="-trigger-transcript"]')
    const toolbar = ".workspace-actions .session-toolbar-slot"
    await until(() => one(toolbar + ' button[aria-label="Copy transcript"]'), "Copy transcript in the top toolbar")
    await click(toolbar + ' button[aria-label="Search transcript"]')
    await until(() => one(toolbar + ' input[aria-label="Search transcript"]'), "Transcript search opens in the top toolbar")
    await click(toolbar + ' button[aria-label="Close search"]')
    results.transcriptToolbar = true
    await seekAndShow('.speaker-heading button[aria-label*="0:17"]',17,"turn")
    await close()
    await seekAndShow('.transcript-word[data-word="1"]',17/15,"word")
    await close()
    await click(".audio-launcher")
    await click('[role="tab"][id$="-trigger-analytics"]')
    await click('.play-button[aria-label="Pause"]')
    await until(()=>audio().paused && one(".audio-launcher").dataset.playing==="false","Pause settles waveform")
    if ([...document.querySelectorAll(".playback-wave i")].some(el=>getComputedStyle(el).transform!=="none")) {
      throw Error("Paused wave is still animated")
    }
    const atSwitch=audio().currentTime
    const picker=one(".session-audio-source select")
    picker.value="mic"
    picker.dispatchEvent(new Event("change",{bubbles:true}))
    await until(()=>audio().src.endsWith("?track=mic") && audio().readyState>=1 &&
      Math.abs(audio().currentTime-atSwitch)<.1 && audio().paused,"Switch to microphone at the same paused timestamp")
    await click('.play-button[aria-label="Play"]')
    await until(()=>!audio().paused && viewer() && one(".audio-launcher").dataset.playing==="true","Play button resumes")
    picker.value="system"
    picker.dispatchEvent(new Event("change",{bubbles:true}))
    await until(()=>audio().src.endsWith("?track=system") && audio().readyState>=1 &&
      !audio().paused && audio().currentTime>=atSwitch-.1,"Switch to Mac audio and continue playback")
    results.playbackSources=true
    await click('.play-button[aria-label="Pause"]')
    await close()
    await click(".timeline-overlay input",.65)
    await until(()=>audio().paused && !viewer() && Math.abs(audio().currentTime-62.4)<2,"Analytics scrub seeks silently")
    results.scrub=+audio().currentTime.toFixed(2)
    await click(".chat-launcher")
    await seekAndShow(".chat-citation",35,"citation")
    await until(()=>one('[role="tab"][data-state="active"]')?.textContent==="Transcript","Citation returns to transcript")
    audio().currentTime=audio().duration-.18
    await until(()=>audio().ended && one(".audio-launcher").dataset.playing==="false","Natural end resets waveform")
    results.ended=true
    await report({frames:results,viewport:[innerWidth,innerHeight],desktop:!!window.jardDesktop})
  } catch(error) {
    await report({error:String(error),completed:results,lastClick,media:audio() && {time:audio().currentTime,paused:audio().paused,error:audio().error?.message},levels:levels(),body:document.body.innerText.slice(-650)})
  }
  async function report(value) {
    await fetch("/__parity/report",{method:"POST",headers:{"x-parity-token":token,"content-type":"application/json"},body:JSON.stringify(value)})
  }
})()
