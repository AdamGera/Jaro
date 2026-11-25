import type { JSONContent } from "@tiptap/react"
import { readWorkspaceDraft, rememberWorkspaceDraft } from "./workspace-drafts"

export type PersonalNotes = { document: JSONContent; revision: number }

export const EMPTY_NOTES: JSONContent = { type: "doc", content: [{ type: "paragraph" }] }

export function readNoteDraft(key: string): PersonalNotes | null {
  const draft = readWorkspaceDraft(key) as PersonalNotes | null
  return draft?.document?.type === "doc" && Number.isInteger(draft.revision) ? draft : null
}

export function rememberNoteDraft(key: string, value: PersonalNotes | null) {
  return rememberWorkspaceDraft(key, value)
}
