import { useEffect, useState } from "react"
import { AppWindow, FolderOpen, Globe, Send } from "lucide-react"
import { Dialog } from "radix-ui"
import type { AnnotationDestinations, AnnotationRouteScope, AnnotationRuleDestinationChange, AnnotationRulesAction, DesktopBridge, SavedAnnotationRule } from "@/desktop"
import { portableAnnotationHandoff, portableAnnotationRules } from "@/portable-annotation"
import { useInterfaceLanguage } from "@/interface-language"
import { SettingGroup, SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

const projectPrefix = "project:"

export function AnnotationRules({ native, disabled = false }: { native?: DesktopBridge; disabled?: boolean }) {
  const { t } = useInterfaceLanguage()
  const [rules, setRules] = useState<SavedAnnotationRule[]>()
  const [managed, setManaged] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  const [editing, setEditing] = useState<SavedAnnotationRule | "new">()
  const title = t("Auto-send rules")

  async function update(action: AnnotationRulesAction) {
    setPending(true)
    setError("")
    try {
      const result = native ? await native.request("annotationRules", action) : await portableAnnotationRules(action)
      setManaged(result.managed)
      setRules(result.rules)
      return true
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("Could not read the annotation rules."))
      return false
    } finally { setPending(false) }
  }

  return <SettingGroup title={t("After an annotation")} id="annotation-routing">
    <Dialog.Root onOpenChange={open => { if (open) { setRules(undefined); setEditing(undefined); void update({ action: "list" }) } }}>
      <SettingRow icon={Send} title={title} label={false}
        detail={t("Choose which saved OpenCode chats receive annotations automatically.")}>
        <Dialog.Trigger asChild><Button variant="outline" size="sm" disabled={pending || disabled}>{t("Manage rules")}</Button></Dialog.Trigger>
      </SettingRow>
      <Dialog.Portal>
        <Dialog.Overlay className="note-language-scrim" />
        <Dialog.Content className="note-language-confirm meeting-calls-dialog dictation-preferences">
          <header className="meeting-calls-header">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              <Dialog.Description>{t("Add a suggested chat for matching annotations. You can enable automatic delivery for a saved rule.")}</Dialog.Description>
            </div>
            <Dialog.Close asChild><Button variant="outline" size="sm">{t("Done")}</Button></Dialog.Close>
          </header>
          <div className="meeting-calls-body">
            {!managed && rules && !editing && <div className="note-language-actions">
              <Button size="sm" variant="outline" disabled={pending || disabled}
                onClick={() => { setError(""); setEditing("new") }}>{t("Add rule")}</Button>
            </div>}
            {managed ? <p role="status">{t("OpenCode is disabled by your administrator.")}</p>
              : editing ? <EditAnnotationRule key={editing === "new" ? "new" : editing.id} native={native}
                rule={editing === "new" ? undefined : editing} disabled={pending || disabled}
                onCancel={() => { setEditing(undefined); setError("") }} onSave={async action => {
                  if (await update(action)) setEditing(undefined)
                }} />
              : rules?.length ? <SettingGroup id="annotation-saved-rules" title={t("Saved destinations")}>
                {rules.map(rule => <SettingRow key={`${rule.kind}:${rule.source}`} title={rule.source} context={rule.kind} find={false} label={false}
                  icon={rule.kind === "site" ? Globe : rule.kind === "terminal" ? FolderOpen : AppWindow}
                  detail={<span id={`annotation-chat-${rule.id}`}>{rule.title}</span>}>
                  <div className="dictation-row-actions">
                    <select aria-label={t("Delivery for {source}", { source: rule.source })} aria-describedby={`annotation-chat-${rule.id}`}
                      value={rule.automatic ? "automatic" : "suggest"}
                      disabled={pending} onChange={event => void update({ action: "setAutomatic", id: rule.id, enabled: event.target.value === "automatic" })}>
                      <option value="suggest">{t("Suggest")}</option><option value="automatic">{t("Send automatically")}</option>
                    </select>
                    <Button size="sm" variant="outline" disabled={pending || disabled}
                      aria-label={t(rule.target.startsWith(projectPrefix) ? "Change project for {source}" : "Change chat for {source}",
                        { source: rule.source })}
                      aria-describedby={`annotation-chat-${rule.id}`}
                      onClick={() => { setError(""); setEditing(rule) }}>
                      {t(rule.target.startsWith(projectPrefix) ? "Change project" : "Change chat")}
                    </Button>
                    <Button size="sm" variant="ghost" disabled={pending || disabled} aria-label={t("Remove rule for {source}", { source: rule.source })}
                      onClick={() => void update({ action: "remove", id: rule.id })}>{t("Remove")}</Button>
                  </div>
                </SettingRow>)}
              </SettingGroup> : !error && <p role="status">{rules ? t("No saved destinations.") : t("Loading…")}</p>}
            {error && <div>
              <p role="alert">{error}</p>
              <Button size="sm" variant="outline" disabled={pending} onClick={() => {
                setEditing(undefined)
                void update({ action: "list" })
              }}>{t("Refresh rules")}</Button>
            </div>}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </SettingGroup>
}

