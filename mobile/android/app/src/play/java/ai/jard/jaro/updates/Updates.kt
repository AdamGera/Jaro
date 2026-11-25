package ai.jard.jaro.updates

import android.content.Context
import androidx.compose.runtime.Composable
import ai.jard.jaro.keyboard.StripNotice

/** Play builds (debug and release) have no update system: Google Play updates them, and Play forbids an app
 * updating itself any other way. Jaro Preview's updater is in src/preview. These do nothing and are inlined
 * where they are called, so not even this object reaches the Play build's code. */
@Suppress("NOTHING_TO_INLINE", "UNUSED_PARAMETER")
internal object Updates {
    const val isPreview = false
    inline fun keyboardHidden() = Unit
    inline fun appOpened(context: Context) = Unit
    inline fun keyboardShown(context: Context, secure: Boolean, show: (StripNotice?) -> Unit) = Unit
    @Composable inline fun HomeCard() = Unit
    @Composable inline fun SettingsCard() = Unit
}
