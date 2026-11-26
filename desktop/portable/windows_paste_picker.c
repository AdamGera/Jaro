#include "windows_dictation.h"
#include <stdlib.h>

enum { LIST = 1, COPY, PASTE, CLOSE };
static HWND popup, list, preview;
static PastePickerRows *contents;
static void (*selection)(int index, bool copy);

static bool read_bytes(HANDLE pipe, void *buffer, DWORD size) {
    BYTE *bytes = buffer;
    while (size) {
        DWORD read;
        if (!ReadFile(pipe, bytes, size, &read, NULL) || !read) return false;
        size -= read;
        bytes += read;
    }
    return true;
}

void free_paste_picker(PastePickerRows *rows) {
    if (!rows) return;
    for (unsigned index = 0; index < PASTE_PICKER_ROWS; ++index) free(rows->text[index]);
    free(rows);
}

PastePickerRows *read_paste_picker(HANDLE pipe) {
    DWORD header[2], lengths[PASTE_PICKER_ROWS];
    if (!read_bytes(pipe, header, sizeof(header)) || header[1] > PASTE_PICKER_ROWS) return NULL;
    if (!read_bytes(pipe, lengths, header[1] * sizeof(DWORD))) return NULL;
    PastePickerRows *rows = calloc(1, sizeof(*rows));
    if (!rows) return NULL;
    rows->sequence = header[0];
    rows->count = header[1];
    char *utf8 = malloc(PASTE_PICKER_TEXT_BYTES);
    if (!utf8) goto invalid;
    for (DWORD index = 0; index < rows->count; ++index) {
        DWORD bytes = lengths[index];
        if (!bytes || bytes > PASTE_PICKER_TEXT_BYTES || !read_bytes(pipe, utf8, bytes)) goto invalid;
        int characters = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8, (int)bytes, NULL, 0);
        if (!characters || !(rows->text[index] = calloc((size_t)characters + 1, sizeof(WCHAR)))) goto invalid;
        if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, utf8, (int)bytes, rows->text[index], characters)) goto invalid;
    }
    free(utf8);
    return rows;
invalid:
    free(utf8);
    free_paste_picker(rows);
    return NULL;
}

static void select_row(int index) {
    SendMessageW(list, LB_SETCURSEL, (WPARAM)index, 0);
    SetWindowTextW(preview, contents->text[index]);
    SendMessageW(preview, EM_SETSEL, 0, 0);
    SendMessageW(preview, EM_SCROLLCARET, 0, 0);
}

void paste_picker_key(WORD key, bool backwards) {
    if (!popup) return;
    int index = (int)SendMessageW(list, LB_GETCURSEL, 0, 0);
    int count = (int)contents->count;
    if (key == VK_ESCAPE) selection(-1, false);
    else if (key == VK_RETURN && index >= 0) selection(index, false);
    else if (key == VK_PRIOR || key == VK_NEXT) SendMessageW(preview, EM_SCROLL, key == VK_PRIOR ? SB_PAGEUP : SB_PAGEDOWN, 0);
    else if (count && (key == VK_TAB || key == 'V' || key == VK_UP || key == VK_DOWN))
        select_row((index + (backwards || key == VK_UP ? count - 1 : 1)) % count);
}

