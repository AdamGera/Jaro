#define COBJMACROS
#include <windows.h>
#include <shlobj.h>
#include <stdio.h>
#include <wchar.h>

static BOOL join(wchar_t *output, const wchar_t *folder, const wchar_t *name)
{
    int length = swprintf(output, MAX_PATH, L"%ls\\%ls", folder, name);
    return length > 0 && length < MAX_PATH;
}

static BOOL versioned_host(const wchar_t *path, const wchar_t *versions)
{
    size_t prefix = wcslen(versions);
    if (_wcsnicmp(path, versions, prefix) || path[prefix] != L'\\')
        return FALSE;
    const wchar_t *version = path + prefix + 1;
    const wchar_t *cursor = version;
    for (int part = 0; part < 3; part++) {
        size_t digits = wcsspn(cursor, L"0123456789");
        if (digits < 1 || digits > 5)
            return FALSE;
        cursor += digits;
        if (part < 2 && *cursor++ != L'.')
            return FALSE;
    }
    return _wcsicmp(cursor, L"\\bin\\jaro.exe") == 0;
}

static BOOL expanded_path(const wchar_t *path, wchar_t *expanded)
{
    DWORD length = ExpandEnvironmentStringsW(path, expanded, MAX_PATH);
    return length > 0 && length <= MAX_PATH;
}

static void save_link(IPersistFile *file, const wchar_t *folder, const wchar_t *path)
{
    wchar_t temporary[MAX_PATH];
    if (!GetTempFileNameW(folder, L"jro", 0, temporary))
        return;
    if (SUCCEEDED(IPersistFile_Save(file, temporary, FALSE)))
        MoveFileExW(temporary, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    DeleteFileW(temporary);
}

static void repair_link(const wchar_t *folder, const wchar_t *path, const wchar_t *versions,
                        const wchar_t *launcher, const wchar_t *bin, const wchar_t *icon)
{
    IShellLinkW *link = NULL;
    if (FAILED(CoCreateInstance(&CLSID_ShellLink, NULL, CLSCTX_INPROC_SERVER, &IID_IShellLinkW, (void **)&link)))
        return;
    IPersistFile *file = NULL;
    if (FAILED(IShellLinkW_QueryInterface(link, &IID_IPersistFile, (void **)&file))) {
        IShellLinkW_Release(link);
        return;
    }
    wchar_t value[MAX_PATH], target[MAX_PATH];
    // Read the saved target without resolving or starting the linked application.
    if (FAILED(IPersistFile_Load(file, path, STGM_READ)) ||
        IShellLinkW_GetPath(link, value, MAX_PATH, NULL, SLGP_RAWPATH) != S_OK ||
        !expanded_path(value, target) || !versioned_host(target, versions))
        goto done;

    wchar_t working[MAX_PATH], old_folder[MAX_PATH];
    wcscpy(old_folder, target);
    *wcsrchr(old_folder, L'\\') = L'\0';
    if (SUCCEEDED(IShellLinkW_GetWorkingDirectory(link, value, MAX_PATH)) &&
        expanded_path(value, working)) {
        BOOL old_working_directory = _wcsicmp(working, old_folder) == 0;
        *wcsrchr(old_folder, L'\\') = L'\0';
        if ((old_working_directory || !_wcsicmp(working, old_folder)) &&
            FAILED(IShellLinkW_SetWorkingDirectory(link, bin)))
            goto done;
    }

    int index;
    if (SUCCEEDED(IShellLinkW_GetIconLocation(link, value, MAX_PATH, &index)) &&
        expanded_path(value, working) && versioned_host(working, versions)) {
        if (FAILED(IShellLinkW_SetIconLocation(link, icon, 0)))
            goto done;
    }
    if (SUCCEEDED(IShellLinkW_SetPath(link, launcher)))
        save_link(file, folder, path);

done:
    IPersistFile_Release(file);
    IShellLinkW_Release(link);
}

static void repair_folder(const wchar_t *folder, const wchar_t *versions, const wchar_t *launcher,
                          const wchar_t *bin, const wchar_t *icon)
{
    if (!folder[0] || folder[1] != L':' || folder[2] != L'\\')
        return;
    wchar_t drive[] = {folder[0], L':', L'\\', L'\0'};
    // A redirected Desktop may live on an unavailable network share.
    if (GetDriveTypeW(drive) != DRIVE_FIXED)
        return;
    wchar_t pattern[MAX_PATH], path[MAX_PATH];
    if (!join(pattern, folder, L"*.lnk"))
        return;
    WIN32_FIND_DATAW entry;
    HANDLE files = FindFirstFileW(pattern, &entry);
    if (files == INVALID_HANDLE_VALUE)
        return;
    do {
        DWORD ignored = FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_OFFLINE;
        if (!(entry.dwFileAttributes & ignored) && join(path, folder, entry.cFileName))
            repair_link(folder, path, versions, launcher, bin, icon);
    } while (FindNextFileW(files, &entry));
    FindClose(files);
}

void repair_versioned_shortcuts(const wchar_t *install)
{
    wchar_t launcher[MAX_PATH], versions[MAX_PATH], bin[MAX_PATH], icon[MAX_PATH];
    if (!join(versions, install, L"versions") || !join(bin, install, L"bin") ||
        !join(launcher, bin, L"Jaro.exe") || !join(icon, install, L"Jaro.ico"))
        return;
    if (FAILED(CoInitializeEx(NULL, COINIT_APARTMENTTHREADED)))
        return;
    wchar_t folder[MAX_PATH], appdata[MAX_PATH];
    if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_PROGRAMS, NULL, SHGFP_TYPE_CURRENT, folder)))
        repair_folder(folder, versions, launcher, bin, icon);
    if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_APPDATA, NULL, SHGFP_TYPE_CURRENT, appdata)) &&
        join(folder, appdata, L"Microsoft\\Internet Explorer\\Quick Launch\\User Pinned\\TaskBar"))
        repair_folder(folder, versions, launcher, bin, icon);
    if (SUCCEEDED(SHGetFolderPathW(NULL, CSIDL_DESKTOPDIRECTORY, NULL, SHGFP_TYPE_CURRENT, folder)))
        repair_folder(folder, versions, launcher, bin, icon);
    CoUninitialize();
}
