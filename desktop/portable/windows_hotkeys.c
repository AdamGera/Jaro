#include "windows_dictation.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { HOLD_MS = 180, DOUBLE_TAP_MS = 350, KEY_POLL_MS = 20 };
enum { EVENT_START = WM_APP + 1, EVENT_FINISH, EVENT_CANCEL, EVENT_PASTE_FOCUS_LOST, EVENT_RESET, EVENT_ACTIVITY };
typedef enum { HOLD, TAP, HOLD_DOUBLE, DOUBLE_TAP } ShortcutMode;

static DWORD monitor_thread;
static ShortcutMode mode;
static bool held, interrupted, recording;
static bool locked, tap_armed, escape_owned;
static bool recovery_key_down, recovery_pending, recovery_mask_alt;
static DWORD pressed_at, last_tap;
static volatile LONG *activity;
static bool activity_pending;

typedef struct {
    DictationTarget target;
    DWORD since;
} FocusWatch;

static FocusWatch dictation_focus, recovery_focus;

DictationTarget active_target(void) {
    DictationTarget result = {0};
    result.window = GetForegroundWindow();
    if (!result.window) return result;
    DWORD thread = GetWindowThreadProcessId(result.window, &result.process);
    GUITHREADINFO info = {.cbSize = sizeof(info)};
    if (thread && GetGUIThreadInfo(thread, &info)) result.focus = info.hwndFocus;
    return result;
}

bool same_target(DictationTarget left, DictationTarget right) {
    return left.window && left.focus && left.process
        && left.window == right.window && left.focus == right.focus && left.process == right.process;
}

INPUT keyboard_input(WORD key, DWORD flags) {
    INPUT input = {.type = INPUT_KEYBOARD, .ki = {.wVk = key, .dwFlags = flags, .dwExtraInfo = JARO_DICTATION_INPUT}};
    return input;
}

static void input_activity(void) {
    if (!activity) return;
    InterlockedIncrement(activity);
    if (!activity_pending) activity_pending = PostThreadMessageW(monitor_thread, EVENT_ACTIVITY, 0, 0);
}

static bool modifier_key(DWORD key) {
    return (key >= VK_LSHIFT && key <= VK_RMENU) || key == VK_CONTROL || key == VK_SHIFT
        || key == VK_MENU || key == VK_LWIN || key == VK_RWIN;
}

static bool key_down(int key) {
    return (GetAsyncKeyState(key) & 0x8000) != 0;
}

static bool conflicting_modifiers(void) {
    return key_down(VK_CONTROL) || key_down(VK_SHIFT) || key_down(VK_LWIN)
        || key_down(VK_RWIN) || key_down(VK_RMENU);
}

static bool recovery_chord(void) {
    return key_down(VK_LMENU) && key_down(VK_CONTROL) && !key_down(VK_RMENU)
        && !key_down(VK_SHIFT) && !key_down(VK_LWIN) && !key_down(VK_RWIN);
}

static void watch_current_focus(FocusWatch *watch) {
    watch->target = active_target();
    watch->since = GetTickCount() + 1;
}

static void emit(const char *event) {
    puts(event);
    fflush(stdout);
}

static void lose_focus(FocusWatch *watch, const char *message) {
    if (watch->target.window) {
        watch->target = (DictationTarget){0};
        emit(message);
    }
}

static void lose_recovery_focus(void) {
    if (recovery_focus.target.window) {
        recovery_focus.target = (DictationTarget){0};
        PostThreadMessageW(monitor_thread, EVENT_PASTE_FOCUS_LOST, 0, 0);
    }
}

static void interrupt_hold(void) {
    tap_armed = false;
    if (held) {
        interrupted = true;
        if (recording && !locked) {
            recording = false;
            PostThreadMessageW(monitor_thread, EVENT_CANCEL, 0, 0);
        }
    }
}

static void start_dictation(bool hands_free) {
    tap_armed = false;
    if (!same_target(dictation_focus.target, active_target())) {
        if (dictation_focus.target.window) input_activity();
        lose_focus(&dictation_focus, "focus-lost");
        watch_current_focus(&dictation_focus);
    }
    locked = hands_free;
    recording = true;
    PostThreadMessageW(monitor_thread, EVENT_START, 0, 0);
}

