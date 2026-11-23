#include "windows_dictation.h"
#include <ole2.h>
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <string.h>

enum {
    MAX_TRANSCRIPT_BYTES = 128 * 1024,
    MAX_CLIPBOARD_BYTES = 32 * 1024 * 1024,
    MAX_FORMATS = 64,
    RESTORE_MS = 750,
    PASTE_TARGET_LOST = 3,
    PASTE_CLIPBOARD_UNAVAILABLE = 4,
    PASTE_INPUT_UNAVAILABLE = 5
};

typedef struct {
    UINT format;
    HANDLE data;
} Format;

typedef struct {
    Format entries[MAX_FORMATS];
    size_t length;
} Clipboard;

static void release_data(Format entry) {
    if (entry.format == CF_BITMAP || entry.format == CF_PALETTE) DeleteObject(entry.data);
    else if (entry.format == CF_ENHMETAFILE) DeleteEnhMetaFile(entry.data);
    else GlobalFree(entry.data);
}

static void release_clipboard(Clipboard *clipboard) {
    for (size_t index = 0; index < clipboard->length; ++index) {
        Format entry = clipboard->entries[index];
        if (entry.data) release_data(entry);
    }
}

static bool duplicate_clipboard(Clipboard *clipboard) {
    size_t remaining = MAX_CLIPBOARD_BYTES;
    for (UINT previous = 0;;) {
        SetLastError(ERROR_SUCCESS);
        UINT format = EnumClipboardFormats(previous);
        if (!format) return GetLastError() == ERROR_SUCCESS;
        previous = format;
        if (clipboard->length == MAX_FORMATS || format == CF_OWNERDISPLAY || format == CF_METAFILEPICT
            || (format >= CF_PRIVATEFIRST && format <= CF_GDIOBJLAST)) return false;
        HANDLE original = GetClipboardData(format);
        if (!original) return false;

        size_t bytes = GlobalSize(original);
        if (format == CF_BITMAP) {
            BITMAP bitmap;
            if (GetObjectW(original, sizeof(bitmap), &bitmap) != sizeof(bitmap)
                || bitmap.bmHeight < 0 || bitmap.bmWidthBytes < 0) return false;
            bytes = (size_t)bitmap.bmHeight * (size_t)bitmap.bmWidthBytes;
        } else if (format == CF_ENHMETAFILE) {
            bytes = GetEnhMetaFileBits(original, 0, NULL);
        } else if (format == CF_PALETTE) {
            bytes = GetPaletteEntries(original, 0, 0, NULL) * sizeof(PALETTEENTRY);
        }
        if (!bytes || bytes > remaining) return false;
        HANDLE copy = OleDuplicateData(original, (CLIPFORMAT)format, GMEM_MOVEABLE);
        if (!copy) return false;
        clipboard->entries[clipboard->length++] = (Format){format, copy};
        remaining -= bytes;
    }
}

static bool open_clipboard(HWND owner) {
    for (int attempt = 0; attempt < 9; ++attempt) {
        if (OpenClipboard(owner)) return true;
        Sleep(15);
    }
    return false;
}

static void restore_clipboard(HWND owner, DWORD sequence, Clipboard *clipboard) {
    if (GetClipboardSequenceNumber() != sequence || !open_clipboard(owner)) return;
    if (GetClipboardOwner() == owner && GetClipboardSequenceNumber() == sequence && EmptyClipboard()) {
        for (size_t index = 0; index < clipboard->length; ++index) {
            Format *entry = &clipboard->entries[index];
            if (SetClipboardData(entry->format, entry->data)) entry->data = NULL;
        }
    }
    CloseClipboard();
}

static HGLOBAL transcript_data(void) {
    if (_setmode(_fileno(stdin), _O_BINARY) < 0) return NULL;
    char utf8[MAX_TRANSCRIPT_BYTES + 1];
    size_t length = fread(utf8, 1, sizeof(utf8), stdin);
    if (!length || length == sizeof(utf8) || ferror(stdin) || memchr(utf8, 0, length)) return NULL;
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8, (int)length, NULL, 0);
    if (!count) return NULL;
    HGLOBAL handle = GlobalAlloc(GMEM_MOVEABLE, ((size_t)count + 1) * sizeof(WCHAR));
    if (!handle) return NULL;
    WCHAR *text = GlobalLock(handle);
    if (!text) {
        GlobalFree(handle);
        return NULL;
    }
    int converted = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8, (int)length, text, count);
    text[count] = 0;
    GlobalUnlock(handle);
    if (converted == count) return handle;
    GlobalFree(handle);
    return NULL;
}

static void disable_clipboard_persistence(const WCHAR *label) {
    UINT format = RegisterClipboardFormatW(label);
    if (!format) return;
    HGLOBAL data = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, sizeof(DWORD));
    if (data && !SetClipboardData(format, data)) GlobalFree(data);
}

