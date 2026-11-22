#ifndef JARO_CAPTURE_TIMELINE_H
#define JARO_CAPTURE_TIMELINE_H

#include <stdbool.h>
#include <stdint.h>

enum { CAPTURE_RATE = 16000, QUEUE_FRAMES = CAPTURE_RATE * 2, TIMESTAMP_JITTER = CAPTURE_RATE / 50 };

typedef struct {
    int16_t samples[QUEUE_FRAMES];
    int64_t through;
    uint64_t device_through;
    bool has_packet;
} CaptureTimeline;

static inline int64_t capture_frame(int64_t elapsed_100ns) {
    return (elapsed_100ns * CAPTURE_RATE + (elapsed_100ns < 0 ? -5000000 : 5000000)) / 10000000;
}

static inline void capture_packet(CaptureTimeline *track, int64_t output_frame,
                                  int64_t first_frame, uint64_t device_frame,
                                  const int16_t *samples, uint32_t length,
                                  bool timed, bool discontinuity) {
    // Device positions preserve continuity when adjacent QPC timestamps jitter.
    // Re-anchor on a reported discontinuity or a difference outside the jitter
    // budget. Both tracks still share the output clock.
    if (track->has_packet && device_frame >= track->device_through) {
        uint64_t gap = device_frame - track->device_through;
        if (gap < QUEUE_FRAMES) {
            int64_t expected = track->through + (int64_t)gap;
            int64_t difference = first_frame - expected;
            if (!timed || (!discontinuity && difference >= -TIMESTAMP_JITTER && difference <= TIMESTAMP_JITTER)) {
                first_frame = expected;
            }
        }
    }
    track->through = first_frame + length;
    track->device_through = device_frame + length;
    track->has_packet = true;
    int64_t start = first_frame > output_frame ? first_frame : output_frame;
    int64_t end = track->through < output_frame + QUEUE_FRAMES ? track->through : output_frame + QUEUE_FRAMES;
    for (int64_t frame = start; frame < end; ++frame) {
        track->samples[frame % QUEUE_FRAMES] = samples ? samples[frame - first_frame] : 0;
    }
}

static inline int16_t capture_take(CaptureTimeline *track, int64_t frame) {
    int64_t index = frame % QUEUE_FRAMES;
    int16_t sample = track->samples[index];
    track->samples[index] = 0;
    return sample;
}

#endif
