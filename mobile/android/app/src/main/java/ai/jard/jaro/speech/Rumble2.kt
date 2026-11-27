package ai.jard.jaro.speech

/** Optional native Rumble2 Flash runtime. The core library is linked only in configured builds. */
internal object Rumble2 {
    const val AUTOMATIC = "auto"

    init { System.loadLibrary("jarorumblejni") }

    external fun load(file: String, threads: Int): Long
    external fun free(handle: Long)
    external fun decode(handle: Long, pcm: ShortArray, count: Int, language: String?, job: Long): ByteArray?
    external fun abort(job: Long)
}

/** A NUL-terminated language name followed by the transcript, both in UTF-8. */
internal fun rumbleResult(bytes: ByteArray): SpeechResult? {
    val separator = bytes.indexOf(0)
    if (separator < 0) return null
    val text = bytes.decodeToString(startIndex = separator + 1).trim()
    if (text.isEmpty()) return null
    val name = bytes.decodeToString(endIndex = separator)
    val language = when (name) {
        "Norwegian Bokmål", "Norwegian Nynorsk" -> DictationLanguage.Norwegian.code
        else -> DictationLanguage.entries.firstOrNull { it.name == name }?.code ?: "und"
    }
    return SpeechResult(text, language)
}
