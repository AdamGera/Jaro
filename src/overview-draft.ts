import type { JSONContent } from "@tiptap/core"

export function overviewDraftKey(session: string, language: string) {
  return `jard.overviewDraft.${session}.${language}`
}

export function readOverviewDraft(key: string) {
  let draft: { revision?: unknown; document?: JSONContent } | null
  try {
    draft = JSON.parse(localStorage.getItem(key) ?? "null")
  } catch {
    return null
  }
  return typeof draft?.revision === "string" && draft.document?.type === "doc" && Array.isArray(draft.document.content)
    ? { revision: draft.revision, document: draft.document }
    : null
}

export function rememberOverviewDraft(key: string, draft: { revision: string; document: JSONContent } | null) {
  try {
    if (draft) localStorage.setItem(key, JSON.stringify(draft))
    else localStorage.removeItem(key)
    return true
  } catch {
    return false
  }
}
