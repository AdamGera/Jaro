#include "windows_dictation.h"
#include <ole2.h>
#include <stdint.h>
#include <string.h>

enum {
    MAX_TRANSCRIPT_BYTES = 128 * 1024,
    MAX_CLIPBOARD_BYTES = 32 * 1024 * 1024,
    MAX_FORMATS = 64,
    SETTLE_MS = 100,
    RESTORE_MS = 750,
    PASTE_TARGET_LOST = 3,
    PASTE_CLIPBOARD_UNAVAILABLE = 4,
    PASTE_INPUT_UNAVAILABLE = 5,
    PASTE_TARGET_ELEVATED = 7
};

typedef struct {
    UINT format;
    HANDLE data;
} Format;

typedef struct {
    Format entries[MAX_FORMATS];
    size_t length;
} Clipboard;

typedef struct {
    HWND owner;
    Clipboard original;
    DWORD sequence;
    ULONGLONG restore_at;
} PasteClipboard;

// The Python controller encodes this fixed-width header in little-endian order.
typedef struct {
    uint64_t window, focus;
    uint32_t process, terminal, revision, length;
    char channel[32];
} PasteRequest;
_Static_assert(sizeof(PasteRequest) == 64, "Windows paste request layout");

typedef struct {
    HANDLE request_ready, reply_ready;
    PasteRequest request;
    char payload[MAX_TRANSCRIPT_BYTES];
    BYTE status;
} PasteWorker;

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
    clipboard->length = 0;
}

