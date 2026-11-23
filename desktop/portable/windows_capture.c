#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <audiopolicy.h>
#include <functiondiscoverykeys_devpkey.h>

#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#include "capture_timeline.h"

int jaro_browser_pages(int count, wchar_t **binaries);

enum { OUTPUT_BLOCK = CAPTURE_RATE / 50, CAPTURE_LAG = CAPTURE_RATE / 5, MAX_SECONDS = 3 * 3600,
       MAX_MEETING_OUTPUTS = 32 };

typedef struct {
    IAudioClient *client;
    IAudioCaptureClient *capture;
    CaptureTimeline timeline;
    bool started;
} Input;

typedef struct Output {
    Input input;
    wchar_t *id;
    bool seen;
    int64_t first_frame;
    struct Output *next;
} Output;

typedef struct {
    int32_t samples[QUEUE_FRAMES];
    int64_t written, cutover;
} OutputTail;

typedef enum { ALL_OUTPUTS, CALL_APP, MIC_HOLDER } RouteScope;

typedef struct {
    RouteScope scope;
    unsigned count;
    const wchar_t *ids[MAX_MEETING_OUTPUTS];
} Route;

typedef struct {
    int binaries_count;
    wchar_t **binaries;
    HANDLE ready, stop;
    SRWLOCK lock;
    Route latest;
} RouteProbe;

static bool route_contains(const Route *route, const wchar_t *id) {
    for (unsigned i = 0; i < route->count; ++i) {
        if (!wcscmp(route->ids[i], id)) return true;
    }
    return false;
}

static HRESULT microphone_device(IMMDeviceEnumerator *enumerator, const wchar_t *id, IMMDevice **device) {
    HRESULT result = IMMDeviceEnumerator_GetDevice(enumerator, id, device);
    if (FAILED(result)) return result;
    DWORD state = 0;
    result = IMMDevice_GetState(*device, &state);
    if (SUCCEEDED(result) && state != DEVICE_STATE_ACTIVE) result = AUDCLNT_E_DEVICE_INVALIDATED;
    if (SUCCEEDED(result)) {
        IMMEndpoint *endpoint = NULL;
        result = IMMDevice_QueryInterface(*device, &IID_IMMEndpoint, (void **)&endpoint);
        if (SUCCEEDED(result)) {
            EDataFlow flow;
            result = IMMEndpoint_GetDataFlow(endpoint, &flow);
            if (SUCCEEDED(result) && flow != eCapture) result = E_INVALIDARG;
            IMMEndpoint_Release(endpoint);
        }
    }
    if (FAILED(result)) {
        IMMDevice_Release(*device);
        *device = NULL;
    }
    return result;
}

static HRESULT open_input(IMMDeviceEnumerator *enumerator, Input *input, bool loopback, const wchar_t *id) {
    IMMDevice *device = NULL;
    HRESULT result = id ? (loopback ? IMMDeviceEnumerator_GetDevice(enumerator, id, &device)
                                   : microphone_device(enumerator, id, &device))
                        : IMMDeviceEnumerator_GetDefaultAudioEndpoint(
                            enumerator, loopback ? eRender : eCapture, eMultimedia, &device);
    if (FAILED(result)) return result;
    result = IMMDevice_Activate(device, &IID_IAudioClient, CLSCTX_ALL, NULL, (void **)&input->client);
    IMMDevice_Release(device);
    if (FAILED(result)) return result;

    WAVEFORMATEX format = {
        .wFormatTag = WAVE_FORMAT_PCM, .nChannels = 1, .nSamplesPerSec = CAPTURE_RATE,
        .nAvgBytesPerSec = CAPTURE_RATE * sizeof(int16_t), .nBlockAlign = sizeof(int16_t),
        .wBitsPerSample = 16,
    };
    DWORD flags = AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    if (loopback) flags |= AUDCLNT_STREAMFLAGS_LOOPBACK;
    result = IAudioClient_Initialize(input->client, AUDCLNT_SHAREMODE_SHARED, flags, 500000, 0, &format, NULL);
    if (FAILED(result)) return result;
    return IAudioClient_GetService(input->client, &IID_IAudioCaptureClient, (void **)&input->capture);
}

