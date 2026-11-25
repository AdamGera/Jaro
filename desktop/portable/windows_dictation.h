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
INPUT keyboard_input(WORD key, DWORD flags, HKL layout);
bool dictation_activity_name(const char *token, char name[64]);
int serve_dictation_pastes(void);

#endif
