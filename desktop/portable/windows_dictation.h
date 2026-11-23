#ifndef JARO_WINDOWS_DICTATION_H
#define JARO_WINDOWS_DICTATION_H

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdbool.h>

typedef struct {
    HWND window;
    HWND focus;
    DWORD process;
} DictationTarget;

enum { JARO_DICTATION_INPUT = 0x4a61726f, PASTE_CONTINUATION_CHANGED = 6 };

DictationTarget active_target(void);
bool same_target(DictationTarget left, DictationTarget right);
INPUT keyboard_input(WORD key, DWORD flags);
int paste_dictation(DictationTarget target, bool terminal, const volatile LONG *activity, DWORD expected);

#endif
