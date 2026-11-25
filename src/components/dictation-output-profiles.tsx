import { useInterfaceLanguage } from "@/interface-language"
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react"
import { ArrowDown, ArrowUp, FileCode, FolderOpen, Plus, Sparkles, Trash2 } from "lucide-react"
import type { DesktopBridge } from "@/desktop"
import type { ProfileApplication, DictationLocalTransform, DictationOutputPreview, DictationOutputProfile, DictationScriptReview, DictationTransformGrants, DictationStatus, SystemDictationControls } from "@/system-dictation"
import { profileName } from "@/system-dictation"
import { Button } from "./ui/button"
import { SettingGroup } from "./settings-rows"

const stepNames: Record<string, string> = {
  trim: "Trim whitespace",
  replace: "Apply word replacements",
  fillers: "Remove filler words",
  format: "Apply spoken formatting",
  symbols: "Apply spoken symbols",
  sentence: "Capitalize and punctuate",
  compact: "Compact whitespace",
  lowercase: "Lowercase",
  "spongebob-case": "SpongeBob case",
  "no-trailing-punctuation": "No trailing punctuation",
  rewrite: "Rewrite with local Qwen",
  script: "Run local script",
}

function stepLabel(step: string, transforms: DictationLocalTransform[]) {
  return stepNames[step] ?? transforms.find(item => `local:${item.id}` === step)?.name ?? step
}

function SavedOutputSteps({ profile, fallback = [], transforms }: {
  profile?: DictationOutputProfile; fallback?: string[]; transforms: DictationLocalTransform[]
}) {
  const { t } = useInterfaceLanguage()
  const configured = profile?.steps ?? fallback
  const steps = profile?.corrections && !configured.includes("replace") ? ["replace", ...configured] : configured
  return steps.length ? <ol className="dictation-profile-steps">
    {steps.map((step, index) => <li key={index}>{t(stepLabel(step, transforms))}</li>)}
  </ol> : <p className="dictation-output-note">{t("No text transformations.")}</p>
}

