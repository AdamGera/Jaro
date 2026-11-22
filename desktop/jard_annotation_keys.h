#pragma once
#include <stdbool.h>

typedef enum {
    JardAnnotationUnavailable, JardAnnotationReady, JardAnnotationPicking, JardAnnotationPickingTarget, JardAnnotationRecording,
} JardAnnotationInputMode;
typedef enum { JardAnnotationNoPress, JardAnnotationStartPress, JardAnnotationDrawPress } JardAnnotationPress;
typedef enum {
    JardAnnotationChordDown, JardAnnotationChordUp, JardAnnotationEscapeDown, JardAnnotationEscapeUp,
    JardAnnotationModifiers, JardAnnotationOtherDown, JardAnnotationHoldTimer,
} JardAnnotationKeyEvent;
enum {
    JardAnnotationTake = 1 << 0, JardAnnotationBegin = 1 << 1, JardAnnotationSave = 1 << 2,
    JardAnnotationCancelPicker = 1 << 3, JardAnnotationArmDraw = 1 << 4, JardAnnotationEndDrawing = 1 << 5,
    JardAnnotationConfirmTarget = 1 << 6,
};
enum {
    JardAnnotationOption = 1 << 0, JardAnnotationShift = 1 << 1, JardAnnotationControl = 1 << 2,
    JardAnnotationCommand = 1 << 3, JardAnnotationFn = 1 << 4,
    JardAnnotationChord = JardAnnotationOption | JardAnnotationShift,
};

static const double JardAnnotationHoldDelay = .25;
static const double JardAnnotationStartDebounce = .4;

typedef struct {
    bool chordCaptured, escapeCaptured;
    bool shiftPen, blockShiftUntilUp, heldPen;
    JardAnnotationPress press;
    double pressedAt;
} JardAnnotationKeys;

static inline unsigned JardAnnotationRelease(JardAnnotationKeys *keys, JardAnnotationInputMode mode, double now, bool keyUp) {
    JardAnnotationPress press = keys->press;
    bool drew = keys->heldPen || now - keys->pressedAt >= JardAnnotationHoldDelay;
    unsigned action = keyUp && keys->chordCaptured ? JardAnnotationTake : 0;
    if (keyUp) keys->chordCaptured = false;
    keys->press = JardAnnotationNoPress;
    keys->heldPen = false;
    if (press == JardAnnotationStartPress && mode == JardAnnotationReady) return action | JardAnnotationBegin;
    if (press == JardAnnotationStartPress && mode == JardAnnotationPickingTarget) return action | JardAnnotationConfirmTarget;
    if (press == JardAnnotationDrawPress && mode == JardAnnotationRecording)
        return action | (drew ? JardAnnotationEndDrawing : JardAnnotationSave);
    return action;
}

static inline unsigned JardAnnotationKey(JardAnnotationKeys *keys, JardAnnotationInputMode mode, JardAnnotationKeyEvent event,
        double now, double startedAt, unsigned required, unsigned modifiers, bool repeat, bool latchedPen) {
    bool chordModifiers = (modifiers & required) == required;
    bool shiftDown = (modifiers & JardAnnotationShift) != 0;
    switch (event) {
        case JardAnnotationChordDown:
            if (keys->chordCaptured) return JardAnnotationTake;
            if (modifiers != required || repeat || mode == JardAnnotationUnavailable) return 0;
            keys->chordCaptured = true;
            keys->pressedAt = now;
            keys->shiftPen = false;
            keys->blockShiftUntilUp = shiftDown;
            if (mode == JardAnnotationReady || mode == JardAnnotationPickingTarget) keys->press = JardAnnotationStartPress;
            if (mode == JardAnnotationRecording && now - startedAt >= JardAnnotationStartDebounce) {
                keys->press = JardAnnotationDrawPress;
                return JardAnnotationTake | JardAnnotationArmDraw;
            }
            return JardAnnotationTake;
        case JardAnnotationChordUp:
            if (keys->press == JardAnnotationStartPress && required && chordModifiers) {
                keys->chordCaptured = false;
                return JardAnnotationTake;
            }
            return JardAnnotationRelease(keys, mode, now, true);
        case JardAnnotationModifiers:
            if (!shiftDown) keys->blockShiftUntilUp = false;
            keys->shiftPen = mode == JardAnnotationRecording && modifiers == JardAnnotationShift && !keys->blockShiftUntilUp;
            if (keys->press != JardAnnotationNoPress && !chordModifiers) return JardAnnotationRelease(keys, mode, now, false);
            return 0;
        case JardAnnotationEscapeDown:
            if (keys->escapeCaptured) return JardAnnotationTake;
            if (repeat) return 0;
            bool picking = mode == JardAnnotationPicking || mode == JardAnnotationPickingTarget;
            if (picking || keys->press != JardAnnotationNoPress ||
                (mode == JardAnnotationRecording && (keys->shiftPen || keys->heldPen || latchedPen))) {
                keys->escapeCaptured = true;
                keys->shiftPen = keys->heldPen = false;
                keys->blockShiftUntilUp = shiftDown;
                keys->press = JardAnnotationNoPress;
                return JardAnnotationTake | JardAnnotationEndDrawing | (picking ? JardAnnotationCancelPicker : 0);
            }
            return 0;
        case JardAnnotationEscapeUp:
            if (!keys->escapeCaptured) return 0;
            keys->escapeCaptured = false;
            return JardAnnotationTake;
        case JardAnnotationOtherDown:
            if (keys->press == JardAnnotationStartPress) keys->press = JardAnnotationNoPress;
            return 0;
        case JardAnnotationHoldTimer:
            keys->heldPen = mode == JardAnnotationRecording && keys->press == JardAnnotationDrawPress &&
                now - keys->pressedAt >= JardAnnotationHoldDelay;
            return 0;
    }
    return 0;
}