static LRESULT CALLBACK picker_window(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (message == WM_CLOSE) {
        selection(-1, false);
        return 0;
    }
    if (message == WM_COMMAND) {
        int index = (int)SendMessageW(list, LB_GETCURSEL, 0, 0);
        switch (LOWORD(wparam)) {
            case LIST:
                if (index >= 0 && HIWORD(wparam) == LBN_SELCHANGE) select_row(index);
                if (index >= 0 && HIWORD(wparam) == LBN_DBLCLK) selection(index, false);
                break;
            case COPY:
            case PASTE:
                if (index >= 0) selection(index, LOWORD(wparam) == COPY);
                break;
            case CLOSE: selection(-1, false); break;
        }
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

static HWND control(DWORD exstyle, const WCHAR *class, const WCHAR *title, DWORD style,
                    int x, int y, int width, int height, unsigned id) {
    HWND child = CreateWindowExW(exstyle, class, title, WS_CHILD | WS_VISIBLE | style, x, y, width, height,
                                 popup, (HMENU)(UINT_PTR)id, GetModuleHandleW(NULL), NULL);
    if (child) SendMessageW(child, WM_SETFONT, (WPARAM)GetStockObject(DEFAULT_GUI_FONT), FALSE);
    return child;
}

bool show_paste_picker(PastePickerRows *rows, void (*choose)(int index, bool copy)) {
    close_paste_picker();
    HINSTANCE module = GetModuleHandleW(NULL);
    WNDCLASSW definition = {
        .lpfnWndProc = picker_window, .hInstance = module, .hCursor = LoadCursorW(NULL, MAKEINTRESOURCEW(32512)),
        .hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1), .lpszClassName = L"JaroPastePicker"
    };
    if (!RegisterClassW(&definition) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) return false;
    POINT cursor;
    MONITORINFO screen = {.cbSize = sizeof(screen)};
    if (!GetCursorPos(&cursor) || !GetMonitorInfoW(MonitorFromPoint(cursor, MONITOR_DEFAULTTONEAREST), &screen)) return false;
    RECT work = screen.rcWork;
    int width = min(700, work.right - work.left - 16), height = min(352, work.bottom - work.top - 16);
    if (width < 330 || height < 180) return false;
    popup = CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST, definition.lpszClassName,
                            L"Jaro · Paste Last", WS_POPUP | WS_BORDER,
                            work.left + (work.right - work.left - width) / 2,
                            work.top + (work.bottom - work.top - height) / 2,
                            width, height, NULL, NULL, module, NULL);
    if (!popup) return false;
    selection = choose;
    int rail = width * 36 / 100, body = height - 90;
    control(0, L"STATIC", L"Paste Last", 0, 16, 12, width - 32, 20, 0);
    list = control(WS_EX_CLIENTEDGE, L"LISTBOX", L"", LBS_NOTIFY | LBS_NOINTEGRALHEIGHT | WS_VSCROLL,
                   16, 40, rail, body, LIST);
    preview = control(WS_EX_CLIENTEDGE, L"EDIT", L"", ES_MULTILINE | ES_READONLY | WS_VSCROLL,
                      rail + 24, 40, width - rail - 42, body, 0);
    HWND copy = control(0, L"BUTTON", L"Copy", 0, width - 248, height - 36, 68, 24, COPY);
    HWND paste = control(0, L"BUTTON", L"Paste", BS_DEFPUSHBUTTON, width - 172, height - 36, 68, 24, PASTE);
    control(0, L"BUTTON", L"Close", 0, width - 96, height - 36, 76, 24, CLOSE);
    if (!list || !preview || !copy || !paste) {
        close_paste_picker();
        return false;
    }
    contents = rows;
    for (DWORD index = 0; index < rows->count; ++index) {
        WCHAR snippet[95];
        unsigned length = 0;
        while (length < 88 && rows->text[index][length]) {
            WCHAR unit = rows->text[index][length];
            snippet[length++] = unit == L'\r' || unit == L'\n' || unit == L'\t' ? L' ' : unit;
        }
        if (length && IS_HIGH_SURROGATE(snippet[length - 1])) --length;
        if (rows->text[index][length]) snippet[length++] = L'…';
        snippet[length] = 0;
        SendMessageW(list, LB_ADDSTRING, 0, (LPARAM)snippet);
    }
    if (rows->count) select_row(0);
    else SetWindowTextW(preview, L"No recent dictations.");
    EnableWindow(copy, rows->count != 0);
    EnableWindow(paste, rows->count != 0);
    ShowWindow(popup, SW_SHOWNOACTIVATE);
    return true;
}

bool paste_picker_contains(POINT point) {
    RECT bounds;
    return popup && GetWindowRect(popup, &bounds) && PtInRect(&bounds, point);
}

void close_paste_picker(void) {
    if (popup) DestroyWindow(popup);
    popup = list = preview = NULL;
    free_paste_picker(contents);
    contents = NULL;
}
