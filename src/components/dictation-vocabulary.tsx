import { useEffect, useMemo, useRef, useState } from "react"
import { BookOpen, ChevronRight, Plus, X } from "lucide-react"
import { Dialog } from "radix-ui"
import { useInterfaceLanguage } from "@/interface-language"
import type { DictationVocabularyEntry } from "@/system-dictation"
import { SettingRow } from "./settings-rows"
import { Button } from "./ui/button"

type Row = { id: string; term: string; spellings: string }
type Editor = { rows: Row[]; base: DictationVocabularyEntry[] }

function termKey(term: string) {
  return term.trim().normalize().toLowerCase()
}

function sameVocabulary(left: DictationVocabularyEntry[], right: DictationVocabularyEntry[]) {
  return left.length === right.length && left.every(({ term, soundsLike }, index) =>
    term === right[index].term && soundsLike.length === right[index].soundsLike.length &&
    soundsLike.every((word, at) => word === right[index].soundsLike[at]))
}

function parseSpellings(text: string) {
  return text.split(",").map(word => word.trim()).filter(Boolean)
}

function savedRows(entries: DictationVocabularyEntry[], previous: Row[] = []) {
  const rows = new Map(previous.map(row => [termKey(row.term), row]))
  return [
    ...entries.map(entry => {
      const row = rows.get(termKey(entry.term))
      return {
        id: row?.id ?? `term:${entry.term}`,
        term: row?.term.trim() === entry.term ? row.term : entry.term,
        spellings: row && JSON.stringify(parseSpellings(row.spellings)) === JSON.stringify(entry.soundsLike)
          ? row.spellings : entry.soundsLike.join(", "),
      }
    }),
    ...previous.filter(row => !row.term.trim()),
  ]
}

function project(rows: Row[]) {
  const terms = new Map<string, DictationVocabularyEntry>()
  for (const row of rows) {
    const term = row.term.trim()
    const spellings = parseSpellings(row.spellings)
    if (!term) {
      if (spellings.length) return { error: "Enter a term for these alternate spellings." }
      continue
    }
    if (term.length > 80) return { error: "Use up to 80 characters per term." }
    if (spellings.some(word => word.length < 2 || word.length > 80)) {
      return { error: "Use 2 to 80 characters per alternate spelling." }
    }
    const key = termKey(term)
    const entry = terms.get(key) ?? { term, soundsLike: [] }
    const used = new Set([key, ...entry.soundsLike.map(termKey)])
    for (const spelling of spellings) {
      const spellingKey = termKey(spelling)
      if (used.has(spellingKey)) continue
      used.add(spellingKey)
      entry.soundsLike.push(spelling)
    }
    if (entry.soundsLike.length > 8) return { error: "Use up to eight alternate spellings per term." }
    terms.set(key, entry)
  }
  return { entries: [...terms.values()] }
}

export type SaveDictationVocabulary = (base: DictationVocabularyEntry[], entries: DictationVocabularyEntry[]) => Promise<DictationVocabularyEntry[]>

