#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initguid.h>
#include <d3d11.h>
#include <dwmapi.h>
#include <roapi.h>
#include <wincodec.h>
#include <winstring.h>

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <io.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "windows_screen_abi.h"

enum { FRAME_RATE = 24, MAX_WIDTH = 1920, MAX_HEIGHT = 1080, POOL_BUFFERS = 2, MAX_TARGETS = 100 };
static const DXGI_FORMAT pixel_format = DXGI_FORMAT_B8G8R8A8_UNORM;
static HRESULT (WINAPI *get_factory)(HSTRING, REFIID, void **);
static HRESULT (WINAPI *make_string)(PCWSTR, UINT32, HSTRING_HEADER *, HSTRING *);

#define RELEASE(pointer) do { if (pointer) IUnknown_Release((IUnknown *)(pointer)); } while (0)

typedef struct {
    uintptr_t handle;
    DWORD process;
    bool window;
} Target;

typedef struct {
    UINT x, y, width, height;
    CaptureSize frame;
} CaptureRegion;

typedef struct {
    ID3D11Device *device;
    ID3D11DeviceContext *context;
    IInspectable *graphics;
    CapturePool *pool;
    CaptureSession *session;
    ID3D11Texture2D *staging;
    CaptureSize pool_size;
    CaptureSize initial_size;
    CaptureSize output;
    const CaptureRegion *region;
    bool allow_resize;
    BYTE *pixels;
    INT64 first_frame;
} Capture;

static bool window_target(HWND window, DWORD *process) {
    LONG_PTR style = GetWindowLongPtrW(window, GWL_EXSTYLE);
    DWORD cloaked = 0, affinity = 0;
    if (!IsWindowVisible(window) || IsIconic(window) || (style & WS_EX_TOOLWINDOW) ||
            (GetWindow(window, GW_OWNER) && !(style & WS_EX_APPWINDOW))) return false;
    if (SUCCEEDED(DwmGetWindowAttribute(window, DWMWA_CLOAKED, &cloaked, sizeof(cloaked))) && cloaked)
        return false;
    if (GetWindowDisplayAffinity(window, &affinity) && affinity != WDA_NONE) return false;
    return GetWindowThreadProcessId(window, process) != 0;
}

static bool target_available(Target target) {
    if (target.window) {
        DWORD process = 0;
        return window_target((HWND)target.handle, &process) && process == target.process;
    }
    MONITORINFO info = {.cbSize = sizeof(info)};
    return GetMonitorInfoW((HMONITOR)target.handle, &info);
}

static bool emit_target(uintptr_t handle, DWORD process, WCHAR *name, unsigned *count) {
    char utf8[1024];
    if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, name, -1, utf8, sizeof(utf8), NULL, NULL)) {
        for (char *c = utf8; *c; ++c) if ((unsigned char)*c < 32) *c = ' ';
        printf("%" PRIxPTR "\t%lu\t%s\n", handle, (unsigned long)process, utf8);
        ++*count;
    }
    return *count < MAX_TARGETS;
}

static BOOL CALLBACK list_window(HWND window, LPARAM state) {
    DWORD process = 0;
    WCHAR title[256];
    if (!window_target(window, &process) || !GetWindowTextW(window, title, ARRAYSIZE(title))) return TRUE;
    return emit_target((uintptr_t)window, process, title, (unsigned *)state);
}

static BOOL CALLBACK list_monitor(HMONITOR monitor, HDC dc, LPRECT rect, LPARAM state) {
    (void)dc;
    (void)rect;
    MONITORINFOEXW info = {.cbSize = sizeof(info)};
    if (!GetMonitorInfoW(monitor, (MONITORINFO *)&info)) return TRUE;
    return emit_target((uintptr_t)monitor, 0, info.szDevice, (unsigned *)state);
}

static HRESULT activation_factory(const WCHAR *name, REFIID iid, void **result) {
    HSTRING_HEADER header;
    HSTRING value;
    HRESULT hr = make_string(name, (UINT32)wcslen(name), &header, &value);
    return FAILED(hr) ? hr : get_factory(value, iid, result);
}

