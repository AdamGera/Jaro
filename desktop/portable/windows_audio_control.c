#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audiopolicy.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

void control_media(HANDLE owner);

typedef struct {
    ISimpleAudioVolume *session;
    LPWSTR id;
    float before;
    float applied;
} Change;

static bool tracked_session(const Change *changes, int count, const wchar_t *id) {
    for (int i = 0; i < count; ++i) {
        if (changes[i].id && wcscmp(changes[i].id, id) == 0) return true;
    }
    return false;
}

static ISimpleAudioVolume *active_session(IAudioSessionEnumerator *enumerator, int index, DWORD exclude_pid, LPWSTR *id_out) {
    IAudioSessionControl *control;
    *id_out = NULL;
    if (FAILED(IAudioSessionEnumerator_GetSession(enumerator, index, &control))) return NULL;
    ISimpleAudioVolume *volume = NULL;
    AudioSessionState state;
    if (SUCCEEDED(IAudioSessionControl_GetState(control, &state)) && state == AudioSessionStateActive) {
        IAudioSessionControl2 *control2 = NULL;
        bool own_process = false;
        if (SUCCEEDED(IAudioSessionControl_QueryInterface(control, &IID_IAudioSessionControl2, (void **)&control2))) {
            DWORD pid = 0;
            if (SUCCEEDED(IAudioSessionControl2_GetProcessId(control2, &pid))
                && (pid == GetCurrentProcessId() || (exclude_pid && pid == exclude_pid))) {
                own_process = true;
            } else {
                IAudioSessionControl2_GetSessionInstanceIdentifier(control2, id_out);
            }
            IAudioSessionControl2_Release(control2);
        }
        if (!own_process) {
            IAudioSessionControl_QueryInterface(control, &IID_ISimpleAudioVolume, (void **)&volume);
        }
        if (!volume && *id_out) {
            CoTaskMemFree(*id_out);
            *id_out = NULL;
        }
    }
    IAudioSessionControl_Release(control);
    return volume;
}

static bool adjust(ISimpleAudioVolume *session, LPWSTR id, bool mute, float scale, Change *change) {
    float before, applied;
    if (mute) {
        BOOL already_muted;
        if (FAILED(ISimpleAudioVolume_GetMute(session, &already_muted)) || already_muted ||
            FAILED(ISimpleAudioVolume_SetMute(session, TRUE, NULL))) return false;
        before = 0.0f;
        applied = 1.0f;
    } else {
        if (FAILED(ISimpleAudioVolume_GetMasterVolume(session, &before))) return false;
        applied = before * scale;
        if (before == applied || FAILED(ISimpleAudioVolume_SetMasterVolume(session, applied, NULL))) return false;
    }
    *change = (Change){session, id, before, applied};
    return true;
}

static void restore_sessions(Change *changes, int count, bool mute) {
    for (int i = 0; i < count; ++i) {
        ISimpleAudioVolume *session = changes[i].session;
        if (mute) {
            BOOL current;
            if (SUCCEEDED(ISimpleAudioVolume_GetMute(session, &current)) && current)
                ISimpleAudioVolume_SetMute(session, FALSE, NULL);
        } else {
            float current;
            if (SUCCEEDED(ISimpleAudioVolume_GetMasterVolume(session, &current))
                && fabsf(current - changes[i].applied) < .00001f)
                ISimpleAudioVolume_SetMasterVolume(session, changes[i].before, NULL);
        }
        if (changes[i].id) CoTaskMemFree(changes[i].id);
        ISimpleAudioVolume_Release(session);
    }
}

