#include "windows_dictation.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    HOLD_MS = 180, DOUBLE_TAP_MS = 350, KEY_POLL_MS = 20, PASTE_PICKER_HOLD_MS = 200,
    WATCHDOG_MS = 1000, HOOK_SILENCE_MS = 3000, MAX_REARM_MS = 60000
};
enum {
    EVENT_START = WM_APP + 1, EVENT_FINISH, EVENT_CANCEL, EVENT_ESCAPE,
    EVENT_RECOVERY_TARGET, EVENT_PASTE_FOCUS_LOST, EVENT_RESET, EVENT_ACTIVITY, EVENT_START_RAW,
    EVENT_COMMAND_TARGET, EVENT_PICKER_ROWS, EVENT_PICKER_KEY
};
typedef enum { HOLD, TAP, HOLD_DOUBLE, DOUBLE_TAP } ShortcutMode;
typedef struct {
    WORD key;
    DWORD modifiers;
    bool down, owned;
} ShortcutTrigger;

static DWORD monitor_thread;
static ShortcutMode mode;
static ShortcutTrigger triggers[2] = {{.key = VK_LMENU}, {0}};
static ShortcutTrigger *gesture_trigger = &triggers[0], *tap_trigger;
static bool mask_chord_alt, mask_chord_win;
static bool held, interrupted, recording;
static bool locked, tap_armed, escape_owned;
static volatile LONG cancel_armed;
static bool recovery_key_down, recovery_pending, recovery_mask_alt;
static HWND recovery_window;
static DWORD recovery_pressed_at, picker_sequence, active_picker;
static bool picker_held, picker_copy, picker_guard;
static int picker_choice = -1;
static bool picker_keys[256];
static PVOID volatile pending_picker_rows;
static DWORD pressed_at, last_tap;
static volatile LONG *activity;
static bool activity_pending;
static DWORD last_hook_event, last_rearm, rearm_delay = HOOK_SILENCE_MS;

typedef struct {
    DictationTarget target;
    DWORD since;
    HWND object_window;
    LONG object, child;
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
    return left.window && left.process
        && left.window == right.window && left.process == right.process
        && (!left.focus || !right.focus || left.focus == right.focus);
}

