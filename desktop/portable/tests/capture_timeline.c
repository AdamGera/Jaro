#include <assert.h>
#include <stddef.h>

#include "../capture_timeline.h"

int main(void) {
    assert(capture_frame(10000000) == CAPTURE_RATE);
    assert(capture_frame(-1000000) == -1600);

    CaptureTimeline mic = {0}, system = {0};
    const int16_t early[] = {99, 10, 20, 30};
    capture_packet(&mic, 0, -1, 0, early, 4, true, false);
    capture_packet(&system, 0, 2, 0, early + 1, 2, true, false);
    assert(capture_take(&mic, 0) == 10);
    assert(capture_take(&mic, 1) == 20);
    assert(capture_take(&system, 0) == 0);
    assert(capture_take(&system, 1) == 0);
    assert(capture_take(&mic, 2) == 30);
    assert(capture_take(&system, 2) == 10);

    // An unreliable clock continues from the previous packet, while a gap
    // explicitly reported by the driver stays empty on the same timeline.
    capture_packet(&mic, 3, 20, 4, early, 2, false, false);
    capture_packet(&mic, 3, 7, 7, early + 1, 1, true, true);
    assert(capture_take(&mic, 3) == 99);
    assert(capture_take(&mic, 4) == 10);
    assert(capture_take(&mic, 5) == 0);
    assert(capture_take(&mic, 7) == 10);

    capture_packet(&mic, 8, 150, 8, early, 2, true, false);
    assert(capture_take(&mic, 8) == 99);
    assert(capture_take(&mic, 9) == 10);

    // Late data cannot overwrite committed frames or reappear on ring wrap.
    capture_packet(&mic, 10, 7, 7, early, 2, true, true);
    assert(capture_take(&mic, QUEUE_FRAMES + 7) == 0);
    capture_packet(&system, QUEUE_FRAMES - 1, QUEUE_FRAMES - 1, 0, early, 2, true, true);
    assert(capture_take(&system, QUEUE_FRAMES - 1) == 99);
    assert(capture_take(&system, QUEUE_FRAMES) == 10);
    capture_packet(&system, QUEUE_FRAMES + 1, QUEUE_FRAMES + 1, 2, NULL, 2, true, false);
    assert(capture_take(&system, QUEUE_FRAMES + 1) == 0);
    return 0;
}