static bool shell_without_editor(DictationTarget target) {
    HWND shell = GetShellWindow();
    DWORD shell_process = 0;
    if (!shell || !GetWindowThreadProcessId(shell, &shell_process) || shell_process != target.process) return false;
    WCHAR window_class[64];
    if (!GetClassNameW(target.window, window_class, sizeof(window_class) / sizeof(window_class[0]))) return true;
    if (_wcsicmp(window_class, L"Progman") && _wcsicmp(window_class, L"WorkerW")
        && _wcsicmp(window_class, L"Shell_TrayWnd") && _wcsicmp(window_class, L"Shell_SecondaryTrayWnd")) return false;
    WCHAR focused_class[64];
    if (!GetClassNameW(target.focus, focused_class, sizeof(focused_class) / sizeof(focused_class[0]))) return true;
    return _wcsicmp(focused_class, L"Edit") && _wcsnicmp(focused_class, L"RichEdit", 8);
}

static bool can_send(DictationTarget target) {
    if (!same_target(target, active_target())) return false;
    const int keys[] = {VK_MENU, VK_SHIFT, VK_CONTROL, VK_LWIN, VK_RWIN};
    for (size_t index = 0; index < sizeof(keys) / sizeof(keys[0]); ++index) {
        if (GetAsyncKeyState(keys[index]) & 0x8000) return false;
    }
    return true;
}

static bool send_paste(bool terminal) {
    INPUT stroke[6];
    size_t count = 0;
    stroke[count++] = keyboard_input(VK_LCONTROL, 0);
    if (terminal) stroke[count++] = keyboard_input(VK_LSHIFT, 0);
    stroke[count++] = keyboard_input('V', 0);
    const unsigned int pressed = (unsigned int)count;
    for (size_t index = pressed; index > 0; --index) {
        stroke[count] = stroke[index - 1];
        stroke[count++].ki.dwFlags = KEYEVENTF_KEYUP;
    }
    UINT sent = SendInput((UINT)count, stroke, sizeof(INPUT));
    if (sent == count) return true;
    if (sent) SendInput(pressed, stroke + pressed, sizeof(INPUT));
    return false;
}

static void serve_clipboard(UINT duration) {
    ULONGLONG deadline = GetTickCount64() + duration;
    for (ULONGLONG now = GetTickCount64(); now < deadline; now = GetTickCount64()) {
        if (MsgWaitForMultipleObjectsEx(0, NULL, (DWORD)(deadline - now), QS_ALLINPUT,
                                        MWMO_INPUTAVAILABLE) != WAIT_OBJECT_0) break;
        MSG message;
        if (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE)) DispatchMessageW(&message);
    }
}

int paste_dictation(DictationTarget target, bool terminal, const volatile LONG *activity, DWORD expected) {
    HGLOBAL text = transcript_data();
    if (!text) return 2;
    int result = PASTE_TARGET_LOST;
    Clipboard saved = {0};
    HWND owner = NULL;
    HRESULT ole = OleInitialize(NULL);
    if (FAILED(ole)) {
        GlobalFree(text);
        return PASTE_CLIPBOARD_UNAVAILABLE;
    }
    for (int attempt = 0; attempt < 25 && !can_send(target); ++attempt) {
        if (!same_target(target, active_target())) break;
        Sleep(12);
    }
    if (!can_send(target) || shell_without_editor(target)) goto done;
    if (activity && (DWORD)*activity != expected) {
        result = PASTE_CONTINUATION_CHANGED;
        goto done;
    }
    result = PASTE_CLIPBOARD_UNAVAILABLE;
    owner = CreateWindowExW(0, L"STATIC", L"", 0, 0, 0, 0, 0, HWND_MESSAGE, NULL, NULL, NULL);
    if (!owner || !open_clipboard(owner)) goto done;
    bool emptied = duplicate_clipboard(&saved) && can_send(target) && EmptyClipboard();
    if (emptied && SetClipboardData(CF_UNICODETEXT, text)) {
        text = NULL;
        disable_clipboard_persistence(L"CanIncludeInClipboardHistory");
        disable_clipboard_persistence(L"CanUploadToCloudClipboard");
    }
    CloseClipboard();
    if (emptied) {
        // Closing can add synthesized formats and advance the sequence number.
        DWORD sequence = GetClipboardSequenceNumber();
        if (GetClipboardOwner() != owner) {
            result = PASTE_TARGET_LOST;
            goto done;
        }
        if (!text && GetClipboardSequenceNumber() == sequence && can_send(target)) {
            if (activity && (DWORD)*activity != expected) result = PASTE_CONTINUATION_CHANGED;
            else result = send_paste(terminal) ? 0 : PASTE_INPUT_UNAVAILABLE;
            if (result == 0) serve_clipboard(RESTORE_MS);
        } else if (!text) {
            result = PASTE_TARGET_LOST;
        }
        restore_clipboard(owner, sequence, &saved);
    }
done:
    if (text) GlobalFree(text);
    release_clipboard(&saved);
    if (owner) DestroyWindow(owner);
    OleUninitialize();
    return result;
}