static BOOL capture_supported(void) {
    CaptureSessionStatics *statics = NULL;
    boolean supported = FALSE;
    if (SUCCEEDED(activation_factory(L"Windows.Graphics.Capture.GraphicsCaptureSession",
                                    &session_statics_id, (void **)&statics))) {
        if (FAILED(statics->lpVtbl->IsSupported(statics, &supported))) supported = FALSE;
        RELEASE(statics);
    }
    return supported;
}

static void close_capture_object(IUnknown *object) {
    CaptureClosable *closable = NULL;
    if (!object) return;
    if (SUCCEEDED(IUnknown_QueryInterface(object, &closable_id, (void **)&closable))) {
        closable->lpVtbl->Close(closable);
        RELEASE(closable);
    }
    IUnknown_Release(object);
}

static void close_capture(Capture *capture) {
    close_capture_object((IUnknown *)capture->session);
    close_capture_object((IUnknown *)capture->pool);
    RELEASE(capture->staging);
    RELEASE(capture->graphics);
    RELEASE(capture->context);
    RELEASE(capture->device);
    free(capture->pixels);
}

static bool valid_size(CaptureSize size) {
    return size.width >= 2 && size.height >= 2 && size.width <= 16384 && size.height <= 16384;
}

static bool same_size(CaptureSize a, CaptureSize b) {
    return a.width == b.width && a.height == b.height;
}

static bool valid_region(const CaptureRegion *region, CaptureSize frame) {
    return same_size(region->frame, frame) && region->width >= 2 && region->height >= 2
        && region->x < (UINT)frame.width && region->y < (UINT)frame.height
        && region->width <= (UINT)frame.width - region->x && region->height <= (UINT)frame.height - region->y;
}

static CaptureSize fit(CaptureSize source, CaptureSize bounds) {
    int width = min(source.width, bounds.width);
    int height = min(source.height, bounds.height);
    if (width * source.height > height * source.width) width = height * source.width / source.height;
    else height = width * source.height / source.width;
    return (CaptureSize){max(2, width & ~1), max(2, height & ~1)};
}

static HRESULT start_capture(Capture *capture, Target target) {
    CaptureInterop *interop = NULL;
    CaptureItem *item = NULL;
    CapturePoolFactory *factory = NULL;
    IDXGIDevice *dxgi = NULL;
    HRESULT hr = activation_factory(L"Windows.Graphics.Capture.GraphicsCaptureItem",
                                    &interop_id, (void **)&interop);
    if (SUCCEEDED(hr))
        hr = target.window
            ? interop->lpVtbl->CreateForWindow(interop, (HWND)target.handle, &item_id, (void **)&item)
            : interop->lpVtbl->CreateForMonitor(interop, (HMONITOR)target.handle, &item_id, (void **)&item);
    if (SUCCEEDED(hr)) hr = item->lpVtbl->get_Size(item, &capture->pool_size);
    if (SUCCEEDED(hr) && (!valid_size(capture->pool_size)
            || (capture->region && !valid_region(capture->region, capture->pool_size)))) hr = E_INVALIDARG;
    if (SUCCEEDED(hr)) hr = D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        NULL, 0, D3D11_SDK_VERSION, &capture->device, NULL, &capture->context);
    if (SUCCEEDED(hr)) hr = ID3D11Device_QueryInterface(capture->device, &IID_IDXGIDevice, (void **)&dxgi);
    if (SUCCEEDED(hr)) {
        HRESULT (WINAPI *create_device)(IDXGIDevice *, IInspectable **) = (void *)GetProcAddress(
            GetModuleHandleW(L"d3d11.dll"), "CreateDirect3D11DeviceFromDXGIDevice");
        hr = create_device ? create_device(dxgi, &capture->graphics) : E_NOTIMPL;
    }
    if (SUCCEEDED(hr)) hr = activation_factory(L"Windows.Graphics.Capture.Direct3D11CaptureFramePool",
                                             &pool_factory_id, (void **)&factory);
    if (SUCCEEDED(hr)) hr = factory->lpVtbl->CreateFreeThreaded(factory, capture->graphics, pixel_format,
                                                            POOL_BUFFERS, capture->pool_size, &capture->pool);
    if (SUCCEEDED(hr)) hr = capture->pool->lpVtbl->CreateCaptureSession(capture->pool, item, &capture->session);
    if (SUCCEEDED(hr)) {
        capture->initial_size = capture->region
            ? (CaptureSize){(INT32)capture->region->width, (INT32)capture->region->height}
            : capture->pool_size;
        capture->output = fit(capture->initial_size, (CaptureSize){MAX_WIDTH, MAX_HEIGHT});
        capture->pixels = calloc((size_t)capture->output.width * capture->output.height, 4);
        hr = capture->pixels ? capture->session->lpVtbl->StartCapture(capture->session) : E_OUTOFMEMORY;
    }
    RELEASE(dxgi);
    RELEASE(factory);
    RELEASE(item);
    RELEASE(interop);
    return hr;
}