static void close_input(Input *input) {
    if (input->started) IAudioClient_Stop(input->client);
    if (input->capture) IAudioCaptureClient_Release(input->capture);
    if (input->client) IAudioClient_Release(input->client);
    input->client = NULL;
    input->capture = NULL;
    input->started = false;
}

static HRESULT start_input(Input *input) {
    HRESULT result = IAudioClient_Start(input->client);
    input->started = SUCCEEDED(result);
    return result;
}

static bool reopen_input(IMMDeviceEnumerator *enumerator, Input *input, bool loopback, const wchar_t *id) {
    memset(&input->timeline, 0, sizeof(input->timeline));
    if (SUCCEEDED(open_input(enumerator, input, loopback, id)) && SUCCEEDED(start_input(input))) return true;
    close_input(input);
    return false;
}

static void free_output(Output *output) {
    close_input(&output->input);
    CoTaskMemFree(output->id);
    free(output);
}

static bool refresh_outputs(IMMDeviceEnumerator *enumerator, Output **outputs, const Route *route, OutputTail *tail) {
    IMMDeviceCollection *devices = NULL;
    if (FAILED(IMMDeviceEnumerator_EnumAudioEndpoints(enumerator, eRender, DEVICE_STATE_ACTIVE, &devices))) return false;
    UINT count = 0;
    if (FAILED(IMMDeviceCollection_GetCount(devices, &count))) {
        IMMDeviceCollection_Release(devices);
        return false;
    }
    for (Output *out = *outputs; out; out = out->next) {
        out->seen = false;
    }
    bool complete = true;
    bool started = false;
    unsigned tracked = 0;
    for (UINT index = 0; index < count; ++index) {
        IMMDevice *device = NULL;
        wchar_t *id = NULL;
        if (SUCCEEDED(IMMDeviceCollection_Item(devices, index, &device))) {
            IMMDevice_GetId(device, &id);
            IMMDevice_Release(device);
        }
        if (!id) {
            complete = false;
            continue;
        }
        if (tracked >= MAX_MEETING_OUTPUTS || (route->scope != ALL_OUTPUTS && !route_contains(route, id))) {
            CoTaskMemFree(id);
            continue;
        }
        Output *out = *outputs;
        while (out && wcscmp(out->id, id) != 0) out = out->next;
        if (!out) {
            out = calloc(1, sizeof(*out));
            if (out) {
                out->id = id;
                id = NULL;
                out->next = *outputs;
                *outputs = out;
            }
        }
        CoTaskMemFree(id);
        if (!out) continue;
        out->seen = true;
        ++tracked;
        if (!out->input.started) {
            out->first_frame = tail->cutover;
            reopen_input(enumerator, &out->input, true, out->id);
        }
        if (out->input.started) started = true;
    }
    IMMDeviceCollection_Release(devices);
    if (!started && (route->scope != ALL_OUTPUTS || !complete)) return false;
    for (Output **cursor = outputs; *cursor;) {
        Output *out = *cursor;
        bool wanted = route->scope == ALL_OUTPUTS || route_contains(route, out->id);
        if (out->seen || (!complete && wanted)) {
            cursor = &out->next;
        } else {
            *cursor = out->next;
            // Keep packets already read from an output up to this switch.
            int64_t end = out->input.timeline.through;
            if (end > tail->cutover) end = tail->cutover;
            if (end > tail->written + QUEUE_FRAMES) end = tail->written + QUEUE_FRAMES;
            for (int64_t frame = tail->written; frame < end; ++frame)
                tail->samples[frame % QUEUE_FRAMES] += capture_take(&out->input.timeline, frame);
            free_output(out);
        }
    }
    return started;
}

