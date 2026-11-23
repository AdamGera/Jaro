#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <uiautomationclient.h>
#include <stdio.h>
#include <wchar.h>

enum { WINDOWS_PER_BROWSER = 6, MAX_BROWSERS = 48, MAX_URL = 4096 };

typedef struct {
    HWND windows[WINDOWS_PER_BROWSER];
    int count;
} Browser;

typedef struct {
    wchar_t **binaries;
    int count;
    DWORD session;
    Browser browsers[MAX_BROWSERS];
} Search;

static BOOL CALLBACK collect(HWND window, LPARAM data) {
    Search *search = (Search *)data;
    if (!IsWindowVisible(window) || GetWindow(window, GW_OWNER) ||
        (GetWindowLongPtrW(window, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) || !GetWindowTextLengthW(window)) return TRUE;
    DWORD pid = 0, session;
    if (!GetWindowThreadProcessId(window, &pid) || !ProcessIdToSessionId(pid, &session) ||
        session != search->session) return TRUE;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return TRUE;
    wchar_t file[MAX_URL];
    DWORD length = ARRAYSIZE(file);
    BOOL found = QueryFullProcessImageNameW(process, 0, file, &length);
    CloseHandle(process);
    if (!found) return TRUE;
    const wchar_t *separator = wcsrchr(file, L'\\');
    const wchar_t *binary = separator ? separator + 1 : file;
    for (int i = 0; i < search->count; i++) {
        Browser *browser = &search->browsers[i];
        if (!_wcsicmp(search->binaries[i], binary) && browser->count < WINDOWS_PER_BROWSER)
            browser->windows[browser->count++] = window;
    }
    return TRUE;
}

static void emit(char kind, int browser, int window, const wchar_t *value) {
    char buffer[MAX_URL * 4 + 1];
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value, -1, buffer, sizeof(buffer), NULL, NULL)) return;
    for (char *at = buffer; *at; at++) {
        if ((unsigned char)*at >= 32) continue;
        if (kind == 'U') return;
        *at = ' ';
    }
    printf("%c\t%d\t%d\t%s\n", kind, browser, window, buffer);
    fflush(stdout); // Keep earlier windows' results if a later provider hangs.
}

static void read_document(IUIAutomation *automation, IUIAutomationCondition *condition, HWND window, int browser, int index) {
    IUIAutomationElement *root = NULL, *document = NULL;
    if (FAILED(IUIAutomation_ElementFromHandle(automation, (UIA_HWND)window, &root)) || !root) return;
    HRESULT result = IUIAutomationElement_FindFirst(root, TreeScope_Descendants, condition, &document);
    IUIAutomationElement_Release(root);
    if (FAILED(result) || !document) return;
    IUIAutomationValuePattern *pattern = NULL;
    result = IUIAutomationElement_GetCurrentPatternAs(
        document, UIA_ValuePatternId, &IID_IUIAutomationValuePattern, (void **)&pattern);
    IUIAutomationElement_Release(document);
    if (FAILED(result) || !pattern) return;
    BSTR address = NULL;
    result = IUIAutomationValuePattern_get_CurrentValue(pattern, &address);
    IUIAutomationValuePattern_Release(pattern);
    if (SUCCEEDED(result) && address && SysStringLen(address) <= MAX_URL && wcslen(address) == SysStringLen(address) &&
        (!wcsncmp(address, L"https://", 8) || !wcsncmp(address, L"http://", 7))) emit('U', browser, index, address);
    SysFreeString(address);
}

int jaro_browser_pages(int count, wchar_t **binaries) {
    Search search = {.binaries = binaries, .count = count};
    if (count < 1 || count > MAX_BROWSERS || !ProcessIdToSessionId(GetCurrentProcessId(), &search.session)) return 2;
    if (!EnumWindows(collect, (LPARAM)&search)) return 2;
    BOOL any = FALSE;
    for (int i = 0; i < count; i++) {
        Browser *browser = &search.browsers[i];
        for (int index = 0; index < browser->count; index++) {
            wchar_t title[512];
            if (GetWindowTextW(browser->windows[index], title, ARRAYSIZE(title))) emit('T', i, index, title);
            any = TRUE;
        }
    }
    if (!any) return 0;
    if (FAILED(CoInitializeEx(NULL, COINIT_MULTITHREADED))) return 2;
    IUIAutomation *automation = NULL;
    HRESULT result = CoCreateInstance(&CLSID_CUIAutomation, NULL, CLSCTX_INPROC_SERVER,
                                      &IID_IUIAutomation, (void **)&automation);
    if (SUCCEEDED(result)) {
        VARIANT type = {.vt = VT_I4, .lVal = UIA_DocumentControlTypeId};
        IUIAutomationCondition *condition = NULL;
        result = IUIAutomation_CreatePropertyCondition(automation, UIA_ControlTypePropertyId, type, &condition);
        if (SUCCEEDED(result)) {
            for (int i = 0; i < count; i++) {
                Browser *browser = &search.browsers[i];
                for (int index = 0; index < browser->count; index++)
                    read_document(automation, condition, browser->windows[index], i, index);
            }
            IUIAutomationCondition_Release(condition);
            puts("done");
        }
        IUIAutomation_Release(automation);
    }
    CoUninitialize();
    return result == S_OK ? 0 : 2;
}