static HRESULT staging_texture(Capture *capture, UINT width, UINT height) {
    if (capture->staging) {
        D3D11_TEXTURE2D_DESC existing;
        ID3D11Texture2D_GetDesc(capture->staging, &existing);
        if (existing.Width == width && existing.Height == height) return S_OK;
        RELEASE(capture->staging);
        capture->staging = NULL;
    }
    D3D11_TEXTURE2D_DESC desc = {
        .Width = width, .Height = height, .MipLevels = 1, .ArraySize = 1,
        .Format = pixel_format, .SampleDesc.Count = 1, .Usage = D3D11_USAGE_STAGING,
        .CPUAccessFlags = D3D11_CPU_ACCESS_READ,
    };
    return ID3D11Device_CreateTexture2D(capture->device, &desc, NULL, &capture->staging);
}

static void copy_pixels(Capture *capture, const D3D11_MAPPED_SUBRESOURCE *input, CaptureSize source) {
    CaptureSize out = capture->output;
    CaptureSize image = same_size(source, capture->initial_size) ? out : fit(source, out);
    size_t pitch = (size_t)out.width * 4;
    if (image.width < out.width || image.height < out.height)
        memset(capture->pixels, 0, pitch * out.height);
    BYTE *picture = capture->pixels + (out.height - image.height) / 2 * pitch + (out.width - image.width) * 2;
    for (int y = 0; y < image.height; ++y) {
        const BYTE *row = (const BYTE *)input->pData + (size_t)(y * source.height / image.height) * input->RowPitch;
        BYTE *line = picture + (size_t)y * pitch;
        if (image.width == source.width) {
            memcpy(line, row, (size_t)image.width * 4);
        } else {
            for (int x = 0; x < image.width; ++x)
                memcpy(line + x * 4, row + (x * source.width / image.width) * 4, 4);
        }
    }
}

static HRESULT map_frame(Capture *capture, ID3D11Texture2D *texture, CaptureSize size) {
    const CaptureRegion *region = capture->region;
    D3D11_TEXTURE2D_DESC desc;
    ID3D11Texture2D_GetDesc(texture, &desc);
    if ((UINT)size.width > desc.Width || (UINT)size.height > desc.Height) return E_UNEXPECTED;

    UINT x = region ? region->x : 0, y = region ? region->y : 0;
    UINT width = region ? region->width : (UINT)size.width;
    UINT height = region ? region->height : (UINT)size.height;
    HRESULT hr = staging_texture(capture, width, height);
    if (FAILED(hr)) return hr;
    D3D11_BOX box = {.left = x, .top = y, .right = x + width, .bottom = y + height, .back = 1};
    ID3D11DeviceContext_CopySubresourceRegion(capture->context, (ID3D11Resource *)capture->staging,
        0, 0, 0, 0, (ID3D11Resource *)texture, 0, &box);

    D3D11_MAPPED_SUBRESOURCE mapped;
    hr = ID3D11DeviceContext_Map(capture->context, (ID3D11Resource *)capture->staging, 0, D3D11_MAP_READ, 0, &mapped);
    if (SUCCEEDED(hr)) {
        copy_pixels(capture, &mapped, (CaptureSize){(INT32)width, (INT32)height});
        ID3D11DeviceContext_Unmap(capture->context, (ID3D11Resource *)capture->staging, 0);
    }
    return hr;
}