static int16_t take_system(Input *system, Output *outputs, OutputTail *tail, int64_t frame) {
    if (system) return capture_take(&system->timeline, frame);
    int64_t index = frame % QUEUE_FRAMES;
    int32_t sample = tail->samples[index];
    tail->samples[index] = 0;
    for (Output *out = outputs; out; out = out->next) {
        sample += capture_take(&out->input.timeline, frame);
    }
    return sample > INT16_MAX ? INT16_MAX : sample < INT16_MIN ? INT16_MIN : (int16_t)sample;
}

static HRESULT read_input(Input *input, int64_t written, int64_t first_tick, int64_t elapsed) {
    if (!input->started) return S_OK;
    for (;;) {
        UINT32 available;
        HRESULT result = IAudioCaptureClient_GetNextPacketSize(input->capture, &available);
        if (FAILED(result) || !available) return result;
        BYTE *data = NULL;
        UINT32 frames;
        DWORD flags;
        UINT64 tick = 0, device_frame = 0;
        result = IAudioCaptureClient_GetBuffer(input->capture, &data, &frames, &flags, &device_frame, &tick);
        if (result == AUDCLNT_S_BUFFER_EMPTY) return S_OK;
        if (FAILED(result)) return result;

        // QPC runs on the same clock for both endpoints. A timestamp marked
        // unreliable falls back to the previous packet or the present clock.
        bool timed = tick != 0 && !(flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR);
        int64_t frame = timed ? capture_frame((int64_t)tick - first_tick) : elapsed - frames;
        const int16_t *samples = flags & AUDCLNT_BUFFERFLAGS_SILENT ? NULL : (const int16_t *)data;
        capture_packet(&input->timeline, written, frame, device_frame, samples, frames, timed,
                       (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) != 0);
        result = IAudioCaptureClient_ReleaseBuffer(input->capture, frames);
        if (FAILED(result)) return result;
    }
}

static int64_t tick_time(int64_t frequency) {
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    return (counter.QuadPart / frequency) * 10000000
        + (counter.QuadPart % frequency) * 10000000 / frequency;
}

static DWORD WINAPI wait_for_stop(void *context) {
    char byte;
    DWORD bytes_read;
    ReadFile((HANDLE)context, &byte, sizeof(byte), &bytes_read, NULL);
    return 0;
}

static bool write_frames(Input *mic, Input *system, Output *outputs, OutputTail *tail, int64_t *written, int64_t target,
                         bool loopback, bool dual) {
    int16_t samples[OUTPUT_BLOCK * 2];
    while (*written < target) {
        int64_t count = target - *written;
        if (count > OUTPUT_BLOCK) count = OUTPUT_BLOCK;
        size_t length = 0;
        for (int64_t frame = *written; frame < *written + count; ++frame) {
            samples[length++] = loopback ? take_system(system, outputs, tail, frame) : capture_take(&mic->timeline, frame);
            if (dual) samples[length++] = take_system(system, outputs, tail, frame);
        }
        if (fwrite(samples, sizeof(samples[0]), length, stdout) != length || fflush(stdout) != 0) return false;
        *written += count;
    }
    return true;
}

