package ai.jard.jaro.speech

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RumbleSpeechTest {
    @Test fun automaticFrenchUsesFrenchTypographyWithAnEnglishKeyboard() {
        val result = checkNotNull(rumbleResult("French\u0000Ça marche?".encodeToByteArray()))
        val options = SpeechOptions(Style.Polished, DictationLanguage.English, false, false,
            rumble2 = true, rumbleAuto = true)

        assertEquals("fr", result.language)
        assertEquals("Ça marche\u202F?", SpokenText.format(result.text, options, result.language))
    }

    @Test fun unknownLanguagesUseGenericFormattingAndMalformedResultsAllowFallback() {
        assertEquals(SpeechResult("你好。", "und"), rumbleResult("Chinese\u0000你好。".encodeToByteArray()))
        assertEquals(SpeechResult("Hei", "no"), rumbleResult("Norwegian Bokmål\u0000Hei".encodeToByteArray()))
        assertNull(rumbleResult("French".encodeToByteArray()))
        assertNull(rumbleResult("French\u0000 ".encodeToByteArray()))
    }

    @Test fun rumble2CoversAllDictationLanguagesWhenEnabled() {
        for (language in DictationLanguage.entries) {
            val options = SpeechOptions(Style.Polished, language, false, false, rumble2 = true, rumbleAuto = false)
            assertEquals(language.rumbleName, options.rumbleLanguage)
        }
        val translated = SpeechOptions(Style.Polished, DictationLanguage.Spanish, false, true, rumble2 = true, rumbleAuto = false)
        assertNull(translated.rumbleLanguage)
    }

    @Test fun optedOutOptionalModelNeverAutoResumesPartialDownloads() {
        assertFalse(ModelStore.shouldAutoResume(enabled = false, running = false, waiting = true, hasPart = true, installed = false))
        assertFalse(ModelStore.shouldAutoResume(enabled = false, running = false, waiting = false, hasPart = true, installed = false))
        assertTrue(ModelStore.shouldAutoResume(enabled = true, running = false, waiting = false, hasPart = true, installed = false))
        assertTrue(ModelStore.shouldAutoResume(enabled = true, running = false, waiting = true, hasPart = false, installed = false))
        assertFalse(ModelStore.shouldAutoResume(enabled = true, running = true, waiting = true, hasPart = true, installed = false))
        assertFalse(ModelStore.shouldAutoResume(enabled = true, running = false, waiting = true, hasPart = true, installed = true))
    }
}