static HRESULT read_frame(Capture *capture) {
    CaptureFrame *frame = NULL;
    HRESULT hr = capture->pool->lpVtbl->TryGetNextFrame(capture->pool, &frame);
    if (FAILED(hr) || !frame) return FAILED(hr) ? hr : S_FALSE;
    CaptureSize pixels;
    hr = frame->lpVtbl->get_ContentSize(frame, &pixels);
    if (FAILED(hr) || !valid_size(pixels)) {
        close_capture_object((IUnknown *)frame);
        return FAILED(hr) ? hr : S_FALSE;
    }
    if (!same_size(pixels, capture->pool_size)) {
        close_capture_object((IUnknown *)frame);
        // A region is bound to the geometry in the approved preview.
        if (!capture->allow_resize) return HRESULT_FROM_WIN32(ERROR_INVALID_DATA);
        hr = capture->pool->lpVtbl->Recreate(capture->pool, capture->graphics, pixel_format, POOL_BUFFERS, pixels);
        if (SUCCEEDED(hr)) capture->pool_size = pixels;
        return FAILED(hr) ? hr : S_FALSE;
    }

    IInspectable *surface = NULL;
    SurfaceAccess *access = NULL;
    ID3D11Texture2D *texture = NULL;
    if (!capture->first_frame) hr = frame->lpVtbl->get_SystemRelativeTime(frame, &capture->first_frame);
    if (SUCCEEDED(hr)) hr = frame->lpVtbl->get_Surface(frame, &surface);
    if (SUCCEEDED(hr)) hr = IInspectable_QueryInterface(surface, &surface_access_id, (void **)&access);
    if (SUCCEEDED(hr)) hr = access->lpVtbl->GetInterface(access, &IID_ID3D11Texture2D, (void **)&texture);
    if (SUCCEEDED(hr)) hr = map_frame(capture, texture, pixels);
    RELEASE(texture);
    RELEASE(access);
    RELEASE(surface);
    close_capture_object((IUnknown *)frame);
    return hr;
}

static DWORD WINAPI wait_for_stop(void *context) {
    char byte;
    DWORD length;
    ReadFile(context, &byte, sizeof(byte), &length, NULL);
    return 0;
}

static HRESULT first_picture(Capture *capture, Target target, HANDLE stop) {
    if (!target_available(target)) return HRESULT_FROM_WIN32(ERROR_NOT_FOUND);
    capture->allow_resize = target.window;
    HRESULT hr = start_capture(capture, target);
    ULONGLONG first_deadline = GetTickCount64() + 7000;
    while (SUCCEEDED(hr)) {
        hr = read_frame(capture);
        if (hr != S_FALSE) break;
        if (stop) {
            if (WaitForSingleObject(stop, 10) == WAIT_OBJECT_0) break;
        } else {
            Sleep(10);
        }
        if (GetTickCount64() >= first_deadline) hr = HRESULT_FROM_WIN32(ERROR_TIMEOUT);
    }
    return hr;
}

