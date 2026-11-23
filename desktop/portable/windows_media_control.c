#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <asyncinfo.h>
#include <roapi.h>
#include <winstring.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

/* The Media.Control WinRT ABI is in the Windows SDK but absent from MinGW. */
static const IID manager_statics_id = {0x2050c4ee, 0x11a0, 0x57de, {0xae,0xd7,0xc9,0x7c,0x70,0x33,0x82,0x45}};
static const IID async_info_id = {0x00000036, 0x0000, 0x0000, {0xc0,0x00,0x00,0x00,0x00,0x00,0x00,0x46}};
static const wchar_t manager_name[] = L"Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager";

typedef struct MediaOperation MediaOperation;
typedef struct MediaManagerStatics MediaManagerStatics;
typedef struct MediaManager MediaManager;
typedef struct MediaSessions MediaSessions;
typedef struct MediaSession MediaSession;
typedef struct MediaPlayback MediaPlayback;
typedef struct MediaProperties MediaProperties;

struct MediaOperation {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *put_Completed)(MediaOperation *, IUnknown *);
        HRESULT (STDMETHODCALLTYPE *get_Completed)(MediaOperation *, IUnknown **);
        /* IAsyncOperation<T> returns by pointer for both objects and boolean. */
        HRESULT (STDMETHODCALLTYPE *GetResults)(MediaOperation *, void *);
    } *lpVtbl;
};

struct MediaManagerStatics {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *RequestAsync)(MediaManagerStatics *, MediaOperation **);
    } *lpVtbl;
};

struct MediaManager {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *GetCurrentSession)(MediaManager *, MediaSession **);
        HRESULT (STDMETHODCALLTYPE *GetSessions)(MediaManager *, MediaSessions **);
    } *lpVtbl;
};

struct MediaSessions {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *GetAt)(MediaSessions *, UINT32, MediaSession **);
        HRESULT (STDMETHODCALLTYPE *get_Size)(MediaSessions *, UINT32 *);
    } *lpVtbl;
};

struct MediaSession {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *get_SourceAppUserModelId)(MediaSession *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *TryGetMediaPropertiesAsync)(MediaSession *, MediaOperation **);
        HRESULT (STDMETHODCALLTYPE *GetTimelineProperties)(MediaSession *, IInspectable **);
        HRESULT (STDMETHODCALLTYPE *GetPlaybackInfo)(MediaSession *, MediaPlayback **);
        HRESULT (STDMETHODCALLTYPE *TryPlayAsync)(MediaSession *, MediaOperation **);
        HRESULT (STDMETHODCALLTYPE *TryPauseAsync)(MediaSession *, MediaOperation **);
    } *lpVtbl;
};

typedef enum { MediaPlaying = 4, MediaPaused = 5 } MediaPlaybackStatus;

struct MediaPlayback {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *get_Controls)(MediaPlayback *, IInspectable **);
        HRESULT (STDMETHODCALLTYPE *get_PlaybackStatus)(MediaPlayback *, MediaPlaybackStatus *);
    } *lpVtbl;
};

struct MediaProperties {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *get_Title)(MediaProperties *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *get_Subtitle)(MediaProperties *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *get_AlbumArtist)(MediaProperties *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *get_Artist)(MediaProperties *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *get_AlbumTitle)(MediaProperties *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *get_TrackNumber)(MediaProperties *, INT32 *);
    } *lpVtbl;
};

typedef struct {
    HSTRING title, artist, album;
    INT32 number;
} Track;

typedef struct {
    MediaSession *session;
    Track track;
} PausedSession;

static void release_object(void *value) {
    if (value) IUnknown_Release((IUnknown *)value);
}

static bool await_result(MediaOperation *operation, DWORD timeout, void *result) {
    if (!operation) return false;
    IAsyncInfo *info = NULL;
    bool success = false;
    if (SUCCEEDED(IUnknown_QueryInterface((IUnknown *)operation, &async_info_id, (void **)&info))) {
        ULONGLONG deadline = GetTickCount64() + timeout;
        AsyncStatus status;
        while (SUCCEEDED(IAsyncInfo_get_Status(info, &status))) {
            if (status == Completed) {
                success = SUCCEEDED(operation->lpVtbl->GetResults(operation, result));
                break;
            }
            if (status != Started) break;
            if (GetTickCount64() >= deadline) {
                IAsyncInfo_Cancel(info);
                break;
            }
            Sleep(5);
        }
        IAsyncInfo_Release(info);
    }
    release_object(operation);
    return success;
}

static MediaSessions *get_sessions(void) {
    HSTRING class_name = NULL;
    if (FAILED(WindowsCreateString(manager_name, ARRAYSIZE(manager_name) - 1, &class_name))) return NULL;
    MediaManagerStatics *statics = NULL;
    RoGetActivationFactory(class_name, &manager_statics_id, (void **)&statics);
    WindowsDeleteString(class_name);
    if (!statics) return NULL;

    MediaOperation *operation = NULL;
    MediaManager *manager = NULL;
    MediaSessions *sessions = NULL;
    if (SUCCEEDED(statics->lpVtbl->RequestAsync(statics, &operation))
        && await_result(operation, 500, &manager) && manager) {
        manager->lpVtbl->GetSessions(manager, &sessions);
    }
    release_object(manager);
    release_object(statics);
    return sessions;
}

