#ifndef JARO_WINDOWS_SCREEN_ABI_H
#define JARO_WINDOWS_SCREEN_ABI_H

/* Graphics Capture WinRT ABI as defined in the Windows SDK metadata.
 * Zig's MinGW headers do not ship these capture interfaces.
 */
#include <inspectable.h>

typedef struct { INT32 width, height; } CaptureSize;

static const IID item_id = {0x79c3f95b, 0x31f7, 0x4ec2, {0xa4,0x64,0x63,0x2e,0xf5,0xd3,0x07,0x60}};
static const IID interop_id = {0x3628e81b, 0x3cac, 0x4c60, {0xb7,0xf4,0x23,0xce,0x0e,0x0c,0x33,0x56}};
static const IID session_statics_id = {0x2224a540, 0x5974, 0x49aa, {0xb2,0x32,0x08,0x82,0x53,0x6f,0x4c,0xb5}};
static const IID pool_factory_id = {0x589b103f, 0x6bbc, 0x5df5, {0xa9,0x91,0x02,0xe2,0x8b,0x3b,0x66,0xd5}};
static const IID surface_access_id = {0xa9b3d012, 0x3df2, 0x4ee3, {0xb8,0xd1,0x86,0x95,0xf4,0x57,0xd3,0xc1}};
static const IID closable_id = {0x30d5a829, 0x7fa4, 0x4026, {0x83,0xbb,0xd7,0x5b,0xae,0x4e,0xa9,0x9e}};

typedef struct CaptureItem CaptureItem;
typedef struct CapturePool CapturePool;
typedef struct CaptureSession CaptureSession;
typedef struct CaptureFrame CaptureFrame;
typedef struct CapturePoolFactory CapturePoolFactory;
typedef struct SurfaceAccess SurfaceAccess;
typedef struct CaptureClosable CaptureClosable;
typedef struct CaptureInterop CaptureInterop;
typedef struct CaptureSessionStatics CaptureSessionStatics;

struct CaptureInterop {
    const struct {
        IUnknownVtbl base;
        HRESULT (STDMETHODCALLTYPE *CreateForWindow)(CaptureInterop *, HWND, REFIID, void **);
        HRESULT (STDMETHODCALLTYPE *CreateForMonitor)(CaptureInterop *, HMONITOR, REFIID, void **);
    } *lpVtbl;
};
struct CaptureSessionStatics {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *IsSupported)(CaptureSessionStatics *, boolean *);
    } *lpVtbl;
};

struct CaptureItem {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *get_DisplayName)(CaptureItem *, HSTRING *);
        HRESULT (STDMETHODCALLTYPE *get_Size)(CaptureItem *, CaptureSize *);
    } *lpVtbl;
};
struct CapturePoolFactory {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *CreateFreeThreaded)(
            CapturePoolFactory *, IInspectable *, DXGI_FORMAT, INT32, CaptureSize, CapturePool **);
    } *lpVtbl;
};
struct CapturePool {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *Recreate)(CapturePool *, IInspectable *, DXGI_FORMAT, INT32, CaptureSize);
        HRESULT (STDMETHODCALLTYPE *TryGetNextFrame)(CapturePool *, CaptureFrame **);
        HRESULT (STDMETHODCALLTYPE *add_FrameArrived)(CapturePool *, IUnknown *, INT64 *);
        HRESULT (STDMETHODCALLTYPE *remove_FrameArrived)(CapturePool *, INT64);
        HRESULT (STDMETHODCALLTYPE *CreateCaptureSession)(CapturePool *, CaptureItem *, CaptureSession **);
    } *lpVtbl;
};
struct CaptureFrame {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *get_Surface)(CaptureFrame *, IInspectable **);
        HRESULT (STDMETHODCALLTYPE *get_SystemRelativeTime)(CaptureFrame *, INT64 *);
        HRESULT (STDMETHODCALLTYPE *get_ContentSize)(CaptureFrame *, CaptureSize *);
    } *lpVtbl;
};
struct CaptureSession {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *StartCapture)(CaptureSession *);
    } *lpVtbl;
};
struct SurfaceAccess {
    const struct {
        IUnknownVtbl base;
        HRESULT (STDMETHODCALLTYPE *GetInterface)(SurfaceAccess *, REFIID, void **);
    } *lpVtbl;
};
struct CaptureClosable {
    const struct {
        IInspectableVtbl base;
        HRESULT (STDMETHODCALLTYPE *Close)(CaptureClosable *);
    } *lpVtbl;
};

#endif
