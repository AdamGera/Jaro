#import "jard_phone_annotation.h"
#import <AVFoundation/AVFoundation.h>
#import <CoreMediaIO/CMIOHardware.h>
#import <IOKit/IOKitLib.h>

// Wired capture discovery and connection handling adapted from work by
// Christopher Langton under the MIT license. See NOTICE.
static id USBProperty(io_service_t device, CFStringRef key, CFStringRef alternate) {
    id value = CFBridgingRelease(IORegistryEntryCreateCFProperty(device, key, kCFAllocatorDefault, 0));
    return value ?: CFBridgingRelease(IORegistryEntryCreateCFProperty(device, alternate, kCFAllocatorDefault, 0));
}

static NSString *normalizedID(NSString *identifier) {
    return [identifier.lowercaseString stringByReplacingOccurrencesOfString:@"-" withString:@""];
}

static NSSet<NSString *> *wiredIDs(void) {
    io_iterator_t iterator = IO_OBJECT_NULL;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOUSBHostDevice"), &iterator) != KERN_SUCCESS)
        return NSSet.set;
    NSMutableSet<NSString *> *identifiers = NSMutableSet.set;
    for (io_service_t device; (device = IOIteratorNext(iterator)); IOObjectRelease(device)) {
        NSNumber *vendor = USBProperty(device, CFSTR("idVendor"), CFSTR("kUSBVendorID"));
        if (![vendor isKindOfClass:NSNumber.class] || vendor.intValue != 0x05ac) continue;
        id product = USBProperty(device, CFSTR("USB Product Name"), CFSTR("kUSBProductString"));
        if (![product isKindOfClass:NSString.class]) {
            io_name_t name = {0};
            if (IORegistryEntryGetName(device, name) != KERN_SUCCESS) continue;
            product = [NSString stringWithUTF8String:name];
        }
        if (![product hasPrefix:@"iPhone"] && ![product hasPrefix:@"iPad"]) continue;
        id serial = USBProperty(device, CFSTR("USB Serial Number"), CFSTR("kUSBSerialNumberString"));
        [identifiers addObject:[serial isKindOfClass:NSString.class] ? normalizedID(serial) : @""];
    }
    IOObjectRelease(iterator);
    return identifiers;
}

BOOL JardHasWiredAnnotationDevice(void) { return wiredIDs().count > 0; }

NSRect JardPhonePictureRect(NSSize pixels, NSRect bounds) {
    if (pixels.width <= 0 || pixels.height <= 0 || NSIsEmptyRect(bounds)) return NSZeroRect;
    return AVMakeRectWithAspectRatioInsideRect(pixels, bounds);
}

static AVCaptureDevice *wiredPhone(void) {
    NSSet<NSString *> *ids = wiredIDs();
    if (!ids.count) return nil;
    AVCaptureDeviceDiscoverySession *discovery = [AVCaptureDeviceDiscoverySession
        discoverySessionWithDeviceTypes:@[AVCaptureDeviceTypeExternal] mediaType:AVMediaTypeMuxed position:AVCaptureDevicePositionUnspecified];
    AVCaptureDevice *fallback = nil;
    for (AVCaptureDevice *device in discovery.devices) {
        if (device.isContinuityCamera) continue;
        if ([ids containsObject:normalizedID(device.uniqueID)]) return device;
        if ([device.modelID isEqualToString:@"iOS Device"] && !fallback) fallback = device;
    }
    return fallback;
}

static void publishWiredDevices(void) {
    CMIOObjectPropertyAddress address = {kCMIOHardwarePropertyAllowScreenCaptureDevices,
        kCMIOObjectPropertyScopeGlobal, kCMIOObjectPropertyElementMain};
    UInt32 enabled = 1;
    CMIOObjectSetPropertyData(kCMIOObjectSystemObject, &address, 0, NULL, sizeof(enabled), &enabled);
}

static BOOL portHasMedia(AVCaptureInputPort *port, AVMediaType type, CMMediaType formatType) {
    return [port.mediaType isEqual:type] || (port.formatDescription &&
        CMFormatDescriptionGetMediaType(port.formatDescription) == formatType);
}

