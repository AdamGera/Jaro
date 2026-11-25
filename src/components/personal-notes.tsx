import { useInterfaceLanguage } from "@/interface-language"
import { useEffect, useEffectEvent, useRef, useState } from "react"
import { EditorContent, useEditor, useEditorState, type JSONContent } from "@tiptap/react"
import { closeHistory } from "@tiptap/pm/history"
import StarterKit from "@tiptap/starter-kit"
import { TaskList, TaskItem } from "@tiptap/extension-list"
import { Mic, RotateCw, Square } from "lucide-react"
import { EMPTY_NOTES, readNoteDraft, rememberNoteDraft, type PersonalNotes } from "@/personal-notes"
import { useNoteDictation } from "@/use-note-dictation"
import { Button } from "./ui/button"
import { NOTES_COMMAND_EVENT, NotesCommands } from "./notes-commands"

const extensions = [StarterKit.configure({
  code: false, codeBlock: false, link: { openOnClick: false },
}), TaskList, TaskItem.configure({ nested: true }), NotesCommands]

type Props = {
  sessionId: string
  initial?: PersonalNotes
  onSaved?: () => void
  onUpdateOverview?: () => void
  notesChanged?: boolean
  overviewBusy?: boolean
  languages: string[]
  dictationEnabled: boolean
  onDictationBusy: (busy: boolean) => void
}

