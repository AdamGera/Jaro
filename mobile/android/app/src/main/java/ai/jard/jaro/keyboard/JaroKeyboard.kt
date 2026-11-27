package ai.jard.jaro.keyboard

import android.Manifest
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.database.ContentObserver
import android.os.Handler
import android.os.Looper
import android.provider.UserDictionary
import java.util.concurrent.Executors
import android.inputmethodservice.InputMethodService
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import ai.jard.jaro.MainActivity
import ai.jard.jaro.R
import ai.jard.jaro.updates.Updates
import ai.jard.jaro.speech.LocalSpeech
import ai.jard.jaro.speech.MicCapture
import ai.jard.jaro.speech.Signature
import ai.jard.jaro.speech.SpeechPhase
import ai.jard.jaro.speech.SpeechResult
import ai.jard.jaro.speech.SpeechModel
import ai.jard.jaro.speech.SpeechPreferences
import ai.jard.jaro.speech.Spoken
import ai.jard.jaro.speech.typingLayout
import ai.jard.jaro.typing.PersonalWords
import ai.jard.jaro.typing.SpatialDecoder
import ai.jard.jaro.typing.TypingModel
import ai.jard.jaro.typing.TypingPreferences

/** The input method: the editor's lifecycle, the views and dictation. Typing lives in [KeyboardEngine]. */
/** Dictation's notes arrive from MicCapture as English sentences: the ones a person sees, in the phone's
 * language (one not listed here shows as it is). */
internal fun Context.speechNote(note: String?) = when (note) {
    "Allow the microphone in Jaro" -> getString(R.string.keyboard_hint_microphone)
    "Microphone unavailable. Try again." -> getString(R.string.keyboard_note_microphone)
    "Couldn’t transcribe. Try again." -> getString(R.string.keyboard_note_transcribe)
    "No speech heard" -> getString(R.string.keyboard_note_no_speech)
    "Speech model unavailable" -> getString(R.string.keyboard_note_model)
    "Stopped at 1 minute" -> getString(R.string.keyboard_note_minute)
    else -> note
}