static HRESULT list_microphones(IMMDeviceEnumerator *enumerator) {
    IMMDeviceCollection *devices = NULL;
    HRESULT result = IMMDeviceEnumerator_EnumAudioEndpoints(enumerator, eCapture, DEVICE_STATE_ACTIVE, &devices);
    if (FAILED(result)) return result;
    IMMDevice *default_device = NULL;
    wchar_t *default_id = NULL;
    if (SUCCEEDED(IMMDeviceEnumerator_GetDefaultAudioEndpoint(enumerator, eCapture, eMultimedia, &default_device))) {
        IMMDevice_GetId(default_device, &default_id);
        IMMDevice_Release(default_device);
    }
    UINT count = 0;
    result = IMMDeviceCollection_GetCount(devices, &count);
    for (UINT index = 0; SUCCEEDED(result) && index < count; ++index) {
        IMMDevice *device = NULL;
        if (FAILED(IMMDeviceCollection_Item(devices, index, &device))) continue;
        wchar_t *id = NULL;
        char identifier[4097], label[1025] = "";
        if (SUCCEEDED(IMMDevice_GetId(device, &id)) &&
            WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, id, -1, identifier, sizeof(identifier), NULL, NULL)) {
            IPropertyStore *properties = NULL;
            if (SUCCEEDED(IMMDevice_OpenPropertyStore(device, STGM_READ, &properties))) {
                PROPVARIANT name = {0};
                if (SUCCEEDED(IPropertyStore_GetValue(properties, &PKEY_Device_FriendlyName, &name)) &&
                    name.vt == VT_LPWSTR && name.pwszVal) {
                    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, name.pwszVal, -1, label, sizeof(label), NULL, NULL))
                        label[0] = '\0';
                }
                PropVariantClear(&name);
                IPropertyStore_Release(properties);
            }
            for (char *p = label; *p; ++p) if ((unsigned char)*p < 32) *p = ' ';
            if (fprintf(stdout, "%d\t%s\t%s\n", default_id && wcscmp(default_id, id) == 0,
                        identifier, label[0] ? label : identifier) < 0) result = E_FAIL;
        }
        CoTaskMemFree(id);
        IMMDevice_Release(device);
    }
    CoTaskMemFree(default_id);
    IMMDeviceCollection_Release(devices);
    return fflush(stdout) == 0 ? result : E_FAIL;
}

static const wchar_t *process_name(DWORD pid, wchar_t *path, DWORD capacity) {
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return NULL;
    BOOL found = QueryFullProcessImageNameW(process, 0, path, &capacity);
    CloseHandle(process);
    if (!found) return NULL;
    const wchar_t *base = wcsrchr(path, L'\\');
    return base ? base + 1 : path;
}

static HRESULT active_session_pid(IAudioSessionControl *session, DWORD *pid) {
    *pid = 0;
    AudioSessionState state;
    HRESULT result = IAudioSessionControl_GetState(session, &state);
    if (FAILED(result) || state != AudioSessionStateActive) return result;
    IAudioSessionControl2 *owner = NULL;
    result = IAudioSessionControl_QueryInterface(session, &IID_IAudioSessionControl2, (void **)&owner);
    if (FAILED(result)) return result;
    result = IAudioSessionControl2_GetProcessId(owner, pid);
    IAudioSessionControl2_Release(owner);
    // A shared audio session has no authoritative owning process.
    if (result != S_OK) *pid = 0;
    return result;
}

typedef HRESULT (*SessionVisitor)(DWORD, void *);
typedef HRESULT (*EndpointVisitor)(IMMDevice *, void *);

static HRESULT visit_sessions(IMMDevice *device, SessionVisitor visit, void *context) {
    IAudioSessionManager2 *manager = NULL;
    HRESULT result = IMMDevice_Activate(device, &IID_IAudioSessionManager2, CLSCTX_ALL, NULL, (void **)&manager);
    if (FAILED(result)) return result;
    IAudioSessionEnumerator *sessions = NULL;
    result = IAudioSessionManager2_GetSessionEnumerator(manager, &sessions);
    IAudioSessionManager2_Release(manager);
    if (FAILED(result)) return result;
    int length = 0;
    result = IAudioSessionEnumerator_GetCount(sessions, &length);
    bool complete = SUCCEEDED(result);
    for (int at = 0; at < length && SUCCEEDED(result); ++at) {
        IAudioSessionControl *session = NULL;
        if (FAILED(IAudioSessionEnumerator_GetSession(sessions, at, &session))) {
            complete = false;
            continue;
        }
        DWORD pid;
        HRESULT read = active_session_pid(session, &pid);
        IAudioSessionControl_Release(session);
        if (FAILED(read) || (pid && FAILED(visit(pid, context)))) complete = false;
    }
    IAudioSessionEnumerator_Release(sessions);
    return complete ? S_OK : E_FAIL;
}