static HRESULT write_snapshot(const Capture *capture) {
    IWICImagingFactory *factory = NULL;
    IWICBitmapEncoder *encoder = NULL;
    IWICBitmapFrameEncode *frame = NULL;
    IStream *stream = NULL;
    HRESULT hr = CoCreateInstance(&CLSID_WICImagingFactory, NULL, CLSCTX_INPROC_SERVER,
                                 &IID_IWICImagingFactory, (void **)&factory);
    if (SUCCEEDED(hr)) hr = CreateStreamOnHGlobal(NULL, TRUE, &stream);
    if (SUCCEEDED(hr)) hr = IWICImagingFactory_CreateEncoder(factory, &GUID_ContainerFormatJpeg, NULL, &encoder);
    if (SUCCEEDED(hr)) hr = IWICBitmapEncoder_Initialize(encoder, stream, WICBitmapEncoderNoCache);
    if (SUCCEEDED(hr)) hr = IWICBitmapEncoder_CreateNewFrame(encoder, &frame, NULL);
    if (SUCCEEDED(hr)) hr = IWICBitmapFrameEncode_Initialize(frame, NULL);
    UINT width = (UINT)capture->output.width, height = (UINT)capture->output.height;
    if (SUCCEEDED(hr)) hr = IWICBitmapFrameEncode_SetSize(frame, width, height);
    WICPixelFormatGUID format = GUID_WICPixelFormat24bppBGR;
    if (SUCCEEDED(hr)) hr = IWICBitmapFrameEncode_SetPixelFormat(frame, &format);
    if (SUCCEEDED(hr) && !IsEqualGUID(&format, &GUID_WICPixelFormat24bppBGR)) hr = E_UNEXPECTED;
    BYTE row[MAX_WIDTH * 3];
    for (UINT y = 0; SUCCEEDED(hr) && y < height; ++y) {
        const BYTE *pixels = capture->pixels + y * width * 4;
        for (UINT x = 0; x < width; ++x) memcpy(row + x * 3, pixels + x * 4, 3);
        hr = IWICBitmapFrameEncode_WritePixels(frame, 1, width * 3, width * 3, row);
    }
    if (SUCCEEDED(hr)) hr = IWICBitmapFrameEncode_Commit(frame);
    if (SUCCEEDED(hr)) hr = IWICBitmapEncoder_Commit(encoder);
    STATSTG info;
    HGLOBAL memory = NULL;
    if (SUCCEEDED(hr)) hr = IStream_Stat(stream, &info, STATFLAG_NONAME);
    if (SUCCEEDED(hr)) hr = GetHGlobalFromStream(stream, &memory);
    if (SUCCEEDED(hr)) {
        const void *jpeg = GlobalLock(memory);
        UINT32 dimensions[] = {width, height, (UINT32)capture->pool_size.width, (UINT32)capture->pool_size.height};
        if (!jpeg) hr = E_OUTOFMEMORY;
        else {
            if (fwrite(dimensions, sizeof(dimensions), 1, stdout) != 1
                    || fwrite(jpeg, 1, (size_t)info.cbSize.QuadPart, stdout) != info.cbSize.QuadPart
                    || fflush(stdout)) hr = E_FAIL;
            GlobalUnlock(memory);
        }
    }
    RELEASE(frame);
    RELEASE(encoder);
    RELEASE(stream);
    RELEASE(factory);
    return hr;
}

static HRESULT snapshot(Target target) {
    Capture capture = {0};
    HRESULT hr = first_picture(&capture, target, NULL);
    if (hr == S_OK) hr = write_snapshot(&capture);
    close_capture(&capture);
    return hr;
}

static HRESULT record(Target target, HANDLE stop, const CaptureRegion *region) {
    Capture capture = {.region = region};
    HRESULT hr = first_picture(&capture, target, stop);
    if (hr == S_OK) {
        UINT32 dimensions[] = {(UINT32)capture.output.width, (UINT32)capture.output.height};
        size_t bytes = (size_t)dimensions[0] * dimensions[1] * 4;
        ULONGLONG start = GetTickCount64();
        size_t frames = 0;
        if (fwrite(dimensions, sizeof(dimensions), 1, stdout) != 1
                || fwrite(&capture.first_frame, sizeof(capture.first_frame), 1, stdout) != 1) hr = E_FAIL;
        while (SUCCEEDED(hr) && target_available(target)) {
            if (fwrite(capture.pixels, 1, bytes, stdout) != bytes || fflush(stdout)) break;
            ULONGLONG due = start + (++frames * 1000) / FRAME_RATE;
            ULONGLONG now = GetTickCount64();
            if (WaitForSingleObject(stop, due > now ? (DWORD)(due - now) : 0) == WAIT_OBJECT_0) break;
            hr = read_frame(&capture);
        }
    }
    close_capture(&capture);
    return hr;
}

