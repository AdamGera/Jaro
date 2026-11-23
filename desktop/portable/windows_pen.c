#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <windowsx.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#ifndef WDA_EXCLUDEFROMCAPTURE
#define WDA_EXCLUDEFROMCAPTURE 0x11
#endif

#define MAX_STROKES 32
#define MAX_POINTS 1024
#define HOLD_US 650000
#define FADE_US 650000
#define ESCAPE_HOTKEY 1

typedef struct {
    int x, y;
    int64_t time;
} Point;

typedef struct {
    size_t count;
    Point points[MAX_POINTS];
    int64_t released;
} Stroke;

typedef struct {
    HWND window;
    RECT bounds;
    SIZE size;
    HDC canvas;
    uint32_t *pixels;
    LARGE_INTEGER frequency;
    Stroke strokes[MAX_STROKES];
    Stroke *current;
    unsigned next;
    bool dirty;
} Pen;

static int64_t clock_us(const Pen *pen) {
    LARGE_INTEGER ticks;
    QueryPerformanceCounter(&ticks);
    return (int64_t)((double)ticks.QuadPart * 1000000 / (double)pen->frequency.QuadPart);
}

static void dab(Pen *pen, double x, double y, double radius, double opacity) {
    double edge = radius + .5;
    int left = max(0, (int)floor(x - edge)), top = max(0, (int)floor(y - edge));
    int right = min(pen->size.cx - 1, (int)ceil(x + edge)), bottom = min(pen->size.cy - 1, (int)ceil(y + edge));
    for (int row = top; row <= bottom; row++) {
        uint32_t *pixel = pen->pixels + (size_t)row * pen->size.cx + left;
        for (int col = left; col <= right; col++, pixel++) {
            double dx = x - col, dy = y - row;
            double coverage = min(1., max(0., edge - sqrt(dx * dx + dy * dy)));
            uint32_t alpha = (uint32_t)round(255 * opacity * coverage);
            if (alpha > (*pixel >> 24)) {
                // UpdateLayeredWindow expects premultiplied BGRA.
                *pixel = alpha << 24 | alpha << 16 | (alpha * 102 / 255) << 8 | (alpha * 56 / 255);
            }
        }
    }
}

static void paint_stroke(Pen *pen, const Stroke *stroke, double radius, double opacity) {
    Point from = stroke->points[0];
    dab(pen, from.x, from.y, radius, opacity);
    for (size_t index = 1; index < stroke->count; index++) {
        Point to = stroke->points[index];
        int dx = to.x - from.x, dy = to.y - from.y;
        int steps = (int)ceil(hypot(dx, dy) / max(.75, radius / 2));
        for (int step = 1; step <= steps; step++) {
            double fraction = (double)step / steps;
            dab(pen, from.x + dx * fraction, from.y + dy * fraction, radius, opacity);
        }
        from = to;
    }
}

static bool present(Pen *pen) {
    size_t count = (size_t)pen->size.cx * pen->size.cy;
    // Zero-alpha pixels in a layered window let mouse input through.
    for (size_t index = 0; index < count; index++) pen->pixels[index] = 0x01000000;
    double radius = max(1.5, min(pen->size.cx, pen->size.cy) * .0035);
    int64_t now = clock_us(pen);
    for (unsigned index = 0; index < MAX_STROKES; index++) {
        Stroke *stroke = &pen->strokes[index];
        if (!stroke->count) continue;
        int64_t age = stroke->released ? now - stroke->released : 0;
        if (age >= HOLD_US + FADE_US) stroke->count = 0;
        else paint_stroke(pen, stroke, radius, min(1., (double)(HOLD_US + FADE_US - age) / FADE_US));
    }
    POINT at = {pen->bounds.left, pen->bounds.top}, origin = {0, 0};
    BLENDFUNCTION blend = {AC_SRC_OVER, 0, 255, AC_SRC_ALPHA};
    pen->dirty = false;
    return UpdateLayeredWindow(pen->window, NULL, &at, &pen->size, pen->canvas, &origin, 0, &blend, ULW_ALPHA);
}

