#undef NDEBUG
#include <assert.h>
#include <wchar.h>
#include "../windows_dictation.h"

static DictationTarget frontend;
static bool keys[256];
static unsigned focus_queries, hook_installs, hook_removals;
static DWORD session_input;
static bool fail_mouse_hook;

static HWND WINAPI fixture_foreground(void) {
    return frontend.window;
}

static DWORD WINAPI fixture_window_thread(HWND window, DWORD *process) {
    assert(window == frontend.window);
    if (process) *process = frontend.process;
    return 1;
}

static BOOL WINAPI fixture_thread_info(DWORD thread, GUITHREADINFO *info) {
    assert(thread == 1);
    ++focus_queries;
    info->hwndFocus = frontend.focus;
    return TRUE;
}

static SHORT WINAPI fixture_key_state(int key) {
    return keys[key] ? (SHORT)0x8000 : 0;
}

static BOOL WINAPI fixture_last_input(LASTINPUTINFO *info) {
    info->dwTime = session_input;
    return TRUE;
}

static HHOOK WINAPI fixture_hook(int type, HOOKPROC callback, HINSTANCE module, DWORD thread) {
    (void)callback; (void)module; (void)thread;
    ++hook_installs;
    if (fail_mouse_hook && type == WH_MOUSE_LL) return NULL;
    return (HHOOK)(uintptr_t)(100 + hook_installs);
}

static BOOL WINAPI fixture_unhook(HHOOK hook) {
    assert(hook);
    ++hook_removals;
    return TRUE;
}

#define main dictation_main
#define GetForegroundWindow fixture_foreground
#define GetWindowThreadProcessId fixture_window_thread
#define GetGUIThreadInfo fixture_thread_info
#define GetAsyncKeyState fixture_key_state
#define GetLastInputInfo fixture_last_input
#define SetWindowsHookExW fixture_hook
#define UnhookWindowsHookEx fixture_unhook
#include "../windows_hotkeys.c"
#undef main
#undef GetForegroundWindow
#undef GetWindowThreadProcessId
#undef GetGUIThreadInfo
#undef GetAsyncKeyState
#undef GetLastInputInfo
#undef SetWindowsHookExW
#undef UnhookWindowsHookEx