static bool parse_region(char *const *args, CaptureRegion *region) {
    UINT values[6];
    for (unsigned i = 0; i < ARRAYSIZE(values); ++i) {
        char *end = NULL;
        errno = 0;
        unsigned long value = strtoul(args[i], &end, 10);
        if (errno || end == args[i] || *end || value > 16384) return false;
        values[i] = (UINT)value;
    }
    CaptureSize frame = {(INT32)values[4], (INT32)values[5]};
    *region = (CaptureRegion){values[0], values[1], values[2], values[3], frame};
    return valid_size(frame) && valid_region(region, frame);
}

int draw_desktop(HMONITOR monitor, RECT crop, SIZE capture_size);

int main(int argc, char **argv) {
    if (argc == 3 && !strcmp(argv[1], "targets")) {
        unsigned count = 0;
        if (!strcmp(argv[2], "window")) EnumWindows(list_window, (LPARAM)&count);
        else if (!strcmp(argv[2], "display")) EnumDisplayMonitors(NULL, NULL, list_monitor, (LPARAM)&count);
        else return 2;
        return fflush(stdout) == EOF ? 1 : 0;
    }
    bool probe = argc == 2 && !strcmp(argv[1], "probe");
    bool still = argc == 5 && !strcmp(argv[1], "snapshot");
    bool pen = argc > 1 && !strcmp(argv[1], "pen");
    CaptureRegion crop;
    const CaptureRegion *region = NULL;
    Target target = {0};
    if (!probe) {
        if ((!still && ((argc != 5 && argc != 11) || (!pen && strcmp(argv[1], "capture"))))
                || (strcmp(argv[2], "window") && strcmp(argv[2], "display"))) return 2;
        target.window = !strcmp(argv[2], "window");
        char *end = NULL;
        errno = 0;
        target.handle = (uintptr_t)strtoull(argv[3], &end, 16);
        if (errno || *end || !target.handle) return 2;
        unsigned long process = strtoul(argv[4], &end, 10);
        if (errno || *end || (target.window != (process != 0)) || _setmode(_fileno(stdout), _O_BINARY) < 0) return 2;
        target.process = (DWORD)process;
        if (argc == 11) {
            if (target.window || !parse_region(argv + 5, &crop)) return 2;
            region = &crop;
        }
    }
    if (pen) {
        if (target.window) return 2;
        RECT bounds = {0};
        SIZE canvas = {0};
        if (region) {
            bounds = (RECT){crop.x, crop.y, crop.x + crop.width, crop.y + crop.height};
            canvas = (SIZE){crop.frame.width, crop.frame.height};
        }
        return draw_desktop((HMONITOR)target.handle, bounds, canvas);
    }

    HMODULE combase = LoadLibraryExW(L"combase.dll", NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!combase) return 1;
    HRESULT (WINAPI *initialize)(RO_INIT_TYPE) = (void *)GetProcAddress(combase, "RoInitialize");
    void (WINAPI *uninitialize)(void) = (void *)GetProcAddress(combase, "RoUninitialize");
    get_factory = (void *)GetProcAddress(combase, "RoGetActivationFactory");
    make_string = (void *)GetProcAddress(combase, "WindowsCreateStringReference");
    if (!initialize || !uninitialize || !get_factory || !make_string) return 1;
    HRESULT hr = initialize(RO_INIT_MULTITHREADED);
    if (FAILED(hr)) return 1;
    HANDLE control = NULL;
    if (!capture_supported()) hr = E_NOTIMPL;
    else if (still) hr = snapshot(target);
    else if (!probe) {
        control = CreateThread(NULL, 0, wait_for_stop, GetStdHandle(STD_INPUT_HANDLE), 0, NULL);
        hr = control ? record(target, control, region) : HRESULT_FROM_WIN32(GetLastError());
        if (control) {
            CancelSynchronousIo(control);
            CloseHandle(control);
        }
    }
    uninitialize();
    FreeLibrary(combase);
    if (FAILED(hr)) fprintf(stderr, "Graphics capture failed (0x%08lx).\n", (unsigned long)(UINT32)hr);
    return FAILED(hr) ? 1 : 0;
}
