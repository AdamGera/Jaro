package ai.jard.jaro.speech

import ai.jard.jaro.words.WordsReader
import ai.jard.jaro.words.WordsText
import java.text.Normalizer
import java.util.Locale

/** One of Custom vocabulary's Word replacements: words heard, and what Jaro writes instead. */
internal data class WordReplacement(val heard: String, val write: String)

/** Local, whole-word replacements shared by keyboard dictation and the app. */
internal object WordReplacements {
    const val maxCount = WordsReader.MAX_REPLACEMENTS
    const val maxWords = WordsReader.MAX_WORDS
    private val joiners = setOf('-', '_', '/', '\\', '@', '#', '&', '+', '=', '~', '*', '.')

    enum class Problem { NoLetters, Hidden, TooLong, TooManyWords, Blank, Duplicate, Full }

    /** The entry as it is kept: the heard words cleaned to one line, the output composed with LF line ends. */
    fun cleaned(heard: String, write: String) = WordReplacement(WordsText.line(heard), WordsText.output(write))

    /** Why [entry] (already [cleaned]) can't join [list], or null. [replacing] is the index it would take the
     * place of, when it is an edit. Empty or blank outputs, which delete words, aren't allowed yet. */
    fun problem(entry: WordReplacement, list: List<WordReplacement>, replacing: Int = -1): Problem? {
        invalid(entry)?.let { return it }
        val key = WordsText.key(entry.heard)
        if (list.withIndex().any { (i, it) -> i != replacing && WordsText.key(it.heard) == key }) return Problem.Duplicate
        if (replacing < 0 && list.size >= maxCount) return Problem.Full
        return null
    }

    private fun invalid(entry: WordReplacement): Problem? =
        when (WordsReader.replacementProblem(entry.heard, entry.write)) {
            "hiddenCharacters" -> Problem.Hidden
            "noLetters" -> Problem.NoLetters
            "tooLong" -> Problem.TooLong
            "tooManyWords" -> Problem.TooManyWords
            else -> Problem.Blank.takeIf { WordsText.blank(entry.write) }
        }

    /** Keeps valid entries in order, including when an older preference needs repair. */
    fun valid(entries: List<WordReplacement>): List<WordReplacement> {
        val keys = HashSet<String>()
        return entries.asSequence().map { cleaned(it.heard, it.write) }
            .filter { invalid(it) == null && keys.add(WordsText.key(it.heard)) }.take(maxCount).toList()
    }

    /** Writes each entry's output in place of its heard words, whole words only, following the original's case
     * unless the output has capitals of its own. */
    fun apply(entries: List<WordReplacement>, text: String): String {
        if (entries.isEmpty() || text.isEmpty()) return text
        val table = LinkedHashMap<List<String>, String>()
        for (entry in entries) {
            val keys = DictationText.words(entry.heard).map { DictationText.key(it.text) }
            if (keys.isEmpty() || keys.size > maxWords || WordsText.blank(entry.write) || table.containsKey(keys)) continue
            table[keys] = entry.write
        }
        return replace(table, text)
    }

    private fun replace(table: Map<List<String>, String>, text: String): String {
        if (table.isEmpty() || text.isEmpty()) return text
        val longest = table.keys.maxOf { it.size }
        val words = DictationText.words(text)
        val out = StringBuilder(text.length)
        var copied = 0
        var index = 0
        while (index < words.size) {
            var matched = 0
            for (count in minOf(longest, words.size - index) downTo 1) {
                val window = words.subList(index, index + count)
                if (!joinedBySpaces(window, text)) continue
                val keys = window.map { DictationText.key(it.text) }
                val meant = table[keys] ?: continue
                val start = window.first().start
                val end = window.last().end
                if (!bounded(start, end, text)) continue
                out.append(text, copied, start)
                out.append(matchCase(meant, text.substring(start, end)))
                copied = end
                matched = count
                break
            }
            index += maxOf(1, matched)
        }
        return out.append(text, copied, text.length).toString()
    }

    private fun joinedBySpaces(window: List<DictationWord>, text: String) = window.indices.drop(1).all { index ->
        val gap = text.substring(window[index - 1].end, window[index].start)
        gap.isNotEmpty() && gap.all { it == ' ' || it == '\t' }
    }

    /** Not part of a longer token: no joiner right before, and none right after except a sentence-ending
     * period. */
    private fun bounded(start: Int, end: Int, text: String): Boolean {
        if (start > 0 && (text[start - 1] in joiners || DictationText.isMark(text[start - 1]))) return false
        if (end >= text.length) return true
        val next = text[end]
        // A combining mark after the match makes it part of a longer word ("cafe" in "cafe" + U+0301).
        if (next !in joiners && !DictationText.isMark(next)) return true
        if (next != '.') return false
        if (end + 1 >= text.length) return true
        val following = text[end + 1]
        return following.isWhitespace() || following == '.' || following in "\"”’')]"
    }

    private fun matchCase(meant: String, like: String): String {
        val letters = like.filter(Char::isLetter)
        if (letters.length >= 2 && letters == letters.uppercase(Locale.ROOT) && letters != letters.lowercase(Locale.ROOT))
            return meant.uppercase(Locale.ROOT)
        if (meant.any(Char::isUpperCase)) return meant
        val first = like.firstOrNull(Char::isLetter)
        return if (first?.isUpperCase() == true) meant.replaceFirstChar { it.uppercaseChar() } else meant
    }
}

private class DictationWord(val text: String, val start: Int, val end: Int)

private object DictationText {
    fun words(text: String): List<DictationWord> {
        val words = ArrayList<DictationWord>()
        var wordStart = -1
        var i = 0
        while (i < text.length) {
            val c = text[i]
            val join = c == '\'' || c == '’'
            // A combining mark belongs to the letter before it ("cafe" + U+0301 is one word, not "cafe" and a mark).
            val word = c.isLetterOrDigit() || (join && wordStart >= 0 && i + 1 < text.length && text[i + 1].isLetter()) ||
                (isMark(c) && wordStart >= 0)
            if (word) { if (wordStart < 0) wordStart = i }
            else if (wordStart >= 0) { words += DictationWord(text.substring(wordStart, i), wordStart, i); wordStart = -1 }
            i++
        }
        if (wordStart >= 0) words += DictationWord(text.substring(wordStart), wordStart, text.length)
        return words
    }

    /** How two words compare: lowercased, with a curly apostrophe made straight. */
    fun key(word: String) = WordsText.key(Normalizer.normalize(word, Normalizer.Form.NFC)).replace('’', '\'')

    /** A combining mark that continues the word before it, never a word on its own. */
    fun isMark(c: Char) = when (Character.getType(c)) {
        Character.NON_SPACING_MARK.toInt(), Character.COMBINING_SPACING_MARK.toInt(), Character.ENCLOSING_MARK.toInt() -> true
        else -> false
    }
}
