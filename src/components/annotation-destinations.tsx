import { useEffect, useId, useRef, useState } from "react"
import { Search } from "lucide-react"
import { AlertDialog, Dialog, Popover, Tooltip } from "radix-ui"
import { useInterfaceLanguage } from "@/interface-language"
import { portableAnnotationHandoff } from "@/portable-annotation"
import type { AnnotationAgent, AnnotationDelivery, AnnotationDestinations, AnnotationRouteStatus, DesktopBridge } from "@/desktop"
import claudeLogo from "@/assets/agent-logos/claude-code.png"
import codexDarkLogo from "@/assets/agent-logos/codex-dark.png"
import codexLightLogo from "@/assets/agent-logos/codex-light.png"
import openCodeDarkLogo from "@/assets/agent-logos/opencode-dark.svg"
import openCodeLightLogo from "@/assets/agent-logos/opencode-light.svg"
import t3Logo from "@/assets/agent-logos/t3-code.svg"
import { Button } from "./ui/button"

const annotationAgents: Record<AnnotationAgent, { name: string; logo: string; darkLogo?: string }> = {
  claude: { name: "Claude Code", logo: claudeLogo },
  codex: { name: "Codex", logo: codexLightLogo, darkLogo: codexDarkLogo },
  opencode: { name: "OpenCode", logo: openCodeLightLogo, darkLogo: openCodeDarkLogo },
  t3: { name: "T3 Code", logo: t3Logo },
}
const agentOrder: AnnotationAgent[] = ["claude", "codex", "opencode", "t3"]

function AgentLogo({ agent }: { agent: AnnotationAgent }) {
  const { logo, darkLogo } = annotationAgents[agent]
  return <picture aria-hidden="true">
    {darkLogo && <source media="(prefers-color-scheme: dark)" srcSet={darkLogo} />}
    <img src={logo} alt="" width={20} height={20} draggable={false} className="annotation-agent-logo" />
  </picture>
}

type AgentPickerProps = {
  native?: DesktopBridge
  id: string
  annotation: boolean
  disabled: boolean
  allowOpenCode: boolean
  onChanged: (agent: AnnotationAgent, status?: string) => void
}

export function AnnotationAgentPicker({ native, id, annotation, disabled, allowOpenCode, onChanged }: AgentPickerProps) {
  const { t } = useInterfaceLanguage()
  const stack = useRef<HTMLButtonElement>(null)
  const handingOff = useRef(false)
  const [expanded, setExpanded] = useState(false)
  const [agent, setAgent] = useState<AnnotationAgent>()
  const label = t("Send to your agent")

  return <>
    <Tooltip.Provider delayDuration={150} skipDelayDuration={300}>
      <Popover.Root open={expanded} onOpenChange={next => {
        if (next) handingOff.current = false
        setExpanded(next)
      }}>
        <Tooltip.Root>
          <Tooltip.Trigger asChild>
            <Popover.Trigger asChild>
              <Button ref={stack} variant="outline" size="icon-sm" aria-label={label} disabled={disabled}
                className="annotation-agent-stack">
                <span aria-hidden="true" className="annotation-agent-stack-fan">
                  {agentOrder.map(provider => <span key={provider} className="annotation-agent-stack-card">
                    <AgentLogo agent={provider} />
                  </span>)}
                </span>
              </Button>
            </Popover.Trigger>
          </Tooltip.Trigger>
          <Tooltip.Portal>
            <Tooltip.Content sideOffset={6} className="annotation-agent-tooltip">{label}</Tooltip.Content>
          </Tooltip.Portal>
        </Tooltip.Root>
        <Popover.Portal>
          <Popover.Content align="end" side="top" sideOffset={7} collisionPadding={12} aria-label={label}
            className="annotation-agent-popover"
            onKeyDown={event => {
              // A focused logo's tooltip can receive Escape before the popover does.
              if (event.key === "Escape") setExpanded(false)
            }}
            onCloseAutoFocus={event => {
              if (handingOff.current) event.preventDefault()
            }}>
            {agentOrder.map(provider => {
              const name = annotationAgents[provider].name
              return <Tooltip.Root key={provider}>
                <Tooltip.Trigger asChild>
                  <Button variant="ghost" size="icon-sm" aria-label={`Send to ${name}…`}
                    disabled={disabled || (provider === "opencode" && !allowOpenCode)}
                    onClick={() => {
                      handingOff.current = true
                      setExpanded(false)
                      setAgent(provider)
                    }}>
                    <AgentLogo agent={provider} />
                  </Button>
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Content sideOffset={6} className="annotation-agent-tooltip">Send to {name}</Tooltip.Content>
                </Tooltip.Portal>
              </Tooltip.Root>
            })}
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </Tooltip.Provider>
    {agent && <AnnotationDestinationPicker key={agent} native={native} id={id} agent={agent} annotation={annotation}
      disabled={disabled || (agent === "opencode" && !allowOpenCode)} onChanged={status => onChanged(agent, status)}
      onClose={() => setAgent(undefined)} onRestoreFocus={() => stack.current?.focus({ preventScroll: true })} />}
  </>
}

