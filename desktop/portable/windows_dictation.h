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

enum { PASTE_PICKER_ROWS = 8, PASTE_PICKER_TEXT_BYTES = 128 * 1024 };
typedef struct {
    DWORD sequence, count;
    WCHAR *text[PASTE_PICKER_ROWS];
} PastePickerRows;

PastePickerRows *read_paste_picker(HANDLE pipe);
void free_paste_picker(PastePickerRows *rows);
bool show_paste_picker(PastePickerRows *rows, void (*choose)(int index, bool copy));
void close_paste_picker(void);
bool paste_picker_contains(POINT point);
void paste_picker_key(WORD key, bool backwards);

DictationTarget active_target(void);
bool same_target(DictationTarget left, DictationTarget right);
INPUT keyboard_input(WORD key, DWORD flags, HKL layout);
bool dictation_activity_name(const char *token, char name[64]);
int serve_dictation_pastes(void);

#endif