static void end_dictation(UINT event) {
    locked = recording = tap_armed = false;
    PostThreadMessageW(monitor_thread, event, 0, 0);
}

static bool release_alt(DWORD now) {
    if (interrupted) return false;
    if (recording) {
        end_dictation(EVENT_FINISH);
        return true;
    }
    if (mode == TAP) {
        start_dictation(true);
        return true;
    }
    if (mode == HOLD || now - pressed_at >= HOLD_MS) {
        tap_armed = false;
        return false;
    }
    if (tap_armed && pressed_at - last_tap <= DOUBLE_TAP_MS) {
        start_dictation(true);
    } else {
        last_tap = now;
        tap_armed = true;
    }
    return true;
}

static bool mask_alt_release(const KBDLLHOOKSTRUCT *key) {
    // The neutral menu-mask key makes this a chord to Windows, so releasing
    // the dictation shortcut does not move focus into the menu bar.
    INPUT keys[] = {keyboard_input(0xe8, 0), keyboard_input(0xe8, KEYEVENTF_KEYUP),
                    keyboard_input(VK_LMENU, KEYEVENTF_KEYUP)};
    keys[2].ki.wScan = (WORD)key->scanCode;
    return SendInput(3, keys, sizeof(INPUT)) == 3;
}

static LRESULT CALLBACK keyboard_hook(int code, WPARAM event, LPARAM payload) {
    if (code != HC_ACTION) return CallNextHookEx(NULL, code, event, payload);
    const KBDLLHOOKSTRUCT *key = (const KBDLLHOOKSTRUCT *)payload;
    bool down = event == WM_KEYDOWN || event == WM_SYSKEYDOWN;
    if (key->flags & LLKHF_INJECTED) {
        if (down && !modifier_key(key->vkCode) && key->dwExtraInfo != JARO_DICTATION_INPUT) {
            input_activity();
            lose_recovery_focus();
        }
        return CallNextHookEx(NULL, code, event, payload);
    }
    if (key->vkCode == VK_ESCAPE && (escape_owned || (down && locked && !key_down(VK_MENU)
                                                    && !conflicting_modifiers()))) {
        if (down && !escape_owned) end_dictation(EVENT_CANCEL);
        escape_owned = down;
        return 1;
    }
    if (key->vkCode == 'V') {
        if (recovery_key_down) {
            if (!down) recovery_key_down = false;
            return 1;
        }
        if (down && !recording && recovery_chord()) {
            lose_recovery_focus();
            watch_current_focus(&recovery_focus);
            recovery_key_down = recovery_pending = recovery_mask_alt = true;
            return 1;
        }
    }
    bool recovery_modifier = key->vkCode == VK_LMENU || key->vkCode == VK_LCONTROL || key->vkCode == VK_RCONTROL;
    if (down && (!recovery_pending || !recovery_modifier)) lose_recovery_focus();
    if (key->vkCode == VK_LMENU) {
        if (down && !held) {
            held = true;
            interrupted = conflicting_modifiers();
            if (interrupted) tap_armed = false;
            pressed_at = GetTickCount();
        } else if (!down && (held || recovery_mask_alt)) {
            bool consumed = held && release_alt(GetTickCount());
            held = false;
            bool mask = recovery_mask_alt || consumed;
            recovery_mask_alt = false;
            if (mask && mask_alt_release(key)) return 1;
        }
    } else if (down) {
        if (!modifier_key(key->vkCode)) input_activity();
        interrupt_hold();
    }
    return CallNextHookEx(NULL, code, event, payload);
}

static LRESULT CALLBACK mouse_hook(int code, WPARAM event, LPARAM payload) {
    if (code == HC_ACTION && (event == WM_LBUTTONDOWN || event == WM_RBUTTONDOWN
                           || event == WM_MBUTTONDOWN || event == WM_XBUTTONDOWN)) {
        input_activity();
        lose_recovery_focus();
        interrupt_hold();
    }
    return CallNextHookEx(NULL, code, event, payload);
}