function AnnotationDestinationPicker({ native, id, agent, annotation, disabled, onChanged, onClose, onRestoreFocus }: {
  native?: DesktopBridge
  id: string
  agent: AnnotationAgent
  annotation: boolean
  disabled: boolean
  onChanged: (status?: string) => void
  onClose: () => void
  onRestoreFocus: () => void
}) {
  const { name } = annotationAgents[agent]
  const search = useRef<HTMLInputElement>(null)
  const group = useId()
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<AnnotationDestinations>()
  const [target, setTarget] = useState("")
  const [failure, setFailure] = useState("")
  const [working, setWorking] = useState<"send" | "remember" | "forget">()
  const [ruleScope, setRuleScope] = useState<string>()
  const [automatic, setAutomatic] = useState(false)
  const sending = working !== undefined
  const scope = results?.scopes?.find(item => `${item.kind}:${item.key}` === ruleScope) ?? results?.scopes?.[0]
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let current = true
    const controller = new AbortController()
    const wait = window.setTimeout(() => {
      const request = !native
        ? portableAnnotationHandoff("destinations", { query, agent, ...(annotation && { id }) }, controller.signal)
        : agent === "opencode"
        ? native.request("annotationDestinations", { query, ...(annotation && { id }) })
        : native.request("annotationAgentDestinations", { agent, query }).then(list => ({ ...list, service: null }))
      void request.then(list => {
        if (current) setResults(list)
      }).catch(error => {
        if (current) setFailure(error instanceof Error ? error.message : `Could not list ${name} sessions.`)
      })
    }, query ? 250 : 0)
    return () => { current = false; controller.abort(); window.clearTimeout(wait) }
  }, [native, query, attempt, id, agent, name, annotation])

  function reset() {
    setTarget("")
    setResults(undefined)
    setFailure("")
  }

  async function deliver() {
    const selected = results?.sessions.find(item => item.id === target)
    const service = results?.service ?? ""
    if (!selected || sending || disabled || (agent === "opencode" && !service)) return
    setWorking("send")
    setFailure("")
    try {
      const delivery = native
        ? await native.request("sendSessionToAgent", { agent, id, target, service: service || undefined, open: true })
        : await portableAnnotationHandoff("send-session", { agent, id, target, service: service || undefined })
      const shared = delivery.images === "attached" ? "Session context and images sent"
        : delivery.images === "references" ? "Session context and image references sent" : "Session context sent"
      onChanged(`${shared} to ${selected.title} in ${name}.`)
      onClose()
    } catch (error) {
      if (agent === "opencode") onChanged()
      setFailure(error instanceof Error ? error.message : "Could not send the session context.")
    } finally {
      setWorking(undefined)
    }
  }

  async function editRule(action: "remember" | "forget") {
    if (!results || sending || disabled) return
    setFailure("")
    setWorking(action)
    try {
      if (action === "remember" && scope && target && results.service) {
        const choice = { id, service: results.service, target, scope: scope.kind, scopeKey: scope.key }
        if (native) await native.request("rememberAnnotationDestination", { ...choice, automatic })
        else await portableAnnotationHandoff("remember", { ...choice, automatic })
      } else if (action === "forget" && results.rule) {
        const choice = { id, ruleId: results.rule.id }
        if (native) await native.request("forgetAnnotationDestination", choice)
        else await portableAnnotationHandoff("forget", choice)
      }
      reset()
      setAttempt(value => value + 1)
    } catch (error) {
      setFailure(error instanceof Error ? error.message : "Could not update the saved destination.")
    } finally {
      setWorking(undefined)
    }
  }

  return <Dialog.Root open onOpenChange={next => { if (!next && !sending) onClose() }}>
    <Dialog.Portal>
      <Dialog.Overlay className="note-language-scrim" />
      <Dialog.Content className="note-language-confirm annotation-destinations"
        onOpenAutoFocus={event => { event.preventDefault(); search.current?.focus() }}
        onCloseAutoFocus={event => { event.preventDefault(); onRestoreFocus() }}>
        <Dialog.Title>Send to {name}</Dialog.Title>
        <Dialog.Description>{agent === "t3"
          ? "Send the overview, transcript, My Notes, and paths to local images to an existing chat."
          : "Send the overview, transcript, My Notes, and available images to an existing chat."}</Dialog.Description>
        <label className="annotation-destination-search">
          <Search aria-hidden="true" />
          <input ref={search} aria-label={`Search ${name} sessions`} placeholder="Search title, project, or session ID"
            maxLength={300} value={query} disabled={sending} onChange={event => { reset(); setQuery(event.target.value) }} />
        </label>
        {failure && <p role="alert">{failure}</p>}
        {!results && !failure && <p role="status">Looking for {name} sessions…</p>}
        {results?.warning && <p role="status">{results.warning}</p>}
        {results && !results.warning && !results.sessions.length && <p role="status">{query ? "No matching sessions." : "No open sessions."}</p>}
        {!!results?.sessions.length && <div role="radiogroup" aria-label={`${name} sessions`} className="annotation-destination-list">
          {results.sessions.map(session => <label key={session.id} className="annotation-destination-row">
            <input type="radio" name={group} checked={target === session.id} disabled={sending}
              onChange={() => { setFailure(""); setTarget(session.id) }} />
            <span>
              <strong>{session.title}</strong>
              {session.directory && <small title={session.directory}>{session.directory}</small>}
              {results.suggestion?.sessionId === session.id && <small className="annotation-destination-reason">{results.suggestion.reason}</small>}
            </span>
          </label>)}
        </div>}
        {scope && <div className="annotation-destination-rule">
          {results?.rule && <div>
            <p role="status">{results.rule.title} {results.rule.available ? "· Saved for " : "· Unavailable · Saved for "}
              {results.rule.scope.label}{results.rule.automatic && " · Automatic"}</p>
            <Button size="sm" variant="ghost" disabled={sending || disabled} onClick={() => void editRule("forget")}>Forget</Button>
          </div>}
          {target && <div>
            <label>Use this destination for
              <select aria-label="Destination scope" value={`${scope.kind}:${scope.key}`} disabled={sending}
                onChange={event => setRuleScope(event.target.value)}>
                {results?.scopes?.map(item => {
                  const value = `${item.kind}:${item.key}`
                  return <option key={value} value={value}>{item.label}</option>
                })}
              </select>
            </label>
            <Button size="sm" variant="outline" disabled={sending || disabled || !results?.service} onClick={() => void editRule("remember")}>Remember destination</Button>
          </div>}
          {target && <label className="annotation-auto-option">
            <input type="checkbox" checked={automatic} disabled={sending} onChange={event => setAutomatic(event.target.checked)} />
            Automatically send future matching annotations after a countdown
          </label>}
        </div>}
        <div className="note-language-actions">
          <Button variant="ghost" size="sm" disabled={sending} onClick={() => { reset(); setAttempt(value => value + 1) }}>Refresh</Button>
          <Dialog.Close asChild><Button variant="outline" size="sm" disabled={sending}>Cancel</Button></Dialog.Close>
          <Button size="sm" disabled={!target || sending || disabled || (agent === "opencode" && !results?.service)} onClick={() => void deliver()}>{working === "send" ? "Sending…" : "Send context"}</Button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