function EditAnnotationRule({ native, rule, disabled, onCancel, onSave }: {
  native?: DesktopBridge; rule?: SavedAnnotationRule; disabled: boolean; onCancel: () => void
  onSave: (action: AnnotationRuleDestinationChange) => Promise<void>
}) {
  const { t } = useInterfaceLanguage()
  const [kind, setKind] = useState<AnnotationRouteScope["kind"]>("site")
  const [source, setSource] = useState("")
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<AnnotationDestinations>()
  const [target, setTarget] = useState("")
  const [project, setProject] = useState(rule?.target.startsWith(projectPrefix) ?? false)
  const [folder, setFolder] = useState(rule?.target.startsWith(projectPrefix) ? rule.target.slice(projectPrefix.length) : "")
  const [failure, setFailure] = useState("")

  useEffect(() => {
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      const request = native ? native.request("annotationDestinations", { query })
        : portableAnnotationHandoff("destinations", { query }, controller.signal)
      void request.then(reply => {
        if (!controller.signal.aborted) setResults(reply)
      }).catch(error => {
        if (!controller.signal.aborted) setFailure(error instanceof Error ? error.message : "Could not list OpenCode chats.")
      })
    }, query ? 250 : 0)
    return () => { controller.abort(); window.clearTimeout(timer) }
  }, [native, query])

  const sourceLabel = kind === "site" ? t("Website domain or path") : kind === "terminal" ? t("Project folder") : t("Application identifier")
  const sourceExample = kind === "site" ? "github.com/org/repo" : kind === "terminal" ? "~/Projects/notes"
    : native ? "com.example.app" : "example-app"
  const folders = [...new Set(results?.sessions.map(chat => chat.directory).filter(Boolean))]
  const selected = project ? folder.trim() : target
  return <SettingGroup id="annotation-change-destination"
    title={rule ? t("Change destination for {source}", { source: rule.source }) : t("New rule")}>
    {!rule && <>
      <SettingRow icon={kind === "site" ? Globe : kind === "terminal" ? FolderOpen : AppWindow} title={t("Match")}>
        {labels => <select {...labels} value={kind} disabled={disabled} onChange={event => {
          setKind(event.target.value as AnnotationRouteScope["kind"])
          setSource("")
        }}>
          <option value="site">{t("Website")}</option>
          <option value="app">{t("Application")}</option>
          <option value="terminal">{t("Terminal folder")}</option>
        </select>}
      </SettingRow>
      <label className="annotation-destination-search">
        <input aria-label={sourceLabel} placeholder={sourceExample} maxLength={kind === "terminal" ? 4096 : kind === "site" ? 2048 : 255}
          value={source} disabled={disabled} spellCheck={false} onChange={event => setSource(event.target.value)} />
      </label>
    </>}
    <SettingRow icon={project ? FolderOpen : Send} title={t("Destination")}>
      {labels => <select {...labels} value={project ? "project" : "chat"} disabled={disabled}
        onChange={event => setProject(event.target.value === "project")}>
        <option value="chat">{t("Specific chat")}</option>
        <option value="project">{t("Project’s newest chat")}</option>
      </select>}
    </SettingRow>
    <label className="annotation-destination-search">
      <input aria-label={t("Search OpenCode sessions")} placeholder={t("Search title, project, or session ID")}
        maxLength={300} disabled={disabled} value={query} onChange={event => {
          setTarget(""); setResults(undefined); setFailure(""); setQuery(event.target.value)
        }} />
    </label>
    {!results && !failure && <p role="status">{t("Looking for OpenCode sessions…")}</p>}
    {results?.warning && <p role="status">{results.warning}</p>}
    {results && !results.warning && !results.sessions.length && <p role="status">{t("No matching sessions.")}</p>}
    {project && <>
      {folders.length > 0 && <select aria-label={t("Known OpenCode projects")} value={folders.includes(folder) ? folder : ""}
        disabled={disabled} onChange={event => setFolder(event.target.value)}>
        <option value="">{t("Choose a project folder")}</option>
        {folders.map(path => <option key={path} value={path}>{path}</option>)}
      </select>}
      <label className="annotation-destination-search">
        <input aria-label={t("OpenCode project folder")} placeholder={t("Absolute folder path")} maxLength={4096}
          value={folder} disabled={disabled} spellCheck={false} onChange={event => setFolder(event.target.value)} />
      </label>
    </>}
    {!project && results?.sessions.length ? <select aria-label={t("OpenCode chat")} value={target} disabled={disabled}
      onChange={event => setTarget(event.target.value)}>
      <option value="">{t("Choose a chat")}</option>
      {results.sessions.map(chat => <option key={chat.id} value={chat.id}>{chat.directory ? `${chat.title} · ${chat.directory}` : chat.title}</option>)}
    </select> : null}
    {failure && <p role="alert">{t(failure)}</p>}
    <div className="note-language-actions">
      <Button size="sm" variant="outline" disabled={disabled} onClick={onCancel}>{t("Cancel")}</Button>
      <Button size="sm" disabled={disabled || !selected || !results?.service || (!rule && !source.trim())} onClick={() => {
        if (!results?.service) return
        const destination = { target: selected, service: results.service, ...(project && { project: true as const }) }
        void onSave(rule ? { action: "retarget", id: rule.id, ...destination }
          : { action: "add", kind, source, ...destination })
      }}>{t("Save destination")}</Button>
    </div>
  </SettingGroup>
}
