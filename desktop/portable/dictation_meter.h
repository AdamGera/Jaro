#ifndef JARO_DICTATION_METER_H
#define JARO_DICTATION_METER_H

#include <math.h>

enum {
    METER_WIDTH = 68, METER_HEIGHT = 24, METER_BARS = 5,
    METER_GAP = 6, METER_BAR_WIDTH = 3, METER_MARGIN = 26
};

static inline double meter_height(int bar, double elapsed, double level, int processing) {
    const double envelope[] = {0.45, 0.76, 1.0, 0.76, 0.45};
    double ripple = 0.5 + 0.5 * sin(elapsed * 11.0 - bar * 1.2);
    if (processing) return 3.0 + 5.0 * ripple * ripple * ripple;
    return 3.0 + envelope[bar] * (2.0 * ripple + 13.0 * level * (0.48 + 0.52 * ripple));
}

/* A private pipe carries stage and level only. A partial line keeps the old frame. */
typedef struct {
    char line[32];
    unsigned int used;
    int processing;
    double level;
} Meter;

static inline void meter_read(Meter *meter, const char *data, unsigned int length) {
    for (unsigned int i = 0; i < length; i++) {
        if (data[i] != '\n') {
            if (meter->used < sizeof(meter->line) - 1) meter->line[meter->used++] = data[i];
            continue;
        }
        meter->line[meter->used] = '\0';
        if (meter->used == 1 && meter->line[0] == 'P') {
            meter->processing = 1;
        } else if (meter->line[0] == 'R') {
            unsigned int level = 0;
            for (unsigned int j = 1; j < meter->used && level < 100; j++) {
                char digit = meter->line[j];
                if (digit < '0' || digit > '9') break;
                level = level * 10 + (unsigned int)(digit - '0');
            }
            meter->processing = 0;
            meter->level = (level > 100 ? 100 : level) / 100.0;
        }
        meter->used = 0;
    }
}

#endif