static bool duplicate_clipboard(Clipboard *clipboard) {
    size_t remaining = MAX_CLIPBOARD_BYTES;
    for (UINT previous = 0;;) {
        SetLastError(ERROR_SUCCESS);
        UINT format = EnumClipboardFormats(previous);
        if (!format) return GetLastError() == ERROR_SUCCESS;
        previous = format;
        if (clipboard->length == MAX_FORMATS) return false;
        if (format == CF_OWNERDISPLAY || format == CF_METAFILEPICT
            || format == CF_DSPBITMAP || format == CF_DSPMETAFILEPICT || format == CF_DSPENHMETAFILE
            || (format >= CF_PRIVATEFIRST && format <= CF_GDIOBJLAST)) return false;
        HANDLE original = GetClipboardData(format);
        if (!original) return false;

        size_t bytes = 0;
        if (format == CF_BITMAP) {
            BITMAP bitmap;
            if (GetObjectW(original, sizeof(bitmap), &bitmap) != sizeof(bitmap)
                || bitmap.bmHeight <= 0 || bitmap.bmWidthBytes <= 0) return false;
            bytes = (size_t)bitmap.bmHeight * (size_t)bitmap.bmWidthBytes;
        } else if (format == CF_ENHMETAFILE) {
            bytes = GetEnhMetaFileBits(original, 0, NULL);
        } else if (format == CF_PALETTE) {
            bytes = GetPaletteEntries(original, 0, 0, NULL) * sizeof(PALETTEENTRY);
        } else {
            bytes = GlobalSize(original);
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

static bool owns_clipboard(const PasteClipboard *clipboard) {
    return clipboard->restore_at && GetClipboardOwner() == clipboard->owner
        && GetClipboardSequenceNumber() == clipboard->sequence;
}

static void restore_clipboard(PasteClipboard *clipboard) {
    if (owns_clipboard(clipboard) && open_clipboard(clipboard->owner)) {
        // Check again while the clipboard is locked. A newer copy always wins.
        if (owns_clipboard(clipboard) && EmptyClipboard()) {
            for (size_t index = 0; index < clipboard->original.length; ++index) {
                Format *entry = &clipboard->original.entries[index];
                if (SetClipboardData(entry->format, entry->data)) entry->data = NULL;
            }
        }
        CloseClipboard();
    }
    clipboard->restore_at = 0;
    release_clipboard(&clipboard->original);
}

static HGLOBAL transcript_data(const char *utf8, size_t length) {
    if (memchr(utf8, 0, length)) return NULL;
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
        && _wcsicmp(window_class, L"Shell_TrayWnd") && _wcsicmp(window_class, L"Shell_SecondaryTrayWnd")
        && _wcsicmp(window_class, L"TopLevelWindowForOverflowXamlIsland")) return false;
    if (!target.focus) return true;
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

static bool process_integrity(HANDLE process, DWORD *integrity) {
    HANDLE token;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) return false;
    union {
        TOKEN_MANDATORY_LABEL label;
        BYTE data[sizeof(TOKEN_MANDATORY_LABEL) + SECURITY_MAX_SID_SIZE];
    } buffer;
    DWORD size;
    bool available = GetTokenInformation(token, TokenIntegrityLevel, &buffer, sizeof(buffer), &size);
    CloseHandle(token);
    if (!available || !IsValidSid(buffer.label.Label.Sid)) return false;
    BYTE count = *GetSidSubAuthorityCount(buffer.label.Label.Sid);
    if (!count) return false;
    *integrity = *GetSidSubAuthority(buffer.label.Label.Sid, count - 1);
    return true;
}

static bool higher_integrity(DWORD process_id, DWORD own_integrity) {
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, process_id);
    if (!process) return false;
    DWORD integrity;
    bool higher = process_integrity(process, &integrity) && integrity > own_integrity;
    CloseHandle(process);
    return higher;
}

static bool target_is_elevated(DictationTarget target) {
    DWORD own_integrity;
    if (!process_integrity(GetCurrentProcess(), &own_integrity)) return false;
    if (higher_integrity(target.process, own_integrity)) return true;
    DWORD focus_process = 0;
    if (target.focus) GetWindowThreadProcessId(target.focus, &focus_process);
    return focus_process && focus_process != target.process && higher_integrity(focus_process, own_integrity);
}

static bool send_paste(DictationTarget target, bool terminal) {
    DWORD thread = GetWindowThreadProcessId(target.window, NULL);
    HKL layout = GetKeyboardLayout(thread);
    INPUT stroke[6];
    size_t count = 0;
    stroke[count++] = keyboard_input(VK_LCONTROL, 0, layout);
    if (terminal) stroke[count++] = keyboard_input(VK_LSHIFT, 0, layout);
    stroke[count++] = keyboard_input('V', 0, layout);
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

static void serve_clipboard(DWORD duration) {
    if (MsgWaitForMultipleObjectsEx(0, NULL, duration, QS_ALLINPUT, MWMO_INPUTAVAILABLE) == WAIT_OBJECT_0) {
        MSG message;
        if (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE)) DispatchMessageW(&message);
    }
}

static int paste_dictation(PasteClipboard *clipboard, HGLOBAL *text, DictationTarget target, bool terminal,
                           const volatile LONG *activity, DWORD expected) {
    for (int attempt = 0; attempt < 25 && !can_send(target); ++attempt) {
        if (!same_target(target, active_target())) break;
        serve_clipboard(12);
    }
    if (!can_send(target) || shell_without_editor(target)) return PASTE_TARGET_LOST;
    if (activity && (DWORD)*activity != expected) return PASTE_CONTINUATION_CHANGED;
    bool elevated = target_is_elevated(target);
    if (!can_send(target)) return PASTE_TARGET_LOST;
    if (elevated) return PASTE_TARGET_ELEVATED;
    if (!open_clipboard(clipboard->owner)) return PASTE_CLIPBOARD_UNAVAILABLE;
    bool retain = owns_clipboard(clipboard);
    if (!retain) {
        clipboard->restore_at = 0;
        release_clipboard(&clipboard->original);
    }
    bool emptied = (retain || duplicate_clipboard(&clipboard->original)) && can_send(target) && EmptyClipboard();
    if (emptied && SetClipboardData(CF_UNICODETEXT, *text)) {
        *text = NULL;
        disable_clipboard_persistence(L"CanIncludeInClipboardHistory");
        disable_clipboard_persistence(L"CanUploadToCloudClipboard");
    }
    CloseClipboard();
    if (!emptied) {
        if (!retain) release_clipboard(&clipboard->original);
        return PASTE_CLIPBOARD_UNAVAILABLE;
    }
    // Closing can add synthesized formats and advance the sequence number.
    clipboard->sequence = GetClipboardSequenceNumber();
    clipboard->restore_at = GetTickCount64() + RESTORE_MS;
    int result = PASTE_CLIPBOARD_UNAVAILABLE;
    if (!*text) {
        if (!owns_clipboard(clipboard) || !can_send(target)) result = PASTE_TARGET_LOST;
        else if (activity && (DWORD)*activity != expected) result = PASTE_CONTINUATION_CHANGED;
        else result = send_paste(target, terminal) ? 0 : PASTE_INPUT_UNAVAILABLE;
    }
    if (result) {
        restore_clipboard(clipboard);
        return result;
    }
    ULONGLONG deadline = GetTickCount64() + SETTLE_MS;
    clipboard->restore_at = deadline + (RESTORE_MS - SETTLE_MS);
    for (ULONGLONG now = GetTickCount64(); now < deadline; now = GetTickCount64())
        serve_clipboard((DWORD)(deadline - now));
    return 0;
}

static int paste_request(PasteClipboard *clipboard, const PasteRequest *request, const char *payload) {
    if (!request->window || !request->process || request->terminal > 1) return 2;
    char token[33] = {0}, name[64];
    HANDLE mapping = NULL;
    const volatile LONG *activity = NULL;
    if (request->channel[0]) {
        memcpy(token, request->channel, sizeof(request->channel));
        if (!dictation_activity_name(token, name)) return 2;
        mapping = OpenFileMappingA(FILE_MAP_READ, FALSE, name);
        if (mapping) activity = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, sizeof(LONG));
        if (!activity) {
            if (mapping) CloseHandle(mapping);
            return PASTE_CONTINUATION_CHANGED;
        }
    }
    HGLOBAL text = transcript_data(payload, request->length);
    DictationTarget target = {(HWND)(uintptr_t)request->window, (HWND)(uintptr_t)request->focus, request->process};
    int result = text ? paste_dictation(clipboard, &text, target, request->terminal, activity, request->revision) : 2;
    if (text) GlobalFree(text);
    if (activity) UnmapViewOfFile((const void *)activity);
    if (mapping) CloseHandle(mapping);
    return result;
}

