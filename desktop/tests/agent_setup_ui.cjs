#!/usr/bin/env node
// Run after bun run build, with playwright-core and its WebKit installed.
const assert = require("node:assert/strict")
const fs = require("node:fs/promises")
const path = require("node:path")
const { webkit } = require("playwright-core")
const dist = path.resolve(__dirname, "../../dist")
const cli = "/Applications/Owner's Jaro.app/Contents/Helpers/jardctl"
const quoted = "'/Applications/Owner'\\''s Jaro.app/Contents/Helpers/jardctl'"

async function run() {
  const browser = await webkit.launch()
  try {
    const page = await browser.newPage({ viewport: {width: 1050, height: 760} })
    page.setDefaultTimeout(5000)
    await page.route("**/*", route => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith("/stream")) return route.fulfill({status: 204})
      if (url.pathname.startsWith("/api/")) {
        return route.fulfill({json: url.pathname === "/api/status" ? {status: "ready", languages: [], diarization: true} : []})
      }
      return route.fulfill({path: path.join(dist, url.pathname === "/" ? "index.html" : url.pathname)})
    })
    await page.addInitScript(path => {
      window.copied = []
      window.guideActions = []
      Object.defineProperty(navigator, "clipboard", {value: {writeText: async text => window.copied.push(text)}})
      const agents = [
        {agent: "codex", found: true, state: "missing", path: "/fixture/.agents/skills/jaro-annotate/SKILL.md"},
        {agent: "claude", found: true, state: "different", path: "/fixture/.claude/skills/jaro-annotate/SKILL.md"},
        {agent: "opencode", found: true, state: "managed", path: "/fixture/.config/opencode/skills/jaro-annotate/SKILL.md"},
        {agent: "pi", found: false, state: "missing", path: "/fixture/.pi/agent/skills/jaro-annotate/SKILL.md"},
      ]
      window.zero = {invoke: async (command, payload) => {
        if (command === "jard.agentSetup") return {cli: path}
        if (command === "jard.permission") return {status: "granted"}
        if (command === "jard.annotation") return {phase: "idle"}
        if (command === "jard.annotationAgentSkill") {
          window.guideActions.push(payload)
          if (payload.action === "guide") return {markdown: "# Jaro guide fixture\n"}
          if (payload.action === "install" || payload.action === "update") {
            agents.find(row => row.agent === payload.agent).state = "installed"
          }
          return {agents: agents.map(row => ({...row}))}
        }
        return {}
      }}
    }, cli)

    await page.goto("http://127.0.0.1:18997/")
    await page.getByRole("button", {name: "Settings", exact: true}).click()
    const settings = page.getByRole("region", {name: "CLI and MCP"})
    const latest = () => page.evaluate(() => window.copied.at(-1))
    async function copy(button, message) {
      await settings.getByRole("button", {name: button}).click()
      await settings.getByRole("status").getByText(message, {exact: true}).waitFor()
      return latest()
    }
    assert.equal(await copy("Copy CLI command", "CLI command copied."), quoted + " --json status")
    const prompt = await copy("Copy agent prompt", "Agent prompt copied.")
    assert(prompt.includes(cli) && prompt.includes("Cite the recording title and timestamps") && prompt.includes("Ask me before"))

    await settings.getByText("Setup guide", {exact: true}).click()
    const chooseClient = settings.getByRole("combobox", {name: "MCP client"})
    const copySetup = () => copy("Copy MCP setup", "MCP setup copied.")
    assert.equal(await copySetup(), `codex mcp add jaro -- ${quoted} mcp`)
    await chooseClient.selectOption("claude")
    assert.equal(await copySetup(), `claude mcp add --transport stdio --scope user jaro -- ${quoted} mcp`)
    await chooseClient.selectOption("opencode")
    assert.deepEqual(JSON.parse(await copySetup()), {mcp: {jaro: {type: "local", command: [cli, "mcp"]}}})
    await chooseClient.selectOption("opencode2")
    assert.deepEqual(JSON.parse(await copySetup()), {mcp: {servers: {jaro: {type: "local", command: [cli, "mcp"]}}}})
    await chooseClient.selectOption("other")
    assert.deepEqual(JSON.parse(await copySetup()), {mcpServers: {jaro: {command: cli, args: ["mcp"]}}})

    await page.setViewportSize({width: 430, height: 720})
    const narrow = await settings.evaluate(section => {
      const {left, right} = section.getBoundingClientRect()
      return Array.from(section.querySelectorAll("pre"), pre => {
        const preview = pre.getBoundingClientRect()
        return {section: [left, right], preview: [preview.left, preview.right]}
      })
    })
    for (const {section, preview} of narrow) {
      assert(preview[0] >= section[0] && preview[1] <= section[1] + 1, JSON.stringify({section, preview}))
    }
    await page.setViewportSize({width: 1050, height: 760})
    await page.getByRole("tablist", {name: "Settings", exact: true}).getByRole("tab", {name: "Annotate"}).click()
    const agents = page.getByRole("region", {name: "Agents"})
    assert.deepEqual(await page.evaluate(() => window.guideActions), [])
    await agents.getByText("Capture review guides", {exact: true}).click()
    await agents.getByRole("button", {name: "Copy guide"}).click()
    assert.equal(await latest(), "# Jaro guide fixture\n")
    await agents.getByText("Managed elsewhere", {exact: true}).waitFor()
    await agents.getByText("Agent not configured", {exact: true}).waitFor()
    await agents.getByRole("button", {name: "Install guide"}).click()
    await agents.getByText("Guide installed. Open a new agent chat to use it.", {exact: true}).waitFor()
    await agents.getByRole("button", {name: "Replace guide"}).click()
    assert.deepEqual(await page.evaluate(() => window.guideActions.filter(command => command.agent)), [
      {action: "install", agent: "codex"}, {action: "update", agent: "claude"},
    ])
    for (const [portableCLI, quotedCLI, command] of [
      ["/usr/bin/jardctl", "'/usr/bin/jardctl'", "'/usr/bin/jardctl'"],
      [String.raw`C:\Users\Owner's PC\Jaro\bin\jardctl.exe`,
        String.raw`'C:\Users\Owner''s PC\Jaro\bin\jardctl.exe'`,
        String.raw`& 'C:\Users\Owner''s PC\Jaro\bin\jardctl.exe'`],
    ]) {
      const portable = await browser.newPage()
      try {
        await portable.addInitScript(() => {
          window.copied = []
          Object.defineProperty(navigator, "clipboard", {value: {writeText: async text => window.copied.push(text)}})
        })
        const html = (await fs.readFile(path.join(dist, "index.html"), "utf8"))
          .replace("<html", "<html data-jaro-portable-workspace")
        await portable.route("**/*", route => {
          const {pathname} = new URL(route.request().url())
          if (pathname === "/") return route.fulfill({contentType: "text/html", body: html})
          if (pathname === "/api/portable/agent-setup") return route.fulfill({json: {cli: portableCLI}})
          if (pathname === "/api/status") return route.fulfill({json: {status: "ready", languages: [], diarization: true}})
          if (pathname.endsWith("/stream")) return route.fulfill({status: 204})
          if (pathname.startsWith("/api/")) return route.fulfill({status: 404, json: {detail: "Unavailable in fixture"}})
          return route.fulfill({path: path.join(dist, pathname)})
        })
        await portable.goto("http://127.0.0.1:18997/")
        await portable.getByRole("button", {name: "Settings", exact: true}).click()
        const group = portable.getByRole("region", {name: "CLI and MCP"})
        await group.getByRole("button", {name: "Copy CLI command"}).click()
        await group.getByRole("status").getByText("CLI command copied.", {exact: true}).waitFor()
        assert.equal(await portable.evaluate(() => window.copied.at(-1)), command + " --json status")
        await group.getByText("Setup guide", {exact: true}).click()
        await group.getByRole("button", {name: "Copy MCP setup"}).click()
        await group.getByRole("status").getByText("MCP setup copied.", {exact: true}).waitFor()
        assert.equal(await portable.evaluate(() => window.copied.at(-1)), `codex mcp add jaro -- ${quotedCLI} mcp`)
      } finally { await portable.close() }
    }
    console.log("WebKit verified CLI and MCP setup for macOS, Linux, and Windows, and annotation guide management.")
  } finally { await browser.close() }
}

run().catch(error => { console.error(error); process.exitCode = 1 })