static void point(Pen *pen, LPARAM cursor) {
    Stroke *stroke = pen->current;
    if (!stroke) return;
    int x = max(0, min(pen->size.cx - 1, GET_X_LPARAM(cursor)));
    int y = max(0, min(pen->size.cy - 1, GET_Y_LPARAM(cursor)));
    if (stroke->count) {
        Point previous = stroke->points[stroke->count - 1];
        if (x == previous.x && y == previous.y) return;
    }
    if (stroke->count == MAX_POINTS) {
        for (size_t index = 1; index < MAX_POINTS / 2; index++)
            stroke->points[index] = stroke->points[index * 2];
        stroke->count = MAX_POINTS / 2;
    }
    stroke->points[stroke->count++] = (Point){x, y, clock_us(pen)};
    pen->dirty = true;
}

static void release(Pen *pen) {
    Stroke *stroke = pen->current;
    if (!stroke) return;
    pen->current = NULL;
    stroke->released = clock_us(pen);
    printf("{\"released\":%lld,\"points\":[", (long long)stroke->released);
    for (size_t index = 0; index < stroke->count; index++) {
        Point at = stroke->points[index];
        printf("%s[%.5f,%.5f,%lld]", index ? "," : "", (double)at.x / pen->size.cx,
               (double)at.y / pen->size.cy, (long long)at.time);
    }
    puts("]}");
    fflush(stdout);
    if (GetCapture() == pen->window) ReleaseCapture();
}

static bool needs_repaint(const Pen *pen) {
    if (pen->dirty) return true;
    int64_t now = clock_us(pen);
    for (unsigned index = 0; index < MAX_STROKES; index++) {
        const Stroke *stroke = &pen->strokes[index];
        if (stroke->count && stroke->released && now - stroke->released >= HOLD_US) return true;
    }
    return false;
}

static LRESULT CALLBACK pen_window(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_NCCREATE) {
        CREATESTRUCTW *creation = (CREATESTRUCTW *)lparam;
        SetWindowLongPtrW(window, GWLP_USERDATA, (LONG_PTR)creation->lpCreateParams);
    }
    Pen *pen = (Pen *)GetWindowLongPtrW(window, GWLP_USERDATA);
    if (!pen) return DefWindowProcW(window, message, wparam, lparam);
    switch (message) {
        case WM_LBUTTONDOWN:
            release(pen);
            pen->current = &pen->strokes[pen->next++ % MAX_STROKES];
            pen->current->count = 0;
            pen->current->released = 0;
            point(pen, lparam);
            SetCapture(window);
            return 0;
        case WM_MOUSEMOVE:
            point(pen, lparam);
            return 0;
        case WM_LBUTTONUP:
            point(pen, lparam);
            release(pen);
            return 0;
        case WM_CAPTURECHANGED:
        case WM_CANCELMODE:
            release(pen);
            return 0;
        case WM_TIMER:
            // A drag can lose its release when the non-activating surface loses capture.
            if (pen->current && !(GetAsyncKeyState(GetSystemMetrics(SM_SWAPBUTTON) ? VK_RBUTTON : VK_LBUTTON) & 0x8000))
                release(pen);
            if (needs_repaint(pen) && !present(pen)) PostQuitMessage(1);
            return 0;
        case WM_HOTKEY:
        case WM_RBUTTONDOWN:
        case WM_DISPLAYCHANGE:
        case WM_DPICHANGED:
        case WM_CLOSE:
            PostQuitMessage(0);
            return 0;
        default:
            return DefWindowProcW(window, message, wparam, lparam);
    }
}

static DWORD WINAPI wait_for_parent(void *context) {
    BYTE ignored;
    DWORD length;
    ReadFile(GetStdHandle(STD_INPUT_HANDLE), &ignored, 1, &length, NULL);
    SetEvent((HANDLE)context);
    return 0;
}