static bool read_request_bytes(HANDLE pipe, void *destination, DWORD length) {
    char *data = destination;
    while (length) {
        DWORD received;
        if (!ReadFile(pipe, data, length, &received, NULL) || !received) return false;
        data += received;
        length -= received;
    }
    return true;
}

static DWORD WINAPI read_paste_requests(void *context) {
    PasteWorker *worker = context;
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE), output = GetStdHandle(STD_OUTPUT_HANDLE);
    while (read_request_bytes(input, &worker->request, sizeof(worker->request))) {
        DWORD length = worker->request.length;
        if (!length || length > sizeof(worker->payload)
            || !read_request_bytes(input, worker->payload, length)) break;
        SetEvent(worker->request_ready);
        if (WaitForSingleObject(worker->reply_ready, INFINITE) != WAIT_OBJECT_0) break;
        DWORD sent;
        if (!WriteFile(output, &worker->status, sizeof(worker->status), &sent, NULL)
            || sent != sizeof(worker->status)) break;
    }
    return 0;
}

int serve_dictation_pastes(void) {
    if (FAILED(OleInitialize(NULL))) return PASTE_CLIPBOARD_UNAVAILABLE;
    PasteClipboard clipboard = {
        .owner = CreateWindowExW(0, L"STATIC", L"", 0, 0, 0, 0, 0, HWND_MESSAGE, NULL, NULL, NULL)
    };
    PasteWorker worker = {
        .request_ready = CreateEventW(NULL, FALSE, FALSE, NULL),
        .reply_ready = CreateEventW(NULL, FALSE, FALSE, NULL)
    };
    HANDLE reader = clipboard.owner && worker.request_ready && worker.reply_ready
        ? CreateThread(NULL, 0, read_paste_requests, &worker, 0, NULL) : NULL;
    if (reader) {
        // Pipe reads can block while idle. Keep the clipboard timer and messages
        // on its owning thread, with just one request in flight.
        HANDLE signal[] = {reader, worker.request_ready};
        for (;;) {
            ULONGLONG now = GetTickCount64();
            if (clipboard.restore_at && now >= clipboard.restore_at) restore_clipboard(&clipboard);
            DWORD wait = clipboard.restore_at ? (DWORD)(clipboard.restore_at - now) : INFINITE;
            DWORD result = MsgWaitForMultipleObjectsEx(2, signal, wait, QS_ALLINPUT, MWMO_INPUTAVAILABLE);
            if (result == WAIT_OBJECT_0) break;
            if (result == WAIT_OBJECT_0 + 1) {
                worker.status = (BYTE)paste_request(&clipboard, &worker.request, worker.payload);
                SetEvent(worker.reply_ready);
            } else if (result == WAIT_OBJECT_0 + 2) {
                serve_clipboard(0);
            } else if (result == WAIT_FAILED) {
                restore_clipboard(&clipboard);
                ExitProcess(PASTE_CLIPBOARD_UNAVAILABLE);
            }
        }
        CloseHandle(reader);
    }
    restore_clipboard(&clipboard);
    if (worker.reply_ready) CloseHandle(worker.reply_ready);
    if (worker.request_ready) CloseHandle(worker.request_ready);
    if (clipboard.owner) DestroyWindow(clipboard.owner);
    OleUninitialize();
    return reader ? 0 : PASTE_CLIPBOARD_UNAVAILABLE;
}
