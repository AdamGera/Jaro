package ai.jard.jaro.speech

import ai.jard.jaro.words.WordsLists
import ai.jard.jaro.words.WordsMerge
import ai.jard.jaro.words.WordsRead
import ai.jard.jaro.words.WordsReader
import ai.jard.jaro.words.WordsWriter
import org.junit.Assert.*
import org.junit.Test

class WordsTest {
    @Test fun exportsAndImportsTermsAndMultilineReplacements() {
        val terms = listOf("Jaro", "São Paulo")
        val replacements = listOf(WordReplacement("note footer", "Name\nOffice"))
        val bytes = WordsWriter.phone("1.0", terms, replacements).bytes
        val read = WordsReader.read(bytes) as WordsRead.Read
        assertEquals(terms, read.vocabulary.map { it.term })
        assertEquals(WordsLists(terms, replacements), WordsMerge.phone(WordsLists(emptyList(), emptyList()), read).after)
    }

    @Test fun importedSoundAlikesAndReplacementsUseExistingRulesUntilChosen() {
        val read = WordsReader.read("""{"format":"jaro.words","version":1,"vocabulary":[
            {"term":"Jaro","soundsLike":["jar oh"]}],"replacements":[{"heard":"test words","write":"New result"}]}"""
            .encodeToByteArray()) as WordsRead.Read
        val before = WordsLists(listOf("Jaro"), listOf(WordReplacement("test words", "Old result")))
        val review = WordsMerge.phone(before, read)
        assertEquals(before.replacements.first(), review.after.replacements.first())
        assertEquals(WordReplacement("jar oh", "Jaro"), review.after.replacements.last())
        val replaced = WordsMerge.phone(before, read, useTheirs = setOf(review.clashes.single().path))
        assertEquals(WordReplacement("test words", "New result"), replaced.after.replacements.first())
    }

    @Test fun flagsFutureArchivesAndRejectsDuplicateJsonKeys() {
        val future = WordsReader.read("""{"format":"jaro.words","version":2}""".encodeToByteArray()) as WordsRead.Read
        assertTrue(future.newerVersion)
        assertTrue(WordsReader.read("""{"format":"jaro.words","version":1,"version":1}""".encodeToByteArray()) is WordsRead.Refused)
        assertTrue(WordsReader.read(byteArrayOf(0xc0.toByte(), 0x80.toByte())) is WordsRead.Refused)
    }

    @Test fun dictationReplacesWholePhrasesOnceWithoutRewritingUrlsOrTranslations() {
        val rules = listOf(WordReplacement("studio", "room"), WordReplacement("studio time", "QA window"))
        val options = SpeechOptions(Style.Verbatim, DictationLanguage.English, clean = false, translate = false, replacements = rules)
        val text = "Studio time at studio.com and studio 5"
        assertEquals("QA window at studio.com and room 5", SpokenText.format(text, options))
        assertEquals(text, SpokenText.format(text, options.copy(translate = true)))
    }

    @Test fun fasterEnglishIsOptInAndNeverUsedForOtherLanguagesOrTranslation() {
        val options = SpeechOptions(Style.Verbatim, DictationLanguage.English, clean = false, translate = false)
        assertFalse(options.useFasterEnglish)
        assertTrue(options.copy(fasterEnglish = true).useFasterEnglish)
        assertFalse(options.copy(fasterEnglish = true, spoken = DictationLanguage.Portuguese).useFasterEnglish)
        assertFalse(options.copy(fasterEnglish = true, translate = true).useFasterEnglish)
    }

    @Test fun rumble2UsesVerifiedLanguagesAndOnlyShortNonTranslatedDictation() {
        val options = SpeechOptions(Style.Verbatim, DictationLanguage.French, clean = false, translate = false)
        assertNull(options.rumbleLanguage(160_000))
        val enabled = options.copy(rumble2 = true)
        assertEquals("French", enabled.rumbleLanguage(160_000))
        assertEquals("Spanish", enabled.copy(spoken = DictationLanguage.Spanish).rumbleLanguage(16_000))
        assertNull(enabled.rumbleLanguage(160_001))
        assertNull(enabled.copy(translate = true).rumbleLanguage(160_000))
        assertEquals("English", enabled.copy(spoken = DictationLanguage.English).rumbleLanguage(160_000))
        assertEquals("Portuguese", enabled.copy(spoken = DictationLanguage.Portuguese).rumbleLanguage(160_000))
        assertEquals(Rumble2.AUTOMATIC, enabled.copy(rumbleAuto = true, spoken = DictationLanguage.English).rumbleLanguage(16_000))
    }
}