export function PersonalNotesEditor({ sessionId, initial, onSaved, onUpdateOverview,
  notesChanged, overviewBusy, languages, dictationEnabled, onDictationBusy }: Props) {
  const { t } = useInterfaceLanguage()
  const storageKey = `jard.personalNotes.${sessionId}`
  const [recovered] = useState(() => readNoteDraft(storageKey))
  const [saveState, setSaveState] = useState<"saved" | "pending" | "error" | "conflict">(recovered ? "pending" : "saved")
  const [draftProtected, setDraftProtected] = useState(true)
  const pending = useRef<JSONContent | null>(recovered?.document ?? null)
  const revision = useRef(recovered?.revision ?? initial?.revision ?? 0)
  const inFlight = useRef<Promise<boolean> | null>(null)
  const timer = useRef<number>(undefined)
  const mounted = useRef(false)
  const conflict = useRef(false)
  const position = useRef(1)
  const fieldRef = useRef<HTMLElement>(null)
  const [linkOpen, setLinkOpen] = useState(false)
  const [url, setUrl] = useState("")
  function save(): Promise<boolean> {
    if (inFlight.current) return inFlight.current
    if (conflict.current) return Promise.resolve(!conflict.current)
    const request = async () => {
      while (pending.current) {
        const document = pending.current
        try {
          const body = JSON.stringify({ document, revision: revision.current })
          const response = await fetch(`/api/sessions/${sessionId}/personal-notes`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body, keepalive: new TextEncoder().encode(body).length < 60_000,
          })
          if (response.status === 409) {
            conflict.current = true
            if (mounted.current) setSaveState("conflict")
            return false
          }
          if (!response.ok) throw new Error("Could not save My notes.")
          revision.current = (await response.json()).revision
          if (pending.current === document) pending.current = null
          rememberNoteDraft(storageKey, pending.current && { document: pending.current, revision: revision.current })
        } catch {
          if (mounted.current) setSaveState("error")
          return false
        }
      }
      if (mounted.current) {
        setSaveState("saved")
        setDraftProtected(true)
        onSaved?.()
      }
      return true
    }
    inFlight.current = request().finally(() => { inFlight.current = null })
    return inFlight.current
  }

  // Keep queued edits through navigation, including a failed request during teardown.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      window.clearTimeout(timer.current)
    }
  }, [])

  const editor = useEditor({
    extensions, textDirection: "auto", content: recovered?.document ?? initial?.document ?? EMPTY_NOTES,
    shouldRerenderOnTransaction: false,
    editorProps: { attributes: { class: "personal-note-document", role: "textbox", "aria-label": "My notes", "aria-multiline": "true" } },
    onTransaction: ({ transaction }) => { position.current = transaction.mapping.map(position.current, 1) },
    onUpdate: ({ editor }) => {
      const document = editor.getJSON()
      pending.current = document
      setDraftProtected(rememberNoteDraft(storageKey, { document, revision: revision.current }))
      if (conflict.current) return
      setSaveState("pending")
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => void save(), 400)
    },
  })
  const saveOnExit = useRef(save)
  useEffect(() => { saveOnExit.current = save })
  useEffect(() => {
    if (pending.current) void saveOnExit.current()
    const saveBeforeHide = () => { if (pending.current) void saveOnExit.current() }
    window.addEventListener("pagehide", saveBeforeHide)
    return () => {
      window.removeEventListener("pagehide", saveBeforeHide)
      saveBeforeHide()
    }
  }, [])

  const empty = useEditorState({
    editor,
    selector: ({ editor }) => !editor.isDestroyed && editor.isEmpty && editor.state.doc.childCount === 1
      && editor.state.doc.firstChild?.type.name === "paragraph",
  })
  const dictation = useNoteDictation(languages, text => {
    if (!mounted.current || editor.isDestroyed) return
    const at = position.current
    const space = (sample: string) => sample && !/\s/u.test(sample) ? " " : ""
    const before = editor.state.doc.textBetween(Math.max(0, at - 1), at)
    const after = editor.state.doc.textBetween(at, Math.min(editor.state.doc.content.size, at + 1))
    editor.view.dispatch(closeHistory(editor.state.tr).insertText(space(before) + text + space(after), at).scrollIntoView())
    editor.view.dispatch(closeHistory(editor.state.tr))
    editor.commands.focus(at + space(before).length + text.length)
  }, onDictationBusy)

  function startDictation() {
    if (!dictationEnabled || dictation.phase !== "idle" || editor.isDestroyed) return
    position.current = editor.state.selection.to
    void dictation.start()
  }
  const runCommand = useEffectEvent((event: Event) => {
    const action = (event as CustomEvent<"dictate" | "link">).detail
    if (action === "dictate") startDictation()
    if (action === "link" && !editor.isDestroyed) {
      setUrl(editor.getAttributes("link").href ?? "")
      setLinkOpen(true)
    }
  })
  useEffect(() => {
    const field = fieldRef.current
    if (!field) return
    const listener = (event: Event) => runCommand(event)
    field.addEventListener(NOTES_COMMAND_EVENT, listener)
    return () => field.removeEventListener(NOTES_COMMAND_EVENT, listener)
  }, [])

  async function resolveConflict(keepEdits: boolean) {
    try {
      const response = await fetch(`/api/sessions/${sessionId}/personal-notes`, { cache: "no-store" })
      if (!response.ok) throw new Error()
      const latest: PersonalNotes = await response.json()
      revision.current = latest.revision
      conflict.current = false
      if (keepEdits) { void save(); return }
      pending.current = null
      rememberNoteDraft(storageKey, null)
      editor.commands.setContent(latest.document, { emitUpdate: false })
      setDraftProtected(true)
      setSaveState("saved")
      onSaved?.()
    } catch {
      setSaveState("conflict")
    }
  }

  return <section ref={fieldRef} className="personal-notes" aria-label="Personal notes editor"
    data-can-dictate={dictationEnabled && dictation.phase === "idle"}>
    <div className="personal-note-field">
      {empty && <span className="personal-note-placeholder" aria-hidden="true">
        Write here or type <kbd>/</kbd> for commands
      </span>}
      <EditorContent editor={editor} className="personal-note-content" />
    </div>
    {linkOpen && <form className="personal-note-link" onSubmit={event => {
      event.preventDefault()
      const address = url.trim()
      const href = /^[a-z][a-z0-9+.-]*:/i.test(address) ? address : `https://${address}`
      if (address && !/^(https?:\/\/|mailto:|tel:)/i.test(href)) {
        const input = event.currentTarget.querySelector("input")
        input?.setCustomValidity("Enter a web, email, or phone link.")
        input?.reportValidity()
        return
      }
      const chain = editor.chain().focus().extendMarkRange("link")
      if (!address) chain.unsetLink().run()
      else if (!editor.state.selection.empty || editor.isActive("link")) chain.setLink({ href }).run()
      else chain.insertContent({ type: "text", text: address, marks: [{ type: "link", attrs: { href } }] }).run()
      setLinkOpen(false)
    }} onKeyDown={event => {
      if (event.key === "Escape") { setLinkOpen(false); editor.commands.focus() }
    }}>
      <input aria-label="Link address" placeholder="https://example.com" value={url} onChange={event => {
        event.target.setCustomValidity("")
        setUrl(event.target.value)
      }} autoFocus />
      <Button type="submit" size="sm">Apply link</Button>
      <Button type="button" size="sm" variant="ghost" onClick={() => { setLinkOpen(false); editor.commands.focus() }}>{t("Cancel")}</Button>
    </form>}
    {(dictation.phase !== "idle" || dictation.notice) && <aside className="dictation-preview">
      <div role="status">{dictation.notice || ({
        starting: "Starting microphone…", recording: "Listening…", transcribing: "Transcribing dictation…",
        idle: "", retry: "",
      })[dictation.phase]}</div>
      {dictation.preview && <p dir="auto">{dictation.preview}</p>}
    </aside>}
    <footer className="personal-note-footer">
      <div className="personal-note-dictation" role="group" aria-label="Note dictation">
        {dictation.phase === "idle" ? <Button variant="ghost" size="sm" disabled={!dictationEnabled}
          onClick={startDictation}><Mic />Dictate</Button> : <>
          {dictation.phase === "recording" && <Button size="sm" onClick={() => void dictation.stop()}><Square />Stop dictation</Button>}
          {dictation.phase === "retry" && <Button size="sm" onClick={dictation.retry}>Retry dictation</Button>}
          <Button variant="ghost" size="sm" onClick={dictation.cancel}>Cancel dictation</Button>
        </>}
      </div>
      {!draftProtected && <span role="alert">{t("Save My Notes before closing or restarting Jaro. This draft could not be stored.")}</span>}
      <span role="status" className={saveState === "saved" ? "sr-only" : undefined}>{{
        saved: "Saved locally", pending: "Saving…", error: "Could not save My notes.",
        conflict: "My notes changed in another window.",
      }[saveState]}</span>
      {saveState === "error" && <Button size="sm" variant="outline" onClick={() => void save()}>Retry save</Button>}
      {saveState === "conflict" && <>
        <Button size="sm" variant="outline" onClick={() => void resolveConflict(false)}>Reload saved notes</Button>
        <Button size="sm" variant="outline" onClick={() => void resolveConflict(true)}>Use my edits</Button>
      </>}
      {onUpdateOverview && (notesChanged || saveState === "pending") &&
        <Button size="sm" className="update-personal-overview"
          disabled={overviewBusy || saveState === "conflict"}
          onClick={async () => {
            window.clearTimeout(timer.current)
            if (await save()) onUpdateOverview()
          }}><RotateCw />Update overview</Button>}
    </footer>
  </section>
}