static HRESULT visit_endpoints(IMMDeviceEnumerator *enumerator, EDataFlow direction, EndpointVisitor visit, void *context) {
    IMMDeviceCollection *devices = NULL;
    HRESULT result = IMMDeviceEnumerator_EnumAudioEndpoints(enumerator, direction, DEVICE_STATE_ACTIVE, &devices);
    if (FAILED(result)) return result;
    UINT count = 0;
    result = IMMDeviceCollection_GetCount(devices, &count);
    bool complete = SUCCEEDED(result);
    for (UINT index = 0; index < count && SUCCEEDED(result); ++index) {
        IMMDevice *device = NULL;
        if (FAILED(IMMDeviceCollection_Item(devices, index, &device))) {
            complete = false;
            continue;
        }
        if (FAILED(visit(device, context))) complete = false;
        IMMDevice_Release(device);
    }
    IMMDeviceCollection_Release(devices);
    return complete ? S_OK : E_FAIL;
}

static HRESULT report_audio_user(DWORD pid, void *unused) {
    (void)unused;
    wchar_t path[4096];
    const wchar_t *base = process_name(pid, path, ARRAYSIZE(path));
    char binary[512];
    if (base && WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, base, -1, binary, sizeof(binary), NULL, NULL)
        && puts(binary) == EOF) return E_FAIL;
    return S_OK;
}

static HRESULT report_endpoint_users(IMMDevice *device, void *unused) {
    return visit_sessions(device, report_audio_user, unused);
}

static void clear_route(Route *route) {
    for (unsigned i = 0; i < route->count; ++i) CoTaskMemFree((void *)route->ids[i]);
    *route = (Route){0};
}

typedef struct {
    const RouteProbe *probe;
    DWORD holders[128];
    unsigned holder_count;
    Route by_app, by_holder;
    bool matches_app, matches_holder;
} RouteInventory;

static bool belongs_to_call(const RouteProbe *probe, DWORD pid) {
    wchar_t path[4096];
    const wchar_t *base = process_name(pid, path, ARRAYSIZE(path));
    if (base) {
        for (int i = 0; i < probe->binaries_count; ++i) {
            if (!_wcsicmp(base, probe->binaries[i])) return true;
        }
    }
    return false;
}

static bool is_holder(const RouteInventory *inventory, DWORD pid) {
    for (unsigned i = 0; i < inventory->holder_count; ++i) {
        if (inventory->holders[i] == pid) return true;
    }
    return false;
}

static HRESULT capture_owner(DWORD pid, void *context) {
    RouteInventory *inventory = context;
    if (inventory->holder_count < ARRAYSIZE(inventory->holders) && !is_holder(inventory, pid)
        && belongs_to_call(inventory->probe, pid)) inventory->holders[inventory->holder_count++] = pid;
    return S_OK;
}

static HRESULT render_owner(DWORD pid, void *context) {
    RouteInventory *inventory = context;
    if (is_holder(inventory, pid)) inventory->matches_holder = true;
    if (inventory->matches_holder || belongs_to_call(inventory->probe, pid)) inventory->matches_app = true;
    return S_OK;
}

static HRESULT collect_holders(IMMDevice *device, void *context) {
    return visit_sessions(device, capture_owner, context);
}

static HRESULT append_endpoint(IMMDevice *device, Route *route) {
    if (route->count == ARRAYSIZE(route->ids)) return S_OK;
    wchar_t *id = NULL;
    HRESULT result = IMMDevice_GetId(device, &id);
    if (SUCCEEDED(result)) route->ids[route->count++] = id;
    return result;
}

static HRESULT collect_render(IMMDevice *device, void *context) {
    RouteInventory *inventory = context;
    inventory->matches_app = inventory->matches_holder = false;
    HRESULT result = visit_sessions(device, render_owner, inventory);
    if (SUCCEEDED(result) && inventory->matches_app) result = append_endpoint(device, &inventory->by_app);
    if (SUCCEEDED(result) && inventory->matches_holder) result = append_endpoint(device, &inventory->by_holder);
    return result;
}