static BOOL configurePhoneVideo(AVCaptureMovieFileOutput *output, AVCaptureDeviceInput *phone) {
    BOOL phoneVideo = NO;
    for (AVCaptureConnection *connection in output.connections) {
        BOOL phoneInput = NO;
        BOOL audio = connection.audioChannels.count > 0, video = NO;
        for (AVCaptureInputPort *port in connection.inputPorts) {
            phoneInput |= port.input == phone;
            video |= portHasMedia(port, AVMediaTypeVideo, kCMMediaType_Video);
            audio |= portHasMedia(port, AVMediaTypeAudio, kCMMediaType_Audio);
        }
        if (phoneInput && audio) {
            if (video) return NO;
            connection.enabled = NO;
        }
        phoneVideo |= phoneInput && video && connection.enabled;
    }
    return phoneVideo;
}

@interface JardPhoneAnnotation () <AVCaptureFileOutputRecordingDelegate, NSWindowDelegate>
@property(nonatomic, strong) NSURL *movie;
@property(nonatomic, readwrite, strong) NSPanel *panel;
@property(nonatomic, strong) AVCaptureSession *session;
@property(nonatomic, strong) AVCaptureMovieFileOutput *output;
@property(nonatomic, strong) AVCaptureDeviceInput *videoInput;
@property(nonatomic, strong) AVCaptureVideoPreviewLayer *preview;
@property(nonatomic, strong) dispatch_queue_t queue;
@property(nonatomic) NSSize pixels;
@property(nonatomic) BOOL stopping, discarded, complete, requested;
@end

@implementation JardPhoneAnnotation

- (instancetype)initWithMovie:(NSURL *)movie {
    if (!(self = [super init])) return nil;
    _movie = movie;
    _session = AVCaptureSession.new;
    _output = AVCaptureMovieFileOutput.new;
    _queue = dispatch_queue_create("com.jard.annotation.phone", DISPATCH_QUEUE_SERIAL);
    return self;
}

- (void)start {
    publishWiredDevices();
    [self findPhoneUntil:NSProcessInfo.processInfo.systemUptime + 12];
}

- (void)findPhoneUntil:(NSTimeInterval)deadline {
    if (self.complete || self.stopping) return;
    AVCaptureDevice *phone = wiredPhone();
    if (phone) { [self attach:phone]; return; }
    if (NSProcessInfo.processInfo.systemUptime >= deadline) {
        [self finish:NO error:@"Connect an unlocked iPhone or iPad by USB and trust this Mac."];
        return;
    }
    __weak typeof(self) weak = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC / 4), dispatch_get_main_queue(), ^{
        [weak findPhoneUntil:deadline];
    });
}

- (void)attach:(AVCaptureDevice *)phone {
    NSNotificationCenter *center = NSNotificationCenter.defaultCenter;
    [center addObserver:self selector:@selector(deviceDisconnected:) name:AVCaptureDeviceWasDisconnectedNotification object:phone];
    [center addObserver:self selector:@selector(runtimeError:) name:AVCaptureSessionRuntimeErrorNotification object:self.session];
    dispatch_async(self.queue, ^{
        NSError *error = nil;
        AVCaptureDeviceInput *video = [AVCaptureDeviceInput deviceInputWithDevice:phone error:&error];
        AVCaptureSession *session = self.session;
        AVCaptureMovieFileOutput *output = self.output;
        [session beginConfiguration];
        if (video && [session canAddInput:video]) [session addInput:video];
        if ([session canAddOutput:output]) [session addOutput:output];
        BOOL ready = video && [session.inputs containsObject:video] && [session.outputs containsObject:output];
        [session commitConfiguration];
        if (ready) [session startRunning];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.complete || self.stopping) return;
            if (!ready || !session.isRunning) {
                [self finish:NO error:error.localizedDescription ?: @"Could not open the wired screen and Mac microphone together."];
                return;
            }
            self.videoInput = video;
            [self showPreview:phone];
            [self waitForPictureUntil:NSProcessInfo.processInfo.systemUptime + 8];
        });
    });
}

