package ai.jard.jaro.speech

/** Faster English dictation (Labs): NVIDIA's Parakeet TDT-CTC 110M through transcribe.cpp, in libjaroenglish.so, which
 * is loaded the first time this is used, so never while that switch is off. */
internal object Parakeet {
    init { System.loadLibrary("jaroenglish") }
    /** A session, or 0 if the model could not be loaded. */
    external fun load(file: String, threads: Int): Long
    external fun free(handle: Long)
    /** UTF-8 transcript followed by word timing lines, or null on failure or cancellation.
     * [vocabulary] is the person's own words, in order. */
    external fun decode(handle: Long, pcm: ShortArray, count: Int, vocabulary: Array<String>?, job: Long): ByteArray?
    external fun abort(job: Long)
}