export function OutputOverview({ native, status }: { native: DesktopBridge; status: DictationStatus }) {
  const { t } = useInterfaceLanguage()
  const profiles = status.outputProfiles ?? ""
  const [saved, setSaved] = useState<{ value: string; rows?: DictationOutputProfile[]; error?: string }>()
  const [sample, setSample] = useState("")
  const [trial, setTrial] = useState<{ sample: string; settings: string; output: DictationOutputPreview }>()
  const [trying, setTrying] = useState(false)
  const [error, setError] = useState("")
  const [actionError, setActionError] = useState("")
  const [copied, setCopied] = useState(false)
  const [transforms, setTransforms] = useState<DictationLocalTransform[]>([])
  const settings = JSON.stringify([profiles, status.replacements, status.fillerWords, status.spokenFormatting,
    status.vocabularyEntries?.map(({ term, soundsLike }) => [term, soundsLike])])
  const current = saved?.value === profiles ? saved : undefined
  const defaults = current?.rows?.find(row => row.target.toLowerCase() === "default")
  const scoped = current?.rows?.filter(row => row.target.toLowerCase() !== "default") ?? []
  const fallback = [
    ...(status.fillerWords ? ["fillers"] : []),
    ...(status.spokenFormatting ? ["format"] : []),
    ...(status.replacements?.trim() ? ["replace"] : []),
  ]
  const result = trial?.sample === sample && trial.settings === settings ? trial.output : undefined

  useEffect(() => {
    let active = true
    void native.request("systemDictation", { action: "describeOutputProfiles", profiles }).then(response => {
      if (active && "profiles" in response) setSaved({ value: profiles, rows: response.profiles })
    }).catch(failure => {
      if (active) setSaved({ value: profiles, error: failure instanceof Error ? failure.message : "Could not load profiles." })
    })
    return () => { active = false }
  }, [native, profiles])

  async function previewSample() {
    if (!sample.trim() || trying) return
    setError("")
    setTrial(undefined)
    setTrying(true)
    try {
      const output = await native.request("systemDictation", { action: "tryOutput", text: sample })
      if ("steps" in output) setTrial({ sample, settings, output })
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not preview the sample.")
    } finally {
      setTrying(false)
    }
  }

  async function runOutputSettingsAction(action: "copyOutputAgentPrompt" | "revealOutputSettings") {
    setActionError("")
    try {
      await native.request("systemDictation", { action })
      if (action === "copyOutputAgentPrompt") setCopied(true)
    } catch (failure) {
      setActionError(failure instanceof Error ? failure.message : "Could not open output settings.")
    }
  }

  return <section className="dictation-settings dictation-preferences" aria-label={t("Dictation output")}>
    <SettingGroup id="dictation-output-settings-title" title={t("Output settings")}>
      <div className="dictation-output-content" data-setting-title={t("Output settings")}
        data-setting-description={t("Edit with agent or reveal the settings file")}>
        <div className="dictation-row-actions">
          <Button type="button" variant="outline" size="sm" onClick={() => void runOutputSettingsAction("revealOutputSettings")}>
            <FolderOpen />{t("Reveal settings file")}
          </Button>
          <Button type="button" size="sm" onClick={() => void runOutputSettingsAction("copyOutputAgentPrompt")}>
            <Sparkles />{t("Edit with agent")}
          </Button>
        </div>
        {copied && <p className="dictation-output-note" role="status">{t("Output instructions copied.")}</p>}
        {actionError && <p className="dictation-error" role="alert">{actionError}</p>}
      </div>
    </SettingGroup>
    <OutputScriptApproval native={native} name={status.outputScriptName ?? ""} onCatalog={setTransforms} />
    <SettingGroup id="dictation-output-default-title" title={t("Every dictation")}>
      <div className="dictation-output-content">
        {current?.rows ? <SavedOutputSteps profile={defaults} fallback={fallback} transforms={transforms} />
          : current?.error ? <p className="dictation-error" role="alert">{current.error}</p>
          : <p className="dictation-output-note" role="status">{t("Loading profiles…")}</p>}
      </div>
    </SettingGroup>
    <SettingGroup id="dictation-output-try-title" title={t("Try")}>
      <form className="dictation-output-content dictation-output-try dictation-rule"
        data-setting-title={t("Preview sample")} data-setting-description={t("Try saved dictation output")}
        onSubmit={event => { event.preventDefault(); void previewSample() }}>
        <label><strong>{t("Sample sentence")}</strong>
          <small>{t("Preview built-in steps and corrections. Rewrites and scripts run during dictation.")}</small>
          <textarea dir="auto" rows={3} maxLength={8000} value={sample} disabled={trying}
            onChange={event => { setSample(event.target.value); setError(""); setTrial(undefined) }} />
        </label>
        <Button type="submit" variant="outline" size="sm" disabled={!sample.trim() || trying}>{t("Preview sample")}</Button>
        {error && <p className="dictation-error" role="alert">{error}</p>}
        {result && <div className="dictation-profile-preview" role="region" aria-label={t("Preview result")} aria-live="polite">
          <p dir="auto">{result.text || t("No output")}</p>
          {result.skipped?.length ? <small className="dictation-output-note">
            {t("Skipped in preview")} · {result.skipped.map(step => t(stepLabel(step, transforms))).join(", ")}
          </small> : null}
          {result.warnings.map(warning => <p className="dictation-error" key={warning}>{warning}</p>)}
        </div>}
      </form>
    </SettingGroup>
    {scoped.length > 0 && <SettingGroup id="dictation-output-scoped-title" title={t("In some apps")}>
      {scoped.map((row, index) => <details key={index} className="dictation-output-profile">
        <summary><bdi>{profileName(row.target)}</bdi></summary>
        <div className="dictation-output-content"><SavedOutputSteps profile={row} transforms={transforms} /></div>
      </details>)}
    </SettingGroup>}
  </section>
}

