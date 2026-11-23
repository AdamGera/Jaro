#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <string.h>

enum { UNAVAILABLE, IDLE, STARTING, RECORDING, SAVING };
enum { OPEN_COMMAND = 1, COPY_COMMAND, QUIT_COMMAND, RECORD_COMMAND, STOP_COMMAND };
enum { TRAY_EVENT = WM_APP + 1, UPDATE_MENU = WM_APP + 2 };
static UINT taskbar_created;
static NOTIFYICONDATAW icon;
static unsigned phase;
static BOOL can_copy;
static DWORD workspace_pid;

static void send(const char *line) {
    puts(line);
    fflush(stdout);
}

static BOOL add_icon(void) {
    if (!Shell_NotifyIconW(NIM_ADD, &icon)) return FALSE;
    icon.uVersion = NOTIFYICON_VERSION_4;
    Shell_NotifyIconW(NIM_SETVERSION, &icon);
    return TRUE;
}

static void open_workspace(void) {
    if (workspace_pid) AllowSetForegroundWindow(workspace_pid);
    send("open");
}

static void menu(HWND window, POINT where) {
    HMENU popup = CreatePopupMenu();
    if (!popup) return;
    AppendMenuW(popup, MF_STRING, OPEN_COMMAND, L"Open Jaro");
    AppendMenuW(popup, MF_SEPARATOR, 0, NULL);
    switch (phase) {
    case IDLE: AppendMenuW(popup, MF_STRING, RECORD_COMMAND, L"Record Meeting"); break;
    case RECORDING: AppendMenuW(popup, MF_STRING, STOP_COMMAND, L"Stop & Save"); break;
    default:
        AppendMenuW(popup, MF_STRING | MF_GRAYED, 0,
                    phase == SAVING ? L"Saving…" : phase == STARTING ? L"Starting…" : L"Record Meeting");
        break;
    }
    AppendMenuW(popup, MF_STRING | (can_copy ? 0 : MF_GRAYED), COPY_COMMAND, L"Copy last meeting transcript");
    AppendMenuW(popup, MF_SEPARATOR, 0, NULL);
    AppendMenuW(popup, MF_STRING, QUIT_COMMAND, L"Quit Jaro");
    SetForegroundWindow(window);
    UINT selected = TrackPopupMenu(popup, TPM_RIGHTBUTTON | TPM_RETURNCMD, where.x, where.y, 0, window, NULL);
    PostMessageW(window, WM_NULL, 0, 0);
    DestroyMenu(popup);
    switch (selected) {
    case OPEN_COMMAND: open_workspace(); break;
    case RECORD_COMMAND: if (phase == IDLE) send("record"); break;
    case STOP_COMMAND: if (phase == RECORDING) send("stop"); break;
    case COPY_COMMAND: if (can_copy) send("copy"); break;
    case QUIT_COMMAND: send("quit"); break;
    default: Shell_NotifyIconW(NIM_SETFOCUS, &icon); break;
    }
}

static LRESULT CALLBACK handle(HWND window, UINT message, WPARAM w, LPARAM l) {
    if (message == taskbar_created) {
        if (!add_icon()) DestroyWindow(window);
        return 0;
    }
    if (message == UPDATE_MENU) {
        phase = LOWORD(w);
        can_copy = HIWORD(w) != 0;
        workspace_pid = (DWORD)l;
        return 0;
    }
    if (message == TRAY_EVENT) {
        switch (LOWORD(l)) {
        case WM_CONTEXTMENU:
        case WM_RBUTTONUP: {
            POINT where;
            GetCursorPos(&where);
            menu(window, where);
            break;
        }
        case NIN_SELECT:
        case NIN_KEYSELECT:
        case WM_LBUTTONUP:
            open_workspace();
            break;
        }
        return 0;
    }
    if (message == WM_DESTROY) {
        Shell_NotifyIconW(NIM_DELETE, &icon);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, w, l);
}

static DWORD WINAPI read_state(void *window) {
    const char *const states[] = {"unavailable", "idle", "starting", "recording", "saving"};
    char line[72], status[16];
    unsigned copy;
    unsigned long pid;
    while (fgets(line, sizeof(line), stdin)) {
        if (sscanf(line, "%15s %u %lu", status, &copy, &pid) != 3 || copy > 1) continue;
        for (unsigned i = 0; i < ARRAYSIZE(states); ++i) {
            if (!strcmp(states[i], status)) {
                PostMessageW(window, UPDATE_MENU, MAKEWPARAM(i, copy), (LPARAM)pid);
                break;
            }
        }
    }
    PostMessageW(window, WM_CLOSE, 0, 0);
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 2 || _setmode(_fileno(stdout), _O_BINARY) < 0) return 2;
    HINSTANCE instance = GetModuleHandleW(NULL);
    WNDCLASSW type = {.lpfnWndProc = handle, .hInstance = instance, .lpszClassName = L"JaroTray"};
    if (!RegisterClassW(&type)) return 2;
    HWND window = CreateWindowExW(WS_EX_TOOLWINDOW, type.lpszClassName, L"Jaro", WS_POPUP,
                                   0, 0, 0, 0, NULL, NULL, instance, NULL);
    if (!window) return 2;
    taskbar_created = RegisterWindowMessageW(L"TaskbarCreated");
    HICON image = LoadImageW(NULL, argv[1], IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE);
    icon = (NOTIFYICONDATAW){.cbSize = sizeof(icon), .hWnd = window, .uID = 1,
        .uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP, .uCallbackMessage = TRAY_EVENT,
        .hIcon = image ? image : LoadIconW(NULL, MAKEINTRESOURCEW(32512)), .szTip = L"Jaro"};
    BOOL added = FALSE;
    for (unsigned i = 0; i < 14 && !(added = add_icon()); ++i) Sleep(600);
    if (!added) return 3;
    HANDLE reader = CreateThread(NULL, 0, read_state, window, 0, NULL);
    if (!reader) {
        DestroyWindow(window);
        return 2;
    }
    send("ready");
    MSG message;
    while (GetMessageW(&message, NULL, 0, 0) > 0) DispatchMessageW(&message);
    CancelSynchronousIo(reader);
    CloseHandle(reader);
    if (image) DestroyIcon(image);
    return 0;
}