static bool focus_event_changed(FocusWatch watch, DictationTarget current, DWORD event, HWND window, DWORD occurred) {
    return watch.target.window && (int32_t)(occurred - watch.since) >= 0
        && (!same_target(watch.target, current)
            || (event == EVENT_OBJECT_FOCUS && (window == watch.target.focus || window == watch.target.window)));
}

static void CALLBACK focus_event(HWINEVENTHOOK hook, DWORD event, HWND window, LONG object,
                                 LONG child, DWORD thread, DWORD occurred) {
    (void)hook; (void)object; (void)child; (void)thread;
    if (!dictation_focus.target.window && !recovery_focus.target.window) return;
    DictationTarget current = active_target();
    bool changed = focus_event_changed(dictation_focus, current, event, window, occurred);
    bool recovery_changed = focus_event_changed(recovery_focus, current, event, window, occurred);
    if (changed || recovery_changed) input_activity();
    if (changed) lose_focus(&dictation_focus, "focus-lost");
    if (recovery_changed) lose_recovery_focus();
}

static void print_target(const char *event, DictationTarget target, bool include_application) {
    char application[MAX_PATH * 4] = "";
    HANDLE process = include_application ? OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, target.process) : NULL;
    if (process) {
        WCHAR path[MAX_PATH];
        DWORD length = MAX_PATH;
        if (QueryFullProcessImageNameW(process, 0, path, &length)) {
            WCHAR *name = wcsrchr(path, L'\\');
            WideCharToMultiByte(CP_UTF8, 0, name ? name + 1 : path, -1,
                                application, sizeof(application), NULL, NULL);
        }
        CloseHandle(process);
    }
    printf("%s %llu %llu %lu %s\n", event, (unsigned long long)(uintptr_t)target.window,
           (unsigned long long)(uintptr_t)target.focus, (unsigned long)target.process, application);
    fflush(stdout);
}

static void poll_gesture(void) {
    if (dictation_focus.target.window || recovery_focus.target.window) {
        DictationTarget current = active_target();
        bool changed = dictation_focus.target.window && !same_target(dictation_focus.target, current);
        bool recovery_changed = recovery_focus.target.window && !same_target(recovery_focus.target, current);
        if (changed || recovery_changed) input_activity();
        if (changed) lose_focus(&dictation_focus, "focus-lost");
        if (recovery_changed) lose_recovery_focus();
    }
    if (recovery_pending && !recovery_key_down && !key_down(VK_MENU) && !key_down(VK_CONTROL)) {
        recovery_pending = false;
        print_target("paste-last", recovery_focus.target, false);
    }
    if (!held) return;
    if (!key_down(VK_LMENU)) {
        interrupt_hold();
        held = false;
    } else if ((mode == HOLD || mode == HOLD_DOUBLE) && !interrupted && !recording
               && GetTickCount() - pressed_at >= HOLD_MS) {
        start_dictation(false);
    }
}

static DWORD WINAPI wait_for_stop(void *context) {
    char byte;
    DWORD received;
    while (ReadFile((HANDLE)context, &byte, sizeof(byte), &received, NULL) && received && byte == 'r') {
        PostThreadMessageW(monitor_thread, EVENT_RESET, 0, 0);
    }
    PostThreadMessageW(monitor_thread, WM_QUIT, 0, 0);
    return 0;
}