- (void)showPreview:(AVCaptureDevice *)phone {
    NSPoint pointer = NSEvent.mouseLocation;
    NSScreen *screen = NSScreen.mainScreen;
    for (NSScreen *candidate in NSScreen.screens)
        if (NSPointInRect(pointer, candidate.frame)) { screen = candidate; break; }
    NSRect available = screen.visibleFrame;
    CGFloat height = MIN(580, NSHeight(available) * .75);
    NSRect frame = NSMakeRect(NSMidX(available) - 145, NSMidY(available) - height / 2, 290, height);
    NSPanel *panel = [[NSPanel alloc] initWithContentRect:frame styleMask:NSWindowStyleMaskTitled |
        NSWindowStyleMaskClosable | NSWindowStyleMaskResizable | NSWindowStyleMaskNonactivatingPanel
        backing:NSBackingStoreBuffered defer:NO];
    panel.releasedWhenClosed = NO;
    panel.title = phone.localizedName.length ? phone.localizedName : @"Wired Screen";
    panel.level = NSFloatingWindowLevel;
    panel.hidesOnDeactivate = NO;
    panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    panel.delegate = self;
    NSView *view = panel.contentView;
    view.wantsLayer = YES;
    view.layer.backgroundColor = NSColor.blackColor.CGColor;
    AVCaptureVideoPreviewLayer *preview = [AVCaptureVideoPreviewLayer layerWithSession:self.session];
    preview.videoGravity = AVLayerVideoGravityResizeAspect;
    preview.frame = view.bounds;
    preview.autoresizingMask = kCALayerWidthSizable | kCALayerHeightSizable;
    [view.layer addSublayer:preview];
    self.panel = panel;
    self.preview = preview;
    [panel orderFrontRegardless];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(pictureFormatChanged:)
        name:AVCaptureInputPortFormatDescriptionDidChangeNotification object:nil];
}

- (NSSize)videoSize {
    for (AVCaptureInputPort *port in self.videoInput.ports) {
        CMFormatDescriptionRef format = port.formatDescription;
        if (format && CMFormatDescriptionGetMediaType(format) == kCMMediaType_Video) {
            CMVideoDimensions dimensions = CMVideoFormatDescriptionGetDimensions(format);
            if (dimensions.width > 0 && dimensions.height > 0) return NSMakeSize(dimensions.width, dimensions.height);
        }
    }
    return NSZeroSize;
}

- (void)pictureFormatChanged:(NSNotification *)notification {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.complete || ((AVCaptureInputPort *)notification.object).input != self.videoInput) return;
        NSSize widthHeight = [self videoSize];
        if (widthHeight.width > 0) {
            self.pixels = widthHeight;
            if (self.pictureChanged) self.pictureChanged();
        }
    });
}

- (NSRect)pictureOnScreen {
    NSView *view = self.panel.contentView;
    if (!self.panel.visible || !view) return NSZeroRect;
    NSRect picture = JardPhonePictureRect(self.pixels, view.bounds);
    return NSIsEmptyRect(picture) ? NSZeroRect : [self.panel convertRectToScreen:[view convertRect:picture toView:nil]];
}

- (void)waitForPictureUntil:(NSTimeInterval)deadline {
    if (self.stopping || self.complete) return;
    self.pixels = [self videoSize];
    if (self.pixels.width > 0) {
        self.requested = YES;
        AVCaptureDeviceInput *video = self.videoInput;
        dispatch_async(self.queue, ^{
            if (configurePhoneVideo(self.output, video)) {
                [self.output startRecordingToOutputFileURL:self.movie recordingDelegate:self];
                return;
            }
            dispatch_async(dispatch_get_main_queue(), ^{
                [self finish:NO error:@"Could not record the wired screen."];
            });
        });
        __weak typeof(self) weak = self;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 8 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
            JardPhoneAnnotation *owner = weak;
            if (owner && owner.started && !owner.stopping)
                [owner finish:NO error:@"The phone mirror opened, but no recording started."];
        });
        return;
    }
    if (NSProcessInfo.processInfo.systemUptime >= deadline) {
        [self finish:NO error:@"The wired screen has no video. Unlock the device and try again."];
        return;
    }
    __weak typeof(self) weak = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC / 20), dispatch_get_main_queue(), ^{
        [weak waitForPictureUntil:deadline];
    });
}