class JaroKeyboard : InputMethodService() {
    private var view: KeyboardView? = null
    private lateinit var capture: MicCapture
    private val dictationField = DictationField()
    internal val preferences by lazy { SpeechPreferences(this) }
    internal var nextEnglish: Boolean? = null
    /** The options the dictation in flight started with: its language decides mid-sentence case. */
    internal val feedback by lazy { TypingPreferences(this) }
    internal val layoutLanguage get() = feedback.layout ?: preferences.spoken.layout ?: Spoken.English
    /** The keys' typing model, or none when the layout is only English's stand-in for a dictation language
     * without one: English keys and accents, but no English next words or autocorrections for its text. */
    internal val typingLanguage get() = typingLayout(feedback.layout, preferences.spoken)?.layoutCode
    // Android's personal dictionary is read off the main thread, and again whenever it changes.
    private val words = Executors.newSingleThreadExecutor { Thread(it, "Jaro personal words") }
    private val dictionaryChanged = object : ContentObserver(Handler(Looper.getMainLooper())) {
        override fun onChange(selfChange: Boolean) = engine.personalWordsChanged()
    }
    internal val engine by lazy {
        KeyboardEngine(InputConnectionTarget(this), feedback, { layoutLanguage }, { typingLanguage },
            { SpatialDecoder(TypingModel.asset(assets, it)) },
            readPersonal = { PersonalWords.read(contentResolver, it) }, background = words,
            emoji = EmojiWords(assets, words))
    }
    private val typingChanged = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
        if (key == TypingPreferences.SUGGEST_SWEAR_WORDS) engine.swearWordsChanged()
        if (key == TypingPreferences.NUMBER_ROW) engine.numberRowChanged()
        if (key == TypingPreferences.LOCAL_CURRENCY) engine.localCurrencyChanged()
        if (key == TypingPreferences.EMOJI_SUGGESTIONS) engine.emojiSuggestionsChanged()
        if (key == TypingPreferences.KEYBOARD_TOOLS) { engine.keyboardToolsChanged(); watchClipboard() }
    }
    // The strip's Paste tool comes and goes with the clipboard (Labs › Keyboard › Keyboard tools): the
    // listener exists only while the lab is on and the input view is shown.
    private val clipboard by lazy { getSystemService(ClipboardManager::class.java) }
    private val clipboardChanged = ClipboardManager.OnPrimaryClipChangedListener { engine.clipboardChanged() }
    private var viewing = false
    private var watchingClipboard = false
    private fun watchClipboard() {
        val watch = viewing && feedback.keyboardTools
        if (watch == watchingClipboard) return
        watchingClipboard = watch
        if (watch) clipboard.addPrimaryClipChangedListener(clipboardChanged)
        else clipboard.removePrimaryClipChangedListener(clipboardChanged)
    }
    internal val previewAllowed get() = !engine.secure

    override fun onCreate() {
        setTheme(R.style.JaroIme)
        super.onCreate()
        getSharedPreferences("typing", MODE_PRIVATE).registerOnSharedPreferenceChangeListener(typingChanged)
        contentResolver.registerContentObserver(UserDictionary.Words.CONTENT_URI, true, dictionaryChanged)
        capture = MicCapture(LocalSpeech(applicationContext),
            update = { phase, note -> view?.status(phase, speechNote(note) ?: idleHint(), note != null) },
            meter = { view?.setLevel(it) }, insert = ::commitSpeech)
    }
    override fun onEvaluateFullscreenMode() = false
    override fun onCreateInputView(): View = KeyboardView(this, this).also {
        it.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) = Unit
            override fun onViewDetachedFromWindow(v: View) {
                Updates.keyboardHidden()
                viewing = false
                watchClipboard()
            }
        })
        view = it; engine.surface = it
    }

    /** The field's type as Jaro last started in it: an app can change it in place (restarting the same box). */
    private var fieldType = 0 to 0
    private var typeChanged = false
    override fun onStartInput(attribute: EditorInfo, restarting: Boolean) {
        super.onStartInput(attribute, restarting)
        typeChanged = restarting && fieldType != attribute.inputType to attribute.imeOptions
        fieldType = attribute.inputType to attribute.imeOptions
        nextEnglish = null
        // Android can restart input when focus moves between two web or Compose fields.
        dictationField.start()
        capture.cancel()
        engine.startInput(attribute)
    }
    override fun onStartInputView(info: EditorInfo, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        view?.cancelGestures()
        // Another type in the same box: back to its keys, as a new field opens (configureTyping draws them).
        if (typeChanged) view?.closeMenu(configure = false)
        engine.configureTyping()
        view?.status(capture.phase, idleHint())
        Updates.keyboardShown(this, engine.secure) { view?.notice(it) }
        viewing = true
        watchClipboard()
    }
    override fun onUpdateSelection(oldSelStart: Int, oldSelEnd: Int, newSelStart: Int, newSelEnd: Int,
                                   candidatesStart: Int, candidatesEnd: Int) {
        super.onUpdateSelection(oldSelStart, oldSelEnd, newSelStart, newSelEnd, candidatesStart, candidatesEnd)
        engine.updateSelection(oldSelStart, oldSelEnd, newSelStart, newSelEnd)
    }

    /** The strip's Paste tool: the editor's own paste, so Jaro never reads the clipboard itself. */
    internal fun pasteClipboard() {
        if (!engine.secure && currentInputConnection?.performContextMenuAction(android.R.id.paste) == true) engine.pasted()
    }
    /** The strip's Settings tool: Jaro opens on its Keyboard page, not Home. */
    internal fun openKeyboardSettings() = startActivity(Intent(this, MainActivity::class.java)
        .putExtra(MainActivity.EXTRA_PAGE, MainActivity.PAGE_KEYBOARD)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
    internal fun nextIme() {
        capture.cancel()
        if (!switchToNextInputMethod(false)) getSystemService(InputMethodManager::class.java).showInputMethodPicker()
    }
    private fun idleHint() = getString(when {
        engine.secure -> R.string.keyboard_hint_password
        checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED -> R.string.keyboard_hint_microphone
        else -> R.string.keyboard_hint_hold
    })
    internal fun beginSpeech(): Boolean {
        if (dictationField.current == 0L || engine.secure || currentInputConnection == null || !capture.canStart ||
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            view?.status(capture.phase, idleHint())
            return false
        }
        engine.clearForDictation()
        capture.start(preferences.options(nextEnglish), dictationField.current)
        nextEnglish = null
        return true
    }
    internal fun toggleSpeech() = when (capture.phase) {
        // A dictation may start while the one before still transcribes; texts go in in order.
        SpeechPhase.Idle, SpeechPhase.Transcribing -> { beginSpeech(); Unit }
        SpeechPhase.Opening, SpeechPhase.Listening -> finishSpeech()
    }
    internal fun finishSpeech() {
        if (capture.phase == SpeechPhase.Listening || capture.phase == SpeechPhase.Opening) capture.finish()
    }
    internal fun cancelSpeech() = capture.cancelLatest()
    internal fun cancelSpeechGesture() {
        if (isInputViewShown) cancelSpeech() else finishSpeech()
    }

    private fun commitSpeech(result: SpeechResult, field: Long) {
        if (!dictationField.accepts(field, currentInputConnection != null, engine.secure)) return
        // Read now, so a toggle or text changed while it transcribed applies to this dictation. Jaro's own
        // fields, such as Settings' practice box, never get the signature.
        val own = currentInputEditorInfo?.packageName == packageName
        val signed = if (own) null else Signature.append(result.text, preferences.signatureText, preferences.signature)
        engine.insertDictation(signed ?: result.text, result.language, signed != null)
    }
    private fun leaveEditor(fieldGone: Boolean) {
        if (fieldGone) {
            dictationField.finish()
            capture.cancel()
        } else {
            finishSpeech()
        }
        Updates.keyboardHidden()
        viewing = false; watchClipboard()
        view?.reset(); view?.cancelGestures(); engine.leave(); nextEnglish = null
    }
    override fun onFinishInputView(finishingInput: Boolean) {
        leaveEditor(finishingInput)
        super.onFinishInputView(finishingInput)
    }
    override fun onFinishInput() { leaveEditor(fieldGone = true); super.onFinishInput() }
    override fun onWindowHidden() { leaveEditor(fieldGone = false); super.onWindowHidden() }
    // The speech model is a few hundred MB while loaded; give it back when Android runs short. Not on
    // TRIM_MEMORY_UI_HIDDEN, which only says the keyboard hid (every app switch), not that memory is low.
    override fun onTrimMemory(level: Int) {
        super.onTrimMemory(level)
        if (level >= TRIM_MEMORY_RUNNING_LOW && level != TRIM_MEMORY_UI_HIDDEN) SpeechModel.releaseSoon()
    }
    override fun onDestroy() {
        Updates.keyboardHidden()
        getSharedPreferences("typing", MODE_PRIVATE).unregisterOnSharedPreferenceChangeListener(typingChanged)
        contentResolver.unregisterContentObserver(dictionaryChanged); words.shutdownNow()
        viewing = false; watchClipboard()
        view?.cancelGestures(); view = null; engine.surface = null; capture.close(); super.onDestroy()
    }
}