int main(void) {
    HWND window = (HWND)(uintptr_t)10, focus = (HWND)(uintptr_t)20;
    frontend = (DictationTarget){window, focus, 30};
    dictation_focus = recovery_focus = (FocusWatch){.target = frontend, .since = 100};
    volatile LONG revision = 0;
    activity = &revision;
    monitor_thread = GetCurrentThreadId();
    MSG message;
    PeekMessageW(&message, NULL, 0, 0, PM_NOREMOVE);

    focus_event(NULL, EVENT_OBJECT_FOCUS, window, OBJID_CLIENT, 6, 1, 101);
    assert(revision == 1 && same_target(dictation_focus.target, frontend));
    focus_event(NULL, EVENT_OBJECT_FOCUS, window, OBJID_CLIENT, 6, 1, 102);
    assert(revision == 1 && same_target(recovery_focus.target, frontend));
    focus_event(NULL, EVENT_OBJECT_FOCUS, window, OBJID_CLIENT, 7, 1, 103);
    assert(revision == 2 && same_target(dictation_focus.target, frontend));
    assert(same_target(recovery_focus.target, frontend));

    recovery_pending = true;
    frontend.focus = (HWND)(uintptr_t)21;
    focus_event(NULL, EVENT_OBJECT_FOCUS, frontend.focus, OBJID_CLIENT, 0, 1, 99);
    assert(revision == 2 && dictation_focus.target.focus == focus);
    assert(recovery_pending);
    focus_event(NULL, EVENT_OBJECT_FOCUS, frontend.focus, OBJID_CLIENT, 0, 1, 104);
    assert(revision == 3 && !dictation_focus.target.window && !recovery_focus.target.window);
    assert(!recovery_pending);

    dictation_focus = (FocusWatch){.target = frontend, .since = 200};
    frontend.window = (HWND)(uintptr_t)11;
    focus_event(NULL, EVENT_SYSTEM_FOREGROUND, frontend.window, OBJID_WINDOW, 0, 1, 201);
    assert(revision == 4 && !dictation_focus.target.window);
    puts("Windows logical focus resets continuation and native focus changes invalidate paste.");

    triggers[0] = (ShortcutTrigger){.key = VK_F9, .modifiers = MOD_CONTROL};
    held = recording = true;
    poll_gesture();
    assert(!recording && !held);
    assert(PeekMessageW(&message, NULL, EVENT_FINISH, EVENT_CANCEL, PM_REMOVE) && message.message == EVENT_FINISH);

    keys[VK_LCONTROL] = keys[VK_LSHIFT] = true;
    held = recording = true;
    poll_gesture();
    assert(!recording && !held);
    assert(PeekMessageW(&message, NULL, EVENT_FINISH, EVENT_CANCEL, PM_REMOVE) && message.message == EVENT_CANCEL);
    puts("The Windows gesture poll finishes on modifier release and cancels on a conflicting modifier.");

    focus_queries = 0;
    start_dictation(true);
    assert(!focus_queries);
    assert(PeekMessageW(&message, NULL, EVENT_START, EVENT_START, PM_REMOVE));
    assert(same_target(posted_target(message.wParam, message.lParam), frontend) && focus_queries == 1);
    ++revision;
    assert(!posted_target(message.wParam, message.lParam).window);
    puts("Shortcut callbacks defer focus inspection and reject a stale target.");

    keys[VK_LCONTROL] = keys[VK_LSHIFT] = false;
    recording = locked = false;
    cancel_armed = true;
    KBDLLHOOKSTRUCT escape = {.vkCode = VK_ESCAPE};
    assert(keyboard_hook(HC_ACTION, WM_KEYDOWN, (LPARAM)&escape) == 1);
    cancel_armed = false;
    assert(keyboard_hook(HC_ACTION, WM_KEYDOWN, (LPARAM)&escape) == 1);
    assert(keyboard_hook(HC_ACTION, WM_KEYUP, (LPARAM)&escape) == 1);
    assert(PeekMessageW(&message, NULL, EVENT_ESCAPE, EVENT_ESCAPE, PM_REMOVE));
    assert(!PeekMessageW(&message, NULL, EVENT_ESCAPE, EVENT_ESCAPE, PM_REMOVE));
    keyboard_hook(HC_ACTION, WM_KEYDOWN, (LPARAM)&escape);
    assert(!escape_owned && !PeekMessageW(&message, NULL, EVENT_ESCAPE, EVENT_ESCAPE, PM_REMOVE));
    puts("Escape emits one cancellation after recording and releases the key cleanly.");

    HHOOK keyboard = (HHOOK)(uintptr_t)20, mouse = (HHOOK)(uintptr_t)21;
    last_hook_event = 1000;
    session_input = 1100;
    check_input_hooks(&keyboard, &mouse, 5000);
    assert(!hook_installs);
    session_input = 4800;
    triggers[0].down = triggers[0].owned = held = recording = true;
    dictation_focus.target = recovery_focus.target = frontend;
    LONG before_rearm = revision;
    check_input_hooks(&keyboard, &mouse, 5000);
    assert(hook_installs == 2 && hook_removals == 2 && keyboard != (HHOOK)(uintptr_t)20);
    assert(!triggers[0].down && !triggers[0].owned && !held && !recording);
    assert(revision == before_rearm + 1 && !dictation_focus.target.window && !recovery_focus.target.window);
    assert(PeekMessageW(&message, NULL, EVENT_FINISH, EVENT_FINISH, PM_REMOVE));
    session_input = 5300;
    check_input_hooks(&keyboard, &mouse, 5400);
    assert(hook_installs == 2);
    fail_mouse_hook = true;
    HHOOK old_keyboard = keyboard, old_mouse = mouse;
    session_input = 11200;
    check_input_hooks(&keyboard, &mouse, 11500);
    assert(hook_installs == 4 && hook_removals == 3 && keyboard == old_keyboard && mouse == old_mouse);
    last_hook_event = UINT32_MAX - 2500;
    last_rearm = 0;
    assert(should_rearm(600, 570));
    puts("The watchdog backs off, replaces both hooks, and preserves the working pair when replacement fails.");

    triggers[0] = (ShortcutTrigger){.key = VK_F9};
    triggers[1] = (ShortcutTrigger){.key = VK_F8};
    KBDLLHOOKSTRUCT normal = {.vkCode = VK_F9}, raw = {.vkCode = VK_F8};
    mode = DOUBLE_TAP;
    assert(custom_chord(&normal, true) && custom_chord(&normal, false));
    assert(custom_chord(&raw, true) && custom_chord(&raw, false));
    assert(!recording);
    assert(custom_chord(&raw, true) && custom_chord(&raw, false));
    assert(recording && locked && gesture_trigger == &triggers[1]);
    assert(PeekMessageW(&message, NULL, EVENT_START_RAW, EVENT_START_RAW, PM_REMOVE));
    assert(custom_chord(&normal, true) && custom_chord(&normal, false));
    assert(recording && locked && gesture_trigger == &triggers[1]);
    assert(!PeekMessageW(&message, NULL, EVENT_FINISH, EVENT_FINISH, PM_REMOVE));
    assert(custom_chord(&raw, true) && custom_chord(&raw, false));
    assert(!recording && PeekMessageW(&message, NULL, EVENT_FINISH, EVENT_FINISH, PM_REMOVE));
    puts("The raw key owns its capture and counts only its own taps.");

    triggers[0] = (ShortcutTrigger){.key = VK_LMENU};
    triggers[1] = (ShortcutTrigger){.key = 'R', .modifiers = MOD_ALT};
    raw.vkCode = 'R';
    mode = TAP;
    gesture_trigger = &triggers[0];
    recording = locked = held = true;
    keys[VK_LMENU] = true;
    assert(custom_chord(&raw, true) && custom_chord(&raw, false));
    assert(recording && locked && !held && gesture_trigger == &triggers[0]);
    assert(!PeekMessageW(&message, NULL, EVENT_FINISH, EVENT_FINISH, PM_REMOVE));
    puts("An Alt chord leaves a locked Left Alt take recording.");

    triggers[1] = (ShortcutTrigger){.key = VK_F8};
    gesture_trigger = &triggers[1];
    recording = held = true;
    locked = false;
    poll_gesture();
    assert(recording && held);
    puts("A Left Alt tap leaves a held raw take recording.");

    held = recording = false;
    keys[VK_CONTROL] = keys[VK_MENU] = true;
    recovery_key_down = recovery_pending = true;
    recovery_pressed_at = GetTickCount() - PASTE_PICKER_HOLD_MS;
    recovery_focus.target = frontend;
    poll_gesture();
    assert(active_picker && picker_guard && !recovery_pending);
    DWORD sequence = active_picker;
    HANDLE reader, writer;
    assert(CreatePipe(&reader, &writer, NULL, 0));
    const char first[] = "Latest", second[] = "Older transcript.\r\nSecond line.";
    DWORD fields[] = {sequence, 2, sizeof(first) - 1, sizeof(second) - 1}, written = 0;
    assert(WriteFile(writer, fields, sizeof(fields), &written, NULL) && written == sizeof(fields));
    assert(WriteFile(writer, first, fields[2], &written, NULL) && written == fields[2]);
    assert(WriteFile(writer, second, fields[3], &written, NULL) && written == fields[3]);
    CloseHandle(writer);
    PastePickerRows *rows = read_paste_picker(reader);
    CloseHandle(reader);
    assert(rows && rows->sequence == sequence && rows->count == 2);
    HWND actual_foreground = GetForegroundWindow();
    assert(show_paste_picker(rows, choose_paste_picker));
    HWND picker = FindWindowW(L"JaroPastePicker", NULL);
    HWND edit = FindWindowExW(picker, NULL, L"EDIT", NULL);
    assert(picker && edit && (GetWindowLongPtrW(picker, GWL_EXSTYLE) & WS_EX_NOACTIVATE));
    assert(GetForegroundWindow() == actual_foreground);

    recovery_key_down = keys[VK_CONTROL] = keys[VK_LMENU] = keys[VK_MENU] = false;
    assert(picker_keyboard(VK_DOWN, true) && picker_keyboard(VK_DOWN, false));
    assert(PeekMessageW(&message, NULL, EVENT_PICKER_KEY, EVENT_PICKER_KEY, PM_REMOVE));
    paste_picker_key((WORD)message.wParam, message.lParam != 0);
    WCHAR transcript[80];
    assert(GetWindowTextW(edit, transcript, 80) && !wcscmp(transcript, L"Older transcript.\r\nSecond line."));
    paste_picker_key(VK_RETURN, false);
    assert(picker_choice == 1 && active_picker == sequence);
    assert(!IsWindow(picker));
    poll_gesture();
    assert(!active_picker && picker_guard && picker_choice == -1);
    focus_event(NULL, EVENT_OBJECT_FOCUS, frontend.focus, OBJID_CLIENT, 1, 1, GetTickCount() + 1);
    assert(!picker_guard && !recovery_focus.target.window);
    puts("Hold opens a nonactivating Paste Last preview and a logical focus change invalidates its target.");
    return 0;
}