static DWORD WINAPI watch_route(void *context) {
    RouteProbe *probe = context;
    HRESULT initialized = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    IMMDeviceEnumerator *enumerator = NULL;
    if (SUCCEEDED(initialized)) CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL,
                                                 &IID_IMMDeviceEnumerator, (void **)&enumerator);
    if (enumerator) {
        do {
            RouteInventory inventory = {.probe = probe, .by_app.scope = CALL_APP, .by_holder.scope = MIC_HOLDER};
            if (SUCCEEDED(visit_endpoints(enumerator, eCapture, collect_holders, &inventory))
                && SUCCEEDED(visit_endpoints(enumerator, eRender, collect_render, &inventory))) {
                Route *chosen = inventory.by_holder.count ? &inventory.by_holder : &inventory.by_app;
                AcquireSRWLockExclusive(&probe->lock);
                clear_route(&probe->latest);
                if (chosen->count) probe->latest = *chosen;
                *chosen = (Route){0};
                ReleaseSRWLockExclusive(&probe->lock);
            }
            clear_route(&inventory.by_holder);
            clear_route(&inventory.by_app);
            SetEvent(probe->ready);
        } while (WaitForSingleObject(probe->stop, 1500) == WAIT_TIMEOUT);
        IMMDeviceEnumerator_Release(enumerator);
    }
    SetEvent(probe->ready);
    if (SUCCEEDED(initialized)) CoUninitialize();
    return 0;
}

static bool keep_current_route(const Route *preferred, const Route *current) {
    if (preferred->scope == ALL_OUTPUTS) return true;
    if (current->scope != MIC_HOLDER || preferred->scope != CALL_APP) return false;
    for (unsigned i = 0; i < current->count; ++i) {
        if (!route_contains(preferred, current->ids[i])) return false;
    }
    return true;
}

static void refresh_meeting_outputs(IMMDeviceEnumerator *enumerator, Output **outputs, RouteProbe *probe,
                                    RouteScope *scope, OutputTail *tail, int64_t written, int64_t elapsed) {
    // Release the probe lock before opening any audio clients.
    Route preferred = {0};
    if (probe) {
        AcquireSRWLockShared(&probe->lock);
        preferred.scope = probe->latest.scope;
        for (unsigned i = 0; i < probe->latest.count; ++i) {
            const wchar_t *id = probe->latest.ids[i];
            size_t size = (wcslen(id) + 1) * sizeof(*id);
            wchar_t *copy = CoTaskMemAlloc(size);
            if (!copy) {
                clear_route(&preferred);
                break;
            }
            memcpy(copy, id, size);
            preferred.ids[preferred.count++] = copy;
        }
        ReleaseSRWLockShared(&probe->lock);
    }
    Route current = {.scope = *scope};
    for (Output *out = *outputs; out && current.count < ARRAYSIZE(current.ids); out = out->next) {
        // A previous failed selection can leave a closed client to retry.
        if (out->seen) current.ids[current.count++] = out->id;
    }
    if (current.scope != ALL_OUTPUTS && !current.count) current.scope = ALL_OUTPUTS;
    const Route *wanted = keep_current_route(&preferred, &current) ? &current : &preferred;
    tail->written = written;
    tail->cutover = elapsed;
    if (refresh_outputs(enumerator, outputs, wanted, tail)) {
        *scope = wanted->scope;
    } else if (wanted->scope != ALL_OUTPUTS) {
        // If selected outputs disappeared or will not open, continue with any available output.
        *scope = ALL_OUTPUTS;
        refresh_outputs(enumerator, outputs, &(Route){0}, tail);
    }
    clear_route(&preferred);
}

