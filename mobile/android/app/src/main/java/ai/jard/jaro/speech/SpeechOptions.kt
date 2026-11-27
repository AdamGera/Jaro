package ai.jard.jaro.speech

import android.content.Context
import android.content.res.Resources
import ai.jard.jaro.BuildConfig
import ai.jard.jaro.words.WordsLists
import ai.jard.jaro.words.WordsRead
import ai.jard.jaro.words.WordsReader
import ai.jard.jaro.words.WordsWriter

internal enum class Style { Polished, Casual, Notes, Verbatim }
/** A keyboard layout's language: its keys, accents and typing model. Dictation languages are
 * [DictationLanguage]; a language can be dictated without a layout of its own. */
internal enum class Spoken(val code: String, val label: String, val hasModel: Boolean = true) {
    English("en", "English"), Spanish("es", "Español"), German("de", "Deutsch"), Norwegian("no", "Norsk"),
    French("fr", "Français"), Italian("it", "Italiano"), Portuguese("pt", "Português"),
    Russian("ru", "Русский", hasModel = false);

    // Whisper calls Bokmål "no"; the keyboard and its model use BCP 47 "nb".
    val layoutCode get() = if (this == Norwegian) "nb" else code
    companion object {
        fun matching(code: String?): Spoken? = when (code) {
            "no", "nb", "nn" -> Norwegian
            else -> entries.firstOrNull { it.code == code }
        }
    }
}

/** What Whisper is asked to transcribe ([code], Whisper's name for it) and the layout the
 * keyboard follows while it is the dictation language, when there is one. */
internal enum class DictationLanguage(val code: String, val label: String, val layout: Spoken?) {
    English("en", "English", Spoken.English), Spanish("es", "Español", Spoken.Spanish),
    German("de", "Deutsch", Spoken.German), Norwegian("no", "Norsk", Spoken.Norwegian),
    French("fr", "Français", Spoken.French), Italian("it", "Italiano", Spoken.Italian), Dutch("nl", "Nederlands", null),
    // Brazilian typing frequencies with Portuguese spellings in the validity list.
    Portuguese("pt", "Português", Spoken.Portuguese),
    // No layouts of their own yet: Polish's letters are held on the English keys, and Russian has no
    // Cyrillic keys, so a Russian speaker dictates here and types on another keyboard.
    Polish("pl", "Polski", null), Russian("ru", "Русский", Spoken.Russian);

    // The mobile runtime accepts English language names for Rumble2 Flash's supported languages.
    val rumbleName get() = when (this) {
        English -> "English"
        Spanish -> "Spanish"
        German -> "German"
        Norwegian -> "Norwegian"
        French -> "French"
        Italian -> "Italian"
        Dutch -> "Dutch"
        Portuguese -> "Portuguese"
        Polish -> "Polish"
        Russian -> "Russian"
    }

    companion object {
        fun matching(code: String?): DictationLanguage? = when (code) {
            "no", "nb", "nn" -> Norwegian
            else -> entries.firstOrNull { it.code == code }
        }
        /** Automatic: the keyboard's layout when one is chosen there, as iPhone dictation follows the
         * keyboard's language, else the first of the phone's languages Jaro dictates, else English. */
        fun automatic(keyboard: Spoken?, system: List<String>): DictationLanguage =
            entries.firstOrNull { keyboard != null && it.layout == keyboard }
                ?: system.firstNotNullOfOrNull(::matching) ?: English
    }
}

/** The layout whose typing model the keyboard reads: the one pinned in Keyboard › Keyboard layout, else the
 * dictation language's own. Null when the keys are only English's stand-in for a language with no layout
 * (Nederlands, Polski, Русский): English's next words and autocorrections are not that language's. */
internal fun typingLayout(pinned: Spoken?, spoken: DictationLanguage): Spoken? =
    (pinned ?: spoken.layout)?.takeIf { it.hasModel }

