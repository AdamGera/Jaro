package ai.jard.jaro.speech

import android.app.Activity
import android.app.Application
import android.content.Context
import android.os.Bundle
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.URL
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import javax.net.ssl.HttpsURLConnection

/** Model downloads are explicit, pinned GET requests from Jaro settings. */
internal class ModelStore private constructor(
    private val name: String,
    private val size: Long,
    private val hash: String,
    private val url: String,
    private val extension: String,
    private val enabled: (Context) -> Boolean = { true },
) {
    companion object {
        val small = ModelStore("whisper-small", 190085487L, "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small-q5_1.bin", "bin")
        val english = ModelStore("parakeet-tdt_ctc-110m", 135373280L, "7dd44c74a331d788a4e5f8b16913b3feb29ced22cf5613aad0e0f6cd30516296",
            "https://huggingface.co/handy-computer/parakeet-tdt_ctc-110m-gguf/resolve/1c3f0d5378fdd358905cc410a93ed9e64f52d170/parakeet-tdt_ctc-110m-Q8_0.gguf",
            "gguf", enabled = { SpeechPreferences(it).fasterEnglish })
        val rumble2 = ModelStore("rumble-2-flash-q4_k", 444897792L, "0e4c0169bfd99973e501b1ecaab946e5589ada4fc110b458a4c029423759cdb2",
            "https://huggingface.co/cstr/rumble-2-flash-GGUF/resolve/780b126551f62603755e534952ad2942ac0ebcef/rumble-2-flash-q4_k.gguf",
            "gguf", enabled = { SpeechPreferences(it).rumble2 })

        internal fun shouldAutoResume(
            enabled: Boolean,
            running: Boolean,
            waiting: Boolean,
            hasPart: Boolean,
            installed: Boolean,
        ): Boolean = enabled && !running && (waiting || hasPart) && !installed
    }

    enum class Problem { Connection, Server, Space }
    sealed interface State {
        data object Available : State
        data object Ready : State
        data class Retry(val problem: Problem) : State
        data class Loading(val percent: Int) : State
    }
    private class Failure(val problem: Problem) : Exception()
    private class Cancelled : Exception()
    private val progress = MutableStateFlow<State>(State.Available)
    val state = progress.asStateFlow()
    private var running = false
    private var waiting = false
    private var watching = false
    private var started = 0
    private var stops = 0
    @Volatile private var cancelled = false
    private var restartAfterCancel = false
    @Volatile private var activeConnection: HttpsURLConnection? = null
    private fun path(context: Context) = File(context.noBackupFilesDir, "$name-$hash.$extension")
    private fun part(context: Context) = File(context.noBackupFilesDir, "$name-$hash.part")
    fun installed(context: Context) = path(context).takeIf { it.isFile && it.length() == size }

    private var verifiedTimestamp: Long? = null

    /** Hash the optional model once per process and after any modification. */
    @Synchronized fun verified(context: Context): File? {
        val file = installed(context) ?: return null
        val timestamp = file.lastModified()
        if (verifiedTimestamp == timestamp) return file
        val digest = MessageDigest.getInstance("SHA-256")
        val read = runCatching { file.inputStream().use { input -> pump(input) { buffer, count ->
            digest.update(buffer, 0, count)
        } } }.isSuccess
        if (read && checksum(digest) == hash) {
            verifiedTimestamp = timestamp
            return file
        }
        file.delete()
        verifiedTimestamp = null
        if (!running) progress.value = State.Available
        return null
    }

    @Synchronized fun refresh(context: Context) {
        val app = context.applicationContext as Application
        if (!watching) { watching = true; app.registerActivityLifecycleCallbacks(foreground) }
        if (installed(app) != null) progress.value = State.Ready
        else if (progress.value == State.Ready) progress.value = State.Available
    }

    // Android closes a backgrounded app's connections, and a swipe from recents or a reboot ends the process.
    // A started download keeps its partial file and carries on whenever a Jaro screen is in front again.
    private val foreground = object : Application.ActivityLifecycleCallbacks {
        override fun onActivityStarted(activity: Activity) {
            if (activity is ai.jard.jaro.QuickDictationActivity) return
            synchronized(this@ModelStore) {
                if (started++ == 0 && shouldAutoResume(
                        enabled = enabled(activity),
                        running = running,
                        waiting = waiting,
                        hasPart = part(activity).exists(),
                        installed = installed(activity) != null,
                    )
                ) download(activity)
            }
        }
        override fun onActivityStopped(activity: Activity) {
            if (activity is ai.jard.jaro.QuickDictationActivity) return
            synchronized(this@ModelStore) { if (--started == 0) stops++ }
        }
        override fun onActivityCreated(activity: Activity, state: Bundle?) = Unit
        override fun onActivityResumed(activity: Activity) = Unit
        override fun onActivityPaused(activity: Activity) = Unit
        override fun onActivitySaveInstanceState(activity: Activity, state: Bundle) = Unit
        override fun onActivityDestroyed(activity: Activity) = Unit
    }

    /** Stops an active download and drops any partial file so an opted-out model never auto-resumes. */
    @Synchronized fun cancel(context: Context) {
        val app = context.applicationContext
        waiting = false
        restartAfterCancel = false
        if (running) {
            cancelled = true
            activeConnection?.disconnect()
        } else {
            part(app).delete()
            progress.value = if (installed(app) != null) State.Ready else State.Available
        }
    }

    @Synchronized fun download(context: Context) {
        val app = context.applicationContext
        if (installed(app) != null) { progress.value = State.Ready; return }
        if (running) {
            if (cancelled) {
                restartAfterCancel = true
                progress.value = State.Loading(0)
            }
            return
        }
        running = true; waiting = false; cancelled = false; restartAfterCancel = false
        val part = part(app)
        progress.value = State.Loading((part.length().coerceIn(0, size) * 100 / size).toInt().coerceAtMost(99))
        val since = stops
        Thread({
            val result = try {
                val resumed = part.length() > 0
                // A resumed file that fails the checksum is discarded and fetched once more from the start.
                if (transfer(app, part) || resumed && !cancelled && transfer(app, part)) State.Ready else State.Retry(Problem.Server)
            } catch (_: Cancelled) { State.Available }
            catch (e: Failure) { State.Retry(e.problem) }
            catch (_: IOException) {
                if (cancelled) State.Available
                else State.Retry(if (app.noBackupFilesDir.usableSpace < 8_000_000) Problem.Space else Problem.Connection)
            }
            catch (_: Exception) { if (cancelled) State.Available else State.Retry(Problem.Server) }
            synchronized(this) {
                running = false
                activeConnection = null
                if (cancelled) {
                    cancelled = false
                    part.delete()
                    val restart = restartAfterCancel
                    restartAfterCancel = false
                    if (restart && enabled(app)) download(app)
                    else progress.value = if (installed(app) != null) State.Ready else State.Available
                } else if (result == State.Retry(Problem.Connection) && (started == 0 || stops != since) && enabled(app)) {
                    waiting = true
                    if (started > 0) download(app)
                } else progress.value = result
            }
        }, "Jaro model download").start()
    }

    /** Returns false, after deleting the partial file, when the finished file fails the checksum. */
    private fun transfer(app: Context, part: File): Boolean {
        if (cancelled) throw Cancelled()
        if (part.length() > size) part.delete()
        if (app.noBackupFilesDir.usableSpace <= size - part.length() + 8_000_000) throw Failure(Problem.Space)
        val digest = MessageDigest.getInstance("SHA-256")
        var total = part.length()
        if (total > 0) part.inputStream().use { input -> pump(input) { buffer, count ->
            if (cancelled) throw Cancelled()
            digest.update(buffer, 0, count)
        } }
        var http = open(total)
        if (total > 0 && if (http == null) total < size else http.responseCode == 206 && !resumes(http, total)) {
            http?.disconnect(); part.delete(); total = 0; digest.reset()
            http = open(0) ?: throw Failure(Problem.Server)
        }
        if (http != null) try {
            activeConnection = http
            if (cancelled) throw Cancelled()
            if (http.responseCode == 200) { total = 0; digest.reset() }
            http.inputStream.use { input ->
                FileOutputStream(part, total > 0).use { output ->
                    pump(input) { buffer, count ->
                        if (cancelled) throw Cancelled()
                        total += count
                        if (total > size) throw Failure(Problem.Server)
                        digest.update(buffer, 0, count); output.write(buffer, 0, count)
                        progress.value = State.Loading((total * 100 / size).toInt().coerceAtMost(99))
                    }
                    output.fd.sync()
                }
            }
        } finally { activeConnection = null; http.disconnect() }
        if (cancelled) throw Cancelled()
        if (total < size) throw IOException("Model download ended early")
        if (checksum(digest) != hash) { part.delete(); return false }
        Files.move(part.toPath(), path(app).toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        return true
    }

    private fun checksum(digest: MessageDigest) = digest.digest().joinToString("") { "%02x".format(it) }

    private inline fun pump(input: InputStream, each: (ByteArray, Int) -> Unit) {
        val buffer = ByteArray(128 * 1024)
        while (true) {
            val count = input.read(buffer)
            if (count == -1) break
            each(buffer, count)
        }
    }

    private fun resumes(http: HttpsURLConnection, from: Long) =
        http.getHeaderField("Content-Range") == "bytes $from-${size - 1}/$size"

    /** Opens the model at [from] bytes; null when the server says nothing is left to send. */
    private fun open(from: Long): HttpsURLConnection? {
        var target = URL(url)
        repeat(6) {
            val host = target.host.lowercase()
            if (target.protocol != "https" || target.userInfo != null || target.port !in listOf(-1, 443) ||
                !(host == "huggingface.co" || host.endsWith(".cdn.hf.co") || host == "cas-bridge.xethub.hf.co")) throw Failure(Problem.Server)
            val http = target.openConnection() as HttpsURLConnection
            try {
                http.instanceFollowRedirects = false
                http.connectTimeout = 15_000; http.readTimeout = 45_000
                // Android's default user agent includes the phone model and OS build.
                http.setRequestProperty("User-Agent", "JaroModelDownloader/1")
                http.setRequestProperty("Accept-Encoding", "identity")
                if (from > 0) http.setRequestProperty("Range", "bytes=$from-")
                when (http.responseCode) {
                    200, 206 -> return http
                    416 -> { http.disconnect(); return null }
                    301, 302, 303, 307, 308 -> target = URL(target, http.getHeaderField("Location") ?: throw Failure(Problem.Server))
                    else -> throw Failure(Problem.Server)
                }
            } catch (e: Exception) { http.disconnect(); throw e }
            http.disconnect()
        }
        throw Failure(Problem.Server)
    }
}
