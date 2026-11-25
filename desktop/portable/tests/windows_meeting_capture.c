#undef NDEBUG
#define wmain capture_main
#include "../windows_capture.c"
#undef wmain

#include <assert.h>

typedef struct {
    IMMDevice device;
    IAudioClient client;
    const wchar_t *id;
    HRESULT start_result;
    unsigned open_count, stop_count;
} FixtureEndpoint;

static HRESULT packet_status = S_OK;

static HRESULT STDMETHODCALLTYPE next_packet(IAudioCaptureClient *capture, UINT32 *frames) {
    (void)capture;
    *frames = 0;
    return packet_status;
}

static ULONG STDMETHODCALLTYPE release_capture(IAudioCaptureClient *capture) {
    (void)capture;
    return 1;
}

static IAudioCaptureClientVtbl capture_methods = {
    .GetNextPacketSize = next_packet, .Release = release_capture,
};
static IAudioCaptureClient packet_source = {&capture_methods};

static HRESULT STDMETHODCALLTYPE initialize(IAudioClient *client, AUDCLNT_SHAREMODE mode, DWORD flags,
                                            REFERENCE_TIME duration, REFERENCE_TIME period,
                                            const WAVEFORMATEX *format, LPCGUID guid) {
    (void)client; (void)mode; (void)flags; (void)duration; (void)period; (void)format; (void)guid;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE get_service(IAudioClient *client, REFIID iid, void **service) {
    (void)client; (void)iid;
    *service = &packet_source;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE release_client(IAudioClient *client) {
    (void)client;
    return 1;
}

static HRESULT STDMETHODCALLTYPE start_client(IAudioClient *client) {
    return CONTAINING_RECORD(client, FixtureEndpoint, client)->start_result;
}

static HRESULT STDMETHODCALLTYPE stop_client(IAudioClient *client) {
    ++CONTAINING_RECORD(client, FixtureEndpoint, client)->stop_count;
    return S_OK;
}

static IAudioClientVtbl client_methods = {
    .Initialize = initialize, .GetService = get_service, .Release = release_client,
    .Start = start_client, .Stop = stop_client,
};

static HRESULT STDMETHODCALLTYPE activate(IMMDevice *device, REFIID iid, DWORD context,
                                           PROPVARIANT *options, void **value) {
    (void)iid; (void)context; (void)options;
    FixtureEndpoint *endpoint = CONTAINING_RECORD(device, FixtureEndpoint, device);
    ++endpoint->open_count;
    *value = &endpoint->client;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE device_id(IMMDevice *device, LPWSTR *id) {
    const wchar_t *value = CONTAINING_RECORD(device, FixtureEndpoint, device)->id;
    size_t size = (wcslen(value) + 1) * sizeof(*value);
    *id = CoTaskMemAlloc(size);
    if (!*id) return E_OUTOFMEMORY;
    memcpy(*id, value, size);
    return S_OK;
}

static ULONG STDMETHODCALLTYPE release_device(IMMDevice *device) {
    (void)device;
    return 1;
}

static IMMDeviceVtbl device_methods = {.Activate = activate, .GetId = device_id, .Release = release_device};
static FixtureEndpoint internal = {{&device_methods}, {&client_methods}, L"Internal", E_ACCESSDENIED, 0, 0};
static FixtureEndpoint headset = {{&device_methods}, {&client_methods}, L"Headset", E_ACCESSDENIED, 0, 0};
static FixtureEndpoint playback = {{&device_methods}, {&client_methods}, L"Playback", S_OK, 0, 0};
static FixtureEndpoint *default_mic = &internal;

static HRESULT STDMETHODCALLTYPE endpoint_count(IMMDeviceCollection *collection, UINT *count) {
    (void)collection;
    *count = 1;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE endpoint_item(IMMDeviceCollection *collection, UINT index, IMMDevice **device) {
    (void)collection;
    assert(index == 0);
    *device = &playback.device;
    return S_OK;
}

static ULONG STDMETHODCALLTYPE release_collection(IMMDeviceCollection *collection) {
    (void)collection;
    return 1;
}

static IMMDeviceCollectionVtbl collection_methods = {
    .GetCount = endpoint_count, .Item = endpoint_item, .Release = release_collection,
};
static IMMDeviceCollection endpoints = {&collection_methods};

static HRESULT STDMETHODCALLTYPE enum_endpoints(IMMDeviceEnumerator *enumerator, EDataFlow flow, DWORD mask,
                                                IMMDeviceCollection **collection) {
    (void)enumerator; (void)mask;
    assert(flow == eRender);
    *collection = &endpoints;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE default_endpoint(IMMDeviceEnumerator *enumerator, EDataFlow direction,
                                                  ERole role, IMMDevice **device) {
    (void)enumerator;
    assert(role == eMultimedia);
    if (direction == eCapture && !default_mic) return E_NOTFOUND;
    *device = direction == eCapture ? &default_mic->device : &playback.device;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE get_endpoint(IMMDeviceEnumerator *enumerator, LPCWSTR id, IMMDevice **device) {
    (void)enumerator;
    assert(!wcscmp(id, playback.id));
    *device = &playback.device;
    return S_OK;
}

static IMMDeviceEnumeratorVtbl enumerator_methods = {
    .EnumAudioEndpoints = enum_endpoints, .GetDefaultAudioEndpoint = default_endpoint, .GetDevice = get_endpoint,
};

int jaro_browser_pages(int count, wchar_t **binaries) {
    (void)count; (void)binaries;
    return 0;
}

int jaro_foreground_page(const wchar_t *window, const wchar_t *process) {
    (void)window; (void)process;
    return 0;
}

static void test_screen_input_reconnect(IMMDeviceEnumerator *enumerator) {
    Input mic = {0}, system = {0};
    ScreenInput screen_mic = {.input = &mic};
    ScreenInput screen_system = {.input = &system, .loopback = true};
    default_mic = &internal;
    assert(SUCCEEDED(open_screen_input(enumerator, &screen_mic)));
    assert(SUCCEEDED(open_screen_input(enumerator, &screen_system)));

    mic.timeline.samples[17] = 523;
    mic.timeline.has_packet = true;
    unsigned system_stops = playback.stop_count;
    packet_status = AUDCLNT_E_DEVICE_INVALIDATED;
    read_screen_input(enumerator, &screen_mic, 0, 0, 50, false);
    assert(!mic.started && capture_take(&mic.timeline, 17) == 523);
    packet_status = S_OK;
    read_screen_input(enumerator, &screen_system, 0, 0, 50, false);
    assert(system.started && playback.stop_count == system_stops);

    unsigned microphone_opens = internal.open_count;
    read_screen_input(enumerator, &screen_mic, 0, 0, 51, false);
    read_screen_input(enumerator, &screen_mic, 0, 0, 50 + CAPTURE_RATE, true);
    assert(!mic.started && internal.open_count == microphone_opens);
    read_screen_input(enumerator, &screen_mic, 0, 0, 50 + CAPTURE_RATE, false);
    assert(mic.started && !mic.timeline.has_packet && internal.open_count == microphone_opens + 1);
    assert(system.started && playback.stop_count == system_stops);
    close_input(&mic);
    close_input(&system);
    puts("Windows screen audio held its playback stream open while the microphone reconnected.");
}

int main(void) {
    IMMDeviceEnumerator enumerator = {&enumerator_methods};
    HANDLE control = CreateEventW(NULL, TRUE, FALSE, NULL);
    assert(control);
    assert(capture(&enumerator, false, true, 0, control, false, NULL, true, NULL, false) == S_OK);
    assert(playback.open_count == 1 && playback.stop_count == 1);
    assert(capture(&enumerator, false, false, 0, control, false, NULL, false, NULL, false) == E_ACCESSDENIED);
    default_mic = NULL;
    assert(capture(&enumerator, false, true, 0, control, false, NULL, true, NULL, false) == S_OK);
    playback.start_result = E_ACCESSDENIED;
    assert(FAILED(capture(&enumerator, false, true, 0, control, false, NULL, true, NULL, false)));
    default_mic = &internal;
    internal.start_result = S_OK;
    assert(capture(&enumerator, false, true, 0, control, false, NULL, true, NULL, false) == S_OK);
    internal.stop_count = 0;
    playback.start_result = S_OK;
    CloseHandle(control);

    Input mic = {0};
    wchar_t *current = NULL;
    assert(refresh_meeting_microphone(&enumerator, &mic, &current));
    assert(!refresh_meeting_microphone(&enumerator, &mic, &current));
    assert(mic.started && !wcscmp(current, internal.id));
    mic.timeline.samples[17] = 523;
    mic.timeline.has_packet = true;
    default_mic = &headset;
    assert(!refresh_meeting_microphone(&enumerator, &mic, &current));
    assert(mic.client == &internal.client && mic.timeline.has_packet && internal.stop_count == 0);
    headset.start_result = S_OK;
    assert(refresh_meeting_microphone(&enumerator, &mic, &current));
    assert(mic.started && !wcscmp(current, headset.id) && internal.stop_count == 1);
    assert(!mic.timeline.has_packet && capture_take(&mic.timeline, 17) == 523);
    close_input(&mic);
    CoTaskMemFree(current);
    puts("Windows meeting microphone fallback and make-before-break handoff passed.");
    test_screen_input_reconnect(&enumerator);
    return 0;
}
