#define WIN32_LEAN_AND_MEAN
#define OEMRESOURCE
#include <windows.h>
#include <shellscalingapi.h>
#include <wchar.h>

enum { OFFER, OFFER_OPTIONS, RECORDING, AUTOMATIC, ENDING, SWITCHING, SAVING };
static int mode, seconds;
static const wchar_t *provider;
static const char *result;
static ULONGLONG started;
static UINT dpi = 96;
static HFONT title_font, body_font;
static BOOL allow_always;

static BOOL countdown(void) {
    return mode == ENDING || mode == SWITCHING;
}

static int scale(int px) {
    return MulDiv(px, dpi, 96);
}

static const wchar_t *summary(void) {
    switch (mode) {
    case OFFER: return L"Call detected";
    case OFFER_OPTIONS: return L"Change anytime in Settings";
    case AUTOMATIC: return L"Recording automatically";
    case ENDING: return L"Call ended";
    case SWITCHING: return L"Call changed";
    case SAVING: return L"Saving meeting";
    default: return L"Recording meeting";
    }
}

static void write_text(HDC dc, RECT area, const wchar_t *content, HFONT font, COLORREF color, UINT style) {
    HGDIOBJ previous = SelectObject(dc, font);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, color);
    DrawTextW(dc, content, -1, &area, style | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS);
    SelectObject(dc, previous);
}

static void panel_paint(HWND window) {
    PAINTSTRUCT paint;
    HDC dc = BeginPaint(window, &paint);
    RECT area;
    GetClientRect(window, &area);
    HBRUSH background = CreateSolidBrush(RGB(32, 32, 41));
    FillRect(dc, &paint.rcPaint, background);
    DeleteObject(background);
    RECT title = {scale(15), scale(11), area.right - scale(15), scale(33)};
    write_text(dc, title, provider, title_font, RGB(240, 238, 246), DT_LEFT);
    wchar_t status[112];
    int time = (int)((GetTickCount64() - started) / 1000);
    const wchar_t *subtitle = summary();
    if (countdown()) {
        time = seconds > time ? seconds - time : 1;
        swprintf(status, ARRAYSIZE(status), L"%ls · Stops in %d seconds", summary(), time);
        subtitle = status;
    } else if (mode == RECORDING || mode == AUTOMATIC) {
        time += seconds;
        swprintf(status, ARRAYSIZE(status), L"%ls · %d:%02d", summary(), time / 60, time % 60);
        subtitle = status;
    }
    RECT detail = {title.left, title.bottom, title.right, scale(54)};
    write_text(dc, detail, subtitle, body_font, RGB(181, 177, 193), DT_LEFT);
    EndPaint(window, &paint);
}

static void button_paint(const DRAWITEMSTRUCT *item) {
    BOOL primary = item->CtlID == 1 && (mode == OFFER || mode == OFFER_OPTIONS || countdown());
    BOOL pressed = (item->itemState & ODS_SELECTED) != 0;
    HBRUSH fill = CreateSolidBrush(primary ? (pressed ? RGB(193, 180, 238) : RGB(222, 214, 255)) :
                                           (pressed ? RGB(68, 66, 82) : RGB(49, 48, 63)));
    HPEN border = CreatePen(PS_SOLID, 1, primary ? RGB(222, 214, 255) : RGB(73, 72, 88));
    HGDIOBJ old_brush = SelectObject(item->hDC, fill), old_pen = SelectObject(item->hDC, border);
    RECT area = item->rcItem;
    RoundRect(item->hDC, area.left, area.top, area.right, area.bottom, scale(16), scale(16));
    SelectObject(item->hDC, old_pen);
    SelectObject(item->hDC, old_brush);
    DeleteObject(fill);
    DeleteObject(border);
    wchar_t label[32];
    GetWindowTextW(item->hwndItem, label, ARRAYSIZE(label));
    write_text(item->hDC, area, label, body_font, primary ? RGB(37, 34, 56) : RGB(240, 238, 246), DT_CENTER);
}

static LRESULT CALLBACK panel(HWND window, UINT message, WPARAM w, LPARAM l) {
    switch (message) {
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case WM_PAINT: panel_paint(window); return 0;
    case WM_DRAWITEM: button_paint((DRAWITEMSTRUCT *)l); return TRUE;
    case WM_TIMER: {
        RECT status = {0, scale(33), scale(328), scale(54)};
        InvalidateRect(window, &status, FALSE);
        return 0;
    }
    case WM_COMMAND:
        if (HIWORD(w) != BN_CLICKED || !l) break;
        if (mode == OFFER) {
            if (LOWORD(w) == 1) result = "record";
            else if (LOWORD(w) == 2) result = "dismiss";
            else if (LOWORD(w) == 3) result = "more";
        } else if (mode == OFFER_OPTIONS) {
            if (LOWORD(w) == 1 && allow_always) result = "always";
            else if (LOWORD(w) == 2) result = "never";
            else if (LOWORD(w) == 3) result = "back";
        } else if (LOWORD(w) == 1) {
            result = countdown() ? "keep" : "stop";
        } else if (LOWORD(w) == 2) result = "stop";
        if (result) DestroyWindow(window);
        return 0;
    case WM_DESTROY: PostQuitMessage(0); return 0;
    }
    return DefWindowProcW(window, message, w, l);
}

