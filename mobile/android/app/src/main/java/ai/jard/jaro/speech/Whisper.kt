package ai.jard.jaro.speech

import android.app.ActivityManager
import android.content.Context
import android.content.res.AssetManager
import ai.jard.jaro.BuildConfig
import ai.jard.jaro.typing.TypingModel
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

internal data class SpeechResult(val text: String, val language: String)

internal interface Recognizer {
    /** Loads the model ahead of a dictation, while it records (on the transcription thread). */
    fun prepare(options: SpeechOptions) {}
    /** [job] names this decode, so [abort] can stop it. */
    fun decode(pcm: ShortArray, count: Int, options: SpeechOptions, job: Long): SpeechResult?
    /** Stops [job]'s decode, from any thread, if it is running: the person discarded it. */
    fun abort(job: Long) {}
}

internal class LocalSpeech(private val app: Context) : Recognizer {
    override fun prepare(options: SpeechOptions) = SpeechModel.prepare(app, options)
    override fun decode(pcm: ShortArray, count: Int, options: SpeechOptions, job: Long): SpeechResult? =
        SpeechModel.decode(app, pcm, count, options, job)
    override fun abort(job: Long) = SpeechModel.abort(job)
}

/** The one speech model in Jaro's process, shared by the keyboard and Home. It loads when a dictation
 * starts, stays loaded between dictations and is freed
 * after [KEEP_MS] without one, or when Android asks for memory. Nothing runs while it waits. */
internal object SpeechModel {
    private const val KEEP_MS = 120_000L
    private val timer = Executors.newSingleThreadScheduledExecutor { Thread(it, "Jaro speech model").apply { isDaemon = true } }
    private var pending: ScheduledFuture<*>? = null
    private var handle = 0L
    private var english = 0L
    private var englishWords: TypingModel? = null
    @Volatile private var englishAvailable: Boolean? = null
    private var rumble = 0L
    @Volatile private var rumbleAvailable: Boolean? = null
    /** The loaded model: the downloaded file's path, "" for the bundled one, null for none. */
    private var loaded: String? = null

    /** One thread per fast core, at most four: the work is split evenly, so a slow core holds up the
     * rest. A core is fast with at least half the strongest core's capacity as the kernel rates it
     * (cpu_capacity: 2 of 8 on a Snapdragon 695, all 8 on an 8 Elite); unrated cores all count. */
    private val threads: Int by lazy {
        val cores = Runtime.getRuntime().availableProcessors()
        val capacity = (0 until cores).mapNotNull {
            runCatching { File("/sys/devices/system/cpu/cpu$it/cpu_capacity").readText().trim().toInt() }.getOrNull()
        }
        val fast = if (capacity.size == cores && capacity.max() > 0) capacity.count { it * 2 >= capacity.max() } else cores
        fast.coerceIn(1, 4)
    }

    @Synchronized fun prepare(app: Context, options: SpeechOptions) {
        val warmRumble = BuildConfig.RUMBLE2_ENABLED && options.rumbleLanguage != null && loadRumble(app)
        if (!warmRumble && (!options.useFasterEnglish || !loadEnglish(app))) load(app)
        keep()
    }

    /** The last decode stopped by [abort]. */
    @Volatile private var abandoned = 0L

    @Synchronized fun decode(app: Context, pcm: ShortArray, count: Int, options: SpeechOptions, job: Long): SpeechResult? {
        try {
            val rumbleLanguage = if (BuildConfig.RUMBLE2_ENABLED) options.rumbleLanguage(count) else null
            if (rumbleLanguage != null && abandoned != job && loadRumble(app) && abandoned != job) {
                val name = rumbleLanguage.takeUnless { it == Rumble2.AUTOMATIC }
                val result = Rumble2.decode(rumble, pcm, count, name, job)?.let(::rumbleResult)
                if (result != null) return result
            }
            if (abandoned == job) return null
            if (options.useFasterEnglish && loadEnglish(app) && abandoned != job) {
                val terms = options.vocabulary.takeIf { it.isNotEmpty() }?.toTypedArray()
                val text = Parakeet.decode(english, pcm, count, terms, job)?.decodeToString()?.let { decoded ->
                    runCatching { SentenceEnds.restore(decoded, pcm, count) { word -> lowercaseEnglish(app, word) } }
                        .getOrElse { decoded.substringBefore('\n') }.trim()
                }
                if (!text.isNullOrEmpty()) return SpeechResult(text, options.outputLanguage)
            }
            if (abandoned == job) return null
            val small = load(app)
            val prompt = options.vocabulary.takeIf { it.isNotEmpty() }?.joinToString(", ")
            if (handle == 0L || abandoned == job) return null
            var bytes = Whisper.decode(handle, small, pcm, count, threads, options.spoken.code, options.translate, prompt, job)
            // Unusable downloaded weights fall back to the bundled model.
            if (bytes == null && small && abandoned != job) {
                unload()
                if (open(app, null) && abandoned != job) {
                    bytes = Whisper.decode(handle, false, pcm, count, threads, options.spoken.code, options.translate, prompt, job)
                }
            }
            return bytes?.let { SpeechResult(it.decodeToString().trim(), options.outputLanguage) }
        } finally {
            keep()
        }
    }