function embeddedConfig(name: string) {
  return /^jaro\.config\.m?(?:js|ts)$/.test(name)
}

function OutputScriptApproval({ native, name, onCatalog }: {
  native: DesktopBridge; name: string; onCatalog: (items: DictationLocalTransform[]) => void
}) {
  const { t } = useInterfaceLanguage()
  const [checked, setChecked] = useState<{ name: string; review: DictationScriptReview }>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [permissionDraft, setPermissionDraft] = useState<{ token: string; grants: DictationTransformGrants }>()
  const revision = useRef(0)
  const review = checked?.name === name ? checked.review : undefined
  const grants = permissionDraft?.token === review?.token ? permissionDraft?.grants : review?.grants
  const disabled = review?.policy === "disabled"
  const localOnly = review?.policy === "loopback"
  const changed = !!grants?.network !== !!review?.grants?.network ||
    (!localOnly && !!grants?.programs !== !!review?.grants?.programs) ||
    !!grants?.pageContext !== !!review?.grants?.pageContext
  useEffect(() => { onCatalog(review?.transforms ?? []) }, [review, onCatalog])

  useEffect(() => {
    let active = true
    let pending = false
    async function inspect() {
      if (pending) return
      pending = true
      const before = revision.current
      try {
        const result = await native.request("systemDictation", { action: "inspectOutputScript" })
        if (active && revision.current === before && "state" in result) setChecked({ name, review: result })
      } catch (failure) {
        if (active && revision.current === before) {
          setChecked({ name, review: { state: "unavailable", message: failure instanceof Error ? failure.message : "Could not check the script." } })
        }
      } finally {
        pending = false
      }
    }
    void inspect()
    const timer = window.setInterval(inspect, embeddedConfig(name) ? 400 : name ? 2500 : 1000)
    return () => { active = false; window.clearInterval(timer) }
  }, [native, name])

  async function act(action: "chooseOutputScript" | "revealOutputScript" | "clearOutputScript" | "createOutputConfig" | "approveOutputScript") {
    const token = review?.token
    if (action === "approveOutputScript" && !token) return
    setBusy(true)
    setError("")
    revision.current++
    try {
      const result = await native.request("systemDictation", action === "approveOutputScript"
        ? { action, token: token ?? "", grants } : { action })
      if (action === "approveOutputScript" && "state" in result) {
        setChecked({ name, review: result })
        setPermissionDraft(undefined)
      }
      if (action === "clearOutputScript" || action === "chooseOutputScript" || action === "createOutputConfig") {
        setChecked(undefined)
        setPermissionDraft(undefined)
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not update the script approval.")
    } finally {
      revision.current++
      setBusy(false)
    }
  }

  return <SettingGroup id="dictation-output-script-title" title={t("Local script")}>
    <div className="dictation-output-content" data-setting-title={t("Local script")}
      data-setting-description={t("Review the approved version of a dictation script")}>
      {name && <p className="dictation-output-note"><bdi>{name}</bdi>{review?.version && ` · ${t("Version")} ${review.version}`}</p>}
      {!name && !disabled && <p className="dictation-output-note">{review?.state === "found"
        ? <>{t("Configuration found")} · <bdi>{review.file}</bdi></>
        : t("Choose an executable, a JSON catalog, or a Jaro JavaScript or TypeScript configuration.")}</p>}
      {name && !review && <p className="dictation-output-note" role="status">{t("Checking the selected script…")}</p>}
      {review?.state === "review" && <div className="dictation-script-review" role="status">
        <FileCode aria-hidden="true" />
        <div><strong>{t("Review this script version")}</strong>
          <p>{t(review.kind === "embedded"
            ? "These steps receive your dictation and available app and site context. Changes to the configuration or its imports need a new approval."
            : "The script receives your dictation and can run programs or connect to the network as you. Jaro asks again when the file changes.")}</p>
        </div>
      </div>}
      {review?.state === "approved" && <p className="dictation-output-note" role="status">{t("This version is approved.")}</p>}
      {localOnly && review?.state !== "unavailable" && <p className="dictation-output-note" role="status">
        {t("Your organization allows network requests to this Mac only. Programs are disabled.")}
      </p>}
      {review?.kind === "embedded" && review.token &&
        (review.permissions?.network || review.permissions?.programs || review.permissions?.pageContext) &&
        <fieldset className="dictation-transform-permissions">
          <legend>{t("Configuration permissions")}</legend>
          {([
            { key: "network", title: "Allow network requests", hint: "Requests can send text and context to a network destination.", names: review.permissions.hosts },
            { key: "programs", title: "Allow programs", hint: "Programs run with your macOS account’s access.", names: review.permissions.commands },
            { key: "pageContext", title: "Share page and window details",
              hint: "Give this version the full page URL and the focused window title.", names: undefined },
          ] as const).filter(({ key }) => review.permissions?.[key]).map(({ key, title, hint, names }) =>
            <label className="dictation-transform-permission" key={key}>
              <input type="checkbox" checked={(key !== "programs" || !localOnly) && !!grants?.[key]}
                disabled={busy || (key === "programs" && localOnly)} onChange={event => {
                if (review.token) setPermissionDraft({ token: review.token, grants: { ...grants, [key]: event.target.checked } })
              }} />
              <span><strong>{t(title)}</strong><small>{t(hint)}</small>
                {!!names?.length && <small dir="auto">{names.join(", ")}</small>}
              </span>
            </label>
          )}
          {review.permissions.partial && <p className="dictation-output-note">{t("The configuration can choose additional destinations when it runs.")}</p>}
        </fieldset>}
      {review?.stale && <p className="dictation-output-note">{t("Definitions from the last valid version")}</p>}
      {!!review?.transforms?.length && <ul className="dictation-profile-steps" aria-label={t("Named transformations")}>
        {review.transforms.map(item => <li key={item.id}>
          <div><strong>{item.name}</strong>{item.description && <p className="dictation-output-note">{item.description}</p>}</div>
        </li>)}
      </ul>}
      {review?.state === "unavailable" && <p className="dictation-error" role="status">{review.message}</p>}
      <div className="dictation-row-actions">
        {!name && !disabled && <Button type="button" size="sm" disabled={busy}
          onClick={() => void act("createOutputConfig")}>
          {t(review?.state === "found" ? "Use configuration" : "Create starter configuration")}
        </Button>}
        {(review?.state === "review" || (review?.state === "approved" && changed)) &&
          <Button type="button" size="sm" disabled={busy} onClick={() => void act("approveOutputScript")}>
            {t(review.state === "approved" ? "Update permissions" : "Allow this version")}
          </Button>}
        {!disabled && <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void act("chooseOutputScript")}>
          {t(name ? "Choose another script…" : "Choose a local script…")}</Button>}
        {name && <>
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void act("revealOutputScript")}>{t("Reveal script")}</Button>
          <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void act("clearOutputScript")}>{t("Remove script")}</Button>
        </>}
      </div>
      {error && <p className="dictation-error" role="alert">{error}</p>}
    </div>
  </SettingGroup>
}

