#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellscalingapi.h>
#include <wchar.h>
#include "dictation_meter.h"

static int processing;
static double level;
static ULONGLONG started;
static UINT dpi = 96;
#define METER_FRAME (WM_APP + 1)

static int scaled(int value) {
    return MulDiv(value, dpi, 96);
}

static void paint(HWND window) {
    PAINTSTRUCT paint;
    HDC destination = BeginPaint(window, &paint);
    RECT rect;
    GetClientRect(window, &rect);
    HDC buffer = CreateCompatibleDC(destination);
    HBITMAP bitmap = buffer ? CreateCompatibleBitmap(destination, rect.right, rect.bottom) : NULL;
    HGDIOBJ old_bitmap = bitmap ? SelectObject(buffer, bitmap) : NULL;
    HDC dc = bitmap ? buffer : destination;
    HBRUSH background = CreateSolidBrush(RGB(23, 24, 36));
    FillRect(dc, &rect, background);
    DeleteObject(background);
    HPEN border = CreatePen(PS_SOLID, 1, RGB(68, 68, 88));
    HGDIOBJ old_pen = SelectObject(dc, border);
    HGDIOBJ old_brush = SelectObject(dc, GetStockObject(NULL_BRUSH));
    RoundRect(dc, 0, 0, rect.right, rect.bottom, rect.bottom, rect.bottom);
    HBRUSH signal = CreateSolidBrush(processing ? RGB(144, 199, 190) : RGB(204, 195, 255));
    SelectObject(dc, GetStockObject(NULL_PEN));
    SelectObject(dc, signal);
    double elapsed = (GetTickCount64() - started) / 1000.0;
    int width = scaled(METER_BAR_WIDTH);
    for (int i = 0; i < METER_BARS; i++) {
        int height = (int)(meter_height(i, elapsed, level, processing) * dpi / 96 + 0.5);
        int x = (rect.right - width) / 2 + scaled((i - 2) * METER_GAP);
        int y = (rect.bottom - height) / 2;
        RoundRect(dc, x, y, x + width, y + height, width, width);
    }
    SelectObject(dc, old_brush);
    SelectObject(dc, old_pen);
    DeleteObject(signal);
    DeleteObject(border);
    if (bitmap) {
        BitBlt(destination, 0, 0, rect.right, rect.bottom, buffer, 0, 0, SRCCOPY);
        SelectObject(buffer, old_bitmap);
        DeleteObject(bitmap);
    }
    if (buffer) DeleteDC(buffer);
    EndPaint(window, &paint);
}

static DWORD WINAPI receive(void *context) {
    HWND window = context;
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    char data[256];
    DWORD length;
    Meter meter = {0};
    while (ReadFile(input, data, sizeof(data), &length, NULL) && length) {
        meter_read(&meter, data, length);
        PostMessageW(window, METER_FRAME, meter.processing, (LPARAM)(meter.level * 100 + .5));
    }
    PostMessageW(window, WM_CLOSE, 0, 0);
    return 0;
}

static LRESULT CALLBACK indicator(HWND window, UINT message, WPARAM w, LPARAM l) {
    switch (message) {
    case WM_PAINT: paint(window); return 0;
    case WM_NCHITTEST: return HTTRANSPARENT;
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case METER_FRAME: processing = (int)w; level = l / 100.0; return 0;
    case WM_TIMER: InvalidateRect(window, NULL, FALSE); return 0;
    case WM_DESTROY: PostQuitMessage(0); return 0;
    default: return DefWindowProcW(window, message, w, l);
    }
}

int wmain(int argc, wchar_t **argv) {
    if (argc != 2 || (wcscmp(argv[1], L"top") && wcscmp(argv[1], L"bottom"))) return 2;
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    HINSTANCE instance = GetModuleHandleW(NULL);
    WNDCLASSW kind = {.lpfnWndProc = indicator, .hInstance = instance, .lpszClassName = L"JaroDictationIndicator"};
    if (!RegisterClassW(&kind)) return 2;
    HWND window = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE |
        WS_EX_TRANSPARENT | WS_EX_LAYERED, kind.lpszClassName, L"Jaro Dictation Indicator",
        WS_POPUP, 0, 0, METER_WIDTH, METER_HEIGHT, NULL, NULL, instance, NULL);
    if (!window) return 2;

    HWND target = GetForegroundWindow();
    HMONITOR monitor = MonitorFromWindow(target, MONITOR_DEFAULTTOPRIMARY);
    MONITORINFO bounds = {.cbSize = sizeof(bounds)};
    if (!GetMonitorInfoW(monitor, &bounds)) return 2;
    UINT vertical_dpi;
    if (GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, &dpi, &vertical_dpi) != S_OK) dpi = 96;
    RECT work = bounds.rcWork;
    int width = scaled(METER_WIDTH), height = scaled(METER_HEIGHT);
    int x = (work.left + work.right - width) / 2;
    int y = wcscmp(argv[1], L"bottom") == 0 ? work.bottom - height - scaled(METER_MARGIN) : work.top + scaled(METER_MARGIN);
    HRGN outline = CreateRoundRectRgn(0, 0, width + 1, height + 1, height, height);
    if (outline && !SetWindowRgn(window, outline, FALSE)) DeleteObject(outline);
    if (!SetLayeredWindowAttributes(window, 0, 255, LWA_ALPHA)) return 3;
    started = GetTickCount64();
    HANDLE control = CreateThread(NULL, 0, receive, window, 0, NULL);
    if (!control || !SetTimer(window, 1, 17, NULL) ||
        !SetWindowPos(window, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)) return 3;
    MSG message;
    while (GetMessageW(&message, NULL, 0, 0) > 0) DispatchMessageW(&message);
    CancelSynchronousIo(control);
    CloseHandle(control);
    return 0;
}
