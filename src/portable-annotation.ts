import type { AnnotationAgent, AnnotationDelivery, AnnotationDestinations, AnnotationRouteScope, AnnotationRouteStatus, AnnotationRulesAction, SavedAnnotationRule } from "@/desktop"

export async function portableAnnotationRules(action: AnnotationRulesAction): Promise<{ managed: boolean; rules: SavedAnnotationRule[] }> {
  const response = await fetch("/api/portable/annotation-rules", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(action),
  })
  if (!response.ok) {
    const result = await response.json().catch(() => null)
    throw new Error(typeof result?.detail === "string" ? result.detail : "Could not manage OpenCode destinations.")
  }
  return response.json()
}

type HandoffRequests = {
  destinations: { query: string; id?: string; agent?: AnnotationAgent }
  "send-session": { id: string; target: string; service?: string; agent?: AnnotationAgent }
  deliveries: { id: string }
  undo: { id: string }
  correct: { id: string }
  remember: { id: string; scope: AnnotationRouteScope["kind"]; scopeKey?: string; target: string; service: string; automatic?: boolean }
  forget: { id: string; ruleId: string }
  route: { id: string; routeAction: "status" | "cancel" }
}

type HandoffResponses = {
  destinations: AnnotationDestinations
  "send-session": { images: "attached" | "references" | "none" }
  deliveries: { deliveries: AnnotationDelivery[] }
  undo: AnnotationDelivery
  correct: AnnotationDelivery
  remember: unknown
  forget: { forgotten: boolean }
  route: AnnotationRouteStatus
}

export async function portableAnnotationHandoff<A extends keyof HandoffRequests>(
  action: A, request: HandoffRequests[A], signal?: AbortSignal,
): Promise<HandoffResponses[A]> {
  const response = await fetch("/api/portable/annotation-handoff", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...request }), signal,
  })
  if (!response.ok) {
    const result = await response.json().catch(() => null)
    throw new Error(typeof result?.detail === "string" ? result.detail : "Could not complete the agent handoff.")
  }
  return response.json()
}