export function OutputProfiles({ native, value, script, pending, save, run }: {
  native: DesktopBridge; value: string; script: string; pending: boolean
  save: (value: string) => Promise<boolean>; run: SystemDictationControls["run"]
}) {
  const [open, setOpen] = useState(false)
  const [catalog, setCatalog] = useState<{ script: string; items: DictationLocalTransform[] }>()
  const transforms = catalog && catalog.script === script ? catalog.items : []
  useEffect(() => {
    if (!open || !script) return
    let active = true
    let checking = false
    async function refresh() {
      if (checking) return
      checking = true
      try {
        const result = await native.request("systemDictation", { action: "inspectOutputScript" })
        if (active && "state" in result) setCatalog({ script, items: result.transforms ?? [] })
      } catch {
        if (active) setCatalog(undefined)
      } finally {
        checking = false
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, embeddedConfig(script) ? 400 : 2500)
    return () => { active = false; window.clearInterval(timer) }
  }, [native, open, script])

  const describe = useCallback(async (profiles: string) => {
    const result = await native.request("systemDictation", { action: "describeOutputProfiles", profiles })
    if ("profiles" in result) return result.profiles
    throw new Error("Could not load profiles.")
  }, [native])
  const preview = useCallback<PreviewOutput>(async input => {
    const result = await native.request("systemDictation", { action: "previewOutput", ...input })
    if ("steps" in result) return result
    throw new Error("Could not preview profiles.")
  }, [native])

  const loadApplications = useCallback(async () => {
    const result = await native.request("systemDictation", { action: "profileApplications" })
    if ("applications" in result) return result.applications
    throw new Error("Could not list installed applications.")
  }, [native])

  const chooseApplication = useCallback(async () => {
    const result = await native.request("systemDictation", { action: "chooseProfileApplication" })
    if ("application" in result) return result.application
    throw new Error("Could not read the selected application.")
  }, [native])

  return <OutputProfileEditor value={value} pending={pending} save={save} describe={describe} previewOutput={preview}
    onOpenChange={setOpen} loadApplications={loadApplications} chooseApplication={chooseApplication} nativeScript={{ path: script, transforms, run }} />
}

type PreviewOutput = (input: { text: string; profiles: string; app: string; site: string }) => Promise<DictationOutputPreview>

function ApplicationTarget({ value, onChange, load, choose, manualLabel }: {
  value: string; onChange: (value: string) => void; load: () => Promise<ProfileApplication[]>; choose?: () => Promise<ProfileApplication | null>; manualLabel: string
}) {
  const { t } = useInterfaceLanguage()
  const [applications, setApplications] = useState<ProfileApplication[]>([])
  const [manual, setManual] = useState(false)
  const [picked, setPicked] = useState<ProfileApplication>()
  const [error, setError] = useState("")
  const mounted = useRef(false)
  useLayoutEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  async function browse() {
    if (!choose) return
    setError("")
    try {
      const app = await choose()
      if (app && mounted.current) {
        setManual(false)
        setPicked(app)
        onChange(app.id)
      }
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "Could not choose an application.")
    }
  }
  useEffect(() => {
    let active = true
    void load().then(items => { if (active) setApplications(items) }).catch(() => { if (active) setApplications([]) })
    return () => { active = false }
  }, [load])
  const choices = picked && !applications.some(app => app.id.toLowerCase() === picked.id.toLowerCase())
    ? [picked, ...applications] : applications
  const selected = manual ? undefined : choices.find(app => app.id.toLowerCase() === value.toLowerCase())
  return <>
    {choices.length > 0 && <label>{t("Installed application")}
      <select value={selected?.id ?? ""} onChange={event => {
        setManual(!event.target.value)
        onChange(event.target.value)
      }}>
        <option value="">{t("Enter an application manually…")}</option>
        {choices.map(app => <option key={app.id} value={app.id}>{app.name}</option>)}
      </select>
    </label>}
    {!selected && <label>{t(manualLabel)}
      <input value={value} maxLength={300} onChange={event => { setManual(true); onChange(event.target.value) }} />
    </label>}
    {choose && <Button type="button" variant="outline" size="sm" onClick={() => void browse()}>
      <FolderOpen />{t("Browse for an application…")}
    </Button>}
    {error && <p className="dictation-error" role="alert">{error}</p>}
  </>
}