internal data class SpeechOptions(val style: Style, val spoken: DictationLanguage, val clean: Boolean, val translate: Boolean,
                                   val lowercase: Boolean = false, val endWithPeriod: Boolean = true,
                                   val formatLists: Boolean = false, val spokenPunctuation: Boolean = false,
                                   val vocabulary: List<String> = emptyList(),
                                   val replacements: List<WordReplacement> = emptyList(),
                                   val fasterEnglish: Boolean = false,
                                   val rumble2: Boolean = false,
                                   val rumbleAuto: Boolean = false) {
    val outputLanguage get() = if (translate) "en" else spoken.code
    val useFasterEnglish get() = fasterEnglish && spoken == DictationLanguage.English && !translate
    val rumbleLanguage get() = when {
        !rumble2 || translate -> null
        rumbleAuto -> Rumble2.AUTOMATIC
        else -> spoken.rumbleName
    }
    fun rumbleLanguage(count: Int) = rumbleLanguage?.takeIf { count in 1..160_000 }
}
internal class SpeechPreferences(context: Context) {
    private val prefs = context.getSharedPreferences("speech", Context.MODE_PRIVATE)
    var style: Style
        get() = Style.entries.firstOrNull { it.name == prefs.getString("style", null) } ?: Style.Polished
        set(value) { prefs.edit().putString("style", value.name).apply() }
    // Null means Automatic; never save the resolved locale over that choice.
    var language: DictationLanguage?
        get() = DictationLanguage.matching(prefs.getString("spoken", null))
        set(value) { prefs.edit().putString("spoken", value?.code).apply() }
    // Keyboard › Keyboard layout when chosen there (null while the layout follows dictation).
    private val typing = context.getSharedPreferences("typing", Context.MODE_PRIVATE)
    val spoken: DictationLanguage
        get() = language ?: Resources.getSystem().configuration.locales.let { locales ->
            DictationLanguage.automatic(Spoken.matching(typing.getString("layout", null)), (0 until locales.size()).map { locales[it].language })
        }
    var clean: Boolean
        get() = prefs.getBoolean("clean", false)
        set(value) { prefs.edit().putBoolean("clean", value).apply() }
    var translation: Boolean
        get() = prefs.getBoolean("translation", false)
        set(value) { prefs.edit().putBoolean("translation", value).apply() }
    // Settings › Dictation › Lowercase: everything dictation writes, after the style.
    var lowercase: Boolean
        get() = prefs.getBoolean("lowercase", false)
        set(value) { prefs.edit().putBoolean("lowercase", value).apply() }
    // Off, no line ends with a full stop; "?" and "!" stay.
    var endWithPeriod: Boolean
        get() = prefs.getBoolean("endWithPeriod", true)
        set(value) { prefs.edit().putBoolean("endWithPeriod", value).apply() }
    // Say "first, second" or "bullet" for a list (English and Spanish).
    var formatLists: Boolean
        get() = prefs.getBoolean("formatLists", true)
        set(value) { prefs.edit().putBoolean("formatLists", value).apply() }
    // Say "comma" or "question mark" for the mark.
    var spokenPunctuation: Boolean
        get() = prefs.getBoolean("spokenPunctuation", true)
        set(value) { prefs.edit().putBoolean("spokenPunctuation", value).apply() }
    // Custom vocabulary: names and terms spelled your way, kept even while the switch is off.
    var customVocabulary: Boolean
        get() = prefs.getBoolean("customVocabulary", false)
        set(value) { prefs.edit().putBoolean("customVocabulary", value).apply() }
    var vocabularyTerms: List<String>
        get() = prefs.getString("vocabularyTerms", null)?.split('\n').orEmpty().filter(String::isNotEmpty)
        set(value) { prefs.edit().putString("vocabularyTerms", VocabularySpelling.sanitize(value).joinToString("\n")).apply() }
    private var replacementsCache: Pair<String, List<WordReplacement>>? = null
    var wordReplacements: List<WordReplacement>
        get() {
            val saved = prefs.getString("wordReplacements", null) ?: return emptyList()
            replacementsCache?.takeIf { it.first == saved }?.let { return it.second }
            val entries = (WordsReader.read(saved.encodeToByteArray()) as? WordsRead.Read)
                ?.replacements.orEmpty().map { WordReplacement(it.heard, it.write) }.let(WordReplacements::valid)
            replacementsCache = saved to entries
            return entries
        }
        set(value) { prefs.edit().putString("wordReplacements", serializedReplacements(value)).apply() }

    fun words() = WordsLists(vocabularyTerms, wordReplacements)

    fun saveWords(lists: WordsLists) {
        prefs.edit()
            .putString("vocabularyTerms", VocabularySpelling.sanitize(lists.terms).joinToString("\n"))
            .putString("wordReplacements", serializedReplacements(lists.replacements))
            .apply()
    }

    private fun serializedReplacements(replacements: List<WordReplacement>) =
        WordsWriter.phone("", emptyList(), WordReplacements.valid(replacements)).bytes.decodeToString()

    var fasterEnglish: Boolean
        get() = prefs.getBoolean("fasterEnglish", false)
        set(value) { prefs.edit().putBoolean("fasterEnglish", value).apply() }
    var rumble2: Boolean
        get() = prefs.getBoolean("rumble2", BuildConfig.RUMBLE2_ENABLED)
        set(value) { prefs.edit().putBoolean("rumble2", value).apply() }
    // Labs › Signature: the line added to each keyboard dictation, off by default.
    var signature: Boolean
        get() = prefs.getBoolean("signature", false)
        set(value) { prefs.edit().putBoolean("signature", value).apply() }
    var signatureText: String
        get() = prefs.getString("signatureText", Signature.DEFAULT_TEXT)
            ?.takeIf { Signature.textProblem(it) == null }?.trim() ?: Signature.DEFAULT_TEXT
        set(value) { prefs.edit().putString("signatureText", value).apply() }
    fun options(nextEnglish: Boolean?): SpeechOptions {
        val resolved = spoken
        return SpeechOptions(style, resolved, clean,
            translation && resolved == DictationLanguage.Spanish && (nextEnglish ?: true),
            lowercase, endWithPeriod, formatLists, spokenPunctuation,
            vocabulary = if (customVocabulary) vocabularyTerms else emptyList(),
            replacements = wordReplacements, fasterEnglish = fasterEnglish,
            rumble2 = rumble2, rumbleAuto = language == null)
    }
}