static HRESULT capture(IMMDeviceEnumerator *enumerator, bool loopback, bool dual, int64_t limit, HANDLE control,
                       bool write_clock, const wchar_t *microphone, bool meeting, RouteProbe *probe, bool dictation) {
    Input mic = {0}, system = {0};
    Output *outputs = NULL;
    OutputTail tail = {0};
    RouteScope route_scope = ALL_OUTPUTS;
    HRESULT result = loopback ? S_OK : open_input(enumerator, &mic, false, microphone);
    if (SUCCEEDED(result) && !meeting && (dual || loopback)) result = open_input(enumerator, &system, true, NULL);

    LARGE_INTEGER frequency;
    if (!QueryPerformanceFrequency(&frequency)) result = E_FAIL;
    int64_t start = 0;
    if (SUCCEEDED(result)) {
        start = tick_time(frequency.QuadPart);
        if (mic.client) result = start_input(&mic);
        if (SUCCEEDED(result) && system.client) result = start_input(&system);
        if (SUCCEEDED(result) && meeting) {
            refresh_meeting_outputs(enumerator, &outputs, probe, &route_scope, &tail, 0, 0);
            Output *ready = outputs;
            while (ready && !ready->input.started) ready = ready->next;
            if (!ready && !mic.started) result = AUDCLNT_E_DEVICE_INVALIDATED;
        }
    }
    if (SUCCEEDED(result) && write_clock && (fwrite(&start, sizeof(start), 1, stdout) != 1 || fflush(stdout)))
        result = E_FAIL;

    int64_t written = 0;
    int64_t refresh_at = CAPTURE_RATE;
    while (SUCCEEDED(result) && written < limit) {
        int64_t elapsed = capture_frame(tick_time(frequency.QuadPart) - start);
        bool stopping = WaitForSingleObject(control, 0) == WAIT_OBJECT_0;
        if (stopping && elapsed < limit) limit = elapsed;
        result = read_input(&mic, written, start, elapsed);
        if (meeting) {
            if (FAILED(result)) close_input(&mic);
            result = S_OK;
            for (Output *out = outputs; out; out = out->next) {
                int64_t first = out->first_frame > written ? out->first_frame : written;
                if (FAILED(read_input(&out->input, first, start, elapsed))) close_input(&out->input);
            }
            if (elapsed >= refresh_at && elapsed < limit) {
                if (!mic.started) reopen_input(enumerator, &mic, false, NULL);
                refresh_meeting_outputs(enumerator, &outputs, probe, &route_scope, &tail, written, elapsed);
                refresh_at = elapsed + CAPTURE_RATE;
            }
        } else if (SUCCEEDED(result)) {
            result = read_input(&system, written, start, elapsed);
        }
        if (FAILED(result)) break;
        // Let both devices deliver packets before filling gaps with silence.
        int64_t target = elapsed - CAPTURE_LAG;
        if (dictation) {
            // Only commit through the last actual packet so a stalled microphone
            // cannot look like continuous input to the dictation watchdog.
            target = mic.timeline.through < elapsed ? mic.timeline.through : elapsed;
            if (stopping && target < limit) limit = target;
        }
        if (target > limit) target = limit;
        if (!write_frames(&mic, meeting ? NULL : &system, outputs, &tail, &written, target, loopback, dual)) break;
        if (written < limit) Sleep(5);
    }
    close_input(&system);
    close_input(&mic);
    while (outputs) {
        Output *next = outputs->next;
        free_output(outputs);
        outputs = next;
    }
    return result;
}

static void close_route_probe(RouteProbe *probe, HANDLE worker) {
    if (!probe) return;
    if (worker) {
        SetEvent(probe->stop);
        DWORD stopped = WaitForSingleObject(worker, 250);
        CloseHandle(worker);
        // If a driver is stuck, leave its storage valid until the helper exits.
        if (stopped != WAIT_OBJECT_0) return;
    }
    if (probe->stop) CloseHandle(probe->stop);
    if (probe->ready) CloseHandle(probe->ready);
    clear_route(&probe->latest);
    free(probe);
}

