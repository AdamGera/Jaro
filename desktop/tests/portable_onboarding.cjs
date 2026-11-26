#!/usr/bin/env node
const assert = require("node:assert/strict")
const { readFileSync } = require("node:fs")
const path = require("node:path")
const { webkit } = require("playwright-core")

async function run() {
  const browser = await webkit.launch()
  try {
    const page = await browser.newPage()
    page.setDefaultTimeout(5000)
    const errors = []
    page.on("pageerror", error => errors.push(error.message))
    const dist = path.resolve(__dirname, "../../dist")
    let id = "172fc2d4-d48a-40e1-8b36-cae5231dd31d"
    let model = false
    let windows = true
    let micDenied = true
    let shortcut = false
    let paste = false
    let configuring = false
    let authorizing = false
    let pasteAuthorizing = false
    let transcript = ""
    let micSettingsRequests = 0
    await page.addInitScript(() => {
      window.EventSource = class extends EventTarget {
        constructor() { super(); window.fixtureStream = this }
        close() {}
      }
      Object.defineProperty(navigator, "clipboard", { value: { writeText: value => Promise.resolve(window.fixtureCopied = value) } })
    })
    await page.route("http://127.0.0.1:43721/**", async route => {
      const name = new URL(route.request().url()).pathname
      if (name === "/") {
        const body = readFileSync(path.join(dist, "index.html"), "utf8").replace("<html", "<html data-jaro-portable-workspace data-jaro-portable-dictation data-jaro-desktop-client")
        return route.fulfill({ contentType: "text/html", body })
      }
      if (name.startsWith("/assets/") || name === "/jard-icon.png") return route.fulfill({ path: path.join(dist, name) })
      if (name === "/api/status") return route.fulfill({ json: model ? { status: "ready", languages: [] }
        : { status: "loading", modelSetup: { phase: "needed", total: 1000 } } })
      if (name === "/api/portable/models") {
        model = true
        return route.fulfill({ json: {} })
      }
      if (name === "/api/portable/onboarding") {
        const request = route.request().postDataJSON()
        assert.equal(request.id, id)
        if (request.action === "microphone") {
          micSettingsRequests++
          micDenied = false
        } else if (request.action === "finish") id = null
        return route.fulfill({ json: { id } })
      }
      if (name === "/api/portable/dictation/history") return route.fulfill({ json: { entries: [], total: 0, words: 0, seconds: 0 } })
      if (name === "/api/portable/dictation/microphone") return route.fulfill({ json: { selected: "", inputs: [], available: true } })
      if (name.startsWith("/api/portable/dictation")) {
        if (name.endsWith("/enable")) {
          if (windows) shortcut = true
          else authorizing = true
        }
        if (name.endsWith("/configure")) shortcut = true
        if (name.endsWith("/allow-paste")) pasteAuthorizing = true
        if (name.endsWith("/cancel-approval")) {
          authorizing = false
          pasteAuthorizing = false
        }
        return route.fulfill({ json: { platform: windows ? "windows" : "wayland", shortcut: shortcut ? (windows ? "Left Alt" : "Ctrl+Alt+D") : "",
          shortcutMode: windows ? "hold" : "tap", enabled: shortcut, microphoneDenied: windows && micDenied,
          configurable: configuring, authorizing, keyboardAllowed: paste, pasteAuthorizing,
          phase: "idle", message: "", text: transcript, historyAvailable: true, retention: 7, indicatorPosition: "top" } })
      }
      return route.fulfill({ json: [] })
    })
    const publish = identifier => page.evaluate(id => window.fixtureStream.dispatchEvent(new MessageEvent("onboarding", {
      data: JSON.stringify({ id }),
    })), identifier)
    const heading = name => page.getByRole("heading", { name, exact: true })
    const button = name => page.getByRole("button", { name, exact: true })
    await page.goto("http://127.0.0.1:43721/")
    await page.waitForFunction(() => window.fixtureStream)
    await publish(id)
    await heading("Speak where you write.").waitFor()
    await button("Get started").click()
    await button("Download models").click()
    await button("Open microphone settings").click()
    assert.equal(micSettingsRequests, 1)
    await button("Enable shortcut").click()
    await button("Continue").click()
    await heading("Try the shortcut").waitFor()
    const practice = page.getByRole("textbox", { name: /Try saying/ })
    await page.waitForFunction(() => document.activeElement?.id === "portable-dictation-practice")
    await practice.dispatchEvent("paste")
    await practice.fill("Jaro practice text.")
    await page.getByText("Your words landed.", { exact: true }).waitFor()
    await button("Continue").click()
    await button("Start using Jaro").click()
    await heading("Dictation is ready").waitFor({ state: "hidden" })
    assert.equal(id, null)

    id = "38c36899-8a55-4492-8200-0ef73de7524c"
    windows = false
    shortcut = false
    await publish(id)
    await button("Get started").click()
    await button("Enable shortcut").click()
    await button("Cancel approval").click()
    await button("Enable shortcut").waitFor()
    configuring = true
    await button("Configure shortcuts").click()
    await button("Allow paste").click()
    await button("Cancel approval").click()
    await button("Allow paste").waitFor()
    await button("Allow paste").click()
    await button("Cancel approval").waitFor()
    pasteAuthorizing = false
    paste = true
    await button("Continue").click()
    await practice.waitFor()
    assert.equal(await practice.inputValue(), "")
    await button("Skip practice").click()
    await button("Start using Jaro").click()
    await heading("Dictation is ready").waitFor({ state: "hidden" })

    id = "126ed612-d12a-4db1-99b0-6cd4f50e3e4c"
    paste = false
    await publish(id)
    await button("Get started").click()
    await button("Continue without automatic paste").click()
    const copyPractice = button("Copy last dictation")
    await copyPractice.waitFor()
    assert.equal(await copyPractice.isEnabled(), false)
    transcript = "Jaro manual practice text."
    await copyPractice.click()
    assert.equal(await page.evaluate(() => window.fixtureCopied), transcript)
    await practice.dispatchEvent("paste")
    await practice.fill(transcript)
    await page.getByText("Your words landed.", { exact: true }).waitFor()
    await button("Continue").click()
    await page.getByText("Use Copy last in Dictation to copy your words. You can allow automatic paste later.", { exact: true }).waitFor()
    await button("Start using Jaro").click()
    await heading("Dictation is ready").waitFor({ state: "hidden" })
    await button("Dictation").click()
    await heading("Saved dictations").waitFor()
    assert.equal(await page.getByRole("tablist", { name: "Dictation tabs" }).count(), 0)
    await button("Open dictation settings").click()
    await heading("Settings").waitFor()
    await page.getByRole("tablist", { name: "Settings" }).getByRole("tab", { name: "Dictation", selected: true }).waitFor()
    await button("Replay setup").waitFor()
    await page.getByRole("region", { name: "Recording controls" }).getByRole("combobox", { name: "Recording indicator" }).waitFor()
    const cleanup = page.getByRole("switch", { name: "Noise cleanup (StuPASE)" })
    await cleanup.waitFor({ state: "hidden" })
    await page.getByText("Audio processing", { exact: true }).click()
    await cleanup.waitFor()
    await button("Back").click()
    await heading("Saved dictations").waitFor()
    assert.deepEqual(errors, [])
    console.log("Portable setup passed model, Windows microphone access, Wayland approval and manual paste, practice, and replay in WebKit.")
  } finally {
    await browser.close()
  }
}

run().catch(error => { console.error(error); process.exitCode = 1 })
