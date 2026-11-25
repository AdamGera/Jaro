import { useState } from "react"
import { Node, type JSONContent } from "@tiptap/core"
import { Markdown, MarkdownManager } from "@tiptap/markdown"
import { EditorContent, useEditor } from "@tiptap/react"
import StarterKit from "@tiptap/starter-kit"
import { clock } from "@/speaker-activity"
import type { EditableOverview } from "@/overview"
import { overviewDraftKey, readOverviewDraft } from "@/overview-draft"
import { rememberWorkspaceDraft } from "@/workspace-drafts"
import { useInterfaceLanguage } from "@/interface-language"
import { Button } from "./ui/button"
import { NotesCommands } from "./notes-commands"

const Citation = Node.create({
  name: "overviewCitation",
  inline: true,
  group: "inline",
  atom: true,
  addAttributes() { return { seconds: { default: "0", rendered: false } } },
  parseHTML() {
    return [{ tag: "span[data-overview-citation]", getAttrs: element => {
      const seconds = element.getAttribute("data-overview-citation")
      return seconds && /^\d+(?:\.\d+)?$/.test(seconds) ? { seconds } : false
    } }]
  },
  renderHTML({ node }) {
    return ["span", { class: "chat-citation", dir: "ltr", "data-overview-citation": node.attrs.seconds },
      clock(Number(node.attrs.seconds))]
  },
  markdownTokenizer: {
    name: "overviewCitation",
    level: "inline",
    start: source => source.indexOf("⟦"),
    tokenize: source => {
      const match = /^⟦(\d+(?:\.\d+)?)⟧/.exec(source)
      if (match) return { type: "overviewCitation", raw: match[0], text: match[1] }
    },
  },
  parseMarkdown: token => ({ type: "overviewCitation", attrs: { seconds: token.text } }),
  renderMarkdown: node => `⟦${node.attrs?.seconds}⟧`,
})

const extensions = [
  StarterKit.configure({ link: { openOnClick: false, enableClickSelection: true } }),
  NotesCommands.configure({ link: false, checklist: false }),
  Citation,
  Markdown,
]
const markdown = new MarkdownManager({ extensions })

type Props = {
  sessionId: string
  version: EditableOverview
  onSave: (version: EditableOverview, summary: string, markdown: string) => Promise<void>
  onClose: () => void
}

export default function OverviewEditor({ sessionId, version, onSave, onClose }: Props) {
  const { t } = useInterfaceLanguage()
  const draftKey = overviewDraftKey(sessionId, version.language)
  const [recovered] = useState(() => readOverviewDraft(draftKey))
  const base = recovered?.revision ?? version.editRevision
  const stale = base !== version.editRevision
  const [initial] = useState(() => {
    if (recovered) return recovered.document
    const document = markdown.parse(version.markdown ?? "")
    const content = document.content ?? []
    if (version.summary) content.unshift({
      type: "paragraph", content: [{ type: "text", text: version.summary }],
    })
    return { ...document, content }
  })
  const [dirty, setDirty] = useState(!!recovered)
  const [draftProtected, setDraftProtected] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState("")
  const editor = useEditor({
    extensions, content: initial, textDirection: "auto", autofocus: "start",
    editorProps: { attributes: { class: "personal-note-document", role: "textbox",
      "aria-label": "Edit overview", "aria-multiline": "true" } },
    onUpdate: ({ editor }) => {
      setDraftProtected(rememberWorkspaceDraft(draftKey, { revision: base, document: editor.getJSON() }))
      setDirty(true)
    },
  })

  async function save() {
    if (!dirty || saving || !editor) return
    const document: JSONContent = editor.getJSON()
    const content = document.content ?? []
    const lead = content[0]
    // Keep the lead as plain text when possible. Any formatting in it belongs to the Markdown body.
    const plainLead = lead?.type === "paragraph" && lead.content?.length === 1 &&
      lead.content[0].type === "text" && !lead.content[0].marks?.length
    const summary = plainLead ? lead.content?.[0].text ?? "" : ""
    setSaving(true)
    setError("")
    editor.setEditable(false, false)
    try {
      await onSave(version, summary, markdown.serialize({ ...document, content: plainLead ? content.slice(1) : content }))
      rememberWorkspaceDraft(draftKey, null)
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save the overview. Try again.")
      editor.setEditable(true, false)
      setSaving(false)
    }
  }

  return <section className="overview-editor" lang={version.language}>
    <div className="overview-edit-actions">
      {error && <span role="alert">{error}</span>}
      {!draftProtected && <span role="alert">{t("Save your overview before closing or restarting Jaro. This draft could not be stored.")}</span>}
      {stale && <span role="status">{t("The saved overview changed since you started this draft.")}</span>}
      <Button type="button" variant="ghost" size="sm" disabled={saving} onClick={() => {
        rememberWorkspaceDraft(draftKey, null)
        onClose()
      }}>{t(stale ? "Discard draft" : "Cancel")}</Button>
      <Button type="button" size="sm" disabled={!dirty || saving} onClick={() => void save()}>
        {t(saving ? "Saving…" : stale ? "Replace saved overview" : "Save changes")}
      </Button>
    </div>
    <EditorContent editor={editor} />
  </section>
}
