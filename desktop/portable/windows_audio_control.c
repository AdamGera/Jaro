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

void control_media(HANDLE owner);

typedef struct {
    ISimpleAudioVolume *session;
    float before;
    float applied;
} Change;

static ISimpleAudioVolume *active_session(IAudioSessionEnumerator *enumerator, int index) {
    IAudioSessionControl *control;
    if (FAILED(IAudioSessionEnumerator_GetSession(enumerator, index, &control))) return NULL;
    ISimpleAudioVolume *volume = NULL;
    AudioSessionState state;
    if (SUCCEEDED(IAudioSessionControl_GetState(control, &state)) && state == AudioSessionStateActive)
        IAudioSessionControl_QueryInterface(control, &IID_ISimpleAudioVolume, (void **)&volume);
    IAudioSessionControl_Release(control);
    return volume;
}

static bool adjust(ISimpleAudioVolume *session, bool mute, float scale, Change *change) {
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
    *change = (Change){session, before, applied};
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
        ISimpleAudioVolume_Release(session);
    }
}

static void control_audio(bool mute, float scale, HANDLE owner) {
    IMMDeviceEnumerator *enumerator = NULL;
    IMMDevice *output = NULL;
    IAudioSessionManager2 *manager = NULL;
    IAudioSessionEnumerator *sessions = NULL;
    int count = 0, changed = 0;
    Change *changes = NULL;
    if (SUCCEEDED(CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator, (void **)&enumerator))
        && SUCCEEDED(IMMDeviceEnumerator_GetDefaultAudioEndpoint(enumerator, eRender, eMultimedia, &output))
        && SUCCEEDED(IMMDevice_Activate(output, &IID_IAudioSessionManager2, CLSCTX_ALL, NULL, (void **)&manager))
        && SUCCEEDED(IAudioSessionManager2_GetSessionEnumerator(manager, &sessions))
        && SUCCEEDED(IAudioSessionEnumerator_GetCount(sessions, &count)) && count > 0) {
        changes = calloc((size_t)count, sizeof(*changes));
        if (changes) {
            for (int i = 0; i < count; ++i) {
                ISimpleAudioVolume *session = active_session(sessions, i);
                if (!session) continue;
                if (adjust(session, mute, scale, &changes[changed])) ++changed;
                else ISimpleAudioVolume_Release(session);
            }
        }
    }
    if (sessions) IAudioSessionEnumerator_Release(sessions);
    if (manager) IAudioSessionManager2_Release(manager);
    if (output) IMMDevice_Release(output);
    if (enumerator) IMMDeviceEnumerator_Release(enumerator);

    puts("ready");
    fflush(stdout);
    if (changed) {
        char byte;
        DWORD received;
        ReadFile(owner, &byte, sizeof(byte), &received, NULL);
        restore_sessions(changes, changed, mute);
    }
    free(changes);
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    bool pause = strcmp(argv[1], "pause") == 0;
    bool mute = strcmp(argv[1], "mute") == 0;
    if (!pause && !mute && strcmp(argv[1], "lower") != 0) return 2;
    char *end;
    long percent = strtol(argv[2], &end, 10);
    if (end == argv[2] || *end || percent < 0 || percent > 100) return 2;
    HANDLE owner = GetStdHandle(STD_INPUT_HANDLE);
    if (!owner || owner == INVALID_HANDLE_VALUE || GetFileType(owner) != FILE_TYPE_PIPE) return 2;
    if (pause) {
        control_media(owner);
        return 0;
    }
    if (FAILED(CoInitializeEx(NULL, COINIT_MULTITHREADED))) return 1;
    control_audio(mute, (float)percent / 100.0f, owner);
    CoUninitialize();
    return 0;
}