export function DictationVocabulary({ saved, saveVocabulary }: {
  saved: DictationVocabularyEntry[] | undefined
  saveVocabulary: SaveDictationVocabulary
}) {
  const { t } = useInterfaceLanguage()
  const [editor, setEditor] = useState<Editor>()
  const [saving, setSaving] = useState(false)
  const [closing, setClosing] = useState(false)
  const [failure, setFailure] = useState("")
  const [added, setAdded] = useState<string>()
  const nextId = useRef(0)
  const list = useRef<HTMLDivElement>(null)
  const rows = editor?.rows
  const projected = useMemo(() => rows ? project(rows) : undefined, [rows])
  const entries = projected?.entries
  const dirty = editor && entries && !sameVocabulary(editor.base, entries)
  const issue = projected?.error || failure

  useEffect(() => {
    if (!editor || saving || dirty || closing || issue || !saved || sameVocabulary(editor.base, saved)) return
    const timer = window.setTimeout(() => {
      setEditor(current => current === editor ? { rows: savedRows(saved, editor.rows), base: saved } : current)
    }, 0)
    return () => window.clearTimeout(timer)
  }, [editor, dirty, saving, closing, issue, saved])

  useEffect(() => {
    if (!editor || saving || issue || !entries || (!dirty && !closing)) return
    const timer = window.setTimeout(() => {
      if (!dirty) {
        setEditor(undefined)
        setClosing(false)
        return
      }
      setSaving(true)
      void saveVocabulary(editor.base, entries).then(result => {
        setEditor(current => {
          if (!current) return current
          return current.rows === editor.rows
            ? { rows: savedRows(result, current.rows), base: result }
            : { rows: current.rows, base: entries }
        })
      }).catch(error => {
        setClosing(false)
        setFailure(error instanceof Error ? error.message : "Jaro could not save the vocabulary.")
      }).finally(() => setSaving(false))
    }, closing ? 0 : 350)
    return () => window.clearTimeout(timer)
  }, [editor, entries, saving, closing, dirty, issue, saveVocabulary])

  useEffect(() => {
    if (!added) return
    const field = list.current?.querySelector<HTMLInputElement>(`[data-row="${added}"] input`)
    field?.focus({ preventScroll: true })
    field?.scrollIntoView({ block: "nearest", behavior: "instant" })
  }, [added])

  function blankRow(): Row {
    return { id: `new-${++nextId.current}`, term: "", spellings: "" }
  }

  function changeOpen(open: boolean) {
    if (open) {
      if (!saved) return
      setFailure("")
      setClosing(false)
      setAdded(undefined)
      setEditor({ rows: saved.length ? savedRows(saved) : [blankRow()], base: saved })
    } else {
      setFailure("")
      setClosing(true)
    }
  }

  function changeRows(update: (rows: Row[]) => Row[]) {
    setFailure("")
    setEditor(current => current && { ...current, rows: update(current.rows) })
  }

  function add() {
    const row = blankRow()
    changeRows(rows => [...rows, row])
    setAdded(row.id)
  }

  function edit(id: string, patch: Partial<Row>) {
    changeRows(rows => rows.map(row => row.id === id ? { ...row, ...patch } : row))
  }

  function remove(id: string) {
    const replacement = blankRow()
    changeRows(rows => {
      const remaining = rows.filter(row => row.id !== id)
      return remaining.length ? remaining : [replacement]
    })
    if (editor?.rows.length === 1 && editor.rows[0].id === id) setAdded(replacement.id)
  }

  const count = saved?.length ?? 0
  return <Dialog.Root open={!!editor} onOpenChange={changeOpen}>
    <SettingRow icon={BookOpen} title={t("Vocabulary")} label={false} find="Names, terms and alternate spellings">
      <Dialog.Trigger asChild>
        <Button type="button" variant="ghost" size="sm" aria-label={t("Edit vocabulary")} disabled={!saved}>
          {count === 0 ? t("None") : count === 1 ? t("1 term") : t("{count} terms").replace("{count}", String(count))}
          <ChevronRight aria-hidden="true" />
        </Button>
      </Dialog.Trigger>
    </SettingRow>
    <Dialog.Portal>
      <Dialog.Overlay className="dictation-tour-overlay" />
      <Dialog.Content className="dictation-tour dictation-vocabulary-dialog">
        <Dialog.Title>{t("Vocabulary")}</Dialog.Title>
        <Dialog.Description>{t("Give Rumble2 Flash up to 32 names or terms. Alternate spellings match whole phrases.")}</Dialog.Description>
        <div className="dictation-vocabulary-headings" aria-hidden="true">
          <span>{t("Term")}</span><span>{t("Sounds like")}</span>
        </div>
        <div className="dictation-vocabulary-list" ref={list}>
          {editor?.rows.map(row => <div className="dictation-vocabulary-row" key={row.id} data-row={row.id} data-added={row.id === added}>
            <input type="text" aria-label={t("Term")} placeholder={t("e.g. Mei Lin")} dir="auto" value={row.term}
              onChange={event => edit(row.id, { term: event.target.value })} />
            <input type="text" aria-label={t("Sounds like")} placeholder={t("Comma-separated spellings")} dir="auto" value={row.spellings}
              onChange={event => edit(row.id, { spellings: event.target.value })} />
            <Button type="button" size="icon-sm" variant="ghost" aria-label={t("Remove term")} onClick={() => remove(row.id)}><X /></Button>
          </div>)}
        </div>
        {issue && <p className="dictation-error" role="alert">{t(issue)}</p>}
        <div className="dictation-vocabulary-footer">
          <Button type="button" variant="outline" size="sm" disabled={(editor?.rows.length ?? 0) >= 32} onClick={add}>
            <Plus />{t("Add term")}
          </Button>
          <div>
            {issue && <Button type="button" size="sm" variant="ghost" onClick={() => {
              setEditor(undefined)
              setClosing(false)
            }}>{t("Discard unsaved changes")}</Button>}
            <Button type="button" size="sm" disabled={closing || !!projected?.error}
              onClick={() => changeOpen(false)}>{saving && closing ? t("Saving…") : t("Done")}</Button>
          </div>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
