package ai.jard.jaro.keyboard

/** A result belongs to the bound editor even when Android hides its keyboard window. */
internal class DictationField {
    private var generation = 0L
    var current = 0L
        private set

    fun start() { current = ++generation }
    fun finish() { current = 0L }
    fun accepts(field: Long, connected: Boolean, secure: Boolean) =
        current != 0L && field == current && connected && !secure
}
