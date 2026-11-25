import type { JSONContent } from "@tiptap/core"
import { readWorkspaceDraft } from "./workspace-drafts"

export function overviewDraftKey(session: string, language: string) {
  return `jard.overviewDraft.${session}.${language}`
}

export function readOverviewDraft(key: string) {
  const draft = readWorkspaceDraft(key) as { revision?: unknown; document?: JSONContent } | null
  return typeof draft?.revision === "string" && draft.document?.type === "doc" && Array.isArray(draft.document.content)
    ? { revision: draft.revision, document: draft.document }
    : null
}