static bool has_status(MediaSession *session, MediaPlaybackStatus expected) {
    MediaPlayback *info = NULL;
    MediaPlaybackStatus status;
    if (FAILED(session->lpVtbl->GetPlaybackInfo(session, &info)) || !info) return false;
    bool matched = SUCCEEDED(info->lpVtbl->get_PlaybackStatus(info, &status)) && status == expected;
    release_object(info);
    return matched;
}

static bool get_track(MediaSession *session, Track *track) {
    MediaOperation *operation = NULL;
    MediaProperties *properties = NULL;
    if (FAILED(session->lpVtbl->TryGetMediaPropertiesAsync(session, &operation))
        || !await_result(operation, 200, &properties) || !properties) return false;
    bool success = SUCCEEDED(properties->lpVtbl->get_Title(properties, &track->title))
        && SUCCEEDED(properties->lpVtbl->get_Artist(properties, &track->artist))
        && SUCCEEDED(properties->lpVtbl->get_AlbumTitle(properties, &track->album))
        && SUCCEEDED(properties->lpVtbl->get_TrackNumber(properties, &track->number));
    release_object(properties);
    return success;
}

static void free_track(Track *track) {
    WindowsDeleteString(track->title);
    WindowsDeleteString(track->artist);
    WindowsDeleteString(track->album);
}

static bool equal_string(HSTRING a, HSTRING b) {
    INT32 order;
    return SUCCEEDED(WindowsCompareStringOrdinal(a, b, &order)) && order == 0;
}

static bool same_track(const Track *a, const Track *b) {
    return a->number == b->number && equal_string(a->title, b->title)
        && equal_string(a->artist, b->artist) && equal_string(a->album, b->album);
}

static bool change_playback(MediaSession *session, bool pause) {
    MediaOperation *operation = NULL;
    HRESULT started = pause ? session->lpVtbl->TryPauseAsync(session, &operation)
                            : session->lpVtbl->TryPlayAsync(session, &operation);
    boolean changed = FALSE;
    return SUCCEEDED(started) && await_result(operation, 500, &changed) && changed;
}

static bool owner_open(HANDLE owner) {
    if (PeekNamedPipe(owner, NULL, 0, NULL, NULL, NULL)) return true;
    DWORD error = GetLastError();
    return error != ERROR_BROKEN_PIPE && error != ERROR_PIPE_NOT_CONNECTED && error != ERROR_NO_DATA;
}

static UINT32 pause_sessions(MediaSessions *sessions, PausedSession *paused, UINT32 length, HANDLE owner) {
    ULONGLONG deadline = GetTickCount64() + 1200;
    UINT32 count = 0;
    for (UINT32 i = 0; i < length && owner_open(owner) && GetTickCount64() < deadline; ++i) {
        MediaSession *session = NULL;
        if (FAILED(sessions->lpVtbl->GetAt(sessions, i, &session)) || !session) continue;
        Track track = {0};
        if (has_status(session, MediaPlaying) && get_track(session, &track)
            && owner_open(owner) && has_status(session, MediaPlaying) && change_playback(session, true)) {
            paused[count++] = (PausedSession){session, track};
        } else {
            free_track(&track);
            release_object(session);
        }
    }
    return count;
}

static void restore_sessions(PausedSession *paused, UINT32 count) {
    for (UINT32 i = 0; i < count; ++i) {
        MediaSession *session = paused[i].session;
        Track current = {0};
        if (has_status(session, MediaPaused) && get_track(session, &current)
            && same_track(&paused[i].track, &current) && has_status(session, MediaPaused)) {
            change_playback(session, false);
        }
        free_track(&current);
        free_track(&paused[i].track);
        release_object(session);
    }
}

void control_media(HANDLE owner) {
    bool initialized = SUCCEEDED(RoInitialize(RO_INIT_MULTITHREADED));
    MediaSessions *sessions = initialized && owner_open(owner) ? get_sessions() : NULL;
    UINT32 length = 0, count = 0;
    PausedSession *paused = NULL;
    if (sessions && SUCCEEDED(sessions->lpVtbl->get_Size(sessions, &length)) && length) {
        paused = calloc(length, sizeof(*paused));
        if (paused) count = pause_sessions(sessions, paused, length, owner);
    }
    release_object(sessions);
    puts("ready");
    fflush(stdout);
    if (count) {
        char byte;
        DWORD received;
        ReadFile(owner, &byte, sizeof(byte), &received, NULL);
        restore_sessions(paused, count);
    }
    free(paused);
    if (initialized) RoUninitialize();
}