static int run_window(Pen *pen) {
    HANDLE stop = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!stop) return 1;
    HANDLE reader = CreateThread(NULL, 0, wait_for_parent, stop, 0, NULL);
    int result = 1;
    if (reader && SetTimer(pen->window, 1, 16, NULL)
            && RegisterHotKey(pen->window, ESCAPE_HOTKEY, MOD_NOREPEAT, VK_ESCAPE)
            && SetWindowPos(pen->window, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)) {
        puts("READY");
        fflush(stdout);
        for (;;) {
            DWORD next = MsgWaitForMultipleObjects(1, &stop, FALSE, INFINITE, QS_ALLINPUT);
            if (next != WAIT_OBJECT_0 + 1) {
                result = next != WAIT_OBJECT_0;
                break;
            }
            MSG message;
            while (PeekMessageW(&message, NULL, 0, 0, PM_REMOVE)) {
                if (message.message == WM_QUIT) {
                    result = (int)message.wParam;
                    goto finished;
                }
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
finished:
    UnregisterHotKey(pen->window, ESCAPE_HOTKEY);
    KillTimer(pen->window, 1);
    release(pen);
    if (reader) {
        CancelSynchronousIo(reader);
        // The process owns the stop handle until the reader is finished.
        if (WaitForSingleObject(reader, 100) == WAIT_OBJECT_0) CloseHandle(stop);
        CloseHandle(reader);
    } else {
        CloseHandle(stop);
    }
    return result;
}

int draw_desktop(HMONITOR monitor, RECT crop, SIZE capture_size) {
    if (!SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)) return 1;
    MONITORINFO info = {.cbSize = sizeof(info)};
    if (!GetMonitorInfoW(monitor, &info)) return 1;
    RECT bounds = info.rcMonitor;
    SIZE monitor_size = {bounds.right - bounds.left, bounds.bottom - bounds.top};
    if (capture_size.cx) {
        if (monitor_size.cx != capture_size.cx || monitor_size.cy != capture_size.cy) return 1;
        bounds = (RECT){bounds.left + crop.left, bounds.top + crop.top,
                        bounds.left + crop.right, bounds.top + crop.bottom};
    }
    SIZE size = {bounds.right - bounds.left, bounds.bottom - bounds.top};
    if (size.cx < 2 || size.cy < 2 || (size_t)size.cx * size.cy > 40 * 1024 * 1024) return 1;
    Pen *pen = calloc(1, sizeof(*pen));
    if (!pen) return 1;
    pen->bounds = bounds;
    pen->size = size;
    QueryPerformanceFrequency(&pen->frequency);
    HINSTANCE instance = GetModuleHandleW(NULL);
    WNDCLASSW type = {.lpfnWndProc = pen_window, .hInstance = instance,
                      .hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_CROSS), .lpszClassName = L"JaroScreenPen"};
    HBITMAP bitmap = NULL;
    HGDIOBJ previous = NULL;
    int result = 1;
    if (RegisterClassW(&type)) {
        pen->window = CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, type.lpszClassName,
            L"Jaro Screen Pen", WS_POPUP, bounds.left, bounds.top, size.cx, size.cy, NULL, NULL, instance, pen);
    }
    if (pen->window) {
        BITMAPINFO bits = {.bmiHeader = {.biSize = sizeof(BITMAPINFOHEADER), .biWidth = size.cx,
            .biHeight = -size.cy, .biPlanes = 1, .biBitCount = 32, .biCompression = BI_RGB}};
        pen->canvas = CreateCompatibleDC(NULL);
        bitmap = CreateDIBSection(pen->canvas, &bits, DIB_RGB_COLORS, (void **)&pen->pixels, NULL, 0);
        if (pen->canvas && bitmap) {
            previous = SelectObject(pen->canvas, bitmap);
            DWORD affinity = WDA_NONE;
            if (previous && present(pen) && SetWindowDisplayAffinity(pen->window, WDA_EXCLUDEFROMCAPTURE)
                    && GetWindowDisplayAffinity(pen->window, &affinity) && affinity == WDA_EXCLUDEFROMCAPTURE)
                result = run_window(pen);
        }
        DestroyWindow(pen->window);
    }
    if (previous) SelectObject(pen->canvas, previous);
    if (bitmap) DeleteObject(bitmap);
    if (pen->canvas) DeleteDC(pen->canvas);
    free(pen);
    return result;
}
