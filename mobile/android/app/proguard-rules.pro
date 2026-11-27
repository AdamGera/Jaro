# The native speech engines bind these methods by their JNI names.
-keep class ai.jard.jaro.speech.Whisper {
    native <methods>;
}
-keep class ai.jard.jaro.speech.Parakeet {
    native <methods>;
}

-keep class ai.jard.jaro.speech.Rumble2 {
    native <methods>;
}

# Drop diagnostic calls from managed release code, including dependencies.
-assumenosideeffects class android.util.Log {
    public static boolean isLoggable(java.lang.String, int) return false;
    public static int v(...);
    public static int d(...);
    public static int i(...);
    public static int w(...);
    public static int e(...);
    public static int wtf(...);
    public static int println(...);
}
