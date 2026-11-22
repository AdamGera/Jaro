#!/usr/bin/env node
// Run after bun run build, with playwright-core and its WebKit installed.
// Every request is intercepted. This test never opens Settings or a capture device.
const assert = require("node:assert/strict")
const path = require("node:path")
const { webkit } = require("playwright-core")
const dist = path.resolve(__dirname, "../../dist")

async function run() {
  const browser = await webkit.launch()
  try {
    const page = await browser.newPage({ viewport: { width: 1050, height: 760 } })
    page.setDefaultTimeout(5000)
    const errors = []
    page.on("pageerror", error => errors.push(error.message))
    await page.route("**/*", route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith("/stream")) return route.fulfill({ status: 204 })
      if (url.pathname.startsWith("/api/")) {
        return route.fulfill({ json: url.pathname === "/api/status" ? { status: "ready", languages: [], diarization: true } : [] })
      }
      return route.fulfill({ path: path.join(dist, url.pathname === "/" ? "index.html" : url.pathname) })
    })
    await page.addInitScript(() => {
      const log = []
      const state = { mic: "unavailable", system: "unavailable" }
      const answer = { mic: "denied", system: "granted" }
      window.fixture = { log, state, answer, refuse: null }
      window.zero = { invoke: async (command, args) => {
        log.push([command, args])
        if (command === "jard.devices") return { microphone: "Fixture Mac input", drafts: [] }
        if (command === "jard.update") return { phase: "current", currentVersion: "0.0.8" }
        if (command === "jard.permission") {
          if (args.prompt) state[args.source] = answer[args.source]
          return { status: state[args.source] }
        }
        if (command === "jard.record") {
          const detail = window.fixture.refuse
            ? { event: "error", permission: window.fixture.refuse, message: "SCStreamErrorDomain -3801" }
            : { event: "started", mode: args.mode, microphone: "Fixture Mac input" }
          setTimeout(() => window.dispatchEvent(new CustomEvent("native-sdk:jard-capture", { detail })), 10)
        }
        return {}
      }}
    })
    const button = name => page.getByRole("button", { name, exact: true })
    const calls = cmd => page.evaluate(cmd => window.fixture.log.filter(([name]) => cmd === name).map(([,args]) => args), cmd)
    const guide = page.getByRole("dialog")
    await page.goto("http://127.0.0.1:18997/")
    await page.waitForFunction(() => document.querySelector(".record-button")?.disabled === false)

    await button("Recording access").click()
    await guide.waitFor()
    assert.deepEqual(await calls("jard.permission"), [{source:"mic", prompt:false}, {source:"system", prompt:false}])
    assert.equal((await calls("jard.record")).length, 0)
    await guide.getByText("Unconfirmed", {exact:true}).waitFor()
    await button("Verify Mac audio access").click()
    await guide.getByText("Mac audio access is ready.").waitFor()
    assert.equal((await calls("jard.record")).length, 0)
    // A negative CoreGraphics preflight is not a rejection from ScreenCaptureKit.
    await page.evaluate(() => { window.fixture.state.system = "unavailable"; window.dispatchEvent(new Event("focus")) })
    await guide.getByText("Allowed", {exact:true}).waitFor()
    await button("Cancel").click()
    await page.reload()
    await button("Recording access").waitFor()
    await button("Record").click()
    const settings = button("Open Microphone settings")
    await settings.waitFor()
    await settings.click()
    assert.deepEqual(await calls("jard.openPrivacy"), [{source:"mic"}])
    assert.deepEqual((await calls("jard.permission")).filter(x=>x.prompt).map(x=>x.source), ["mic", "system"])
    assert.equal((await calls("jard.record")).length, 0)
    await page.getByText("Jard isn’t listed in System Settings").click()
    assert(await button("Copy recovery command").isVisible())
    assert.equal(await guide.locator("code").innerText(), "/usr/bin/tccutil reset Microphone local.jardaudio.desktop")
    await page.evaluate(() => { window.fixture.state.mic = "granted"; window.dispatchEvent(new Event("focus")) })
    await button("Retry recording").waitFor()
    assert.equal((await calls("jard.record")).length, 0)

    // Simulate a helper-side TCC refusal despite favorable host preflight.
    await page.evaluate(() => window.fixture.refuse = "system")
    await button("Retry recording").click()
    await button("Open Screen & System Audio Recording settings").click()
    assert(await guide.getByText("If Jard is already on in System Settings", { exact: false }).isVisible())
    assert.deepEqual((await calls("jard.openPrivacy")).at(-1), {source:"system"})
    await page.getByText("Jard isn’t listed in System Settings").click()
    await button("Show this app in Finder").click()
    assert.equal((await calls("jard.revealApp")).length, 1)
    assert(await button("Retry recording").isVisible())
    await button("Cancel").click()

    const mode = page.getByRole("combobox", { name: "Recording source" })
    await mode.selectOption("mic")
    assert.equal(await button("Recording access").count(), 0)
    const settingsSection = page.getByRole("region", {name:"Recording access"})
    await button("Settings").click()
    await settingsSection.getByText("Microphone", {exact:true}).waitFor()
    await settingsSection.getByText("Mac audio", {exact:true}).waitFor()
    await settingsSection.getByText("Capture refused", {exact:true}).waitFor()
    await settingsSection.getByRole("button", {name:"Open Microphone settings"}).click()
    assert.equal(await button("Retry recording").count(), 0)
    await button("Back").click()
    assert.equal((await calls("jard.record")).length, 1)

    await page.reload()
    await page.evaluate(() => {
      Object.assign(window.fixture.state, {mic:"granted",system:"granted"})
      window.dispatchEvent(new Event("focus"))
    })
    await button("Recording access").waitFor({state:"hidden"})
    await button("Settings").click()
    await settingsSection.getByText("Allowed", {exact:true}).nth(1).waitFor()
    const groups = ["Updates", "Application", "Recording access"].map(name => page.getByRole("region", {name, exact:true}))
    const cards = groups.map(group => group.locator(":scope > .dictation-preference-group"))
    const headings = groups.map(group => group.getByRole("heading", {level:3}))
    const boxes = await Promise.all(cards.map(card => card.boundingBox()))
    const headingBoxes = await Promise.all(headings.map(heading => heading.boundingBox()))
    for (let index = 1; index < boxes.length; index++) {
      assert.equal(boxes[index].x, boxes[0].x)
      assert.equal(boxes[index].width, boxes[0].width)
      assert.equal(headingBoxes[index].x, headingBoxes[0].x)
    }
    assert.equal(headingBoxes[1].y - boxes[0].y - boxes[0].height, headingBoxes[2].y - boxes[1].y - boxes[1].height)
    assert.equal(await settingsSection.locator(".dictation-setting-row").count(), 2)
    await page.setViewportSize({width:430, height:720})
    await settingsSection.scrollIntoViewIfNeeded()
    const narrowCard = await cards[2].boundingBox()
    assert(narrowCard.width <= 430)
    for (const control of await settingsSection.getByRole("button", {name:/Open .* settings|Check access again/}).all()) {
      const bounds = await control.boundingBox()
      assert(bounds.x >= narrowCard.x && bounds.x + bounds.width <= narrowCard.x + narrowCard.width)
    }
    const beforeCheck = (await calls("jard.permission")).length
    await settingsSection.getByRole("button", {name:"Check access again"}).click()
    await settingsSection.getByRole("status").getByText("Access is ready.").waitFor()
    assert.deepEqual((await calls("jard.permission")).slice(beforeCheck), [{source:"mic", prompt:false}, {source:"system", prompt:false}])
    await page.setViewportSize({width:1050, height:760})
    await settingsSection.getByText("Jard isn’t listed in System Settings").click()
    await settingsSection.getByRole("button", {name:"Show this app in Finder"}).waitFor()
    assert.equal((await calls("jard.record")).length, 0)
    assert(!(await calls("jard.permission")).some(args=>args.prompt))
    await button("Back").click()
    await page.evaluate(() => { window.fixture.state.system = "unavailable"; window.dispatchEvent(new Event("focus")) })
    await mode.selectOption("system")
    await button("Recording access").waitFor()
    await mode.selectOption("mic")
    assert.equal(await button("Recording access").count(), 0)
    await page.evaluate(() => { window.fixture.state.mic = "restricted"; window.fixture.log.length = 0 })
    await button("Record").click()
    await page.getByText("Restricted by macOS", {exact:true}).waitFor()
    await page.getByText("Jard isn’t listed in System Settings").click()
    assert(!await button("Copy recovery command").count())
    assert.deepEqual(await calls("jard.permission"), [{source:"mic",prompt:false}])
    await button("Cancel").click()
    await mode.selectOption("system")
    await page.evaluate(() => { window.fixture.state.system = "unavailable"; window.fixture.answer.system = "granted"; window.fixture.refuse = null; window.fixture.log.length = 0 })
    await button("Record").click()
    await button("Stop").waitFor()
    assert.deepEqual(await calls("jard.permission"), [{source:"system",prompt:false},{source:"system",prompt:true}])
    assert.equal((await calls("jard.record")).length,1)
    assert.deepEqual(errors, [])
    console.log("Home permission visibility, Settings inspection for both sources, explicit verification, recovery and scoped recording prompts passed in WebKit.")
  } finally { await browser.close() }
}
run().catch(error => { console.error(error); process.exitCode = 1 })
