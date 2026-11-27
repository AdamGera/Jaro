#ifndef JARO_RUMBLE_H
#define JARO_RUMBLE_H

#include <stdint.h>

#if defined(__GNUC__)
#define JARO_RUMBLE_API __attribute__((visibility("default")))
#else
#define JARO_RUMBLE_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define JARO_RUMBLE_SAMPLE_RATE 16000
#define JARO_RUMBLE_MAX_SAMPLES (10 * JARO_RUMBLE_SAMPLE_RATE)
#define JARO_RUMBLE_LANGUAGE_CAPACITY 48

typedef struct jaro_rumble_model jaro_rumble_model;

/* The model must be a regular file at an absolute path. */
JARO_RUMBLE_API jaro_rumble_model *jaro_rumble_open(const char *path, int threads);

/* Use mono, normalized floats at 16 kHz and a unique nonzero job ID. Pass
 * NULL for automatic language detection or the English name of a language.
 * Returns UTF-8, or NULL for silence, cancellation or a failed inference. */
JARO_RUMBLE_API char *jaro_rumble_transcribe(jaro_rumble_model *model, const float *pcm,
                                          int samples, const char *language_name, uint64_t job);

/* Also writes the model's language name, when available, into a buffer of
 * JARO_RUMBLE_LANGUAGE_CAPACITY bytes. An empty name means undetermined. */
JARO_RUMBLE_API char *jaro_rumble_transcribe_detected(jaro_rumble_model *model, const float *pcm,
                                                   int samples, const char *language_name, uint64_t job,
                                                   char *detected_language);

/* Cancellation is cooperative. Keep the model alive until all calls return. */
JARO_RUMBLE_API void jaro_rumble_cancel(uint64_t job);
JARO_RUMBLE_API void jaro_rumble_free_text(char *text);
JARO_RUMBLE_API void jaro_rumble_close(jaro_rumble_model *model);

#ifdef __cplusplus
}
#endif

#endif