static DWORD WINAPI watch_parent(void *window) {
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    char ignored;
    DWORD count;
    ReadFile(input, &ignored, 1, &count, NULL);
    PostMessageW(window, WM_CLOSE, 0, 0);
    return 0;
}

static BOOL button(HWND window, HINSTANCE instance, const wchar_t *label, int id, int x, int width) {
    return CreateWindowExW(0, L"BUTTON", label, WS_CHILD | WS_VISIBLE | BS_OWNERDRAW,
        scale(x), scale(64), scale(width), scale(29), window, (HMENU)(INT_PTR)id, instance, NULL) != NULL;
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 4 && argc != 5) return 2;
    const wchar_t *const modes[] = {L"offer", L"offer-options", L"recording", L"automatic", L"ending", L"switching", L"saving"};
    while (mode < (int)ARRAYSIZE(modes) && wcscmp(modes[mode], argv[1])) mode++;
    wchar_t *end;
    long parsed = wcstol(argv[3], &end, 10);
    if (mode == (int)ARRAYSIZE(modes) || end == argv[3] || *end || parsed < 0 || parsed > 86400) return 2;
    if (argc == 5 && (mode != OFFER_OPTIONS || wcscmp(argv[4], L"always"))) return 2;
    allow_always = argc == 5;
    provider = argv[2];
    seconds = (int)parsed;
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    HMONITOR monitor = MonitorFromWindow(GetForegroundWindow(), MONITOR_DEFAULTTOPRIMARY);
    MONITORINFO screen = {.cbSize = sizeof(screen)};
    if (!GetMonitorInfoW(monitor, &screen)) return 3;
    UINT vertical_dpi;
    if (GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, &dpi, &vertical_dpi) != S_OK) dpi = 96;
    HINSTANCE instance = GetModuleHandleW(NULL);
    WNDCLASSW kind = {.lpfnWndProc = panel, .hInstance = instance,
        .hCursor = LoadCursorW(NULL, MAKEINTRESOURCEW(OCR_NORMAL)), .lpszClassName = L"JaroMeetingControls"};
    if (!RegisterClassW(&kind)) return 3;
    title_font = CreateFontW(-scale(17), 0, 0, 0, FW_SEMIBOLD, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    body_font = CreateFontW(-scale(14), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
        OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
    HWND window = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        kind.lpszClassName, L"Jaro Meeting Controls", WS_POPUP | WS_CLIPCHILDREN, 0, 0, 0, 0, NULL, NULL, instance, NULL);
    if (!window || !title_font || !body_font) return 3;
    BOOL controls;
    switch (mode) {
    case OFFER: controls = button(window, instance, L"Options", 3, 59, 78) &&
                           button(window, instance, L"Not now", 2, 145, 78) &&
                           button(window, instance, L"Record", 1, 231, 82); break;
    case OFFER_OPTIONS:
        controls = button(window, instance, L"Cancel", 3, allow_always ? 29 : 128, 70) &&
                   button(window, instance, L"Never offer", 2, allow_always ? 107 : 206, 92) &&
                   (!allow_always || button(window, instance, L"Always record", 1, 207, 106));
        break;
    case ENDING:
    case SWITCHING: controls = button(window, instance, L"Keep recording", 1, 80, 116) &&
                            button(window, instance, L"Stop & Save", 2, 204, 109); break;
    case SAVING: controls = TRUE; break;
    default: controls = button(window, instance, L"Stop & Save", 1, 204, 109); break;
    }
    if (!controls) return 3;
    int width = scale(328), height = scale(mode == SAVING ? 66 : 106);
    HRGN region = CreateRoundRectRgn(0, 0, width + 1, height + 1, scale(28), scale(28));
    if (region && !SetWindowRgn(window, region, FALSE)) DeleteObject(region);
    started = GetTickCount64();
    HANDLE parent = CreateThread(NULL, 0, watch_parent, window, 0, NULL);
    if (!parent || !SetTimer(window, 1, 250, NULL) ||
        !SetWindowPos(window, HWND_TOPMOST, screen.rcWork.right - width - scale(24), screen.rcWork.top + scale(38),
                      width, height, SWP_SHOWWINDOW | SWP_NOACTIVATE)) return 3;
    MSG message;
    while (GetMessageW(&message, NULL, 0, 0) > 0) DispatchMessageW(&message);
    CancelSynchronousIo(parent);
    CloseHandle(parent);
    DeleteObject(title_font);
    DeleteObject(body_font);
    if (result) {
        HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
        DWORD count;
        WriteFile(output, result, (DWORD)lstrlenA(result), &count, NULL);
        WriteFile(output, "\n", 1, &count, NULL);
    }
    return 0;
}