static RouteProbe *start_route_probe(int count, wchar_t **binaries, HANDLE *worker) {
    RouteProbe *probe = calloc(1, sizeof(*probe));
    if (!probe) return NULL;
    probe->binaries_count = count;
    probe->binaries = binaries;
    InitializeSRWLock(&probe->lock);
    probe->stop = CreateEventW(NULL, TRUE, FALSE, NULL);
    probe->ready = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (probe->stop && probe->ready) *worker = CreateThread(NULL, 0, watch_route, probe, 0, NULL);
    if (!*worker) {
        close_route_probe(probe, NULL);
        return NULL;
    }
    WaitForSingleObject(probe->ready, 350);
    return probe;
}

int wmain(int argc, wchar_t **argv) {
    if (argc < 2) return 2;
    if (!wcscmp(argv[1], L"browser-pages")) {
        if (_setmode(_fileno(stdout), _O_BINARY) < 0) return 2;
        return jaro_browser_pages(argc - 2, argv + 2);
    }
    bool list = argc == 2 && wcscmp(argv[1], L"microphones") == 0;
    bool users = argc == 2 && wcscmp(argv[1], L"microphone-users") == 0;
    bool playback = argc == 2 && wcscmp(argv[1], L"playback-users") == 0;
    bool dictation = wcscmp(argv[1], L"dictation") == 0;
    bool write_clock = dictation || (argc == 4 && wcscmp(argv[3], L"clock") == 0);
    bool mic = dictation || wcscmp(argv[1], L"mic") == 0;
    bool loopback = wcscmp(argv[1], L"system") == 0;
    bool meeting = wcscmp(argv[1], L"meeting") == 0;
    bool dual = meeting || wcscmp(argv[1], L"both") == 0;
    bool call = meeting && argc >= 5 && argc <= 16 && wcscmp(argv[3], L"call") == 0;
    const wchar_t *microphone = argc == 5 && mic && wcscmp(argv[3], L"device") == 0 && argv[4][0] ? argv[4] : NULL;
    long seconds = 0;
    if (!list && !users && !playback) {
        if ((!mic && !loopback && !dual) ||
            (argc != 3 && !(meeting ? call : microphone || (!dictation && write_clock)))) return 2;
        wchar_t *end;
        seconds = wcstol(argv[2], &end, 10);
        if (*end || seconds < 1 || seconds > MAX_SECONDS) return 2;
    }
    if (_setmode(_fileno(stdout), _O_BINARY) < 0) return 2;
    HRESULT result = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    if (SUCCEEDED(result)) {
        IMMDeviceEnumerator *enumerator = NULL;
        result = CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL,
                                  &IID_IMMDeviceEnumerator, (void **)&enumerator);
        if (SUCCEEDED(result)) {
            if (list) {
                result = list_microphones(enumerator);
            } else if (users || playback) {
                result = visit_endpoints(enumerator, playback ? eRender : eCapture, report_endpoint_users, NULL);
                if (fflush(stdout) == EOF) result = E_FAIL;
            } else {
                HANDLE control = CreateThread(NULL, 0, wait_for_stop, GetStdHandle(STD_INPUT_HANDLE), 0, NULL);
                if (control) {
                    HANDLE route_worker = NULL;
                    RouteProbe *probe = call ? start_route_probe(argc - 4, argv + 4, &route_worker) : NULL;
                    result = capture(enumerator, loopback, dual, (int64_t)seconds * CAPTURE_RATE, control,
                                     write_clock, microphone, meeting, probe, dictation);
                    close_route_probe(probe, route_worker);
                    CancelSynchronousIo(control);
                    CloseHandle(control);
                } else {
                    result = HRESULT_FROM_WIN32(GetLastError());
                }
            }
            IMMDeviceEnumerator_Release(enumerator);
        }
        CoUninitialize();
    }
    if (FAILED(result)) fprintf(stderr, "WASAPI capture failed (0x%08lx).\n", (unsigned long)(uint32_t)result);
    return FAILED(result) ? 1 : 0;
}
