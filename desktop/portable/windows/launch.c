#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <wchar.h>

#define PATH_CAPACITY 32768

#ifndef JARO_CONSOLE
static BOOL background_launch;
#endif

static int fail(const wchar_t *message)
{
#ifdef JARO_CONSOLE
    fputws(message, stderr);
    fputws(L"\n", stderr);
#else
    if (!background_launch)
        MessageBoxW(NULL, message, L"Jaro", MB_OK | MB_ICONERROR);
#endif
    return 1;
}

static HANDLE application_lock(void)
{
    wchar_t folder[MAX_PATH];
    wchar_t path[MAX_PATH];
    if (FAILED(SHGetFolderPathW(NULL, CSIDL_LOCAL_APPDATA, NULL, SHGFP_TYPE_CURRENT, folder)))
        return INVALID_HANDLE_VALUE;
    int length = swprintf(path, MAX_PATH, L"%ls\\Jaro", folder);
    if (length < 0 || length >= MAX_PATH)
        return INVALID_HANDLE_VALUE;
    if (!CreateDirectoryW(path, NULL) && GetLastError() != ERROR_ALREADY_EXISTS)
        return INVALID_HANDLE_VALUE;
    length = swprintf(path, MAX_PATH, L"%ls\\Jaro\\.installer-lock", folder);
    if (length < 0 || length >= MAX_PATH)
        return INVALID_HANDLE_VALUE;
    return CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

static BOOL active_version(const wchar_t *root, wchar_t *version)
{
    wchar_t path[PATH_CAPACITY];
    int length = swprintf(path, PATH_CAPACITY, L"%ls\\current-version", root);
    if (length < 0 || length >= PATH_CAPACITY)
        return FALSE;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return FALSE;
    char data[48];
    DWORD size;
    BOOL read = ReadFile(file, data, sizeof(data), &size, NULL);
    CloseHandle(file);
    if (!read || !size || size == sizeof(data))
        return FALSE;
    while (size && (data[size - 1] == '\n' || data[size - 1] == '\r'))
        size--;
    unsigned dots = 0;
    BOOL digits = FALSE;
    for (DWORD index = 0; index < size; index++) {
        char ch = data[index];
        if (ch >= '0' && ch <= '9') {
            digits = TRUE;
        } else if (ch == '.' && digits && dots < 2) {
            dots++;
            digits = FALSE;
        } else {
            return FALSE;
        }
        version[index] = ch;
    }
    version[size] = L'\0';
    return digits && dots == 2;
}

static const wchar_t *argument_tail(void)
{
    const wchar_t *text = GetCommandLineW();
    if (*text == L'"') {
        text++;
        while (*text && *text != L'"')
            text++;
        if (*text)
            text++;
    } else {
        while (*text && *text != L' ' && *text != L'\t')
            text++;
    }
    return text;
}

#ifndef JARO_CONSOLE
static BOOL webview_installed(const wchar_t *payload)
{
    wchar_t path[PATH_CAPACITY];
    int length = swprintf(path, PATH_CAPACITY, L"%ls\\bin\\WebView2Loader.dll", payload);
    if (length < 0 || length >= PATH_CAPACITY)
        return FALSE;
    HMODULE loader = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!loader)
        return FALSE;
    typedef HRESULT (WINAPI *CheckVersion)(PCWSTR, LPWSTR *);
    CheckVersion check = (CheckVersion)GetProcAddress(loader, "GetAvailableCoreWebView2BrowserVersionString");
    LPWSTR version = NULL;
    BOOL available = check && SUCCEEDED(check(NULL, &version)) && version && *version;
    CoTaskMemFree(version);
    FreeLibrary(loader);
    return available;
}

static BOOL visual_cpp_installed(void)
{
    HMODULE threads = LoadLibraryExW(L"vcruntime140_threads.dll", NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    HMODULE atomic = LoadLibraryExW(L"msvcp140_atomic_wait.dll", NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (threads)
        FreeLibrary(threads);
    if (atomic)
        FreeLibrary(atomic);
    return threads != NULL && atomic != NULL;
}
#endif

static int run_version(const wchar_t *payload)
{
    wchar_t python[PATH_CAPACITY];
    wchar_t command[PATH_CAPACITY];
    int length = swprintf(python, PATH_CAPACITY, L"%ls\\python\\python.exe", payload);
    if (length < 0 || length >= PATH_CAPACITY)
        return fail(L"The Jaro installation path is too long.");
#ifdef JARO_CONSOLE
    length = swprintf(command, PATH_CAPACITY, L"\"%ls\" -B -X utf8 \"%ls\\engine\\jardctl.py\"%ls",
                      python, payload, argument_tail());
    DWORD flags = 0;
#else
    if (!webview_installed(payload))
        return fail(L"Install or repair the Microsoft Edge WebView2 Runtime, then reopen Jaro.");
    if (!visual_cpp_installed())
        return fail(L"Install the latest Microsoft Visual C++ x64 Redistributable, then reopen Jaro.");
    length = swprintf(command, PATH_CAPACITY, L"\"%ls\" -B -X utf8 \"%ls\\desktop\\portable\\open_workspace.py\" --installed \"%ls\\bin\\jaro.exe\"%ls",
                      python, payload, payload, argument_tail());
    DWORD flags = CREATE_NO_WINDOW;
#endif
    if (length < 0 || length >= PATH_CAPACITY)
        return fail(L"The Jaro command is too long.");
    STARTUPINFOW startup = {.cb = sizeof(startup)};
    PROCESS_INFORMATION child = {0};
    if (!CreateProcessW(python, command, NULL, NULL, TRUE, flags, NULL, NULL, &startup, &child))
        return fail(L"Jaro could not start its local Python runtime. Repair the installation and try again.");
    CloseHandle(child.hThread);
    DWORD result = 1;
    if (WaitForSingleObject(child.hProcess, INFINITE) == WAIT_OBJECT_0)
        GetExitCodeProcess(child.hProcess, &result);
    CloseHandle(child.hProcess);
#ifndef JARO_CONSOLE
    if (result != 0)
        return fail(L"Jaro could not open its workspace. Close any open Jaro windows and try again.");
#endif
    return (int)result;
}

static int launch(void)
{
    wchar_t root[PATH_CAPACITY];
    DWORD length = GetModuleFileNameW(NULL, root, PATH_CAPACITY);
    if (!length || length == PATH_CAPACITY)
        return fail(L"Jaro could not locate its installation.");
    wchar_t *separator = wcsrchr(root, L'\\');
    if (separator)
        *separator = L'\0';
    separator = wcsrchr(root, L'\\');
    if (!separator)
        return fail(L"Jaro could not locate its installation.");
    *separator = L'\0';
    HANDLE lock = application_lock();
    if (lock == INVALID_HANDLE_VALUE)
        return fail(L"Jaro could not open its installation lock. If Setup is running, try again after it finishes.");
    wchar_t version[48];
    wchar_t payload[PATH_CAPACITY];
    int size = active_version(root, version) ?
        swprintf(payload, PATH_CAPACITY, L"%ls\\versions\\%ls", root, version) : -1;
    int result = size >= 0 && size < PATH_CAPACITY ? run_version(payload) :
        fail(L"Jaro could not read the installed version. Run Setup to repair the installation.");
    CloseHandle(lock);
    return result;
}

#ifdef JARO_CONSOLE
int wmain(void)
{
    return launch();
}
#else
int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, LPWSTR command, int show)
{
    (void)instance;
    (void)previous;
    (void)show;
    background_launch = wcscmp(command, L"--background") == 0;
    return launch();
}
#endif
