import { useInterfaceLanguage } from "@/interface-language"
import { useCallback, useEffect, useEffectEvent, useRef, useState, type RefObject } from "react"
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
  mode?: "session" | "draft"
  initial?: PersonalNotes
  onSaved?: () => void
  onDraftSaved?: (id: string) => void
  onUpdateOverview?: () => void
  notesChanged?: boolean
  overviewBusy?: boolean
  languages: string[]
  dictationEnabled: boolean
  dictationDisabledReason?: string
  onDictationBusy: (busy: boolean) => void
  flushRef?: RefObject<(() => Promise<boolean>) | null>
}

function noteEndpoint(id: string, useSession: boolean) {
  return useSession ? `/api/sessions/${id}/personal-notes` : `/api/personal-note-drafts/${id}`
}

export function PersonalNotesEditor({ sessionId, mode = "session", initial, onSaved, onDraftSaved, onUpdateOverview,
  notesChanged, overviewBusy, languages, dictationEnabled, dictationDisabledReason, onDictationBusy, flushRef }: Props) {
  const { t } = useInterfaceLanguage()
  const [recovered] = useState(() => readNoteDraft(`jard.personalNotes.${sessionId}`))
  const [saveState, setSaveState] = useState<"saved" | "pending" | "error" | "conflict">(recovered ? "pending" : "saved")
  const [draftProtected, setDraftProtected] = useState(true)
  const target = useRef({ sessionId, mode })
  const promoted = useRef(mode === "session")
  const userEdited = useRef(!!recovered)
  const onSavedRef = useRef(onSaved)
  const onDraftSavedRef = useRef(onDraftSaved)
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

  useEffect(() => {
    onSavedRef.current = onSaved
    onDraftSavedRef.current = onDraftSaved
  }, [onSaved, onDraftSaved])

  const save = useCallback((): Promise<boolean> => {
    if (inFlight.current) return inFlight.current
    if (conflict.current) return Promise.resolve(false)
    const request = async () => {
      while (pending.current) {
        const document = pending.current
        const { sessionId: id, mode: currentMode } = target.current
        const useSession = currentMode === "session" || promoted.current
        const storageKey = `jard.personalNotes.${id}`
        try {
          const body = JSON.stringify({ document, revision: revision.current })
          const response = await fetch(noteEndpoint(id, useSession), {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body, keepalive: new TextEncoder().encode(body).length < 60_000,
          })
          if (response.status === 409) {
            conflict.current = true
            if (mounted.current) setSaveState("conflict")
            return false
          }
          if (!response.ok) throw new Error("Could not save My notes.")
          const saved = await response.json() as { revision: number; promoted?: boolean }
          revision.current = saved.revision
          if (saved.promoted) promoted.current = true
          else if (!useSession) onDraftSavedRef.current?.(id)
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
        onSavedRef.current?.()
      }
      return true
    }
    inFlight.current = request().finally(() => { inFlight.current = null })
    return inFlight.current
  }, [])

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
      userEdited.current = true
      pending.current = document
      const storageKey = `jard.personalNotes.${target.current.sessionId}`
      setDraftProtected(rememberNoteDraft(storageKey, { document, revision: revision.current }))
      if (conflict.current) return
      setSaveState("pending")
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => void save(), 400)
    },
  })

  // Handle seamless draft -> session transitions in-place without unmounting TipTap.
  useEffect(() => {
    const previous = target.current
    target.current = { sessionId, mode }
    if (mode === "session") promoted.current = true
    if (previous.sessionId !== sessionId) {
      rememberNoteDraft(`jard.personalNotes.${previous.sessionId}`, null)
      if (pending.current) {
        const storageKey = `jard.personalNotes.${sessionId}`
        setDraftProtected(rememberNoteDraft(storageKey, { document: pending.current, revision: revision.current }))
        window.clearTimeout(timer.current)
        void save()
      }
    } else if (previous.mode === "draft" && mode === "session" && pending.current) {
      window.clearTimeout(timer.current)
      void save()
    }
  }, [sessionId, mode, save])

  // Hydrate an active server-side draft when reconnecting (e.g. after reload mid-recording).
  useEffect(() => {
    if (mode !== "draft" || recovered || initial) return
    const controller = new AbortController()
    void fetch(`/api/personal-note-drafts/${sessionId}`, { cache: "no-store", signal: controller.signal })
      .then(response => response.ok ? response.json() as Promise<PersonalNotes & { promoted?: boolean }> : null)
      .then(draft => {
        if (!draft || controller.signal.aborted || !mounted.current || userEdited.current || editor.isDestroyed) return
        if (draft.promoted) promoted.current = true
        if (draft.revision > revision.current) {
          revision.current = draft.revision
          editor.commands.setContent(draft.document, { emitUpdate: false })
        }
      })
      .catch(() => {})
    return () => controller.abort()
  }, [sessionId, mode, recovered, initial, editor])

  const flush = useCallback(async () => {
    window.clearTimeout(timer.current)
    if (inFlight.current) {
      const ok = await inFlight.current
      if (!pending.current) return ok
    }
    if (!pending.current) return true
    return save()
  }, [save])

  useEffect(() => {
    if (!flushRef) return
    flushRef.current = flush
    return () => { if (flushRef.current === flush) flushRef.current = null }
  }, [flushRef, flush])

  useEffect(() => {
    if (pending.current) void save()
    const saveNow = () => {
      if (!pending.current) return
      window.clearTimeout(timer.current)
      void save()
    }
    const saveOnHide = () => { if (document.hidden) saveNow() }
    window.addEventListener("pagehide", saveNow)
    window.addEventListener("blur", saveNow)
    document.addEventListener("visibilitychange", saveOnHide)
    return () => {
      window.removeEventListener("pagehide", saveNow)
      window.removeEventListener("blur", saveNow)
      document.removeEventListener("visibilitychange", saveOnHide)
      saveNow()
    }
  }, [save])

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
      const { sessionId: id, mode: currentMode } = target.current
      const useSession = currentMode === "session" || promoted.current
      const storageKey = `jard.personalNotes.${id}`
      const response = await fetch(noteEndpoint(id, useSession), { cache: "no-store" })
      if (!response.ok) throw new Error()
      const latest: PersonalNotes & { promoted?: boolean } = await response.json()
      if (latest.promoted) promoted.current = true
      revision.current = latest.revision
      conflict.current = false
      if (keepEdits) { void save(); return }
      pending.current = null
      rememberNoteDraft(storageKey, null)
      editor.commands.setContent(latest.document, { emitUpdate: false })
      setDraftProtected(true)
      setSaveState("saved")
      onSavedRef.current?.()
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
          title={!dictationEnabled ? dictationDisabledReason : undefined}
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
