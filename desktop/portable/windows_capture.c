#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <mmdeviceapi.h>
#include <audioclient.h>

#include <fcntl.h>
#include <io.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "capture_timeline.h"

enum { OUTPUT_BLOCK = CAPTURE_RATE / 50, CAPTURE_LAG = CAPTURE_RATE / 5, MAX_SECONDS = 3 * 3600 };

typedef struct {
    IAudioClient *client;
    IAudioCaptureClient *capture;
    CaptureTimeline timeline;
    bool started;
} Input;

static HRESULT open_input(IMMDeviceEnumerator *enumerator, Input *input, bool loopback) {
    IMMDevice *device = NULL;
    HRESULT result = IMMDeviceEnumerator_GetDefaultAudioEndpoint(
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

static bool write_frames(Input *mic, Input *system, int64_t *written, int64_t target, bool loopback, bool dual) {
    int16_t samples[OUTPUT_BLOCK * 2];
    while (*written < target) {
        int64_t count = target - *written;
        if (count > OUTPUT_BLOCK) count = OUTPUT_BLOCK;
        size_t length = 0;
        for (int64_t frame = *written; frame < *written + count; ++frame) {
            samples[length++] = capture_take(loopback ? &system->timeline : &mic->timeline, frame);
            if (dual) samples[length++] = capture_take(&system->timeline, frame);
        }
        if (fwrite(samples, sizeof(samples[0]), length, stdout) != length || fflush(stdout) != 0) return false;
        *written += count;
    }
    return true;
}

static HRESULT capture(bool loopback, bool dual, int64_t limit, HANDLE control) {
    Input mic = {0}, system = {0};
    IMMDeviceEnumerator *enumerator = NULL;
    HRESULT result = CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL,
                                     &IID_IMMDeviceEnumerator, (void **)&enumerator);
    if (FAILED(result)) return result;
    if (!loopback) result = open_input(enumerator, &mic, false);
    if (SUCCEEDED(result) && (dual || loopback)) result = open_input(enumerator, &system, true);
    IMMDeviceEnumerator_Release(enumerator);

    LARGE_INTEGER frequency;
    if (!QueryPerformanceFrequency(&frequency)) result = E_FAIL;
    int64_t start = 0;
    if (SUCCEEDED(result)) {
        start = tick_time(frequency.QuadPart);
        if (mic.client) {
            result = IAudioClient_Start(mic.client);
            mic.started = SUCCEEDED(result);
        }
        if (SUCCEEDED(result) && system.client) {
            result = IAudioClient_Start(system.client);
            system.started = SUCCEEDED(result);
        }
    }

    int64_t written = 0;
    while (SUCCEEDED(result) && written < limit) {
        int64_t elapsed = capture_frame(tick_time(frequency.QuadPart) - start);
        if (WaitForSingleObject(control, 0) == WAIT_OBJECT_0 && elapsed < limit) limit = elapsed;
        result = read_input(&mic, written, start, elapsed);
        if (SUCCEEDED(result)) result = read_input(&system, written, start, elapsed);
        if (FAILED(result)) break;
        // Let both devices deliver packets before filling gaps with silence.
        int64_t target = elapsed - CAPTURE_LAG;
        if (target > limit) target = limit;
        if (!write_frames(&mic, &system, &written, target, loopback, dual)) break;
        if (written < limit) Sleep(5);
    }
    close_input(&system);
    close_input(&mic);
    return result;
}

int main(int argc, char **argv) {
    if (argc != 3 || (strcmp(argv[1], "mic") && strcmp(argv[1], "system") && strcmp(argv[1], "both"))) return 2;
    char *end;
    long seconds = strtol(argv[2], &end, 10);
    if (*end || seconds < 1 || seconds > MAX_SECONDS || _setmode(_fileno(stdout), _O_BINARY) < 0) return 2;
    HRESULT result = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    if (SUCCEEDED(result)) {
        HANDLE control = CreateThread(NULL, 0, wait_for_stop, GetStdHandle(STD_INPUT_HANDLE), 0, NULL);
        if (control) {
            result = capture(strcmp(argv[1], "system") == 0, strcmp(argv[1], "both") == 0,
                             (int64_t)seconds * CAPTURE_RATE, control);
            CancelSynchronousIo(control);
            CloseHandle(control);
        } else {
            result = HRESULT_FROM_WIN32(GetLastError());
        }
        CoUninitialize();
    }
    if (FAILED(result)) fprintf(stderr, "WASAPI capture failed (0x%08lx).\n", (unsigned long)(uint32_t)result);
    return FAILED(result) ? 1 : 0;
}