static void scan_sessions(IAudioSessionManager2 *manager, bool mute, float scale, DWORD exclude_pid,
                          Change **changes, int *changed, int *capacity) {
    IAudioSessionEnumerator *sessions = NULL;
    int count = 0;
    if (!manager || FAILED(IAudioSessionManager2_GetSessionEnumerator(manager, &sessions))
        || FAILED(IAudioSessionEnumerator_GetCount(sessions, &count)) || count <= 0) {
        if (sessions) IAudioSessionEnumerator_Release(sessions);
        return;
    }
    for (int i = 0; i < count; ++i) {
        LPWSTR id = NULL;
        ISimpleAudioVolume *session = active_session(sessions, i, exclude_pid, &id);
        if (!session) continue;
        if (id && tracked_session(*changes, *changed, id)) {
            CoTaskMemFree(id);
            ISimpleAudioVolume_Release(session);
            continue;
        }
        if (*changed >= *capacity) {
            int next = *capacity ? *capacity * 2 : 16;
            Change *grown = realloc(*changes, (size_t)next * sizeof(*grown));
            if (!grown) {
                if (id) CoTaskMemFree(id);
                ISimpleAudioVolume_Release(session);
                break;
            }
            *changes = grown;
            *capacity = next;
        }
        if (adjust(session, id, mute, scale, &(*changes)[*changed])) {
            ++(*changed);
        } else {
            if (id) CoTaskMemFree(id);
            ISimpleAudioVolume_Release(session);
        }
    }
    IAudioSessionEnumerator_Release(sessions);
}

static void control_audio(bool mute, float scale, DWORD exclude_pid, HANDLE owner) {
    IMMDeviceEnumerator *enumerator = NULL;
    IMMDevice *output = NULL;
    IAudioSessionManager2 *manager = NULL;
    int changed = 0, capacity = 0;
    Change *changes = NULL;
    if (SUCCEEDED(CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator, (void **)&enumerator))
        && SUCCEEDED(IMMDeviceEnumerator_GetDefaultAudioEndpoint(enumerator, eRender, eMultimedia, &output))
        && SUCCEEDED(IMMDevice_Activate(output, &IID_IAudioSessionManager2, CLSCTX_ALL, NULL, (void **)&manager))) {
        scan_sessions(manager, mute, scale, exclude_pid, &changes, &changed, &capacity);
    }

    puts("ready");
    fflush(stdout);
    if (manager) {
        for (;;) {
            DWORD available = 0;
            if (!PeekNamedPipe(owner, NULL, 0, NULL, &available, NULL)) break;
            if (available > 0) {
                char byte;
                DWORD received;
                if (!ReadFile(owner, &byte, sizeof(byte), &received, NULL) || received == 0) break;
            }
            Sleep(150);
            scan_sessions(manager, mute, scale, exclude_pid, &changes, &changed, &capacity);
        }
        restore_sessions(changes, changed, mute);
    }
    if (manager) IAudioSessionManager2_Release(manager);
    if (output) IMMDevice_Release(output);
    if (enumerator) IMMDeviceEnumerator_Release(enumerator);
    free(changes);
}

int main(int argc, char **argv) {
    if (argc != 3 && argc != 4) return 2;
    bool pause = strcmp(argv[1], "pause") == 0;
    bool mute = strcmp(argv[1], "mute") == 0;
    if (!pause && !mute && strcmp(argv[1], "lower") != 0) return 2;
    char *end;
    long percent = strtol(argv[2], &end, 10);
    if (end == argv[2] || *end || percent < 0 || percent > 100) return 2;
    DWORD exclude_pid = 0;
    if (argc == 4) {
        unsigned long parsed = strtoul(argv[3], &end, 10);
        if (end == argv[3] || *end) return 2;
        exclude_pid = (DWORD)parsed;
    }
    HANDLE owner = GetStdHandle(STD_INPUT_HANDLE);
    if (!owner || owner == INVALID_HANDLE_VALUE || GetFileType(owner) != FILE_TYPE_PIPE) return 2;
    if (pause) {
        control_media(owner);
        return 0;
    }
    if (FAILED(CoInitializeEx(NULL, COINIT_MULTITHREADED))) return 1;
    control_audio(mute, (float)percent / 100.0f, exclude_pid, owner);
    CoUninitialize();
    return 0;
}