static int monitor(void) {
    monitor_thread = GetCurrentThreadId();
    MSG message;
    PeekMessageW(&message, NULL, 0, 0, PM_NOREMOVE);
    HHOOK keyboard = SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_hook, GetModuleHandleW(NULL), 0);
    HHOOK mouse = SetWindowsHookExW(WH_MOUSE_LL, mouse_hook, GetModuleHandleW(NULL), 0);
    HWINEVENTHOOK focus = SetWinEventHook(EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, NULL, focus_event,
                                         0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
    HWINEVENTHOOK foreground = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, NULL, focus_event,
                                              0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
    UINT_PTR timer = SetTimer(NULL, 0, KEY_POLL_MS, NULL);
    HANDLE control = CreateThread(NULL, 0, wait_for_stop, GetStdHandle(STD_INPUT_HANDLE), 0, NULL);
    int result = 1;
    if (keyboard && mouse && focus && foreground && timer && control) {
        emit("ready");
        BOOL status;
        while ((status = GetMessageW(&message, NULL, 0, 0)) > 0) {
            if (message.message == WM_TIMER) poll_gesture();
            else if (message.message == EVENT_START) print_target("start", dictation_focus.target, true);
            else if (message.message == EVENT_FINISH) emit("finish");
            else if (message.message == EVENT_CANCEL) emit("cancel");
            else if (message.message == EVENT_PASTE_FOCUS_LOST) emit("paste-focus-lost");
            else if (message.message == EVENT_ACTIVITY) {
                activity_pending = false;
                printf("activity %lu\n", (unsigned long)(DWORD)*activity);
                fflush(stdout);
            }
            else if (message.message == EVENT_RESET) {
                locked = recording = tap_armed = false;
                interrupted = held;
            }
            else DispatchMessageW(&message);
        }
        result = status < 0;
    }
    if (keyboard) UnhookWindowsHookEx(keyboard);
    if (mouse) UnhookWindowsHookEx(mouse);
    if (focus) UnhookWinEvent(focus);
    if (foreground) UnhookWinEvent(foreground);
    if (timer) KillTimer(NULL, timer);
    if (control) {
        CancelSynchronousIo(control);
        CloseHandle(control);
    }
    return result;
}

static bool argument_number(const char *argument, unsigned long long *value) {
    char *end;
    if (*argument < '0' || *argument > '9') return false;
    *value = strtoull(argument, &end, 10);
    return !*end;
}

static bool activity_name(const char *token, char name[64]) {
    if (strlen(token) != 32 || strspn(token, "0123456789abcdef") != 32) return false;
    snprintf(name, 64, "Local\\JaroDictation-%s", token);
    return true;
}

int main(int argc, char **argv) {
    if (argc >= 2 && argc <= 4 && !strcmp(argv[1], "listen")) {
        if (argc == 2 || !strcmp(argv[2], "hold")) mode = HOLD;
        else if (!strcmp(argv[2], "tap")) mode = TAP;
        else if (!strcmp(argv[2], "holdDouble")) mode = HOLD_DOUBLE;
        else if (!strcmp(argv[2], "doubleTap")) mode = DOUBLE_TAP;
        else return 2;
        if (argc != 4) return monitor();
        char name[64];
        if (!activity_name(argv[3], name)) return 2;
        HANDLE mapping = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, sizeof(LONG), name);
        if (!mapping) return 2;
        if (GetLastError() != ERROR_ALREADY_EXISTS) activity = MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, sizeof(LONG));
        int result = activity ? monitor() : 2;
        if (activity) UnmapViewOfFile((const void *)activity);
        CloseHandle(mapping);
        return result;
    }
    if ((argc != 6 && argc != 8) || strcmp(argv[1], "paste")
        || (strcmp(argv[5], "standard") && strcmp(argv[5], "terminal"))) return 2;
    unsigned long long window, focus, process;
    if (!argument_number(argv[2], &window) || !argument_number(argv[3], &focus)
        || !argument_number(argv[4], &process) || !window || !focus || !process || process > UINT32_MAX) return 2;
    DictationTarget destination = {(HWND)(uintptr_t)window, (HWND)(uintptr_t)focus, (DWORD)process};
    bool terminal = !strcmp(argv[5], "terminal");
    if (argc == 6) return paste_dictation(destination, terminal, NULL, 0);
    char name[64];
    unsigned long long expected;
    if (!activity_name(argv[6], name) || !argument_number(argv[7], &expected) || expected > UINT32_MAX) return 2;
    HANDLE mapping = OpenFileMappingA(FILE_MAP_READ, FALSE, name);
    if (!mapping) return PASTE_CONTINUATION_CHANGED;
    const volatile LONG *status = MapViewOfFile(mapping, FILE_MAP_READ, 0, 0, sizeof(LONG));
    int result = status ? paste_dictation(destination, terminal, status, (DWORD)expected) : PASTE_CONTINUATION_CHANGED;
    if (status) UnmapViewOfFile((const void *)status);
    CloseHandle(mapping);
    return result;
}
