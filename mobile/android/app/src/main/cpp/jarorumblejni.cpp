#include <jni.h>

#include <algorithm>
#include <cstring>
#include <vector>

#include "jaro_rumble.h"

extern "C" JNIEXPORT jlong JNICALL
Java_ai_jard_jaro_speech_Rumble2_load(JNIEnv *env, jobject, jstring file, jint threads) {
    const char *path = env->GetStringUTFChars(file, nullptr);
    if (!path) return 0;
    auto *model = jaro_rumble_open(path, threads);
    env->ReleaseStringUTFChars(file, path);
    return reinterpret_cast<jlong>(model);
}

extern "C" JNIEXPORT void JNICALL
Java_ai_jard_jaro_speech_Rumble2_free(JNIEnv *, jobject, jlong handle) {
    jaro_rumble_close(reinterpret_cast<jaro_rumble_model *>(handle));
}

extern "C" JNIEXPORT void JNICALL
Java_ai_jard_jaro_speech_Rumble2_abort(JNIEnv *, jobject, jlong job) {
    jaro_rumble_cancel(static_cast<uint64_t>(job));
}

extern "C" JNIEXPORT jbyteArray JNICALL
Java_ai_jard_jaro_speech_Rumble2_decode(JNIEnv *env, jobject, jlong handle, jshortArray samples,
                                       jint count, jstring language, jlong job) {
    auto *model = reinterpret_cast<jaro_rumble_model *>(handle);
    if (!model || !samples || count <= 0 || count > JARO_RUMBLE_MAX_SAMPLES || count > env->GetArrayLength(samples)) return nullptr;

    std::vector<float> pcm(count);
    jshort page[2048];
    for (int i = 0; i < count; i += 2048) {
        const int n = std::min(2048, count - i);
        env->GetShortArrayRegion(samples, i, n, page);
        if (env->ExceptionCheck()) return nullptr;
        for (int j = 0; j < n; ++j) pcm[i + j] = page[j] / 32768.0f;
    }
    const char *name = language ? env->GetStringUTFChars(language, nullptr) : nullptr;
    if (language && !name) return nullptr;
    char detected[JARO_RUMBLE_LANGUAGE_CAPACITY];
    char *text = jaro_rumble_transcribe_detected(model, pcm.data(), count, name, static_cast<uint64_t>(job), detected);
    if (name) env->ReleaseStringUTFChars(language, name);
    if (!text) return nullptr;
    const jsize size = static_cast<jsize>(std::strlen(text));
    const jsize prefixSize = static_cast<jsize>(std::strlen(detected) + 1);
    jbyteArray result = env->NewByteArray(prefixSize + size);
    if (result) {
        env->SetByteArrayRegion(result, 0, prefixSize, reinterpret_cast<const jbyte *>(detected));
        env->SetByteArrayRegion(result, prefixSize, size, reinterpret_cast<const jbyte *>(text));
    }
    jaro_rumble_free_text(text);
    return result;
}