const outcome: Record<AnnotationDelivery["status"], string> = {
  queued: "Queued",
  withdrawn: "Withdrawn",
  received: "Already in the conversation",
  missing: "No longer in the OpenCode inbox",
  unconfirmed: "Delivery unconfirmed",
  "withdrawal-unconfirmed": "Withdrawal unconfirmed",
  refused: "Refused by OpenCode",
  corrected: "Correction sent",
  "correction-unconfirmed": "Correction unconfirmed",
  reverted: "Turn rolled back",
  "rollback-files": "File restoration needs confirmation",
  "rollback-staged": "Revert staged in OpenCode",
  "rollback-unconfirmed": "Rollback unconfirmed",
  "rollback-commit-unconfirmed": "Rollback unconfirmed",
}

const deliveryTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" })

const rememberedOutcome = {
  pending: "",
  saved: "Automatic destination saved",
  changed: "Destination changed separately",
  restored: "Previous destination restored",
  removed: "Automatic destination removed",
}

export function AnnotationDeliveryLog({ native, id, revision, remoteEnabled }: {
  native?: DesktopBridge; id: string; revision: number; remoteEnabled: boolean
}) {
  const [deliveries, setDeliveries] = useState<AnnotationDelivery[]>([])
  const [working, setWorking] = useState("")
  const [confirm, setConfirm] = useState<{ item: AnnotationDelivery; action: "correct" | "restore" }>()
  const [route, setRoute] = useState<AnnotationRouteStatus>()
  const [routeRevision, setRouteRevision] = useState(0)
  const [error, setError] = useState("")
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!native) {
      const controller = new AbortController()
      let timer: number
      let phase: AnnotationRouteStatus["phase"] | undefined
      let receiptStatus: AnnotationRouteStatus["receiptStatus"]
      async function poll() {
        try {
          const result = await portableAnnotationHandoff("route", { id, routeAction: "status" }, controller.signal)
          if (controller.signal.aborted) return
          setRoute(result)
          if ((phase !== result.phase || receiptStatus !== result.receiptStatus) &&
            (result.phase === "sent" || result.phase === "unconfirmed")) {
            setRouteRevision(value => value + 1)
          }
          phase = result.phase
          receiptStatus = result.receiptStatus
        } catch {
          // The ordinary delivery log remains available if routing is unavailable.
        }
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 1000)
      }
      void poll()
      return () => { controller.abort(); window.clearTimeout(timer) }
    }
    let valid = true
    let latest = false
    const received = (event: WindowEventMap["native-sdk:jard-annotation-routing"]) => {
      if (event.detail.capture !== id) return
      latest = true
      setRoute(event.detail)
      if (event.detail.phase === "sent") setRouteRevision(value => value + 1)
    }
    const changed = (event: WindowEventMap["native-sdk:jard-annotation-delivery-changed"]) => {
      if (event.detail.capture === id) setRouteRevision(value => value + 1)
    }
    window.addEventListener("native-sdk:jard-annotation-routing", received)
    window.addEventListener("native-sdk:jard-annotation-delivery-changed", changed)
    void native.request("annotationRoute", { id, action: "status" }).then(result => {
      if (valid && !latest) setRoute(result)
    }).catch(() => {})
    return () => {
      valid = false
      window.removeEventListener("native-sdk:jard-annotation-routing", received)
      window.removeEventListener("native-sdk:jard-annotation-delivery-changed", changed)
    }
  }, [native, id])

  useEffect(() => {
    let current = true
    const controller = new AbortController()
    const request = native
      ? native.request("annotationDeliveries", { id })
      : portableAnnotationHandoff("deliveries", { id }, controller.signal)
    void request.then(result => {
      if (current) setDeliveries(result.deliveries)
    }).catch(error => {
      if (current) setError(error instanceof Error ? error.message : "Could not read annotation deliveries.")
    })
    return () => { current = false; controller.abort() }
  }, [native, id, revision, routeRevision])

  useEffect(() => {
    const next = Math.min(...deliveries.filter(item => item.canUndo && item.undoUntil > now).map(item => item.undoUntil))
    if (!Number.isFinite(next)) return
    const wait = window.setTimeout(() => setNow(Date.now()), Math.max(0, next - Date.now()) + 10)
    return () => window.clearTimeout(wait)
  }, [deliveries, now])

  async function changeDelivery(deliveryId: string, action: "undoAnnotationDelivery" | "correctAnnotationDelivery", restoreFiles = false) {
    if (!remoteEnabled || working) return
    setError("")
    setWorking(deliveryId)
    try {
      const result = !native
        ? await portableAnnotationHandoff(action === "undoAnnotationDelivery" ? "undo" : "correct", { id: deliveryId })
        : action === "undoAnnotationDelivery"
        ? await native.request(action, { deliveryId, ...(restoreFiles && { restoreFiles: true }) })
        : await native.request(action, { deliveryId })
      setDeliveries(rows => rows.map(item => item.deliveryId === deliveryId ? result : item))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not update the annotation delivery.")
    } finally {
      setWorking("")
    }
  }

  const routePending = route && ["transcribing", "waiting", "offer", "countdown", "held", "preparing"].includes(route.phase)
  const routeMessage = route?.phase === "held" ? "Countdown paused."
    : route?.phase === "countdown" || route?.phase === "offer" ? "Automatic send countdown."
      : route?.message

  return <>
    {route?.capture === id && route.title && routeMessage && <div className="annotation-route-status">
      <p role="status"><strong>{route.title}</strong> · {routeMessage}</p>
      {routePending && <Button variant="outline" size="sm" onClick={() => {
        const request = native ? native.request("annotationRoute", { id, action: "cancel" })
          : portableAnnotationHandoff("route", { id, routeAction: "cancel" })
        void request.then(setRoute).catch(failure => {
          setError(failure instanceof Error ? failure.message : "Could not cancel the automatic send.")
        })
      }}>Cancel automatic send</Button>}
    </div>}
    {!!deliveries.length && <section className="annotation-deliveries" aria-label="OpenCode deliveries">
      <h3>OpenCode deliveries</h3>
      <ul>{deliveries.map(item => {
        const canUndo = item.canUndo && item.undoUntil > now &&
          (native || item.status === "queued" || item.status === "withdrawal-unconfirmed")
        const needsReview = native && (item.status === "rollback-staged" || item.status === "rollback-unconfirmed"
          || item.status === "rollback-commit-unconfirmed")
        const restoring = item.status === "rollback-files"
        return <li key={item.deliveryId}>
          <div>
            <strong>{item.session.title}</strong>
            <p role="status">{outcome[item.status]} · <time dateTime={new Date(item.createdAt).toISOString()}>
              {deliveryTime.format(item.createdAt)}
            </time></p>
            {item.rememberedRule && rememberedOutcome[item.rememberedRule.status] && <p>
              {rememberedOutcome[item.rememberedRule.status]} · {item.rememberedRule.label}
            </p>}
            {needsReview && <p>Review any staged revert in OpenCode before sending another prompt.</p>}
          </div>
          <div className="annotation-delivery-actions">
            {(canUndo || needsReview) && <Button variant="outline" size="sm" disabled={!remoteEnabled || !!working} onClick={() => {
              if (restoring) setConfirm({ item, action: "restore" })
              else void changeDelivery(item.deliveryId, "undoAnnotationDelivery")
            }}>
              {working === item.deliveryId ? "Checking…" : needsReview ? "Check rollback status" : restoring ? "Restore files and undo…" : "Undo send"}
            </Button>}
            {item.canCorrect && (!canUndo || restoring) && <Button variant="outline" size="sm" disabled={!remoteEnabled || !!working}
              onClick={() => setConfirm({ item, action: "correct" })}>
              {working === item.deliveryId ? "Sending…" : "Send correction…"}
            </Button>}
          </div>
        </li>
      })}</ul>
    </section>}
    {error && <p role="alert">{error}</p>}
    <AlertDialog.Root open={Boolean(confirm)} onOpenChange={open => { if (!open) setConfirm(undefined) }}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="note-language-scrim" />
        <AlertDialog.Content className="note-language-confirm">
          <AlertDialog.Title>{confirm?.action === "restore" ? "Restore files and undo this turn?" : "Send a correction?"}</AlertDialog.Title>
          <AlertDialog.Description>{confirm?.action === "restore"
            ? "OpenCode can restore files changed during this turn, including edits you made outside OpenCode."
            : `Ask ${confirm?.item.session.title} to disregard this annotation.`}</AlertDialog.Description>
          <div className="note-language-actions">
            <AlertDialog.Cancel asChild><Button size="sm" variant="outline">Cancel</Button></AlertDialog.Cancel>
            <AlertDialog.Action asChild><Button size="sm" onClick={() => {
              if (!confirm) return
              if (confirm.action === "restore") void changeDelivery(confirm.item.deliveryId, "undoAnnotationDelivery", true)
              else void changeDelivery(confirm.item.deliveryId, "correctAnnotationDelivery")
            }}>{confirm?.action === "restore" ? "Restore files and undo" : "Send correction"}</Button></AlertDialog.Action>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  </>
}