INPUT keyboard_input(WORD key, DWORD flags, HKL layout) {
    WORD scan = key == 0xe8 ? 0 : (WORD)MapVirtualKeyExW(key, MAPVK_VK_TO_VSC, layout);
    INPUT input = {.type = INPUT_KEYBOARD, .ki = {.wVk = key, .wScan = scan, .dwFlags = flags, .dwExtraInfo = JARO_DICTATION_INPUT}};
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

static bool modifier_held(DWORD excluded, int left, int right) {
    return (excluded != (DWORD)left && key_down(left)) || (excluded != (DWORD)right && key_down(right));
}

static DWORD chord_modifier(DWORD key) {
    switch (key) {
        case VK_LMENU: return MOD_ALT;
        case VK_CONTROL: case VK_LCONTROL: case VK_RCONTROL: return MOD_CONTROL;
        case VK_SHIFT: case VK_LSHIFT: case VK_RSHIFT: return MOD_SHIFT;
        case VK_LWIN: case VK_RWIN: return MOD_WIN;
        default: return 0;
    }
}

static DWORD chord_modifiers(DWORD released) {
    DWORD actual = 0;
    if (released != VK_LMENU && key_down(VK_LMENU)) actual |= MOD_ALT;
    if (modifier_held(released, VK_LCONTROL, VK_RCONTROL)) actual |= MOD_CONTROL;
    if (modifier_held(released, VK_LSHIFT, VK_RSHIFT)) actual |= MOD_SHIFT;
    if (modifier_held(released, VK_LWIN, VK_RWIN)) actual |= MOD_WIN;
    return actual;
}

static bool chord_modifiers_match(const ShortcutTrigger *trigger, DWORD released) {
    // Right Alt can carry a synthetic Ctrl on AltGr layouts.
    return (released == VK_RMENU || !key_down(VK_RMENU)) && chord_modifiers(released) == trigger->modifiers;
}

static bool recovery_chord(void) {
    return key_down(VK_LMENU) && key_down(VK_CONTROL) && !key_down(VK_RMENU)
        && !key_down(VK_SHIFT) && !key_down(VK_LWIN) && !key_down(VK_RWIN);
}

static void watch_focus(FocusWatch *watch, DictationTarget target) {
    *watch = (FocusWatch){.target = target, .since = GetTickCount() + 1};
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

static bool reset_recovery_focus(void) {
    bool had_focus = recovery_focus.target.window != NULL;
    recovery_pending = false;
    recovery_window = NULL;
    active_picker = 0;
    picker_guard = picker_held = false;
    picker_choice = -1;
    recovery_focus.target = (DictationTarget){0};
    return had_focus;
}

static void lose_recovery_focus(void) {
    if (reset_recovery_focus())
        PostThreadMessageW(monitor_thread, EVENT_PASTE_FOCUS_LOST, 0, 0);
}

static void choose_paste_picker(int index, bool copy) {
    close_paste_picker();
    if (!active_picker) return;
    DictationTarget current = active_target();
    if (index < 0 || !same_target(recovery_focus.target, current) || current.focus != recovery_focus.target.focus) {
        lose_recovery_focus();
        return;
    }
    picker_choice = index;
    picker_copy = copy;
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
    locked = hands_free;
    recording = true;
    UINT event = gesture_trigger == &triggers[1] ? EVENT_START_RAW : EVENT_START;
    PostThreadMessageW(monitor_thread, event, (WPARAM)GetForegroundWindow(), activity ? *activity : 0);
}

static void end_dictation(UINT event) {
    locked = recording = tap_armed = false;
    PostThreadMessageW(monitor_thread, event, 0, 0);
}

static bool release_shortcut(DWORD now) {
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
    if (tap_armed && tap_trigger == gesture_trigger && pressed_at - last_tap <= DOUBLE_TAP_MS) {
        start_dictation(true);
    } else {
        last_tap = now;
        tap_trigger = gesture_trigger;
        tap_armed = true;
    }
    return true;
}

static bool mask_menu_release(const KBDLLHOOKSTRUCT *key) {
    // The neutral menu-mask key makes this a chord to Windows, so releasing
    // the dictation shortcut does not move focus into the menu bar.
    INPUT keys[] = {keyboard_input(0xe8, 0, NULL), keyboard_input(0xe8, KEYEVENTF_KEYUP, NULL),
                    keyboard_input((WORD)key->vkCode, KEYEVENTF_KEYUP |
                        ((key->flags & LLKHF_EXTENDED) ? KEYEVENTF_EXTENDEDKEY : 0), NULL)};
    keys[2].ki.wScan = (WORD)key->scanCode;
    return SendInput(3, keys, sizeof(INPUT)) == 3;
}

static bool custom_chord(const KBDLLHOOKSTRUCT *key, bool down) {
    ShortcutTrigger *matched = NULL;
    for (size_t i = 0; i < sizeof(triggers) / sizeof(*triggers); ++i) {
        ShortcutTrigger *trigger = &triggers[i];
        if (!trigger->key || trigger->key == VK_LMENU || key->vkCode != trigger->key) continue;
        if (trigger->owned || (down && !trigger->down && chord_modifiers_match(trigger, 0))) matched = trigger;
        trigger->down = down;
    }
    if (matched) {
        if (down && !matched->owned) {
            matched->owned = true;
            if (matched->modifiers & MOD_ALT) mask_chord_alt = true;
            if (matched->modifiers & MOD_WIN) mask_chord_win = true;
            // Left Alt can be the primary trigger and a modifier of the raw chord.
            // The chord wins until a Left Alt take has actually started.
            bool provisional_alt = held && gesture_trigger->key == VK_LMENU && !recording;
            if ((!held && !recording) || gesture_trigger == matched || provisional_alt) {
                gesture_trigger = matched;
                held = true;
                interrupted = false;
                pressed_at = GetTickCount();
            } else if (held && locked && gesture_trigger->key == VK_LMENU) {
                // An Alt chord is not a solo Alt tap to finish a locked take.
                held = false;
            }
        }
        if (!down) {
            if (held && gesture_trigger == matched) {
                release_shortcut(GetTickCount());
                held = false;
            }
            matched->owned = false;
        }
        return true;
    }
    if (!down && modifier_key(key->vkCode) && held && gesture_trigger->key != VK_LMENU
        && !chord_modifiers_match(gesture_trigger, key->vkCode)) {
        release_shortcut(GetTickCount());
        held = false;
    }
    if (!down && key->vkCode == VK_LMENU &&
        (mask_chord_alt || (triggers[0].key != VK_LMENU && recovery_mask_alt))) {
        if (held && gesture_trigger->key == VK_LMENU) {
            release_shortcut(GetTickCount());
            held = false;
        }
        mask_chord_alt = recovery_mask_alt = false;
        return mask_menu_release(key);
    }
    if (!down && mask_chord_win && (key->vkCode == VK_LWIN || key->vkCode == VK_RWIN)) {
        mask_chord_win = modifier_held(key->vkCode, VK_LWIN, VK_RWIN);
        return mask_menu_release(key);
    }
    return false;
}

static bool picker_keyboard(DWORD key, bool down) {
    if (key >= sizeof(picker_keys) / sizeof(*picker_keys)) return false;
    if (picker_keys[key]) {
        if (!down) picker_keys[key] = false;
        return true;
    }
    if (!active_picker || !down) return false;
    bool shift = key_down(VK_SHIFT);
    if (key != VK_ESCAPE && !(key == 'V' && recovery_chord())) {
        if (key_down(VK_MENU) || key_down(VK_CONTROL) || key_down(VK_LWIN) || key_down(VK_RWIN)) return false;
        if (key != VK_RETURN && key != VK_TAB && key != VK_UP && key != VK_DOWN &&
            key != VK_PRIOR && key != VK_NEXT && key != 'V') return false;
        if (shift && key != VK_TAB) return false;
    }
    if (!PostThreadMessageW(monitor_thread, EVENT_PICKER_KEY, key, shift)) return false;
    picker_keys[key] = true;
    return true;
}

static LRESULT CALLBACK keyboard_hook(int code, WPARAM event, LPARAM payload) {
    if (code != HC_ACTION) return CallNextHookEx(NULL, code, event, payload);
    last_hook_event = GetTickCount();
    rearm_delay = HOOK_SILENCE_MS;
    const KBDLLHOOKSTRUCT *key = (const KBDLLHOOKSTRUCT *)payload;
    bool down = event == WM_KEYDOWN || event == WM_SYSKEYDOWN;
    if (key->flags & LLKHF_INJECTED) {
        if (down && !modifier_key(key->vkCode) && key->dwExtraInfo != JARO_DICTATION_INPUT) {
            input_activity();
            lose_recovery_focus();
        }
        return CallNextHookEx(NULL, code, event, payload);
    }
    if (key->vkCode == 'V' && recovery_key_down) {
        if (!down) {
            recovery_key_down = false;
            picker_held = recovery_pending && recovery_chord() &&
                GetTickCount() - recovery_pressed_at >= PASTE_PICKER_HOLD_MS;
        }
        return 1;
    }
    if (picker_keyboard(key->vkCode, down)) return 1;
    if (key->vkCode == VK_ESCAPE && (escape_owned || (down && (recording || cancel_armed)
                                                    && !key_down(VK_MENU) && !conflicting_modifiers()))) {
        if (down && !escape_owned) {
            interrupted = held;
            end_dictation(EVENT_ESCAPE);
        }
        escape_owned = down;
        return 1;
    }
    if (key->vkCode == 'V') {
        if (down && !recording && recovery_chord()) {
            lose_recovery_focus();
            recovery_window = GetForegroundWindow();
            recovery_pressed_at = GetTickCount();
            recovery_key_down = recovery_pending = recovery_mask_alt = true;
            PostThreadMessageW(monitor_thread, EVENT_RECOVERY_TARGET, (WPARAM)recovery_window,
                               activity ? *activity : 0);
            return 1;
        }
    }
    bool recovery_modifier = key->vkCode == VK_LMENU || key->vkCode == VK_LCONTROL || key->vkCode == VK_RCONTROL;
    if (down && (!recovery_pending || !recovery_modifier)) lose_recovery_focus();
    if (custom_chord(key, down)) return 1;
    if (triggers[0].key == VK_LMENU && key->vkCode == VK_LMENU) {
        if ((held || recording) && gesture_trigger != &triggers[0])
            return CallNextHookEx(NULL, code, event, payload);
        if (down && !held) {
            gesture_trigger = &triggers[0];
            held = true;
            interrupted = conflicting_modifiers();
            if (interrupted) tap_armed = false;
            pressed_at = GetTickCount();
        } else if (!down && (held || recovery_mask_alt)) {
            bool consumed = held && release_shortcut(GetTickCount());
            held = false;
            bool mask = recovery_mask_alt || consumed;
            recovery_mask_alt = false;
            if (mask && mask_menu_release(key)) return 1;
        }
    } else if (down) {
        if (!modifier_key(key->vkCode)) input_activity();
        if (gesture_trigger->key == VK_LMENU || !(chord_modifier(key->vkCode) & gesture_trigger->modifiers))
            interrupt_hold();
    }
    return CallNextHookEx(NULL, code, event, payload);
}

static LRESULT CALLBACK mouse_hook(int code, WPARAM event, LPARAM payload) {
    if (code == HC_ACTION) {
        last_hook_event = GetTickCount();
        if (event == WM_LBUTTONDOWN || event == WM_RBUTTONDOWN
            || event == WM_MBUTTONDOWN || event == WM_XBUTTONDOWN) {
            const MSLLHOOKSTRUCT *mouse = (const MSLLHOOKSTRUCT *)payload;
            if (active_picker && paste_picker_contains(mouse->pt))
                return CallNextHookEx(NULL, code, event, payload);
            input_activity();
            lose_recovery_focus();
            interrupt_hold();
        }
    }
    return CallNextHookEx(NULL, code, event, payload);
}

static bool watching_event(const FocusWatch *watch, DWORD occurred) {
    return watch->target.window && (int32_t)(occurred - watch->since) >= 0;
}

static bool focus_event_changed(const FocusWatch *watch, DictationTarget current, DWORD occurred) {
    return watching_event(watch, occurred) && !same_target(watch->target, current);
}

static bool focus_object_changed(FocusWatch *watch, HWND window, LONG object, LONG child, DWORD occurred) {
    if (!window || (window != watch->target.focus && window != watch->target.window) ||
        !watching_event(watch, occurred)) return false;
    bool changed = window != watch->object_window || object != watch->object || child != watch->child;
    watch->object_window = window;
    watch->object = object;
    watch->child = child;
    return changed;
}

static void CALLBACK focus_event(HWINEVENTHOOK hook, DWORD event, HWND window, LONG object,
                                 LONG child, DWORD thread, DWORD occurred) {
    (void)hook; (void)thread;
    if (!dictation_focus.target.window && !recovery_focus.target.window) return;
    DictationTarget current = active_target();
    bool changed = focus_event_changed(&dictation_focus, current, occurred);
    bool recovery_changed = focus_event_changed(&recovery_focus, current, occurred);
    bool object_changed = false, recovery_object_changed = false;
    if (event == EVENT_OBJECT_FOCUS) {
        // Chromium can report logical focus changes through one native HWND.
        // Use them to end continuation without invalidating the foreground target.
        object_changed = focus_object_changed(&dictation_focus, window, object, child, occurred);
        recovery_object_changed = focus_object_changed(&recovery_focus, window, object, child, occurred);
    }
    if (changed || recovery_changed || object_changed || recovery_object_changed) input_activity();
    if (changed) lose_focus(&dictation_focus, "focus-lost");
    if (recovery_changed || (picker_guard && recovery_object_changed)) lose_recovery_focus();
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

static DictationTarget posted_target(WPARAM window, LPARAM revision) {
    DictationTarget target = active_target();
    if (target.window != (HWND)window || (activity && *activity != (LONG)revision))
        return (DictationTarget){0};
    return target;
}

static void assign_dictation_focus(DictationTarget target) {
    if (!same_target(dictation_focus.target, target)) {
        if (dictation_focus.target.window) {
            input_activity();
            lose_focus(&dictation_focus, "focus-lost");
        }
        watch_focus(&dictation_focus, target);
    }
}

static void announce_start(WPARAM window, LPARAM revision, bool raw) {
    DictationTarget target = posted_target(window, revision);
    assign_dictation_focus(target);
    print_target(raw ? "start-raw" : "start", target, true);
}

static void command_target(DWORD request, bool recovery) {
    DictationTarget target = active_target();
    if (active_picker) {
        close_paste_picker();
        if (reset_recovery_focus()) emit("paste-focus-lost");
    }
    if (recovery) {
        recovery_pending = false;
        recovery_window = NULL;
        watch_focus(&recovery_focus, target);
    } else {
        assign_dictation_focus(target);
    }
    char event[32];
    snprintf(event, sizeof(event), "command-focus %lu", (unsigned long)request);
    print_target(event, target, !recovery);
}

static void request_paste_picker(void) {
    recovery_pending = false;
    if (++picker_sequence == 0) ++picker_sequence;
    active_picker = picker_sequence;
    picker_guard = true;
    char event[32];
    snprintf(event, sizeof(event), "paste-picker %lu", (unsigned long)active_picker);
    print_target(event, recovery_focus.target, false);
}

static void poll_gesture(void) {
    if (dictation_focus.target.window || recovery_focus.target.window) {
        DictationTarget current = active_target();
        bool changed = dictation_focus.target.window && !same_target(dictation_focus.target, current);
        bool recovery_changed = recovery_focus.target.window && (!same_target(recovery_focus.target, current) ||
            (picker_guard && recovery_focus.target.focus != current.focus));
        if (changed || recovery_changed) input_activity();
        if (changed) lose_focus(&dictation_focus, "focus-lost");
        if (recovery_changed) lose_recovery_focus();
    }
    bool released = !recovery_key_down && !key_down(VK_MENU) && !key_down(VK_CONTROL);
    if (recovery_pending && recovery_focus.target.window &&
        ((recovery_key_down && recovery_chord() && GetTickCount() - recovery_pressed_at >= PASTE_PICKER_HOLD_MS) ||
         (released && picker_held))) {
        request_paste_picker();
    } else if (recovery_pending && released) {
        recovery_pending = false;
        if (recovery_focus.target.window && recovery_focus.target.process)
            print_target("paste-last", recovery_focus.target, false);
    }
    if (active_picker && picker_choice >= 0 && released && !conflicting_modifiers()) {
        printf("%s %lu %d\n", picker_copy ? "picker-copy" : "picker-paste", (unsigned long)active_picker, picker_choice);
        fflush(stdout);
        active_picker = 0;
        picker_choice = -1;
    }
    if (!held) return;
    // A consumed regular key does not reliably update GetAsyncKeyState.
    // Its hook edges own the gesture, while modifier state can still show
    // that the chord was broken without a key-up reaching the listener.
    bool left_alt = gesture_trigger->key == VK_LMENU;
    DWORD modifiers = left_alt ? 0 : chord_modifiers(0);
    bool right_alt = !left_alt && key_down(VK_RMENU);
    if (gesture_trigger == &triggers[1] && triggers[0].key == VK_LMENU && !(gesture_trigger->modifiers & MOD_ALT))
        modifiers &= ~MOD_ALT;
    if (left_alt ? !key_down(VK_LMENU) : right_alt || modifiers != gesture_trigger->modifiers) {
        // A required modifier can release before its hook message arrives.
        // Match the release path if the chord lost a key without gaining one.
        bool released = left_alt ? !conflicting_modifiers()
            : !right_alt && !(modifiers & ~gesture_trigger->modifiers);
        if (released && recording && !locked && !interrupted)
            release_shortcut(GetTickCount());
        else
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
    while (ReadFile((HANDLE)context, &byte, sizeof(byte), &received, NULL) && received) {
        if (byte == 'r') PostThreadMessageW(monitor_thread, EVENT_RESET, 0, 0);
        else if (byte == 'c' || byte == 'i') InterlockedExchange(&cancel_armed, byte == 'c');
        else if (byte == 'k') {
            PastePickerRows *rows = read_paste_picker((HANDLE)context);
            if (!rows) break;
            free_paste_picker(InterlockedExchangePointer(&pending_picker_rows, rows));
            if (!PostThreadMessageW(monitor_thread, EVENT_PICKER_ROWS, 0, 0)) break;
        } else if (byte == 'f' || byte == 'p') {
            DWORD request, offset = 0;
            while (offset < sizeof(request)) {
                if (!ReadFile((HANDLE)context, (char *)&request + offset, sizeof(request) - offset, &received, NULL)
                    || !received) goto closed;
                offset += received;
            }
            PostThreadMessageW(monitor_thread, EVENT_COMMAND_TARGET, request, byte == 'p');
        }
        else break;
    }
closed:
    PostThreadMessageW(monitor_thread, WM_QUIT, 0, 0);
    return 0;
}

static bool install_input_hooks(HHOOK *keyboard, HHOOK *mouse) {
    HINSTANCE module = GetModuleHandleW(NULL);
    HHOOK new_keyboard = SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_hook, module, 0);
    if (!new_keyboard) return false;
    HHOOK new_mouse = SetWindowsHookExW(WH_MOUSE_LL, mouse_hook, module, 0);
    if (!new_mouse) {
        UnhookWindowsHookEx(new_keyboard);
        return false;
    }
    if (*keyboard) UnhookWindowsHookEx(*keyboard);
    if (*mouse) UnhookWindowsHookEx(*mouse);
    *keyboard = new_keyboard;
    *mouse = new_mouse;
    return true;
}

static bool should_rearm(DWORD now, DWORD session_input) {
    return now - session_input <= WATCHDOG_MS && now - last_hook_event >= HOOK_SILENCE_MS
        && (int32_t)(session_input - last_hook_event) > 0
        && (!last_rearm || now - last_rearm >= rearm_delay);
}

static void check_input_hooks(HHOOK *keyboard, HHOOK *mouse, DWORD now) {
    LASTINPUTINFO session = {.cbSize = sizeof(session)};
    if (!GetLastInputInfo(&session) || !should_rearm(now, session.dwTime)) return;
    last_rearm = now;
    rearm_delay = rearm_delay < MAX_REARM_MS / 2 ? rearm_delay * 2 : MAX_REARM_MS;
    // Input may have moved the caret or changed the focused field while the hooks were silent.
    input_activity();
    lose_focus(&dictation_focus, "focus-lost");
    lose_recovery_focus();
    if (install_input_hooks(keyboard, mouse)) {
        // A key-up can be lost along with a hook. The next down starts a fresh gesture.
        if (!key_down(gesture_trigger->key)) {
            if (held && recording && !locked) end_dictation(EVENT_FINISH);
            held = false;
        }
        for (size_t i = 0; i < sizeof(triggers) / sizeof(*triggers); ++i)
            if (triggers[i].key && !key_down(triggers[i].key)) triggers[i].down = triggers[i].owned = false;
        if (!key_down(VK_ESCAPE)) escape_owned = false;
        if (!key_down('V')) recovery_key_down = false;
        for (size_t key = 0; key < sizeof(picker_keys) / sizeof(*picker_keys); ++key)
            if (picker_keys[key] && !key_down((int)key)) picker_keys[key] = false;
    }
}

static int monitor(void) {
    monitor_thread = GetCurrentThreadId();
    MSG message;
    PeekMessageW(&message, NULL, 0, 0, PM_NOREMOVE);
    HHOOK keyboard = NULL, mouse = NULL;
    install_input_hooks(&keyboard, &mouse);
    DWORD last_watchdog = last_hook_event = GetTickCount();
    HWINEVENTHOOK focus = SetWinEventHook(EVENT_OBJECT_FOCUS, EVENT_OBJECT_FOCUS, NULL, focus_event,
                                         0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
    HWINEVENTHOOK foreground = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, NULL, focus_event,
                                              0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS);
    UINT_PTR timer = SetTimer(NULL, 0, KEY_POLL_MS, NULL);
    HANDLE control = CreateThread(NULL, 0, wait_for_stop, GetStdHandle(STD_INPUT_HANDLE), 0, NULL);
    int result = 1;
    for (size_t i = 0; i < sizeof(triggers) / sizeof(*triggers); ++i)
        if (triggers[i].key && triggers[i].key != VK_LMENU) triggers[i].down = key_down(triggers[i].key);
    if (keyboard && mouse && focus && foreground && timer && control) {
        emit("ready");
        BOOL status;
        while ((status = GetMessageW(&message, NULL, 0, 0)) > 0) {
            if (message.message == WM_TIMER) {
                poll_gesture();
                DWORD now = GetTickCount();
                if (now - last_watchdog >= WATCHDOG_MS) {
                    last_watchdog = now;
                    check_input_hooks(&keyboard, &mouse, now);
                }
            }
            else if (message.message == EVENT_START || message.message == EVENT_START_RAW)
                announce_start(message.wParam, message.lParam, message.message == EVENT_START_RAW);
            else if (message.message == EVENT_COMMAND_TARGET)
                command_target((DWORD)message.wParam, message.lParam != 0);
            else if (message.message == EVENT_FINISH) emit("finish");
            else if (message.message == EVENT_CANCEL) emit("cancel");
            else if (message.message == EVENT_ESCAPE) emit("escape");
            else if (message.message == EVENT_RECOVERY_TARGET) {
                if (recovery_pending && recovery_window && (HWND)message.wParam == recovery_window)
                    watch_focus(&recovery_focus, posted_target(message.wParam, message.lParam));
            }
            else if (message.message == EVENT_PASTE_FOCUS_LOST) {
                if (!active_picker) close_paste_picker();
                emit("paste-focus-lost");
            }
            else if (message.message == EVENT_PICKER_ROWS) {
                PastePickerRows *rows = InterlockedExchangePointer(&pending_picker_rows, NULL);
                if (!rows) continue;
                if (rows->sequence != active_picker || !active_picker) free_paste_picker(rows);
                else if (!rows->count || !show_paste_picker(rows, choose_paste_picker)) {
                    free_paste_picker(rows);
                    lose_recovery_focus();
                }
            }
            else if (message.message == EVENT_PICKER_KEY && active_picker) {
                if (message.wParam == VK_ESCAPE) choose_paste_picker(-1, false);
                else paste_picker_key((WORD)message.wParam, message.lParam != 0);
            }
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
    close_paste_picker();
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

static bool read_trigger(ShortcutTrigger *trigger, const char *key_arg, const char *modifiers_arg) {
    unsigned long long key, flags;
    if (!argument_number(key_arg, &key) || !argument_number(modifiers_arg, &flags) ||
        key < VK_BACK || key > VK_F24 || modifier_key((DWORD)key) ||
        flags & ~(MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN) ||
        (!flags && (key < VK_F1 || key > VK_F24)) ||
        (key == 'V' && flags == (MOD_CONTROL | MOD_ALT))) return false;
    *trigger = (ShortcutTrigger){.key = (WORD)key, .modifiers = (DWORD)flags};
    return true;
}

static int monitor_annotation(WORD key, UINT modifiers) {
    monitor_thread = GetCurrentThreadId();
    MSG message;
    PeekMessageW(&message, NULL, 0, 0, PM_NOREMOVE);
    if (!RegisterHotKey(NULL, 1, modifiers | MOD_NOREPEAT, key)) {
        emit("conflict");
        return 1;
    }
    HANDLE control = CreateThread(NULL, 0, wait_for_stop, GetStdHandle(STD_INPUT_HANDLE), 0, NULL);
    UINT_PTR timer = 0;
    int result = 1;
    if (control) {
        emit("ready");
        BOOL status;
        while ((status = GetMessageW(&message, NULL, 0, 0)) > 0) {
            if (message.message == WM_HOTKEY && message.wParam == 1 && !timer) {
                emit("down");
                if (key_down(key)) timer = SetTimer(NULL, 0, KEY_POLL_MS, NULL);
                if (!timer) emit(key_down(key) ? "cancel" : "up");
            } else if (message.message == WM_TIMER && timer && !key_down(key)) {
                KillTimer(NULL, timer);
                timer = 0;
                emit("up");
            }
        }
        result = status < 0;
        CancelSynchronousIo(control);
        CloseHandle(control);
    }
    if (timer) KillTimer(NULL, timer);
    UnregisterHotKey(NULL, 1);
    return result;
}

bool dictation_activity_name(const char *token, char name[64]) {
    if (strlen(token) != 32 || strspn(token, "0123456789abcdef") != 32) return false;
    snprintf(name, 64, "Local\\JaroDictation-%s", token);
    return true;
}

int main(int argc, char **argv) {
    if (argc == 4 && !strcmp(argv[1], "annotate")) {
        unsigned long long key, flags;
        if (!argument_number(argv[2], &key) || !argument_number(argv[3], &flags) ||
            key < VK_BACK || key > VK_F24 || modifier_key((DWORD)key) || key == VK_F12 ||
            flags & ~(MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_WIN) ||
            (!flags && (key < VK_F1 || key > VK_F24)) ||
            (key == 'V' && flags == (MOD_CONTROL | MOD_ALT))) return 2;
        return monitor_annotation((WORD)key, (UINT)flags);
    }
    if (argc >= 2 && !strcmp(argv[1], "listen")) {
        if (argc == 2 || !strcmp(argv[2], "hold")) mode = HOLD;
        else if (!strcmp(argv[2], "tap")) mode = TAP;
        else if (!strcmp(argv[2], "holdDouble")) mode = HOLD_DOUBLE;
        else if (!strcmp(argv[2], "doubleTap")) mode = DOUBLE_TAP;
        else return 2;
        if (argc < 4) return monitor();
        int next = 4;
        if (next < argc && strcmp(argv[next], "--raw")) {
            if (argc < next + 2 || !read_trigger(&triggers[0], argv[next], argv[next + 1])) return 2;
            next += 2;
        }
        if (next < argc) {
            if (argc != next + 3 || strcmp(argv[next], "--raw") ||
                !read_trigger(&triggers[1], argv[next + 1], argv[next + 2]) ||
                (triggers[0].key == triggers[1].key && triggers[0].modifiers == triggers[1].modifiers))
                return 2;
        }
        char name[64];
        if (!dictation_activity_name(argv[3], name)) return 2;
        HANDLE mapping = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, sizeof(LONG), name);
        if (!mapping) return 2;
        if (GetLastError() != ERROR_ALREADY_EXISTS) activity = MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, sizeof(LONG));
        int result = activity ? monitor() : 2;
        if (activity) UnmapViewOfFile((const void *)activity);
        CloseHandle(mapping);
        return result;
    }
    if (argc == 2 && !strcmp(argv[1], "paste-server")) return serve_dictation_pastes();
    return 2;
}
