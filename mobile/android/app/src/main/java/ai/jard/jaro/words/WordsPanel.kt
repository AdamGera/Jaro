package ai.jard.jaro.words

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.OutlinedTextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import ai.jard.jaro.Action
import ai.jard.jaro.Card
import ai.jard.jaro.Entry
import ai.jard.jaro.Label
import ai.jard.jaro.R
import ai.jard.jaro.Rule
import ai.jard.jaro.speech.SpeechPreferences
import ai.jard.jaro.speech.WordReplacement
import ai.jard.jaro.speech.WordReplacements
import java.io.ByteArrayOutputStream
import java.io.InputStream
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private data class WordsUndo(val before: WordsLists, val after: WordsLists)

@Composable
internal fun WordsPanel(speech: SpeechPreferences, revision: Int) {
    val context = LocalContext.current
    val resolver = context.contentResolver
    val scope = rememberCoroutineScope()
    val lists = remember(revision) { speech.words() }
    var notice by remember { mutableStateOf<Int?>(null) }
    var busy by remember { mutableStateOf(false) }
    var imported by remember { mutableStateOf<WordsRead.Read?>(null) }
    var undo by remember { mutableStateOf<WordsUndo?>(null) }
    var heard by remember { mutableStateOf("") }
    var write by remember { mutableStateOf("") }
    var editing by remember { mutableStateOf<WordReplacement?>(null) }
    var problem by remember { mutableStateOf<WordReplacements.Problem?>(null) }

    val choose = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch {
            busy = true
            notice = null
            val read = withContext(Dispatchers.IO) {
                runCatching { resolver.openInputStream(uri)?.use(::readWords) }.getOrNull()
            }
            if (read is WordsRead.Read) imported = read
            else notice = R.string.words_import_error
            busy = false
        }
    }
    val save = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument(WordsWriter.MIME_TYPE)) { uri ->
        if (uri != null) scope.launch {
            busy = true
            val files = speech.words()
            val archive = WordsWriter.phone("", files.terms, files.replacements)
            val written = withContext(Dispatchers.IO) {
                runCatching { resolver.openOutputStream(uri, "wt")?.use { it.write(archive.bytes) } != null }.getOrDefault(false)
            }
            notice = when {
                !written -> R.string.words_export_error
                archive.notExported.isNotEmpty() -> R.string.words_export_partial
                else -> R.string.words_exported
            }
            busy = false
        }
    }

    fun clearEditor() { heard = ""; write = ""; editing = null; problem = null }
    fun changeReplacements(value: List<WordReplacement>) {
        speech.wordReplacements = value
        undo = null
        clearEditor()
    }

    Card {
        Entry(stringResource(R.string.words_import), action = if (busy) null else ({ choose.launch(arrayOf("*/*")) }))
        Rule()
        Entry(stringResource(R.string.words_export), action = if (busy) null else ({ save.launch(WordsWriter.FILE_NAME) }))
        val previous = undo
        if (previous != null && lists == previous.after) {
            Rule()
            Entry(stringResource(R.string.words_undo), value = "", action = {
                if (speech.words() == previous.after) speech.saveWords(previous.before)
                undo = null
                clearEditor()
            })
        }
    }
    notice?.let { Label(stringResource(it), muted = true, size = 14, lines = 3) }

    Label(stringResource(R.string.words_replacements), size = 15)
    Label(stringResource(R.string.words_replacements_detail), muted = true, size = 14, lines = 3)
    Card {
        Column(Modifier.padding(horizontal = 14.dp, vertical = 8.dp)) {
            OutlinedTextField(heard, { heard = it; problem = null }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Label(stringResource(R.string.words_heard), size = 14) })
            OutlinedTextField(write, { write = it; problem = null }, Modifier.fillMaxWidth(), maxLines = 6,
                label = { Label(stringResource(R.string.words_write), size = 14) })
            problem?.let { Label(stringResource(replacementProblem(it)), muted = true, size = 13, lines = 3) }
            Row {
                Action(stringResource(if (editing != null) R.string.words_save else R.string.words_add)) {
                    val list = speech.wordReplacements
                    val original = editing
                    val index = list.indexOf(original)
                    if (original != null && index < 0) { clearEditor(); return@Action }
                    val entry = WordReplacements.cleaned(heard, write)
                    problem = WordReplacements.problem(entry, list, index)
                    if (problem == null) changeReplacements(
                        if (index < 0) list + entry else list.toMutableList().apply { set(index, entry) })
                }
                if (editing != null) Action(stringResource(R.string.words_cancel), ::clearEditor)
            }
        }
    }
    if (lists.replacements.isNotEmpty()) Card {
        lists.replacements.forEachIndexed { index, entry ->
            if (index > 0) Rule()
            Row(Modifier.fillMaxWidth().padding(start = 14.dp), verticalAlignment = Alignment.CenterVertically) {
                Label("${WordsText.shown(entry.heard)} → ${WordsText.shown(entry.write)}", lines = 2,
                    mod = Modifier.weight(1f))
                Action(stringResource(R.string.words_edit)) { heard = entry.heard; write = entry.write; editing = entry; problem = null }
                Action(stringResource(R.string.words_remove)) {
                    val current = speech.wordReplacements
                    if (current.getOrNull(index) == entry) changeReplacements(current.filterIndexed { at, _ -> at != index })
                }
            }
        }
    }
    imported?.let { incoming ->
        var useIncoming by remember(incoming) { mutableStateOf(emptySet<String>()) }
        val review = remember(lists, incoming, useIncoming) { WordsMerge.phone(lists, incoming, useTheirs = useIncoming) }
        AlertDialog(onDismissRequest = { imported = null },
            title = { Label(stringResource(R.string.words_review), size = 19) },
            text = {
                LazyColumn(Modifier.heightIn(max = 280.dp)) {
                    item {
                        Label(stringResource(R.string.words_review_counts, review.summary.addedTerms,
                            review.summary.addedReplacements + review.summary.changedReplacements), lines = 4)
                        if (incoming.newerVersion) Label(stringResource(R.string.words_review_newer),
                            size = 14, muted = true, lines = 3)
                        val skipped = incoming.skipped.size + review.notApplied.size
                        if (skipped > 0) Label(stringResource(R.string.words_review_skipped, skipped), size = 14, muted = true, lines = 3)
                        if (review.clashes.isNotEmpty()) {
                            Label(stringResource(R.string.words_review_clashes), size = 14, muted = true, lines = 3)
                        }
                    }
                    items(review.clashes, key = { it.path }) { clash ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Label(WordsText.shown(clash.heard), size = 15, lines = 2)
                                Label(stringResource(R.string.words_review_saved, WordsText.shown(clash.yours)),
                                    size = 13, muted = true, lines = 2)
                                Label(stringResource(R.string.words_review_imported, WordsText.shown(clash.theirs)),
                                    size = 13, muted = true, lines = 2)
                            }
                            Checkbox(clash.path in useIncoming, onCheckedChange = { checked ->
                                useIncoming = if (checked) useIncoming + clash.path else useIncoming - clash.path
                            })
                        }
                    }
                }
            },
            confirmButton = {
                if (!review.summary.nothingNew) Action(stringResource(R.string.words_add)) {
                    val before = speech.words()
                    val currentClashes = WordsMerge.phone(before, incoming).clashes.toSet()
                    val unchangedChoices = review.clashes.filter { it in currentClashes && it.path in useIncoming }
                        .mapTo(mutableSetOf()) { it.path }
                    val after = WordsMerge.phone(before, incoming, useTheirs = unchangedChoices).after
                    speech.saveWords(after)
                    undo = WordsUndo(before, speech.words())
                    imported = null
                    clearEditor()
                }
            },
            dismissButton = { Action(stringResource(R.string.words_cancel)) { imported = null } })
    }
}

internal fun readWords(input: InputStream): WordsRead {
    val out = ByteArrayOutputStream()
    val buffer = ByteArray(8192)
    while (true) {
        val n = input.read(buffer)
        if (n < 0) return WordsReader.read(out.toByteArray())
        if (n > WordsReader.MAX_BYTES - out.size()) return WordsRead.Refused("tooLarge")
        out.write(buffer, 0, n)
    }
}

private fun replacementProblem(problem: WordReplacements.Problem) = when (problem) {
    WordReplacements.Problem.NoLetters -> R.string.words_need_letters
    WordReplacements.Problem.Hidden -> R.string.words_hidden
    WordReplacements.Problem.TooLong, WordReplacements.Problem.TooManyWords -> R.string.words_too_long
    WordReplacements.Problem.Blank -> R.string.words_need_output
    WordReplacements.Problem.Duplicate -> R.string.words_duplicate
    WordReplacements.Problem.Full -> R.string.words_full
}