- (void)deviceDisconnected:(NSNotification *)notification {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.complete || self.stopping) return;
        if (!self.requested || self.started) [self finish:NO error:@"The iPhone or iPad disconnected before recording started."];
        else if (self.stopRequested) self.stopRequested();
    });
}

- (void)runtimeError:(NSNotification *)notification {
    NSError *error = notification.userInfo[AVCaptureSessionErrorKey];
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.complete || self.stopping) return;
        if (!self.requested || self.started) [self finish:NO error:error.localizedDescription];
        else if (self.stopRequested) self.stopRequested();
    });
}

- (void)windowDidMove:(NSNotification *)notification { if (self.pictureChanged) self.pictureChanged(); }
- (void)windowDidResize:(NSNotification *)notification { if (self.pictureChanged) self.pictureChanged(); }

- (BOOL)windowShouldClose:(NSWindow *)window {
    if (self.stopRequested) self.stopRequested();
    return NO;
}

- (void)stopDiscarding:(BOOL)discard {
    if (self.complete || self.stopping) return;
    self.stopping = YES;
    self.discarded = discard;
    [self.panel orderOut:nil];
    if (!self.requested) { [self finish:NO error:nil]; return; }
    dispatch_async(self.queue, ^{
        if (self.output.isRecording) [self.output stopRecording];
        else [self.session stopRunning];
    });
    __weak typeof(self) weak = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        JardPhoneAnnotation *owner = weak;
        if (owner && !owner.complete) dispatch_async(owner.queue, ^{ [owner.session stopRunning]; });
    });
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        JardPhoneAnnotation *owner = weak;
        if (owner && !owner.complete) [owner finish:NO error:@"The wired recording did not finish."];
    });
}

- (void)captureOutput:(AVCaptureFileOutput *)output didStartRecordingToOutputFileAtURL:(NSURL *)url
        startPTS:(CMTime)pts fromConnections:(NSArray<AVCaptureConnection *> *)connections {
    CMClockRef clock = self.session.synchronizationClock;
    CMClockRef host = CMClockGetHostTimeClock();
    CMTime start = clock ? CMSyncConvertTime(pts, clock, host) : kCMTimeInvalid;
    double age = CMTimeGetSeconds(CMTimeSubtract(CMClockGetTime(host), start));
    double uptime = NSProcessInfo.processInfo.systemUptime - (isfinite(age) && age >= 0 && age < 12 ? age : 0);
    dispatch_async(dispatch_get_main_queue(), ^{
        if (!self.complete && !self.stopping && self.started) self.started(uptime);
        self.started = nil;
    });
}

- (void)captureOutput:(AVCaptureFileOutput *)output didFinishRecordingToOutputFileAtURL:(NSURL *)url
        fromConnections:(NSArray<AVCaptureConnection *> *)connections error:(NSError *)error {
    double duration = CMTimeGetSeconds(output.recordedDuration);
    dispatch_async(dispatch_get_main_queue(), ^{
        BOOL usable = !self.discarded && (!error || [error.userInfo[AVErrorRecordingSuccessfullyFinishedKey] boolValue] ||
            (self.stopping && isfinite(duration) && duration > .1)) &&
            [NSFileManager.defaultManager fileExistsAtPath:url.path];
        [self finish:usable error:usable ? nil : error.localizedDescription];
    });
}

- (void)finish:(BOOL)usable error:(NSString *)error {
    if (self.complete) return;
    self.complete = YES;
    [NSNotificationCenter.defaultCenter removeObserver:self];
    [self.panel orderOut:nil];
    self.panel.delegate = nil;
    self.panel = nil;
    self.preview = nil;
    dispatch_async(self.queue, ^{
        [self.session stopRunning];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.finished) self.finished(usable, error);
            self.started = nil;
            self.finished = nil;
            self.stopRequested = nil;
            self.pictureChanged = nil;
        });
    });
}

@end
