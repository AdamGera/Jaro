#ifdef _WIN32
#define COBJMACROS
#include <windows.h>
#include <shobjidl.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

int wmain(int argc, wchar_t **argv) {
    if (argc < 2 || argc > 3) return 1;
    const BOOL storage = wcscmp(argv[1], L"--choose-annotation-storage-folder") == 0;
    if (argc == 3 && !storage) return 1;
    HRESULT status = CoInitializeEx(NULL, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    if (FAILED(status)) return 1;
    const BOOL folder = storage || wcscmp(argv[1], L"--choose-annotation-export-folder") == 0;
    const BOOL open = folder || wcscmp(argv[1], L"--choose-application") == 0;
    IFileDialog *dialog = NULL;
    IShellItem *item = NULL;
    PWSTR path = NULL;
    const COMDLG_FILTERSPEC filter = {open ? L"Applications" : L"MP4 video", open ? L"*.exe" : L"*.mp4"};
    status = CoCreateInstance(open ? &CLSID_FileOpenDialog : &CLSID_FileSaveDialog, NULL, CLSCTX_INPROC_SERVER,
                              &IID_IFileDialog, (void **)&dialog);
    if (SUCCEEDED(status)) status = IFileDialog_SetTitle(dialog, storage ? L"Choose an Annotations folder" :
                                                                        folder ? L"Choose an annotation export folder" :
                                                                        open ? L"Choose an application" : L"Save video");
    if (!folder && SUCCEEDED(status)) status = IFileDialog_SetFileTypes(dialog, 1, &filter);
    if (!open && SUCCEEDED(status)) status = IFileDialog_SetDefaultExtension(dialog, L"mp4");
    if (!open && SUCCEEDED(status)) status = IFileDialog_SetFileName(dialog, argv[1]);
    if (SUCCEEDED(status)) status = IFileDialog_SetOptions(dialog, FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR |
                                     (folder ? FOS_PICKFOLDERS : open ? FOS_FILEMUSTEXIST : FOS_OVERWRITEPROMPT));
    if (argc == 3 && SUCCEEDED(status)) {
        IShellItem *initial = NULL;
        if (SUCCEEDED(SHCreateItemFromParsingName(argv[2], NULL, &IID_IShellItem, (void **)&initial))) {
            IFileDialog_SetFolder(dialog, initial);
            IShellItem_Release(initial);
        }
    }
    if (SUCCEEDED(status)) status = IFileDialog_Show(dialog, NULL);
    if (SUCCEEDED(status)) status = IFileDialog_GetResult(dialog, &item);
    if (SUCCEEDED(status)) status = IShellItem_GetDisplayName(item, SIGDN_FILESYSPATH, &path);
    int result = status == HRESULT_FROM_WIN32(ERROR_CANCELLED) ? 0 : 1;
    if (SUCCEEDED(status)) {
        int length = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, path, -1, NULL, 0, NULL, NULL);
        char *text = length > 0 ? malloc((size_t)length) : NULL;
        if (text && WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, path, -1, text, length, NULL, NULL)) {
            result = fputs(text, stdout) == EOF || fflush(stdout) == EOF;
        }
        free(text);
    }
    CoTaskMemFree(path);
    if (item) IShellItem_Release(item);
    if (dialog) IFileDialog_Release(dialog);
    CoUninitialize();
    return result;
}
#else
#include <gtk/gtk.h>
#include <stdio.h>

struct result {
    GMainLoop *loop;
    int status;
    gboolean open;
    gboolean folder;
};

static void selected(GObject *dialog, GAsyncResult *reply, gpointer userdata) {
    struct result *result = userdata;
    GError *error = NULL;
    GFile *file = result->folder ? gtk_file_dialog_select_folder_finish(GTK_FILE_DIALOG(dialog), reply, &error)
               : result->open ? gtk_file_dialog_open_finish(GTK_FILE_DIALOG(dialog), reply, &error)
                              : gtk_file_dialog_save_finish(GTK_FILE_DIALOG(dialog), reply, &error);
    if (file) {
        char *path = g_file_get_path(file);
        if (path) result->status = fputs(path, stdout) == EOF || fflush(stdout) == EOF;
        g_free(path);
        g_object_unref(file);
    } else if (g_error_matches(error, GTK_DIALOG_ERROR, GTK_DIALOG_ERROR_DISMISSED)) {
        result->status = 0;
    }
    g_clear_error(&error);
    g_main_loop_quit(result->loop);
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) return 1;
    const gboolean storage = g_str_equal(argv[1], "--choose-annotation-storage-folder");
    if ((argc == 3 && !storage) || !gtk_init_check()) return 1;
    g_set_application_name("Jaro");
    GtkFileDialog *dialog = gtk_file_dialog_new();
    struct result result = {g_main_loop_new(NULL, FALSE), 1, g_str_equal(argv[1], "--choose-application"),
                            storage || g_str_equal(argv[1], "--choose-annotation-export-folder")};
    if (result.folder) {
        gtk_file_dialog_set_title(dialog, storage ? "Choose an Annotations folder" : "Choose an annotation export folder");
        if (argc == 3) {
            GFile *initial = g_file_new_for_path(argv[2]);
            gtk_file_dialog_set_initial_folder(dialog, initial);
            g_object_unref(initial);
        }
        gtk_file_dialog_select_folder(dialog, NULL, NULL, selected, &result);
    } else if (result.open) {
        gtk_file_dialog_set_title(dialog, "Choose an application");
        GListStore *filters = g_list_store_new(GTK_TYPE_FILE_FILTER);
        GtkFileFilter *launchers = gtk_file_filter_new();
        gtk_file_filter_set_name(launchers, "Application launchers");
        gtk_file_filter_add_pattern(launchers, "*.desktop");
        g_list_store_append(filters, launchers);
        GtkFileFilter *all = gtk_file_filter_new();
        gtk_file_filter_set_name(all, "All files");
        gtk_file_filter_add_pattern(all, "*");
        g_list_store_append(filters, all);
        gtk_file_dialog_set_filters(dialog, G_LIST_MODEL(filters));
        g_object_unref(filters);
        g_object_unref(launchers);
        g_object_unref(all);
        gtk_file_dialog_open(dialog, NULL, NULL, selected, &result);
    } else {
        gtk_file_dialog_set_title(dialog, "Save video");
        gtk_file_dialog_set_initial_name(dialog, argv[1]);
        gtk_file_dialog_save(dialog, NULL, NULL, selected, &result);
    }
    g_main_loop_run(result.loop);
    g_main_loop_unref(result.loop);
    g_object_unref(dialog);
    return result.status;
}
#endif