    /** Called independently of the decode lock. Native code checks this job number during inference. */
    fun abort(job: Long) {
        abandoned = job
        Whisper.abort(job)
        if (englishAvailable == true) Parakeet.abort(job)
        if (rumbleAvailable == true) Rumble2.abort(job)
    }

    /** Frees the model off the caller's thread (it may wait for a dictation in progress). */
    fun releaseSoon() { timer.execute(::release) }

    @Synchronized private fun release() {
        pending?.cancel(false)
        pending = null
        unload()
        freeEnglish()
        freeRumble()
    }

    /** Returns whether the downloaded model is the one loaded. */
    private fun load(app: Context): Boolean {
        val path = ModelStore.small.installed(app)?.absolutePath
        if (handle != 0L && loaded == (path ?: "")) return path != null
        unload()
        freeEnglish()
        freeRumble()
        if (path != null && open(app, path)) return true
        open(app, null)
        return false
    }

    private fun open(app: Context, path: String?): Boolean {
        handle = Whisper.load(app.assets, path)
        loaded = if (handle != 0L) path ?: "" else null
        return handle != 0L
    }

    private fun unload() { if (handle != 0L) Whisper.free(handle); handle = 0L; loaded = null }

    private fun loadEnglish(app: Context): Boolean {
        if (english != 0L) return true
        if (englishAvailable == false) return false
        val file = ModelStore.english.verified(app) ?: return false
        freeRumble()
        if (!enoughMemory(app, 350_000_000L)) return false
        unload()
        return try {
            english = Parakeet.load(file.absolutePath, threads)
            englishAvailable = true
            english != 0L
        } catch (_: LinkageError) {
            englishAvailable = false
            false
        }
    }

    private fun lowercaseEnglish(app: Context, word: String): Boolean {
        val model = englishWords ?: TypingModel.asset(app.assets, "en").also { englishWords = it }
        return model.index(word) >= 0 && (model.capitals?.percent(word) ?: -1) < 0
    }

    private fun freeEnglish() {
        if (english != 0L) Parakeet.free(english)
        english = 0L
    }

    private fun loadRumble(app: Context): Boolean {
        if (rumble != 0L) {
            // Re-check headroom before allocating decode scratch/KV buffers (~540 MiB peak on top of open).
            if (enoughMemory(app, 640L shl 20)) return true
            freeRumble()
            return false
        }
        if (rumbleAvailable == false || ModelStore.rumble2.installed(app) == null) return false
        // A 10-second decode needed about 720 MiB in the ARM64 test, before keyboard and host memory.
        if (!enoughMemory(app, 1L shl 30)) return false
        val file = ModelStore.rumble2.verified(app) ?: return false
        unload()
        freeEnglish()
        return try {
            rumble = Rumble2.load(file.absolutePath, threads)
            rumbleAvailable = true
            rumble != 0L
        } catch (_: LinkageError) {
            rumbleAvailable = false
            false
        }
    }

    private fun freeRumble() {
        if (rumble != 0L) Rumble2.free(rumble)
        rumble = 0L
    }

    private fun enoughMemory(app: Context, minimum: Long): Boolean {
        val manager = app.getSystemService(ActivityManager::class.java) ?: return false
        val memory = ActivityManager.MemoryInfo().also(manager::getMemoryInfo)
        return !memory.lowMemory && memory.availMem >= minimum
    }

    private fun keep() {
        pending?.cancel(false)
        pending = timer.schedule(::release, KEEP_MS, TimeUnit.MILLISECONDS)
    }
}

internal object Whisper {
    init { System.loadLibrary("jarospeech") }
    external fun load(assets: AssetManager, file: String?): Long
    external fun free(handle: Long)
    external fun decode(handle: Long, small: Boolean, pcm: ShortArray, count: Int, threads: Int,
                        language: String, translate: Boolean, prompt: String?, job: Long): ByteArray?
    external fun abort(job: Long)
}
