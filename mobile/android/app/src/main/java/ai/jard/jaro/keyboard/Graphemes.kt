package ai.jard.jaro.keyboard

import java.util.regex.Pattern

// Java 17's desktop BreakIterator splits emoji sequences that Android treats as one
// character. Extended grapheme clusters give cursor and delete the same boundaries.
private val graphemePattern = Pattern.compile("\\X")

internal fun forEachGrapheme(text: String, action: (start: Int, end: Int) -> Unit) {
    val matches = graphemePattern.matcher(text)
    while (matches.find()) action(matches.start(), matches.end())
}