export function OutputProfileEditor({ value, pending, save, describe, previewOutput, loadApplications, chooseApplication, nativeScript, onOpenChange, wayland, websiteRules }: {
  value: string
  pending: boolean
  save: (value: string, base: string) => Promise<boolean | void>
  describe: (value: string) => Promise<DictationOutputProfile[]>
  chooseApplication?: () => Promise<ProfileApplication | null>
  loadApplications: () => Promise<ProfileApplication[]>
  previewOutput: PreviewOutput
  nativeScript?: { path: string; transforms: DictationLocalTransform[]; run: SystemDictationControls["run"] }
  onOpenChange?: (open: boolean) => void
  wayland?: boolean
  websiteRules?: boolean
}) {
  const { t } = useInterfaceLanguage()
  const websites = websiteRules || !!nativeScript
  const [open, setOpen] = useState(false)
  const transforms = nativeScript?.transforms ?? []
  const stepOptions = [...Object.entries(stepNames).filter(([step]) => nativeScript || step !== "script"),
    ...transforms.map(item => [`local:${item.id}`, item.name])]
  const [saved, setSaved] = useState<{ value: string; rows: DictationOutputProfile[] }>()
  const [draft, setDraft] = useState<DictationOutputProfile[]>()
  const [selected, setSelected] = useState(0)
  const [sample, setSample] = useState("")
  const [app, setApp] = useState("")
  const [site, setSite] = useState("")
  const [preview, setPreview] = useState<DictationOutputPreview>()
  const [working, setWorking] = useState(false)
  const [error, setError] = useState("")
  const [loadError, setLoadError] = useState("")
  const rows = draft ?? (saved?.value === value ? saved.rows : undefined)
  const profile = rows?.[selected]
  const busy = working || pending
  const definition = rows && JSON.stringify(rows.map(row => ({
    ...row, target: row.target.trim(), instruction: row.instruction.trim(), corrections: row.corrections.trim(),
    vocabulary: row.vocabulary.map(term => term.trim()).filter(Boolean),
  })))

  useEffect(() => {
    if (!open || draft || saved?.value === value) return
    let active = true
    void describe(value).then(rows => {
      if (active) {
        setSaved({ value, rows })
        setLoadError("")
      }
    }).catch(failure => {
      if (active) setLoadError(failure instanceof Error ? failure.message : "Could not load profiles.")
    })
    return () => { active = false }
  }, [describe, draft, open, saved?.value, value])

  function update(patch: Partial<DictationOutputProfile>) {
    setDraft(current => (current ?? rows)?.map((row, index) => index === selected ? { ...row, ...patch } : row))
    setPreview(undefined)
  }

  async function browseApplication() {
    if (!chooseApplication) return null
    setWorking(true)
    try {
      return await chooseApplication()
    } finally {
      setWorking(false)
    }
  }

  function add() {
    if (!rows) return
    const target = rows.some(row => row.target.toLowerCase() === "default") ? "app:" : "default"
    setDraft([...rows, { target, steps: [], vocabulary: [], instruction: "", deadline: 30, corrections: "" }])
    setSelected(rows.length)
    setPreview(undefined)
  }

  function moveStep(from: number, to: number) {
    if (!profile) return
    const reordered = [...profile.steps]
    const [step] = reordered.splice(from, 1)
    reordered.splice(to, 0, step)
    update({ steps: reordered })
  }

  async function apply(write: boolean) {
    if (!definition) return
    setError("")
    setWorking(true)
    try {
      if (write) {
        const normalized = await describe(definition)
        if (await save(definition, saved?.value ?? value) === false) return
        setSaved({ value: definition, rows: normalized })
        setDraft(undefined)
      } else {
        setPreview(await previewOutput({ text: sample, profiles: definition, app, site }))
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not apply output profiles.")
    } finally {
      setWorking(false)
    }
  }

  const scope = profile?.target.toLowerCase().split(":")[0] || "default"
  return <details className="dictation-history" data-setting-title="Ordered output profiles"
    data-setting-description={nativeScript ? "App and website rules, scripts and rewriting" : "App and website rules and local rewriting"}
    onToggle={event => {
      const open = event.currentTarget.open
      setOpen(open)
      onOpenChange?.(open)
    }}>
    <summary data-setting-control>Ordered output profiles</summary>
    {open && <div className="dictation-options dictation-profile-editor">
      {!rows && (loadError ? <>
        <p role="alert" className="dictation-error">{loadError}</p>
        <Button type="button" size="sm" variant="outline" onClick={() => { setDraft([]); setSelected(0) }}>Replace invalid profiles</Button>
      </> : <p role="status">Loading profiles…</p>)}
      {rows && <>
        <div className="dictation-profile-toolbar">
          {rows.length > 0 && <label>Profile
            <select value={selected} disabled={busy} onChange={event => { setSelected(Number(event.target.value)); setPreview(undefined) }}>
              {rows.map((row, index) => <option value={index} key={index}>{profileName(row.target)}</option>)}
            </select>
          </label>}
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={add}><Plus />Add profile</Button>
        </div>
        {nativeScript?.path && <p className="dictation-output-note">Review the selected version in Dictation → Output.</p>}
        {profile && <fieldset className="dictation-profile-card" disabled={busy} aria-label="Profile settings">
          <label>Applies to
            {wayland && <small>{t("On Wayland, Jaro uses the default when the compositor cannot identify the active app.")}</small>}
            <select value={scope} onChange={event => update({ target: event.target.value === "default" ? "default" : event.target.value + ":" })}>
              <option value="default">{t("Default")}</option><option value="app">{t("Application")}</option>
              {websites && <option value="site">{t("Website")}</option>}
            </select>
          </label>
          {scope === "app" && <ApplicationTarget key={selected} value={profile.target.slice(4)} load={loadApplications} choose={chooseApplication && browseApplication}
            manualLabel={nativeScript ? "App name or bundle ID" : "Application name"} onChange={id => update({ target: "app:" + id })} />}
          {scope === "site" && <label>Website host or URL
            <input value={profile.target.slice(5)} maxLength={300} placeholder="example.com or *.example.com"
              onChange={event => update({ target: "site:" + event.target.value })} />
          </label>}
          {profile.steps.length > 0 && <ol className="dictation-profile-steps" aria-label="Output steps">
            {profile.steps.map((step, index) => <li key={index}>
              <div className="dictation-profile-step">
                <span>{stepLabel(step, transforms)}</span>
                <div className="dictation-row-actions">
                  <Button type="button" variant="ghost" size="icon-xs" aria-label={`Move ${stepLabel(step, transforms)} up`}
                    disabled={index === 0} onClick={() => moveStep(index, index - 1)}><ArrowUp /></Button>
                  <Button type="button" variant="ghost" size="icon-xs" aria-label={`Move ${stepLabel(step, transforms)} down`}
                    disabled={index === profile.steps.length - 1} onClick={() => moveStep(index, index + 1)}><ArrowDown /></Button>
                  <Button type="button" variant="ghost" size="icon-xs" aria-label={`Remove ${stepLabel(step, transforms)}`}
                    onClick={() => update({ steps: profile.steps.filter((_, at) => at !== index) })}><Trash2 /></Button>
                </div>
              </div>
            </li>)}
          </ol>}
          <label>Add a step
            <select value="" disabled={profile.steps.length >= 20} onChange={event => update({ steps: [...profile.steps, event.target.value] })}>
              <option value="" disabled>Choose a step…</option>
              {stepOptions.map(([key, name]) => <option value={key} key={key}>{name}</option>)}
            </select>
          </label>
          <label>Profile vocabulary
            <small>One term per line. These terms get priority over shared vocabulary.</small>
            <textarea dir="auto" rows={3} maxLength={3000} value={profile.vocabulary.join("\n")}
              onChange={event => update({ vocabulary: event.target.value.split("\n") })} />
          </label>
          <label>Profile word replacements
            <small>Enter one phrase =&gt; replacement per line. Leave the replacement blank to delete a phrase.</small>
            <small>{profile.steps.includes("replace")
              ? "Shared replacements run first at the Apply word replacements step."
              : "Shared and profile replacements run before the ordered steps."}</small>
            <textarea dir="auto" rows={3} maxLength={4000} value={profile.corrections}
              onChange={event => update({ corrections: event.target.value })} />
          </label>
          {(nativeScript || profile.steps.includes("rewrite")) && <label>Profile rewrite instructions
            <small>{nativeScript ? "Applies when this profile uses the local Qwen step. Leave empty to use shared instructions."
              : "Qwen runs locally before pasting. Describe how the text should read."}</small>
            <textarea dir="auto" rows={3} maxLength={2500} value={profile.instruction}
              onChange={event => update({ instruction: event.target.value })} />
          </label>}
          {profile.steps.includes("rewrite") && <label>Rewrite deadline (seconds)
            <small>Jaro pastes the text from before the rewrite if the deadline expires.</small>
            <input type="number" min={1} max={60} step={1} value={profile.deadline || ""}
              onChange={event => update({ deadline: Number(event.target.value) })} />
          </label>}
          <Button type="button" size="sm" variant="ghost" onClick={() => {
            setDraft(rows.filter((_, index) => index !== selected))
            setSelected(Math.max(0, selected - 1))
            setPreview(undefined)
          }}>Remove profile</Button>
        </fieldset>}
        {nativeScript && <div className="dictation-profile-toolbar">
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void nativeScript.run({ action: "chooseOutputScript" })}>
            {nativeScript.path || "Choose a local script…"}
          </Button>
          {nativeScript.path && <Button type="button" size="sm" variant="ghost" disabled={busy}
            onClick={() => void nativeScript.run({ action: "clearOutputScript" })}>Remove script</Button>}
        </div>}
        <details><summary>Preview with sample text</summary>
          <div className="dictation-options dictation-profile-card">
            <label>Sample<textarea dir="auto" rows={3} value={sample} disabled={busy}
              onChange={event => { setSample(event.target.value); setPreview(undefined) }} maxLength={8000} /></label>
            <label>{nativeScript ? "Sample app name or bundle ID" : "Sample application name"}<input value={app} disabled={busy}
              onChange={event => { setApp(event.target.value); setPreview(undefined) }} maxLength={300} /></label>
            {websites && <label>Sample website host or URL<input value={site} disabled={busy}
              onChange={event => { setSite(event.target.value); setPreview(undefined) }} maxLength={300} /></label>}
            <small>{nativeScript ? "Preview skips the executable script." : "Preview runs the text steps. Qwen runs during dictation."}</small>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void apply(false)}>Preview</Button>
            {preview && <div className="dictation-profile-preview" aria-live="polite">
              <strong>{preview.profile ? profileName(preview.profile) : "No matching profile"}</strong>
              {preview.vocabulary.length > 0 && <p>Vocabulary · <span dir="auto">{preview.vocabulary.join(", ")}</span></p>}
              <p dir="auto">{preview.text || "No output"}</p>
              {preview.warnings.map(warning => <p className="dictation-error" key={warning}>{warning}</p>)}
            </div>}
          </div>
        </details>
        <div className="dictation-profile-toolbar">
          <Button type="button" size="sm" variant="outline" disabled={busy || !draft || definition === JSON.stringify(saved?.rows) || definition === value}
            onClick={() => void apply(true)}>Save profiles</Button>
          {draft && <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => {
            setDraft(undefined)
            setSelected(0)
            setPreview(undefined)
            setError("")
          }}>Discard changes</Button>}
        </div>
      </>}
      {error && <p role="alert" className="dictation-error">{error}</p>}
    </div>}
  </details>
}
