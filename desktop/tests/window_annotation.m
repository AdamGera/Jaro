// Pass --verify-fade with a two-second black input at 30 fps for pixel checks.
#import "../jard_annotation_ink.h"
#import "../jard_annotation_region.h"
#import "../jard_annotation.h"
#import "../jard_annotation_keys.h"
#import "../jard_annotation_result_panel.h"
#import "../jard_interface_language.h"
#import "../jard_phone_annotation.h"
#import <AVFoundation/AVFoundation.h>
#import <assert.h>
#import <Carbon/Carbon.h>
#import <ImageIO/ImageIO.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>

@interface JardAnnotationResultPanel (TimingFixture)
- (void)tick;
@end

@interface JardVisibleResultPanelFixture : JardAnnotationResultPanel
@property(nonatomic) BOOL onScreen;
@end

@implementation JardVisibleResultPanelFixture
- (BOOL)routePanelOnScreen { return self.onScreen; }
@end

@interface JardAnnotation (KeyboardFixture)
@property(nonatomic, copy) NSString *identifier;
@property(nonatomic, copy) NSString *source;
@property(nonatomic, strong) SCStream *stream;
@property(nonatomic, strong) SCRecordingOutput *recording;
@property(nonatomic, strong) NSURL *directory;
@property(nonatomic, strong) NSURL *movie;
@property(nonatomic, strong) NSURL *narration;
@property(nonatomic) CGWindowID windowID;
@property(nonatomic) pid_t ownerPID;
@property(nonatomic) BOOL leased;
@property(nonatomic) BOOL compositeInk;
@property(nonatomic, strong) SCContentFilter *displaySelection, *displayWithoutOverlays, *activeDisplayFilter;
@property(nonatomic, strong) NSMutableArray<NSValue *> *displayInkRanges;
@property(nonatomic, strong) NSNumber *displayInkStart;
@property(nonatomic) BOOL displayFilterPending;
@property(nonatomic, copy) NSDictionary *originSnapshot;
@property(nonatomic) BOOL screenStartRequested, narrationRequested;
@property(nonatomic, strong, readonly) NSPanel *overlay;
@property(nonatomic, strong, readonly) NSPanel *controls;
@property(nonatomic, strong, readonly) NSPanel *feedbackPanel;
@property(nonatomic, strong) JardSelectionPanel *selectionPanel;
@property(nonatomic, strong, readonly) NSButton *penButton;
@property(nonatomic, strong, readonly) NSView *ink;
- (void)preparePanels:(NSScreen *)screen target:(NSRect)target;
- (void)updateWindowOverlayWithInfo:(NSArray<NSDictionary *> *)info frontmostPID:(pid_t)frontmostPID;
- (void)drawPointerAtScreenPoint:(NSPoint)point;
- (NSArray<NSNumber *> *)floatingPickerWindowIDs:(NSArray<NSDictionary *> *)windows;
- (SCContentFilter *)displayFilterExcludingOverlays:(SCContentFilter *)selection content:(SCShareableContent *)content;
- (SCDisplay *)displayAtPoint:(CGPoint)pointer fromDisplays:(NSArray<SCDisplay *> *)displays;
- (void)beginDisplayAtPoint:(CGPoint)pointer identifier:(NSString *)identifier reply:(JardDictationReply)reply;
- (void)beginWindowSelection:(NSString *)identifier reply:(JardDictationReply)reply;
- (void)resolveWindowSelection:(CGWindowID)window owner:(pid_t)pid pointer:(CGPoint)pointer identifier:(NSString *)identifier;
- (void)state:(NSString *)phase message:(NSString *)message;
- (void)expireDiscardPromptAt:(NSTimeInterval)now pointer:(NSPoint)pointer;
- (void)expireFeedbackAt:(NSTimeInterval)now pointer:(NSPoint)pointer;
- (void)revealRecording:(NSURL *)recording;
- (void)fail:(NSString *)message keepMovie:(BOOL)keep;
- (void)recordingOutputDidFinishRecording:(SCRecordingOutput *)output;
- (void)recordingOutput:(SCRecordingOutput *)output didFailWithError:(NSError *)error;
- (void)submitAnnotation;
@end

@interface AnnotationFeedbackFixture : JardAnnotation
@property(nonatomic, strong) NSURL *revealedRecording;
@end
@implementation AnnotationFeedbackFixture
- (void)revealRecording:(NSURL *)recording { self.revealedRecording = recording; }
@end

@interface AnnotationShareableFixture : NSObject
@property(nonatomic, copy) NSArray *includedDisplays, *applications, *windows, *displays;
@property(nonatomic, strong) AnnotationShareableFixture *owningApplication;
@property(nonatomic) pid_t processID;
@property(nonatomic) NSInteger windowLayer;
@property(nonatomic) BOOL includeMenuBar;
@property(nonatomic) CGRect frame;
@property(nonatomic) CGDirectDisplayID displayID;
@property(nonatomic) CGWindowID windowID;
@property(nonatomic, getter=isOnScreen) BOOL onScreen;
@end
@implementation AnnotationShareableFixture
@end

@interface AnnotationPointerFixture : JardAnnotation
@property(nonatomic, copy) void (^loaded)(SCShareableContent *);
@property(nonatomic, strong) SCDisplay *pickedDisplay;
@property(nonatomic, strong) SCShareableContent *recordingContent;
@property(nonatomic, strong) SCWindow *pickedWindow;
@property(nonatomic) NSUInteger pickerRequests;
@property(nonatomic) NSUInteger windowSelectorRequests;
@end

@implementation AnnotationPointerFixture
- (void)loadScreenContent:(void (^)(SCShareableContent *))completion { self.loaded = completion; }
- (SCContentFilter *)filterForPointerDisplay:(SCDisplay *)display {
    self.pickedDisplay = display;
    return (id)NSObject.new;
}
- (SCContentFilter *)filterForSelectedWindow:(SCWindow *)window {
    self.pickedWindow = window;
    return (id)NSObject.new;
}
- (BOOL)showWindowSelector:(SCShareableContent *)content message:(NSString *)message {
    if (!content.displays.count) return NO;
    self.windowSelectorRequests++;
    [self state:@"selecting" message:message];
    return YES;
}
- (void)beginRecording:(SCContentFilter *)filter content:(SCShareableContent *)content {
    assert(filter && content);
    self.recordingContent = content;
    [self state:@"starting" message:@""];
}
- (void)presentPicker:(NSString *)identifier reply:(JardDictationReply)reply {
    assert([self.identifier isEqual:identifier]);
    self.pickerRequests++;
    if (reply) reply(self.status, nil);
}
@end

static void testPointerDisplaySelection(void) {
    AnnotationPointerFixture *annotation = [[AnnotationPointerFixture alloc] initWithSend:nil stateChanged:nil];
    AnnotationShareableFixture *primary = AnnotationShareableFixture.new, *side = AnnotationShareableFixture.new;
    primary.displayID = CGMainDisplayID();
    primary.frame = CGRectMake(0, 0, 1440, 900);
    side.displayID = primary.displayID + 1;
    side.frame = CGRectMake(-1920, -120, 1920, 1080);
    AnnotationShareableFixture *content = AnnotationShareableFixture.new;
    content.displays = @[side, primary];
    assert([annotation displayAtPoint:CGPointMake(-800, -100) fromDisplays:content.displays] == (id)side);
    assert([annotation displayAtPoint:CGPointMake(200, 250) fromDisplays:content.displays] == (id)primary);
    assert([annotation displayAtPoint:CGPointMake(1600, 0) fromDisplays:content.displays] == (id)primary);
    annotation.originSnapshot = @{@"bounds": [NSValue valueWithRect:NSMakeRect(-800, 600, 200, 150)]};
    assert([annotation displayAtPoint:CGPointMake(1600, 0) fromDisplays:content.displays] == (id)side);

    annotation.identifier = @"pointer-direct";
    [annotation beginDisplayAtPoint:CGPointMake(-800, 40) identifier:annotation.identifier reply:^(id result, NSString *error) {
        assert([result[@"phase"] isEqual:@"starting"] && !error);
    }];
    void (^resolved)(SCShareableContent *) = annotation.loaded;
    annotation.loaded = nil;
    resolved((id)content);
    assert(annotation.pickedDisplay == (id)side && annotation.recordingContent == (id)content && !annotation.pickerRequests);
    assert([annotation cancel]);

    annotation.identifier = @"no-permission";
    [annotation beginDisplayAtPoint:CGPointMake(-800, 40) identifier:annotation.identifier reply:^(id result, NSString *error) {
        assert([result[@"phase"] isEqual:@"picking"] && !error);
    }];
    resolved = annotation.loaded;
    annotation.loaded = nil;
    resolved(nil);
    assert(annotation.pickerRequests == 1 && [annotation cancel]);

    annotation.identifier = @"cancel-lookup";
    __block BOOL cancelled = NO;
    [annotation beginDisplayAtPoint:CGPointMake(-800, 40) identifier:annotation.identifier reply:^(id result, NSString *error) {
        cancelled = !result && [error containsString:@"cancelled"];
    }];
    resolved = annotation.loaded;
    annotation.loaded = nil;
    assert([annotation cancel]);
    resolved((id)content);
    assert(cancelled && !annotation.busy && annotation.pickerRequests == 1);
    puts("Shortcut display discovery matched Quartz coordinates, kept the native picker fallback, and ignored a cancelled lookup.");
}

static NSDictionary *pickerWindow(NSInteger number, pid_t owner, NSInteger layer, CGRect frame) {
    return @{(__bridge NSString *)kCGWindowNumber: @(number), (__bridge NSString *)kCGWindowOwnerPID: @(owner),
        (__bridge NSString *)kCGWindowLayer: @(layer), (__bridge NSString *)kCGWindowBounds:
            CFBridgingRelease(CGRectCreateDictionaryRepresentation(frame))};
}

static void testWindowSelection(void) {
    CGPoint hit = CGPointMake(-320, 235);
    CGRect strong = CGRectMake(-500, 100, 600, 300);
    NSDictionary *front = pickerWindow(50, 8, 0, strong), *back = pickerWindow(49, 9, 0, strong);
    NSDictionary *owners = @{@50: @8, @49: @9, @48: @9};
    NSDictionary *popup = pickerWindow(48, 9, 25, strong);
    NSDictionary *reused = pickerWindow(50, 18, 0, strong);
    NSDictionary *small = pickerWindow(48, 9, 0, CGRectMake(-329, 200, 79, 70));
    assert(JardAnnotationWindowAtPoint(hit, @[popup, reused, small, front, back], owners) == front);
    assert(JardAnnotationWindowAtPoint(hit, @[back, front], owners) == back);
    assert(!JardAnnotationWindowAtPoint(CGPointMake(900, 900), @[front, back], owners));

    AnnotationPointerFixture *annotation = [[AnnotationPointerFixture alloc] initWithSend:nil stateChanged:nil];
    AnnotationShareableFixture *display = AnnotationShareableFixture.new;
    display.frame = CGRectMake(-1200, 0, 1400, 900);
    AnnotationShareableFixture *content = AnnotationShareableFixture.new;
    content.displays = @[display];
    AnnotationShareableFixture *app = AnnotationShareableFixture.new;
    app.processID = 8;
    AnnotationShareableFixture *window = AnnotationShareableFixture.new;
    window.windowID = 50;
    window.owningApplication = app;
    window.onScreen = YES;
    content.windows = @[window];

    annotation.identifier = @"window-select";
    annotation.source = @"window";
    [annotation beginWindowSelection:annotation.identifier reply:^(id result, NSString *error) {
        assert(!error && [result[@"phase"] isEqual:@"selecting"]);
    }];
    void (^loaded)(SCShareableContent *) = annotation.loaded;
    annotation.loaded = nil;
    loaded((id)content);
    assert(annotation.windowSelectorRequests == 1 && !annotation.pickerRequests);
    [annotation resolveWindowSelection:50 owner:8 pointer:hit identifier:annotation.identifier];
    loaded = annotation.loaded;
    annotation.loaded = nil;
    app.processID = 18;
    loaded((id)content);
    assert(annotation.windowSelectorRequests == 2 && !annotation.pickedWindow && !annotation.recordingContent);
    app.processID = 8;
    [annotation resolveWindowSelection:50 owner:8 pointer:hit identifier:annotation.identifier];
    loaded = annotation.loaded;
    annotation.loaded = nil;
    loaded((id)content);
    assert(annotation.pickedWindow == (id)window && annotation.recordingContent == (id)content);
    assert([annotation cancel]);

    annotation.identifier = @"window-empty";
    annotation.source = @"window";
    [annotation resolveWindowSelection:0 owner:0 pointer:hit identifier:annotation.identifier];
    loaded = annotation.loaded;
    annotation.loaded = nil;
    loaded((id)content);
    assert([annotation.source isEqual:@"display"] && annotation.pickedDisplay == (id)display);
    assert([annotation cancel]);

    annotation.identifier = @"window-scoped";
    annotation.source = @"window";
    [annotation beginWindowSelection:annotation.identifier reply:nil];
    loaded = annotation.loaded;
    annotation.loaded = nil;
    loaded(nil);
    assert(annotation.pickerRequests == 1 && [annotation cancel]);
    puts("The window picker matched front-to-back geometry, fenced a reused window ID, and kept the display and scoped picker fallbacks.");
}

@interface AnnotationFilterFixture : JardAnnotation
@property(nonatomic, strong) SCDisplay *display;
@property(nonatomic, strong) SCRunningApplication *owner;
@property(nonatomic, copy) NSArray<SCWindow *> *workspace;
@property(nonatomic) BOOL menuBar;
@end
@implementation AnnotationFilterFixture
- (SCContentFilter *)filterForDisplay:(SCDisplay *)display excluding:(SCRunningApplication *)application
    keepingWindows:(NSArray<SCWindow *> *)windows menuBar:(BOOL)menuBar {
    self.display = display;
    self.owner = application;
    self.workspace = windows;
    self.menuBar = menuBar;
    return (id)NSObject.new;
}
@end

static void testAnnotationCaptureFilters(void) {
    AnnotationFilterFixture *annotation = [[AnnotationFilterFixture alloc] initWithSend:nil stateChanged:nil];
    pid_t pid = NSProcessInfo.processInfo.processIdentifier;
    NSMutableArray<NSDictionary *> *windows = NSMutableArray.array;
    for (NSArray *entry in @[@[@1, @(pid), @0], @[@2, @(pid), @25], @[@3, @(pid + 1), @25]]) {
        [windows addObject:@{(__bridge NSString *)kCGWindowNumber: entry[0],
            (__bridge NSString *)kCGWindowOwnerPID: entry[1], (__bridge NSString *)kCGWindowLayer: entry[2]}];
    }
    assert([[annotation floatingPickerWindowIDs:windows] isEqual:@[@2]]);

    AnnotationShareableFixture *owner = AnnotationShareableFixture.new, *foreign = AnnotationShareableFixture.new;
    owner.processID = pid;
    foreign.processID = pid + 1;
    AnnotationShareableFixture *workspace = AnnotationShareableFixture.new, *toolbar = AnnotationShareableFixture.new;
    workspace.owningApplication = toolbar.owningApplication = owner;
    toolbar.windowLayer = 25;
    AnnotationShareableFixture *foreignWindow = AnnotationShareableFixture.new;
    foreignWindow.owningApplication = foreign;
    AnnotationShareableFixture *content = AnnotationShareableFixture.new;
    content.applications = @[foreign];
    content.windows = @[toolbar, foreignWindow];

    NSObject *display = NSObject.new;
    AnnotationShareableFixture *selection = AnnotationShareableFixture.new;
    selection.includedDisplays = @[display];
    selection.includeMenuBar = YES;
    assert([annotation displayFilterExcludingOverlays:(id)selection content:(id)content] != (id)selection);
    assert(annotation.display == (id)display && annotation.owner == (id)owner && !annotation.workspace.count && annotation.menuBar);

    content.applications = @[foreign, owner];
    content.windows = @[workspace, toolbar, foreignWindow];
    selection.includeMenuBar = NO;
    assert([annotation displayFilterExcludingOverlays:(id)selection content:(id)content] != (id)selection);
    assert(annotation.owner == (id)owner && [annotation.workspace isEqual:@[workspace]] && !annotation.menuBar);
    content.applications = @[foreign];
    content.windows = @[foreignWindow];
    assert([annotation displayFilterExcludingOverlays:(id)selection content:(id)content] == (id)selection);
    puts("Capture filtering kept the selected display, menu bar preference and workspace window, and excluded Jaro's floating controls.");
}

static NSButton *annotationButton(NSView *view, NSString *label) {
    for (NSView *child in view.subviews) {
        if ([child isKindOfClass:NSButton.class]) {
            NSButton *button = (NSButton *)child;
            if ([(button.accessibilityLabel ?: button.title) isEqual:label]) return button;
        } else {
            NSButton *button = annotationButton(child, label);
            if (button) return button;
        }
    }
    return nil;
}

@interface AnnotationStreamFixture : NSObject
@property(nonatomic) NSUInteger stops;
@property(nonatomic, copy) void (^completion)(NSError *error);
@property(nonatomic, strong) SCContentFilter *requestedFilter;
@property(nonatomic, copy) void (^filterCompletion)(NSError *error);
- (void)stopCaptureWithCompletionHandler:(void (^)(NSError *error))completion;
@end
@implementation AnnotationStreamFixture
- (void)stopCaptureWithCompletionHandler:(void (^)(NSError *error))completion {
    self.stops++;
    self.completion = completion;
}
- (void)updateContentFilter:(SCContentFilter *)filter completionHandler:(void (^)(NSError *error))completion {
    self.requestedFilter = filter;
    self.filterCompletion = completion;
}
@end

@interface AnnotationClockFixture : NSObject
@property(nonatomic) CMTime recordedDuration;
@end
@implementation AnnotationClockFixture
@end

static void settleFilterReply(AnnotationStreamFixture *stream, NSError *error) {
    void (^reply)(NSError *) = stream.filterCompletion;
    assert(reply);
    stream.filterCompletion = nil;
    reply(error);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
}

static void testLiveCaptureFilter(void) {
    JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:nil stateChanged:nil];
    AnnotationStreamFixture *stream = AnnotationStreamFixture.new;
    AnnotationClockFixture *recording = AnnotationClockFixture.new;
    SCContentFilter *plain = (id)NSObject.new, *filtered = (id)NSObject.new;
    annotation.identifier = @"overlay-fixture";
    annotation.source = @"area";
    annotation.stream = (id)stream;
    annotation.recording = (id)recording;
    annotation.compositeInk = YES;
    annotation.displaySelection = plain;
    annotation.displayWithoutOverlays = annotation.activeDisplayFilter = filtered;
    annotation.displayInkRanges = NSMutableArray.array;
    annotation.displayInkStart = @0;
    NSScreen *screen = NSScreen.mainScreen;
    [annotation preparePanels:screen target:screen.visibleFrame];
    [annotation state:@"recording" message:@""];
    assert(annotation.controls.sharingType == NSWindowSharingNone);

    annotation.showOverlaysInRecordings = YES;
    assert(annotation.controls.sharingType == NSWindowSharingReadOnly && stream.requestedFilter == plain);
    annotation.showOverlaysInRecordings = NO;
    assert(stream.requestedFilter == plain && annotation.displayFilterPending);
    recording.recordedDuration = CMTimeMake(7, 10);
    settleFilterReply(stream, nil);
    assert(stream.requestedFilter == filtered && annotation.activeDisplayFilter == plain && !annotation.displayInkStart);
    assert(annotation.overlay.sharingType == NSWindowSharingReadOnly);
    assert(annotation.displayInkRanges.count == 1);
    CMTimeRange first = annotation.displayInkRanges.firstObject.CMTimeRangeValue;
    assert(CMTimeCompare(first.start, kCMTimeZero) == 0 && CMTimeCompare(first.duration, CMTimeMake(7, 10)) == 0);

    recording.recordedDuration = CMTimeMake(13, 10);
    settleFilterReply(stream, nil);
    assert(annotation.activeDisplayFilter == filtered && !annotation.displayFilterPending);
    assert(fabs(annotation.displayInkStart.doubleValue - 1.3) < .0001 && annotation.overlay.sharingType == NSWindowSharingNone);
    annotation.showOverlaysInRecordings = YES;
    settleFilterReply(stream, [NSError errorWithDomain:@"Fixture" code:1 userInfo:nil]);
    assert(annotation.activeDisplayFilter == filtered && annotation.displayInkStart && !stream.filterCompletion);
    assert([annotation.status[@"message"] hasPrefix:@"Could not change"]);

    annotation.showOverlaysInRecordings = NO;
    annotation.showOverlaysInRecordings = YES;
    assert(stream.filterCompletion);
    [annotation shutdown];
    settleFilterReply(stream, nil);
    assert(!annotation.activeDisplayFilter && !annotation.displayInkRanges && !annotation.busy);
    puts("Live overlay changes serialized filter updates, kept ink ranges, and ignored a late or failed switch.");
}

static NSDictionary *windowSnapshot(CGWindowID identifier, pid_t pid, NSRect frame, BOOL visible) {
    CGFloat primaryHeight = CGRectGetHeight(CGDisplayBounds(CGMainDisplayID()));
    return @{
        (__bridge NSString *)kCGWindowNumber: @(identifier),
        (__bridge NSString *)kCGWindowOwnerPID: @(pid),
        (__bridge NSString *)kCGWindowIsOnscreen: @(visible),
        (__bridge NSString *)kCGWindowBounds: CFBridgingRelease(CGRectCreateDictionaryRepresentation(
            JardAnnotationAppKitRect(frame, primaryHeight)))
    };
}

static void testWindowTracking(void) {
    NSScreen *screen = NSScreen.mainScreen;
    NSRect available = screen.visibleFrame;
    NSRect start = NSMakeRect(NSMidX(available) - 90, NSMidY(available) - 60, 180, 120);
    NSRect moved = NSMakeRect(floor(NSMinX(start)) + 10, floor(NSMinY(start)) + 20.5, 210, 160);
    const CGWindowID picked = 180;
    const pid_t owner = 210;
    JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload,
        JardDictationReply reply) { assert(NO); } stateChanged:nil];
    AnnotationStreamFixture *stream = AnnotationStreamFixture.new;
    annotation.identifier = @"window-track";
    annotation.source = @"window";
    annotation.windowID = picked;
    annotation.stream = (id)stream;
    [annotation preparePanels:screen target:start];
    [annotation state:@"recording" message:@""];
    [annotation.controls orderFrontRegardless];
    NSRect toolbar = annotation.controls.frame;
    NSDictionary *other = windowSnapshot(picked + 1, owner, start, YES);
    NSDictionary *selection = windowSnapshot(picked, owner, moved, YES);
    [annotation updateWindowOverlayWithInfo:@[other, selection] frontmostPID:owner];
    NSRect actual = annotation.overlay.frame;
    assert(annotation.ownerPID == owner && annotation.overlay.visible && NSEqualSizes(actual.size, moved.size));
    assert(fabs(NSMinX(actual) - NSMinX(moved)) < 1 && fabs(NSMinY(actual) - NSMinY(moved)) < 1);
    assert(NSEqualRects(annotation.controls.frame, toolbar));
    [annotation drawPointerAtScreenPoint:NSMakePoint(NSMidX(moved), NSMidY(moved))];
    assert([[annotation.ink valueForKey:@"strokeActive"] boolValue]);
    [annotation updateWindowOverlayWithInfo:@[selection] frontmostPID:owner];
    assert([[annotation.ink valueForKey:@"strokeActive"] boolValue]);

    [annotation updateWindowOverlayWithInfo:@[selection] frontmostPID:owner + 1];
    assert(!annotation.overlay.visible && annotation.controls.visible && !stream.stops);
    assert(![[annotation.ink valueForKey:@"strokeActive"] boolValue]);
    [annotation updateWindowOverlayWithInfo:@[windowSnapshot(picked, owner, moved, NO)] frontmostPID:owner];
    assert(!annotation.overlay.visible && !stream.stops);
    [annotation updateWindowOverlayWithInfo:nil frontmostPID:owner];
    assert(!annotation.overlay.visible && !stream.stops);
    [annotation updateWindowOverlayWithInfo:@[windowSnapshot(picked, owner, NSZeroRect, YES)] frontmostPID:owner];
    assert(!annotation.overlay.visible && !stream.stops);
    [annotation updateWindowOverlayWithInfo:@[selection] frontmostPID:owner];
    assert(annotation.overlay.visible && annotation.controls.visible && !stream.stops);

    [annotation updateWindowOverlayWithInfo:@[windowSnapshot(picked, owner + 1, moved, YES)] frontmostPID:owner + 1];
    assert(stream.stops == 1 && annotation.busy && !annotation.overlay.visible && !annotation.controls.visible);
    assert([[annotation status][@"phase"] isEqual:@"saving"]);
    [annotation shutdown];
    puts("Selected-window tracking checked motion, resizing, focus, minimization, query failure, and a reused window ID.");
}

static void testAnnotationControls(void) {
    __block JardDictationReply leaseReply, importReply, audioReply;
    __block NSString *released;
    __block NSUInteger imports = 0, releases = 0;
    JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.annotationLease"]) {
            if ([payload[@"active"] boolValue]) leaseReply = [reply copy];
            else { released = payload[@"id"]; releases++; }
        } else if ([command isEqual:@"jard.annotationAudio"]) {
            assert([payload[@"action"] isEqual:@"stop"] && [payload[@"at"] doubleValue] > 0);
            audioReply = [reply copy];
        } else {
            assert([command isEqual:@"jard.importAnnotation"]);
            assert([payload[@"narration"] hasSuffix:@"/Narration.wav"]);
            imports++;
            importReply = [reply copy];
        }
    } stateChanged:nil];
    __block NSUInteger saved = 0;
    annotation.saved = ^(NSString *session, NSImage *preview, NSDictionary *routing) {
        assert([session isEqual:@"stream-save"] && !preview && [routing[@"phase"] isEqual:@"transcribing"]);
        saved++;
    };
    assert(![annotation cancel] && ![annotation stop]);
    __block BOOL cancelledStart = NO;
    [annotation start:@[] source:@"display" microphone:nil origin:nil quartzPointer:nil reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"cancelled"]);
        cancelledStart = YES;
    }];
    NSString *pending = [annotation status][@"capture"];
    assert([[NSUUID alloc] initWithUUIDString:pending] && [annotation cancel] && annotation.busy);
    assert([[annotation status][@"phase"] isEqual:@"cancelling"] && releases == 0);
    leaseReply(@{@"active": @YES}, nil);
    leaseReply = nil;
    assert(cancelledStart && !annotation.busy && releases == 1 && [released isEqual:pending]);
    assert([[annotation status][@"last"][@"id"] isEqual:pending]);

    NSURL *folder = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString] isDirectory:YES];
    assert([NSFileManager.defaultManager createDirectoryAtURL:folder withIntermediateDirectories:NO attributes:nil error:nil]);
    NSURL *movie = [folder URLByAppendingPathComponent:@"Screen annotation.mp4"];
    assert([@"disposable fixture" writeToURL:movie atomically:YES encoding:NSUTF8StringEncoding error:nil]);
    AnnotationStreamFixture *stream = AnnotationStreamFixture.new;
    SCRecordingOutput *output = (id)NSObject.new;
    annotation.identifier = @"stream-cancel";
    annotation.source = @"window";
    annotation.stream = (id)stream;
    annotation.recording = output;
    annotation.screenStartRequested = YES;
    annotation.leased = YES;
    annotation.directory = folder;
    annotation.movie = movie;
    [annotation state:@"recording" message:@""];
    NSScreen *screen = NSScreen.mainScreen;
    [annotation preparePanels:screen target:screen.frame];
    NSWindow *focused = NSApp.keyWindow;
    [annotation.controls orderFrontRegardless];
    NSRect toolbarFrame = annotation.controls.frame;
    NSButton *ask = annotationButton(annotation.controls.contentView, JardLocalized(@"Discard…"));
    NSButton *keep = annotationButton(annotation.controls.contentView, JardLocalized(@"Keep recording"));
    NSButton *discard = annotationButton(annotation.controls.contentView, JardLocalized(@"Discard"));
    assert(ask && keep && discard && discard.isHiddenOrHasHiddenAncestor && !annotation.controls.canBecomeKeyWindow);
    NSTextField *question = (NSTextField *)keep.superview.subviews.firstObject;
    assert([question.stringValue isEqual:JardLocalized(@"Discard this annotation?")]);
    assert(NSMaxX(question.frame) < NSMinX(keep.frame) && NSMaxX(keep.frame) < NSMinX(discard.frame));
    if ([NSProcessInfo.processInfo.environment[@"JARD_TEST_FRENCH"] boolValue]) {
        assert([question.stringValue isEqual:@"Abandonner cette annotation ?"]);
        puts("The native discard confirmation used the bundled French interface language.");
    }
    [discard sendAction:discard.action to:discard.target];
    assert(stream.stops == 0); // A hidden confirmation cannot discard a take.
    [ask performClick:nil];
    assert(ask.isHiddenOrHasHiddenAncestor && !discard.isHiddenOrHasHiddenAncestor && stream.stops == 0);
    assert(NSEqualRects(annotation.controls.frame, toolbarFrame) && NSApp.keyWindow == focused);
    [keep performClick:nil];
    assert(!ask.isHiddenOrHasHiddenAncestor && discard.isHiddenOrHasHiddenAncestor && stream.stops == 0);
    [ask performClick:nil];
    NSTimeInterval later = NSProcessInfo.processInfo.systemUptime + 5;
    [annotation expireDiscardPromptAt:later pointer:NSMakePoint(NSMidX(toolbarFrame), NSMidY(toolbarFrame))];
    assert(!discard.isHiddenOrHasHiddenAncestor);
    [annotation expireDiscardPromptAt:later pointer:NSMakePoint(NSMinX(toolbarFrame) - 1, NSMinY(toolbarFrame) - 1)];
    assert(discard.isHiddenOrHasHiddenAncestor && stream.stops == 0 && !ask.isHiddenOrHasHiddenAncestor);
    [ask performClick:nil];
    [discard performClick:nil];
    assert(annotation.busy && NSApp.keyWindow == focused);
    assert([[annotation status][@"phase"] isEqual:@"cancelling"] && [[annotation status][@"capture"] isEqual:@"stream-cancel"]);
    assert(![annotation cancel] && ![annotation stop] && stream.stops == 1 && releases == 1);
    [annotation recordingOutputDidFinishRecording:output];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(!imports && [NSFileManager.defaultManager fileExistsAtPath:movie.path]);
    stream.completion(nil);
    stream.completion = nil;
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    NSDictionary *cancelled = [annotation status];
    assert(!annotation.busy && stream.stops == 1 && releases == 2 && [released isEqual:@"stream-cancel"]);
    assert([cancelled[@"phase"] isEqual:@"cancelled"] && [cancelled[@"last"][@"source"] isEqual:@"window"]);
    assert(![NSFileManager.defaultManager fileExistsAtPath:folder.path] && imports == 0);

    annotation.identifier = @"stream-save";
    annotation.source = @"display";
    annotation.stream = (id)(stream = AnnotationStreamFixture.new);
    annotation.leased = YES;
    annotation.movie = movie;
    annotation.narration = [folder URLByAppendingPathComponent:@"Narration.wav"];
    annotation.narrationRequested = YES;
    [annotation state:@"recording" message:@""];
    [annotation preparePanels:screen target:screen.frame];
    [annotation.controls orderFrontRegardless];
    [annotationButton(annotation.controls.contentView, JardLocalized(@"Discard…")) performClick:nil];
    [discard sendAction:discard.action to:discard.target];
    assert(stream.stops == 0 && annotation.controls.visible);
    assert([annotation stop] && [[annotation status][@"phase"] isEqual:@"saving"] && stream.stops == 1);
    assert(!annotation.controls.visible);
    assert(![annotation stop] && ![annotation cancel]);
    [annotation submitAnnotation];
    assert(!imports && audioReply);
    audioReply(@{@"ready": @YES}, nil);
    audioReply = nil;
    assert(imports == 1 && annotation.busy && !saved);
    importReply(@{@"id": @"stream-save", @"routing": @{@"phase": @"transcribing"}}, nil);
    importReply = nil;
    NSDictionary *finished = [annotation status];
    assert(!annotation.busy && saved == 1 && releases == 3 && [released isEqual:@"stream-save"]);
    assert([finished[@"session"] isEqual:@"stream-save"] && [finished[@"last"][@"id"] isEqual:finished[@"session"]]);
    assert([finished[@"last"][@"phase"] isEqual:@"finished"] && ![annotation cancel]);
    puts("Annotation toolbar checked nonactivating discard confirmation, Keep Recording, hover expiry, late leases, and disposable recording cleanup.");
}

static void testAnnotationFeedback(NSURL *source) {
    __block JardDictationReply importReply = nil;
    AnnotationFeedbackFixture *annotation = [[AnnotationFeedbackFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([command isEqual:@"jard.annotationAudio"]) {
                assert([payload[@"action"] isEqual:@"stop"]);
                reply(@{}, nil);
            } else {
                assert([command isEqual:@"jard.importAnnotation"] && !importReply);
                importReply = [reply copy];
            }
        } stateChanged:nil];
    NSWindow *focus = NSApp.keyWindow;
    NSScreen *screen = NSScreen.mainScreen;
    NSButton *previousReveal = nil;
    NSURL *olderRecovery = nil;
    for (NSUInteger attempt = 0; attempt < 3; attempt++) {
        NSURL *folder = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString]
            isDirectory:YES];
        NSURL *movie = [folder URLByAppendingPathComponent:@"Feedback fixture.mp4"];
        assert([NSFileManager.defaultManager createDirectoryAtURL:folder withIntermediateDirectories:NO attributes:nil error:nil]);
        assert([NSFileManager.defaultManager copyItemAtURL:source toURL:movie error:nil]);
        annotation.identifier = NSUUID.UUID.UUIDString.lowercaseString;
        annotation.source = @"display";
        annotation.recording = (id)NSObject.new;
        annotation.stream = (id)AnnotationStreamFixture.new;
        annotation.directory = folder;
        annotation.movie = movie;
        annotation.narration = [folder URLByAppendingPathComponent:@"Narration.wav"];
        annotation.narrationRequested = YES;
        [annotation preparePanels:screen target:screen.frame];
        [annotation.controls orderFrontRegardless];
        NSRect toolbar = annotation.controls.frame;
        [annotation state:@"recording" message:@""];
        assert([annotation stop] && !annotation.feedbackPanel && !annotation.controls.visible);
        [annotation recordingOutputDidFinishRecording:annotation.recording];
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:8];
        while (!importReply && [deadline timeIntervalSinceNow] > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        NSPanel *panel = annotation.feedbackPanel;
        assert(importReply && panel.visible && !panel.canBecomeKeyWindow && NSApp.keyWindow == focus);
        assert(NSEqualRects(panel.frame, toolbar));
        assert([((NSTextField *)panel.contentView.subviews.firstObject).stringValue isEqual:JardLocalized(@"Processing…")]);
        [previousReveal sendAction:previousReveal.action to:previousReveal.target];
        assert(!annotation.revealedRecording && annotation.feedbackPanel == panel);
        JardDictationReply reply = importReply;
        importReply = nil;
        if (attempt == 2) {
            reply(@{@"id": @"saved-fixture"}, nil);
            assert(!annotation.feedbackPanel && !panel.visible && !annotation.busy);
            assert([[annotation status][@"session"] isEqual:@"saved-fixture"]);
        } else {
            reply(nil, @"Fixture could not publish the annotation.");
            assert(annotation.feedbackPanel == panel && panel.visible && !annotation.busy);
            assert([[annotation status][@"recoverable"] boolValue]);
            assert([((NSTextField *)panel.contentView.subviews.firstObject).stringValue isEqual:JardLocalized(@"Annotation failed")]);
            NSButton *reveal = annotationButton(panel.contentView, JardLocalized(@"Show Recording in Finder"));
            assert(reveal && annotationButton(panel.contentView, JardLocalized(@"Dismiss")));
            if (!attempt) {
                [reveal performClick:nil];
                assert([annotation.revealedRecording isEqual:movie]);
                annotation.revealedRecording = nil;
            } else {
                NSTimeInterval later = NSProcessInfo.processInfo.systemUptime + 11;
                [annotation expireFeedbackAt:later pointer:NSMakePoint(NSMidX(panel.frame), NSMidY(panel.frame))];
                assert(panel.visible);
                [annotation expireFeedbackAt:later pointer:NSMakePoint(NSMinX(panel.frame) - 1, NSMinY(panel.frame) - 1)];
            }
            assert(!annotation.feedbackPanel && !panel.visible && [[annotation status][@"recoverable"] boolValue]);
            assert([NSFileManager.defaultManager fileExistsAtPath:movie.path]);
            if (!attempt) assert([NSFileManager.defaultManager removeItemAtURL:folder error:nil]);
            else olderRecovery = movie;
            previousReveal = reveal;
        }
        assert(NSApp.keyWindow == focus);
    }
    annotation.identifier = NSUUID.UUID.UUIDString.lowercaseString;
    [annotation state:@"starting" message:@""];
    [annotation fail:@"Fixture could not start the capture." keepMovie:NO];
    NSPanel *failure = annotation.feedbackPanel;
    assert(failure.visible && [[annotation status][@"recoverable"] boolValue]);
    assert(!annotationButton(failure.contentView, JardLocalized(@"Show Recording in Finder")));
    [annotationButton(failure.contentView, JardLocalized(@"Dismiss")) performClick:nil];
    assert(!annotation.feedbackPanel && [NSFileManager.defaultManager fileExistsAtPath:olderRecovery.path]);
    assert([NSFileManager.defaultManager removeItemAtURL:olderRecovery.URLByDeletingLastPathComponent error:nil]);
    [annotation shutdown];
    puts("Annotation feedback followed file finalization, revealed the exact recovered file, and cleared for the saved preview.");
}

static void testAnnotationResultPanel(void) {
    assert(NSScreen.mainScreen);
    __block BOOL available = YES;
    __block BOOL pasted = NO;
    __block NSString *selected;
    __block JardDictationReply finish;
    __block JardDictationReply routeReply;
    NSMutableArray<NSString *> *routeActions = NSMutableArray.array;
    JardVisibleResultPanelFixture *panel = [[JardVisibleResultPanelFixture alloc] initWithAvailability:^BOOL{
        return available;
    } action:^(NSString *session, BOOL paste, JardDictationReply reply) {
        selected = session;
        pasted = paste;
        finish = [reply copy];
    } routeAction:^(NSString *session, NSString *action, BOOL held, JardDictationReply reply) {
        assert(session.length);
        [routeActions addObject:action];
        routeReply = [reply copy];
    }];
    NSImage *preview = [NSImage imageWithSystemSymbolName:@"square" accessibilityDescription:nil];
    NSString *first = NSUUID.UUID.UUIDString, *second = NSUUID.UUID.UUIDString;
    [panel showSession:first preview:preview routing:nil];
    NSPanel *window = [panel valueForKey:@"window"];
    assert(window && !window.canBecomeKeyWindow && !window.canBecomeMainWindow);
    assert(window.sharingType == NSWindowSharingNone);
    panel.showOverlaysInRecordings = YES;
    assert(window.sharingType == NSWindowSharingReadOnly);
    panel.showOverlaysInRecordings = NO;
    assert(window.sharingType == NSWindowSharingNone);
    assert(window.styleMask & NSWindowStyleMaskNonactivatingPanel);
    assert(window.collectionBehavior & NSWindowCollectionBehaviorCanJoinAllSpaces);
    NSButton *inspect = [panel valueForKey:@"inspectButton"], *thumbnail = [panel valueForKey:@"thumbnail"];
    assert(thumbnail.image == preview && thumbnail.enabled && inspect.enabled);
    [thumbnail performClick:nil];
    assert([selected isEqual:first] && !pasted && finish && !inspect.enabled);
    finish(nil, @"Contact sheet unavailable");
    assert(inspect.enabled && [[[panel valueForKey:@"status"] stringValue] isEqual:@"Contact sheet unavailable"]);
    [inspect performClick:nil];
    JardDictationReply stale = finish;
    [panel showSession:second preview:nil routing:nil];
    stale(@YES, nil);
    assert([[panel valueForKey:@"session"] isEqual:second]);
    assert(![[panel valueForKey:@"inspectButton"] isEnabled]);
    [[panel valueForKey:@"pasteButton"] performClick:nil];
    assert([selected isEqual:second] && pasted && finish);
    finish(@YES, nil);
    assert(![panel valueForKey:@"window"]);

    NSDictionary *(^route)(NSString *, NSString *) = ^NSDictionary *(NSString *capture, NSString *phase) {
        return @{@"capture": capture, @"phase": phase, @"title": @"Test session", @"seconds": @3, @"message": @""};
    };
    [panel showSession:first preview:preview routing:route(first, @"transcribing")];
    window = [panel valueForKey:@"window"];
    NSButton *delivery = [panel valueForKey:@"pasteButton"];
    assert(delivery.enabled && [delivery.title isEqual:JardLocalized(@"Cancel send")] && !routeActions.count);
    assert(![panel updateRouting:route(second, @"offer")]);
    assert([panel updateRouting:route(first, @"offer")] && [panel valueForKey:@"window"] == window);
    assert([panel updateRouting:route(first, @"offer")] && [routeActions isEqualToArray:@[@"shown"]]);
    NSPoint mouse = NSEvent.mouseLocation;
    [window setFrameOrigin:NSMakePoint(mouse.x - 5, mouse.y - 5)];
    assert([panel updateRouting:route(first, @"countdown")] && [routeActions.lastObject isEqual:@"hold"]);
    [window setFrameOrigin:NSMakePoint(mouse.x + 10, mouse.y + 10)];
    assert([panel updateRouting:route(first, @"held")] && [routeActions.lastObject isEqual:@"resume"]);
    assert([panel updateRouting:route(first, @"preparing")]);
    assert(![panel confirmationStateForCapture:second]);
    assert([[panel confirmationStateForCapture:first] isEqual:@"unavailable"]);
    panel.onScreen = YES;
    assert([[panel confirmationStateForCapture:first] isEqual:@"ready"]);
    [window setFrameOrigin:NSMakePoint(mouse.x - 5, mouse.y - 5)];
    assert([[panel confirmationStateForCapture:first] isEqual:@"held"]);
    [window setFrameOrigin:NSMakePoint(mouse.x + 10, mouse.y + 10)];
    [[panel valueForKey:@"inspectButton"] performClick:nil];
    assert([selected isEqual:first] && !pasted);
    finish(@YES, nil);
    assert([panel valueForKey:@"window"] == window);
    [delivery performClick:nil];
    assert([routeActions.lastObject isEqual:@"cancel"] && routeReply && !delivery.enabled);
    assert([[panel confirmationStateForCapture:first] isEqual:@"unavailable"]);
    routeReply(route(first, @"cancelled"), nil);
    assert(delivery.enabled && [delivery.title isEqual:JardLocalized(@"Paste")]);
    assert([panel updateRouting:route(first, @"sending")] && delivery.hidden);
    [[panel valueForKey:@"inspectButton"] performClick:nil];
    finish(@YES, nil);
    assert([panel valueForKey:@"window"] == window);
    assert([panel updateRouting:route(first, @"sent")] && delivery.hidden && [[panel valueForKey:@"inspectButton"] isEnabled]);
    NSMutableDictionary *sent = [route(first, @"sent") mutableCopy];
    sent[@"receiptStatus"] = @"queued";
    sent[@"undoUntil"] = @((NSDate.date.timeIntervalSince1970 + 60) * 1000);
    [window setFrameOrigin:NSMakePoint(mouse.x - 5, mouse.y - 5)];
    assert([panel updateRouting:sent] && !delivery.hidden && [delivery.title isEqual:JardLocalized(@"Undo send")]);
    assert(!delivery.keyEquivalent.length);
    NSProgressIndicator *progress = [panel valueForKey:@"undoProgress"];
    assert(!progress.hidden && progress.doubleValue == 5);
    [panel setValue:@(NSProcessInfo.processInfo.systemUptime - 6) forKey:@"undoLastTick"];
    [panel tick];
    assert(!delivery.hidden && progress.doubleValue == 5);
    [window setFrameOrigin:NSMakePoint(mouse.x + 10, mouse.y + 10)];
    [panel tick];
    [panel setValue:@(NSProcessInfo.processInfo.systemUptime - 6) forKey:@"undoLastTick"];
    [panel tick];
    assert(delivery.hidden && progress.hidden);
    assert([panel updateRouting:sent] && delivery.hidden);
    [panel showSession:first preview:preview routing:sent];
    delivery = [panel valueForKey:@"pasteButton"];
    [delivery performClick:nil];
    assert([routeActions.lastObject isEqual:@"undo"] && routeReply && !delivery.enabled);
    NSMutableDictionary *withdrawn = [route(first, @"sent") mutableCopy];
    withdrawn[@"receiptStatus"] = @"withdrawn";
    routeReply(withdrawn, nil);
    assert(delivery.hidden && [[[panel valueForKey:@"heading"] stringValue] isEqual:JardLocalized(@"Taken back")]);
    [panel showRoute:route(second, @"offer")];
    assert([[panel valueForKey:@"session"] isEqual:second] && [routeActions.lastObject isEqual:@"shown"]);
    [panel endForNextCapture];
    assert(![panel valueForKey:@"window"] && [routeActions.lastObject isEqual:@"cancel"]);
    available = NO;
    [panel showSession:first preview:preview routing:nil];
    assert(![panel valueForKey:@"window"]);
    NSUInteger before = routeActions.count;
    [panel showRoute:route(first, @"offer")];
    assert(routeActions.count == before + 1 && [routeActions.lastObject isEqual:@"cancel"]);
    puts("The annotation preview shared the countdown and the bounded Undo action, including hover, expiry and withdrawal.");
}

static void testAnnotationDestinations(void) {
    JardAnnotationResultPanel *panel = [[JardAnnotationResultPanel alloc] initWithAvailability:^BOOL{ return YES; }
        action:^(NSString *session, BOOL paste, JardDictationReply reply) { assert(NO); }
        routeAction:^(NSString *session, NSString *action, BOOL held, JardDictationReply reply) { assert(NO); }];
    NSString *key = NSUUID.UUID.UUIDString, *delivery = NSUUID.UUID.UUIDString;
    NSMutableArray<NSDictionary *> *requests = NSMutableArray.array;
    NSMutableArray *replies = NSMutableArray.array;
    NSMutableArray<NSString *> *cues = NSMutableArray.array;
    panel.playCue = ^(NSString *name) { [cues addObject:name]; };
    panel.destinationRequest = ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        assert([payload[@"id"] isEqual:key]);
        [requests addObject:@{@"command": command, @"payload": payload}];
        [replies addObject:[reply copy]];
    };
    [panel showSession:key preview:nil routing:nil];
    NSWindow *window = [panel valueForKey:@"window"];
    NSRect collapsed = window.frame;
    NSButton *open = [panel valueForKey:@"sendToButton"];
    assert(open.enabled && !open.hidden && !window.canBecomeKeyWindow);
    [open performClick:nil];
    id picker = [panel valueForKey:@"picker"];
    assert(picker && window.canBecomeKeyWindow && NSMaxY(window.frame) == NSMaxY(collapsed));
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:1];
    while (!requests.count && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
    assert([requests.lastObject[@"command"] isEqual:@"jard.annotationDestinations"]);
    assert([requests.lastObject[@"payload"][@"query"] isEqual:@""]);
    NSSearchField *field = [picker valueForKey:@"search"];
    field.stringValue = @"route";
    id<NSSearchFieldDelegate> delegate = field.delegate;
    [delegate controlTextDidChange:[NSNotification notificationWithName:NSControlTextDidChangeNotification object:field]];
    NSDictionary *list = @{@"service": @"pinned", @"sessions": @[
        @{@"id": @"ses_first", @"title": @"Shared title", @"directory": @"/one"},
        @{@"id": @"ses_second", @"title": @"Shared title", @"directory": @"/two"}],
        @"suggestion": @{@"sessionId": @"ses_first", @"reason": @"Same project"},
        @"scopes": @[@{@"kind": @"site", @"label": @"example.test"}, @{@"kind": @"app", @"label": @"Browser"}]};
    ((JardDictationReply)replies[0])(list, nil);
    NSTableView *table = [picker valueForKey:@"table"];
    assert(table.numberOfRows == 0);
    deadline = [NSDate dateWithTimeIntervalSinceNow:1];
    while (requests.count < 2 && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
    assert(requests.count == 2 && [requests.lastObject[@"payload"][@"query"] isEqual:@"route"]);
    ((JardDictationReply)replies[1])(list, nil);
    assert(table.numberOfRows == 2 && table.selectedRow == -1 && [[picker valueForKey:@"reason"] isEqual:@"Same project"]);
    NSButton *remember = [picker valueForKey:@"rememberButton"];
    assert(!remember.hidden && remember.state == NSControlStateValueOff && [remember.title containsString:@"example.test"]);
    NSTextView *fieldEditor = [[NSTextView alloc] initWithFrame:NSZeroRect];
    [delegate control:field textView:fieldEditor doCommandBySelector:@selector(insertNewline:)];
    assert(requests.count == 2);
    [delegate control:field textView:fieldEditor doCommandBySelector:@selector(moveDown:)];
    [delegate control:field textView:fieldEditor doCommandBySelector:@selector(moveDown:)];
    assert(table.selectedRow == 1);
    NSEvent *shiftReturn = [NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:NSEventModifierFlagShift
        timestamp:0 windowNumber:window.windowNumber context:nil characters:@"\r" charactersIgnoringModifiers:@"\r"
        isARepeat:NO keyCode:kVK_Return];
    [table keyDown:shiftReturn];
    assert(requests.count == 3 && [requests.lastObject[@"command"] isEqual:@"jard.annotationPanelSend"]);
    assert([requests.lastObject[@"payload"][@"target"] isEqual:@"ses_second"] && [requests.lastObject[@"payload"][@"service"] isEqual:@"pinned"]);
    assert([requests.lastObject[@"payload"][@"rememberScope"] isEqual:@"site"]);
    [delegate control:field textView:fieldEditor doCommandBySelector:@selector(insertNewline:)];
    assert(requests.count == 3);
    NSDictionary *sent = @{@"capture": key, @"phase": @"sent", @"title": @"Shared title", @"deliveryId": delivery,
        @"receiptStatus": @"queued", @"undoUntil": @((NSDate.date.timeIntervalSince1970 + 60) * 1000)};
    ((JardDictationReply)replies[2])(sent, nil);
    assert([panel updateRouting:sent] && [cues isEqual:@[@"annotationSent"]]);
    assert(![panel valueForKey:@"picker"] && !window.canBecomeKeyWindow && NSEqualRects(window.frame, collapsed) && open.hidden);
    [[panel valueForKey:@"pasteButton"] performClick:nil];
    assert([requests.lastObject[@"command"] isEqual:@"jard.annotationPanelUndo"] && [requests.lastObject[@"payload"][@"deliveryId"] isEqual:delivery]);
    NSDictionary *withdrawn = @{@"capture": key, @"phase": @"sent", @"receiptStatus": @"withdrawn", @"deliveryId": delivery};
    ((JardDictationReply)replies[3])(withdrawn, nil);
    assert(([panel updateRouting:withdrawn] && [cues isEqual:@[@"annotationSent", @"annotationUndo"]]));
    assert(!open.hidden);
    [open performClick:nil];
    [(NSResponder *)[panel valueForKey:@"picker"] cancelOperation:nil];
    assert(![panel valueForKey:@"picker"] && !window.canBecomeKeyWindow);
    [panel dismiss];
    puts("The native destination picker kept search generations, required an explicit selection, and sent and undid the exact session IDs.");
}

static NSEvent *mouseEvent(NSEventType type, NSPoint location, NSPanel *panel);

static const CGKeyCode annotationFixtureCode = kVK_ANSI_O;
static const CGEventFlags annotationFixtureFlags = kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
static const JardAnnotationShortcut annotationFixtureShortcut = {annotationFixtureCode, annotationFixtureFlags, 0};

static BOOL annotationKey(JardAnnotation *annotation, CGEventType type, CGKeyCode code, CGEventFlags flags, BOOL allowStart,
                          void (^start)(CGPoint)) {
    CGEventRef event = CGEventCreateKeyboardEvent(NULL, code, type != kCGEventKeyUp);
    CGEventSetFlags(event, flags);
    CGEventSetLocation(event, type == kCGEventFlagsChanged ? CGPointMake(398, 240) : CGPointMake(95, 112));
    JardAnnotationShortcut shortcut = {annotationFixtureCode, annotationFixtureFlags, flags};
    BOOL consumed = [annotation handleEvent:type event:event shortcut:shortcut allowStart:allowStart start:start];
    CFRelease(event);
    return consumed;
}

static void movePen(JardAnnotation *annotation, CGFloat x, CGFloat y) {
    CGFloat height = CGRectGetHeight(CGDisplayBounds(CGMainDisplayID()));
    CGEventRef event = CGEventCreateMouseEvent(NULL, kCGEventMouseMoved, CGPointMake(x, height - y), kCGMouseButtonLeft);
    assert(![annotation handleEvent:kCGEventMouseMoved event:event shortcut:annotationFixtureShortcut allowStart:NO start:nil]);
    CFRelease(event);
}

static NSArray *inkEvents(JardAnnotation *annotation) { return [annotation.ink valueForKey:@"events"]; }

static void testAnnotationKeys(void) {
    const CGEventFlags chord = kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
    const CGEventFlags shift = kCGEventFlagMaskShift;
    JardAnnotationKeys keys = {0};
    assert(!JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationChordDown, 1.1, 1, JardAnnotationChord, JardAnnotationChord, true, false));
    assert(JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationChordDown, 1.2, 1, JardAnnotationChord, JardAnnotationChord, false, false) == JardAnnotationTake);
    assert(JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationChordUp, 1.3, 1, JardAnnotationChord, JardAnnotationChord, false, false) == JardAnnotationTake);
    assert(JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationChordDown, 2, 1, JardAnnotationChord, JardAnnotationChord, false, false) ==
        (JardAnnotationTake | JardAnnotationArmDraw));
    assert(JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationModifiers, 2.2, 1, JardAnnotationChord, JardAnnotationShift, false, false) ==
        JardAnnotationSave);
    assert(!keys.shiftPen && !keys.heldPen);
    assert(JardAnnotationKey(&keys, JardAnnotationUnavailable, JardAnnotationChordUp, 2.3, 1, JardAnnotationChord, JardAnnotationShift, false, false) ==
        JardAnnotationTake);
    keys = (JardAnnotationKeys){0};
    assert(JardAnnotationKey(&keys, JardAnnotationReady, JardAnnotationChordDown, 3, 0, 0, 0, false, false) == JardAnnotationTake);
    assert(JardAnnotationKey(&keys, JardAnnotationReady, JardAnnotationChordUp, 3.1, 0, 0, 0, false, false) ==
        (JardAnnotationTake | JardAnnotationBegin));
    assert(JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationChordDown, 4, 3.1, JardAnnotationCommand, JardAnnotationCommand, false, false) ==
        (JardAnnotationTake | JardAnnotationArmDraw));
    JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationHoldTimer, 4.3, 3.1, JardAnnotationCommand, JardAnnotationCommand, false, false);
    assert(keys.heldPen);
    assert(JardAnnotationKey(&keys, JardAnnotationRecording, JardAnnotationChordUp, 4.4, 3.1, JardAnnotationCommand, 0, false, false) ==
        (JardAnnotationTake | JardAnnotationEndDrawing));

    JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        assert(false && "Synthetic input must not request a capture lease itself.");
    } stateChanged:nil];
    __block NSUInteger starts = 0;
    void (^start)(CGPoint) = ^(CGPoint pointer) { assert(CGPointEqualToPoint(pointer, CGPointMake(398, 240))); starts++; };
    assert(!annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, NO, start));
    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, YES, start));
    assert(annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, chord, YES, start));
    assert(starts == 0);
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Option, shift, YES, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(starts == 1);
    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, YES, start));
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Shift, 0, NO, start));
    assert(annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, 0, NO, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(starts == 1);

    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, YES, start));
    assert(annotationKey(annotation, kCGEventKeyDown, kVK_Escape, chord, YES, start));
    assert(annotationKey(annotation, kCGEventKeyUp, kVK_Escape, chord, YES, start));
    assert(annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, chord, YES, start));
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Shift, 0, YES, start));
    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, YES, start));
    assert(![annotation handleEvent:kCGEventTapDisabledByTimeout event:NULL shortcut:annotationFixtureShortcut allowStart:NO start:start]);
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Shift, 0, YES, start));
    assert(!annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, 0, YES, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(starts == 1);
    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, YES, start));
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Shift, 0, YES, start));
    assert(![annotation handleEvent:kCGEventTapDisabledByTimeout event:NULL shortcut:annotationFixtureShortcut allowStart:NO start:start]);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(starts == 1);

    annotation.identifier = @"fixture-picker";
    [annotation state:@"picking" message:@""];
    assert(annotationKey(annotation, kCGEventKeyDown, kVK_Escape, 0, YES, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(!annotation.busy);
    assert(annotationKey(annotation, kCGEventKeyUp, kVK_Escape, 0, YES, start));

    NSScreen *screen = NSScreen.mainScreen;
    NSRect bounds = NSMakeRect(NSMidX(screen.frame) - 150, NSMidY(screen.frame) - 150, 300, 180);
    annotation.identifier = @"gesture-fixture";
    [annotation preparePanels:screen target:bounds];
    [annotation.ink setValue:@(NSProcessInfo.processInfo.systemUptime - 1) forKey:@"startTime"];
    [annotation state:@"recording" message:@""];
    [annotation.overlay orderFrontRegardless];
    [annotation.controls orderFrontRegardless];
    NSButton *pen = annotation.penButton;
    assert([annotation.controls.accessibilityLabel isEqual:JardLocalized(@"Annotate")] && !annotation.controls.canBecomeKeyWindow);
    assert([pen.accessibilityRole isEqual:NSAccessibilityCheckBoxRole] && [pen.accessibilityLabel isEqual:JardLocalized(@"Draw")]);
    assert(pen.accessibilityHelp.length && [pen.accessibilityValue isEqual:@0]);
    [pen performClick:nil];
    assert([pen.accessibilityValue isEqual:@1] && !annotation.overlay.ignoresMouseEvents);
    assert([pen accessibilityPerformPress]);
    assert([pen.accessibilityValue isEqual:@0] && annotation.overlay.ignoresMouseEvents);
    assert(!annotationKey(annotation, kCGEventKeyDown, kVK_Escape, 0, NO, start));
    assert(annotation.overlay.ignoresMouseEvents);
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Shift, shift, NO, start));
    assert(!annotation.overlay.ignoresMouseEvents);
    assert([pen.accessibilityValue isEqual:@1]);
    [annotation.ink mouseDown:mouseEvent(NSEventTypeLeftMouseDown, NSMakePoint(60, 36), annotation.overlay)];
    [annotation.ink mouseDragged:mouseEvent(NSEventTypeLeftMouseDragged, NSMakePoint(90, 54), annotation.overlay)];
    [annotation.ink mouseUp:mouseEvent(NSEventTypeLeftMouseUp, NSMakePoint(90, 54), annotation.overlay)];
    assert(inkEvents(annotation).count == 1 && NSEqualPoints([inkEvents(annotation).lastObject[@"to"] pointValue], NSMakePoint(.3, .3)));
    assert([inkEvents(annotation).lastObject[@"liftedAt"] doubleValue] >= [inkEvents(annotation).lastObject[@"time"] doubleValue]);
    assert(annotationKey(annotation, kCGEventKeyDown, kVK_Escape, shift, NO, start));
    assert(annotationKey(annotation, kCGEventKeyUp, kVK_Escape, shift, NO, start));
    assert(annotation.overlay.ignoresMouseEvents && annotation.busy);
    assert([pen.accessibilityValue isEqual:@0]);
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Shift, 0, NO, start));

    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, NO, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.3]];
    assert(!annotation.overlay.ignoresMouseEvents);
    assert([pen.accessibilityValue isEqual:@1] && [pen.accessibilityLabel isEqual:JardLocalized(@"Draw")]);
    movePen(annotation, NSMinX(bounds) - 5, NSMinY(bounds) - 5);
    movePen(annotation, NSMinX(bounds) + 75, NSMinY(bounds) + 45);
    movePen(annotation, NSMinX(bounds) + 150, NSMinY(bounds) + 90);
    NSDictionary *first = inkEvents(annotation).lastObject;
    assert(NSEqualPoints([first[@"from"] pointValue], NSMakePoint(.25, .25)));
    assert(NSEqualPoints([first[@"to"] pointValue], NSMakePoint(.5, .5)));
    NSUInteger count = inkEvents(annotation).count;
    movePen(annotation, NSMaxX(bounds) + 5, NSMaxY(bounds) + 5);
    movePen(annotation, NSMinX(bounds) + 15, NSMinY(bounds) + 9);
    assert(inkEvents(annotation).count == count);
    movePen(annotation, NSMinX(bounds) + 27, NSMinY(bounds) + 18);
    assert(inkEvents(annotation).count == count + 1 && ![first[@"stroke"] isEqual:inkEvents(annotation).lastObject[@"stroke"]]);
    assert(annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, chord, NO, start));
    assert(annotation.overlay.ignoresMouseEvents && [[annotation status][@"phase"] isEqual:@"recording"]);
    assert([pen.accessibilityValue isEqual:@0]);
    NSUInteger savedEdges = inkEvents(annotation).count;
    assert([[annotation.ink valueForKey:@"strokes"] count] > 0);
    NSDate *fadeDeadline = [NSDate dateWithTimeIntervalSinceNow:2.1];
    while ([annotation.ink valueForKey:@"inkTimer"] && fadeDeadline.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(![annotation.ink valueForKey:@"inkTimer"] && ![[annotation.ink valueForKey:@"strokes"] count]);
    assert(inkEvents(annotation).count == savedEdges && inkEvents(annotation).lastObject[@"liftedAt"]);

    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, NO, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.3]];
    assert(!annotation.overlay.ignoresMouseEvents);
    assert(![annotation handleEvent:kCGEventTapDisabledByTimeout event:NULL shortcut:annotationFixtureShortcut allowStart:NO start:nil]);
    assert(annotation.overlay.ignoresMouseEvents && [[annotation status][@"phase"] isEqual:@"recording"]);
    assert(!annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, 0, NO, start));

    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, chord, NO, start));
    assert(annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, chord, NO, start));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert([[annotation status][@"phase"] isEqual:@"saving"] && !annotation.overlay.visible);
    [annotation shutdown];
    puts("Annotation shortcut tap, hold, Shift drag, Escape, target bounds, and tap interruption passed.");
}

static NSEvent *mouseEvent(NSEventType type, NSPoint location, NSPanel *panel) {
    return [NSEvent mouseEventWithType:type location:location modifierFlags:0 timestamp:0
        windowNumber:panel.windowNumber context:nil eventNumber:0 clickCount:1 pressure:1];
}

static void testAreaSelection(void) {
    NSRect bounds = NSMakeRect(0, 0, 1440, 900);
    NSRect drag = JardAnnotationDragRect(NSMakePoint(900, 300), NSMakePoint(-20, 970), bounds);
    assert(NSEqualRects(drag, NSMakeRect(0, 300, 900, 600)));
    NSRect display = NSMakeRect(-1440, 900, 1440, 900);
    NSRect area = NSOffsetRect(drag, display.origin.x, display.origin.y);
    assert(CGRectEqualToRect(JardAnnotationDisplaySourceRect(area, display), CGRectMake(0, 0, 900, 600)));
    assert(CGRectEqualToRect(JardAnnotationDisplaySourceRect(NSMakeRect(-100, 1000, 200, 300), display),
                             CGRectMake(1340, 500, 100, 300)));
    assert(CGRectIsEmpty(JardAnnotationDisplaySourceRect(NSMakeRect(50, 50, 100, 100), display)));
    assert(NSEqualRects(JardAnnotationControlsRect(bounds, NSZeroRect), NSMakeRect(554, 24, 332, 46)));
    NSRect lowerHalf = NSMakeRect(0, 0, 1440, 450);
    assert(NSIsEmptyRect(NSIntersectionRect(lowerHalf, JardAnnotationControlsRect(bounds, lowerHalf))));

    [NSApplication sharedApplication];
    NSScreen *screen = NSScreen.mainScreen;
    assert(screen);
    __block NSRect selected = NSZeroRect;
    __block JardRegionChoice choice = JardRegionCancelled;
    __block NSUInteger selections = 0;
    JardSelectionPanel *(^makePanel)(void) = ^{
        return JardAnnotationRegionPanel(screen, ^(JardRegionChoice next, NSRect result) {
            choice = next;
            selected = result;
            selections++;
        });
    };
    JardSelectionPanel *panel = makePanel();
    NSView *view = panel.contentView;
    [view mouseDown:mouseEvent(NSEventTypeLeftMouseDown, NSMakePoint(200, 200), panel)];
    [view mouseDragged:mouseEvent(NSEventTypeLeftMouseDragged, NSMakePoint(240, 240), panel)];
    [view mouseUp:mouseEvent(NSEventTypeLeftMouseUp, NSMakePoint(201, 201), panel)];
    assert(selections == 0);
    [view mouseDown:mouseEvent(NSEventTypeLeftMouseDown, NSMakePoint(220, 260), panel)];
    [view mouseDragged:mouseEvent(NSEventTypeLeftMouseDragged, NSMakePoint(90, 160), panel)];
    [view mouseUp:mouseEvent(NSEventTypeLeftMouseUp, NSMakePoint(90, 160), panel)];
    assert(selections == 1 && choice == JardRegionArea && NSEqualRects(selected, NSOffsetRect(NSMakeRect(90, 160, 130, 100),
                                                                 NSMinX(screen.frame), NSMinY(screen.frame))));
    [panel confirmSelection];
    assert(selections == 1);

    panel = makePanel();
    view = panel.contentView;
    [view mouseDown:mouseEvent(NSEventTypeLeftMouseDown, NSMakePoint(200, 200), panel)];
    [view mouseUp:mouseEvent(NSEventTypeLeftMouseUp, NSMakePoint(201, 201), panel)];
    assert(selections == 2 && choice == JardRegionDisplay && NSEqualRects(selected, panel.frame));

    panel = makePanel();
    view = panel.contentView;
    NSEvent *escape = [NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0 timestamp:0
        windowNumber:panel.windowNumber context:nil characters:@"" charactersIgnoringModifiers:@"" isARepeat:NO keyCode:kVK_Escape];
    [view keyDown:escape];
    assert(selections == 3 && choice == JardRegionCancelled && NSIsEmptyRect(selected));

    panel = makePanel();
    NSEvent *enter = [NSEvent keyEventWithType:NSEventTypeKeyDown location:NSZeroPoint modifierFlags:0 timestamp:0
        windowNumber:panel.windowNumber context:nil characters:@"\r" charactersIgnoringModifiers:@"\r" isARepeat:NO keyCode:kVK_Return];
    [panel.contentView keyDown:enter];
    assert(selections == 4 && choice == JardRegionDisplay);

    JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:nil stateChanged:nil];
    annotation.identifier = @"region-shortcut";
    annotation.selectionPanel = makePanel();
    [annotation state:@"selecting" message:@""];
    assert(annotationKey(annotation, kCGEventKeyDown, annotationFixtureCode, annotationFixtureFlags, NO, nil));
    assert(annotationKey(annotation, kCGEventKeyUp, annotationFixtureCode, annotationFixtureFlags, NO, nil));
    assert(selections == 4);
    assert(!annotationKey(annotation, kCGEventFlagsChanged, kVK_Option, 0, NO, nil));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.04]];
    assert(selections == 5 && choice == JardRegionDisplay);
    assert([annotation cancel]);
    puts("The area selector clipped the crop, kept short and reversed drags open, and handled click, Return, Escape and the shortcut.");
}

static void testPhonePicture(void) {
    NSRect bounds = NSMakeRect(10, 20, 300, 500);
    assert(NSEqualRects(JardPhonePictureRect(NSMakeSize(1200, 2400), bounds), NSMakeRect(35, 20, 250, 500)));
    assert(NSEqualRects(JardPhonePictureRect(NSMakeSize(2400, 1200), bounds), NSMakeRect(10, 195, 300, 150)));
    assert(NSIsEmptyRect(JardPhonePictureRect(NSZeroSize, bounds)));
    assert(NSIsEmptyRect(JardPhonePictureRect(NSMakeSize(1, 1), NSZeroRect)));
    puts("Wired screen ink bounds passed for portrait and landscape pictures.");
}

static void testRoutingOrigin(void) {
    NSDictionary *browser = @{@"app": @"com.fixture.Browser", @"name": @"Jard"};
    NSDictionary *site = @{@"app": @"com.fixture.Browser", @"name": @"Jard", @"site": @"example.test"};
    CGRect frame = CGRectMake(20, 40, 600, 420);
    NSDictionary *sample = @{@"pid": @42, @"app": site, @"title": @"Example",
        @"bounds": [NSValue valueWithRect:frame]};
    assert([JardAnnotationRoutingOrigin(sample, browser, 42, @"Example", frame) isEqual:site]);
    assert([JardAnnotationRoutingOrigin(sample, browser, 42, @"Different tab", frame) isEqual:browser]);
    assert([JardAnnotationRoutingOrigin(sample, browser, 41, @"Example", frame) isEqual:browser]);
    assert([JardAnnotationRoutingOrigin(nil, browser, 41, @"Window", frame) isEqual:browser]);
    assert([JardAnnotationRoutingOrigin(sample, nil, 0, nil, CGRectMake(0, 0, 700, 500)) isEqual:site]);
    assert(!JardAnnotationRoutingOrigin(sample, nil, 0, nil, CGRectMake(900, 0, 700, 500)));
    NSDictionary *terminal = @{@"app": @"com.apple.Terminal", @"name": @"Terminal"};
    NSDictionary *context = @{@"folder": @"/fixture/project/sub", @"project": @"/fixture/project"};
    NSDictionary *terminalSample = @{@"pid": @43, @"title": @"Project", @"bounds": [NSValue valueWithRect:frame],
        @"app": @{@"terminal": context}};
    assert([JardAnnotationRoutingOrigin(terminalSample, terminal, 43, @"Project", frame)[@"terminal"] isEqual:context]);
    assert(!JardAnnotationRoutingOrigin(terminalSample, terminal, 43, @"Another project", frame)[@"terminal"]);
    puts("Annotation origin matched the selected app, the exact browser window, and the captured display.");
}

static uint8_t sampleInkRed(AVAssetImageGenerator *generator, int frameIndex, CGFloat x, CGFloat y) {
    CGImageRef frame = [generator copyCGImageAtTime:CMTimeMake(frameIndex, 30) actualTime:NULL error:nil];
    assert(frame);
    size_t width = CGImageGetWidth(frame), height = CGImageGetHeight(frame);
    CGImageRef pixel = CGImageCreateWithImageInRect(frame, CGRectMake(floor(x * width), floor((1 - y) * height), 1, 1));
    CGImageRelease(frame);
    assert(pixel);
    uint8_t rgba[4] = {0};
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef sample = CGBitmapContextCreate(rgba, 1, 1, 8, 4, space, (CGBitmapInfo)kCGImageAlphaPremultipliedLast);
    CGColorSpaceRelease(space);
    assert(sample);
    CGContextDrawImage(sample, CGRectMake(0, 0, 1, 1), pixel);
    CGContextRelease(sample);
    CGImageRelease(pixel);
    return rgba[0];
}

static void testFilteredAnnotationMovie(NSURL *source, BOOL verifyPixels) {
    for (NSNumber *drawn in @[@NO, @YES]) {
        __block BOOL imported = NO;
        JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload,
            JardDictationReply reply) {
            if ([command isEqual:@"jard.annotationAudio"]) {
                assert([payload[@"action"] isEqual:@"stop"]);
                reply(@{}, nil);
                return;
            }
            assert([command isEqual:@"jard.importAnnotation"]);
            NSURL *movie = [NSURL fileURLWithPath:payload[@"path"]];
            assert([movie.lastPathComponent isEqual:@"Screen annotation.mp4"]);
            if (drawn.boolValue && verifyPixels) {
                AVURLAsset *asset = [AVURLAsset URLAssetWithURL:movie options:nil];
                AVAssetImageGenerator *generator = [AVAssetImageGenerator assetImageGeneratorWithAsset:asset];
                generator.requestedTimeToleranceBefore = generator.requestedTimeToleranceAfter = kCMTimeZero;
                assert(sampleInkRed(generator, 20, .35, .25) > 100);
                assert(sampleInkRed(generator, 20, .35, .75) < 30);
                assert(sampleInkRed(generator, 29, .35, .25) < 30);
                assert(sampleInkRed(generator, 36, .35, .25) > 100);
            } else if (!drawn.boolValue) {
                assert([[NSData dataWithContentsOfURL:movie] isEqual:[NSData dataWithContentsOfURL:source]]);
            }
            imported = YES;
            reply(@{@"id": @"filtered-fixture"}, nil);
        } stateChanged:nil];
        annotation.identifier = NSUUID.UUID.UUIDString.lowercaseString;
        annotation.directory = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:annotation.identifier]
            isDirectory:YES];
        assert([NSFileManager.defaultManager createDirectoryAtURL:annotation.directory withIntermediateDirectories:NO
            attributes:nil error:nil]);
        annotation.movie = [annotation.directory URLByAppendingPathComponent:@"Screen capture.mp4"];
        annotation.narration = [annotation.directory URLByAppendingPathComponent:@"Narration.wav"];
        assert([NSFileManager.defaultManager copyItemAtURL:source toURL:annotation.movie error:nil]);
        annotation.source = drawn.boolValue ? @"area" : @"display";
        annotation.compositeInk = YES;
        annotation.narrationRequested = YES;
        annotation.recording = (id)NSObject.new;
        NSScreen *screen = NSScreen.mainScreen;
        [annotation preparePanels:screen target:screen.visibleFrame];
        [annotation state:@"recording" message:@""];
        if (drawn.boolValue) {
            [[annotation.ink valueForKey:@"events"] addObject:
                JardAnnotationEdge(NSMakePoint(.15, .25), NSMakePoint(.55, .25), .5)];
            annotation.displayInkRanges = [NSMutableArray arrayWithObject:[NSValue valueWithCMTimeRange:
                CMTimeRangeMake(kCMTimeZero, CMTimeMake(8, 10))]];
            annotation.displayInkStart = @1.1;
        }
        [annotation recordingOutputDidFinishRecording:annotation.recording];
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:15];
        while (annotation.busy && deadline.timeIntervalSinceNow > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        assert(imported && !annotation.busy && [[annotation status][@"session"] isEqual:@"filtered-fixture"]);
    }
    puts("Filtered display capture kept the original video without ink, and area capture composited timed ink before import.");
}

static void testNarrationWithoutVideo(void) {
    __block NSUInteger imports = 0;
    __block JardDictationReply stopped = nil;
    JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload,
        JardDictationReply reply) {
        if ([command isEqual:@"jard.annotationAudio"]) {
            stopped = [reply copy];
            return;
        }
        assert([command isEqual:@"jard.importAnnotation"] && [payload[@"narrationOnly"] boolValue]);
        NSURL *narration = [NSURL fileURLWithPath:payload[@"path"]];
        assert([narration.lastPathComponent isEqual:@"Narration.wav"] && !payload[@"narration"]);
        NSURL *folder = narration.URLByDeletingLastPathComponent;
        NSData *manifest = [NSData dataWithContentsOfURL:[folder URLByAppendingPathComponent:@"annotation.json"]];
        NSDictionary *metadata = [NSJSONSerialization JSONObjectWithData:manifest options:0 error:nil];
        assert([metadata[@"missingVideo"] isEqual:@"permissionDenied"] && [metadata[@"frames"] count] == 0);
        assert([metadata[@"events"] count] == 1 && ![NSFileManager.defaultManager fileExistsAtPath:[folder.path
            stringByAppendingPathComponent:@"contact-sheet.png"]]);
        imports++;
        reply(@{@"id": @"narration-fixture"}, nil);
    } stateChanged:nil];
    annotation.identifier = NSUUID.UUID.UUIDString.lowercaseString;
    annotation.directory = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:annotation.identifier] isDirectory:YES];
    assert([NSFileManager.defaultManager createDirectoryAtURL:annotation.directory withIntermediateDirectories:NO attributes:nil error:nil]);
    annotation.movie = [annotation.directory URLByAppendingPathComponent:@"Screen capture.mp4"];
    annotation.narration = [annotation.directory URLByAppendingPathComponent:@"Narration.wav"];
    annotation.source = @"window";
    annotation.narrationRequested = YES;
    annotation.recording = (id)NSObject.new;
    NSScreen *screen = NSScreen.mainScreen;
    [annotation preparePanels:screen target:screen.visibleFrame];
    [annotation state:@"recording" message:@""];
    [[annotation.ink valueForKey:@"events"] addObject:JardAnnotationEdge(NSMakePoint(.1, .2), NSMakePoint(.5, .2), .3)];
    SCRecordingOutput *output = annotation.recording;
    NSError *failure = [NSError errorWithDomain:SCStreamErrorDomain code:SCStreamErrorUserDeclined userInfo:nil];
    [annotation recordingOutput:output didFailWithError:failure];
    [annotation recordingOutputDidFinishRecording:output];
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:5];
    while (!stopped && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(stopped && !imports);
    assert([[NSMutableData dataWithLength:128] writeToURL:annotation.narration atomically:YES]);
    stopped(@{}, nil);
    while (annotation.busy && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(imports == 1 && [annotation.status[@"session"] isEqual:@"narration-fixture"]);
    puts("Screen permission loss waited for narration, saved strokes and no contact sheet, and rejected a stale finish callback.");
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        BOOL verifyFade = argc == 4 && [@(argv[3]) isEqual:@"--verify-fade"];
        assert(argc == 3 || verifyFade);
        __block BOOL checkedLease = NO;
        JardAnnotation *annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload,
                JardDictationReply reply) {
            assert([command isEqual:@"jard.annotationLease"]);
            NSString *identifier = payload[@"id"];
            assert([[NSUUID alloc] initWithUUIDString:identifier] && [identifier isEqual:identifier.lowercaseString]);
            checkedLease = YES;
            reply(nil, @"Stop the fixture before requesting permissions.");
        } stateChanged:nil];
        [annotation start:@[] source:@"window" microphone:nil origin:nil quartzPointer:nil reply:^(id result, NSString *error) { assert(!result && error); }];
        assert(checkedLease && !annotation.busy);
        NSRect primary = JardAnnotationAppKitRect(CGRectMake(40, 60, 800, 400), 900);
        assert(NSEqualRects(primary, NSMakeRect(40, 440, 800, 400)));
        NSRect above = JardAnnotationAppKitRect(CGRectMake(-800, -200, 800, 100), 900);
        assert(NSEqualRects(above, NSMakeRect(-800, 1000, 800, 100)));
        assert(NSEqualPoints(JardAnnotationUnitPoint(NSMakePoint(100, 30), NSMakeSize(400, 120)), NSMakePoint(.25, .25)));
        assert(NSEqualPoints(JardAnnotationUnitPoint(NSMakePoint(-10, 200), NSMakeSize(400, 120)), NSMakePoint(0, 1)));
        testPhonePicture();
        testRoutingOrigin();
        testAreaSelection();
        testAnnotationKeys();
        testWindowTracking();
        testPointerDisplaySelection();
        testWindowSelection();
        testAnnotationCaptureFilters();
        testLiveCaptureFilter();
        testAnnotationControls();
        testAnnotationFeedback([NSURL fileURLWithPath:@(argv[1])]);
        testAnnotationResultPanel();
        testAnnotationDestinations();
        testFilteredAnnotationMovie([NSURL fileURLWithPath:@(argv[1])], verifyFade);
        testNarrationWithoutVideo();

        assert(JardAnnotationInkAlpha(.65) == 1 && fabs(JardAnnotationInkAlpha(.975) - .25) < .00001);
        assert(JardAnnotationInkAlpha(1.30) == 0);
        NSMutableDictionary *released = [JardAnnotationEdge(NSMakePoint(.15, .25), NSMakePoint(.55, .25), .50) mutableCopy];
        released[@"stroke"] = @1;
        released[@"liftedAt"] = @.50;
        NSArray *events = @[released,
            JardAnnotationEdge(NSMakePoint(.15, .55), NSMakePoint(.55, .55), .63),
            @{@"clear": @YES, @"time": @1.87},
            JardAnnotationEdge(NSMakePoint(.52, .72), NSMakePoint(.85, .72), 1.90)];
        __block BOOL finished = NO;
        __block NSError *failure = nil;
        JardRenderAnnotation([NSURL fileURLWithPath:@(argv[1])], [NSURL fileURLWithPath:@(argv[2])],
                                   events, ^(NSError *error) {
            dispatch_async(dispatch_get_main_queue(), ^{ failure = error; finished = YES; });
        });
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:30];
        while (!finished && deadline.timeIntervalSinceNow > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        if (failure) NSLog(@"%@", failure);
        assert(finished && !failure);
        NSArray *planned = JardAnnotationFrameTimes(events, 2);
        assert(planned.count >= 3 && planned.count <= 6);
        assert([planned[1] doubleValue] > .63 && [planned[1] doubleValue] < .80);
        finished = NO;
        NSURL *movie = [NSURL fileURLWithPath:@(argv[2])];
        if (verifyFade) {
            AVAssetImageGenerator *generator = [AVAssetImageGenerator assetImageGeneratorWithAsset:[AVURLAsset URLAssetWithURL:movie options:nil]];
            generator.requestedTimeToleranceBefore = generator.requestedTimeToleranceAfter = kCMTimeZero;
            uint8_t solid = sampleInkRed(generator, 30, .35, .25);
            uint8_t fading = sampleInkRed(generator, 44, .35, .25);
            uint8_t expired = sampleInkRed(generator, 55, .35, .25);
            uint8_t next = sampleInkRed(generator, 58, .7, .72);
            assert(sampleInkRed(generator, 55, .35, .55) > 160 && sampleInkRed(generator, 58, .35, .55) < 15);
            if (!(solid > 160 && fading > 15 && fading + 30 < solid && expired < 15 && next > 160))
                NSLog(@"Ink frame red samples were %u, %u, %u, %u", solid, fading, expired, next);
            assert(solid > 160 && fading > 15 && fading + 30 < solid && expired < 15 && next > 160);
        }
        NSString *source = [[@(argv[1]) pathExtension] isEqual:@"mov"] ? @"phone" : @"window";
        NSDictionary *origin = [source isEqual:@"window"]
            ? @{@"app": @"com.fixture.Browser", @"name": @"Jard", @"site": @"example.test"} : nil;
        JardWriteAnnotationAssets(movie, source, origin, events, nil, ^(NSImage *preview, NSError *error) {
            assert(error || (preview.size.width > 0 && preview.size.height > 0));
            failure = error;
            finished = YES;
        });
        deadline = [NSDate dateWithTimeIntervalSinceNow:25];
        while (!finished && deadline.timeIntervalSinceNow > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        assert(finished && !failure);
        NSURL *folder = movie.URLByDeletingLastPathComponent;
        NSData *json = [NSData dataWithContentsOfURL:[folder URLByAppendingPathComponent:@"annotation.json"]];
        NSDictionary *manifest = [NSJSONSerialization JSONObjectWithData:json options:0 error:nil];
        assert([manifest[@"source"] isEqual:source] && [manifest[@"version"] isEqual:@1]);
        assert(origin ? [manifest[@"origin"] isEqual:origin] : !manifest[@"origin"]);
        assert([manifest[@"events"] count] == 4 && [manifest[@"frames"] count] >= 2);
        assert([manifest[@"events"][0][@"liftedAt"] isEqual:@.50]);
        assert(([manifest[@"events"][0][@"from"] isEqual:@[@.15, @.25]] && [manifest[@"events"][2][@"clear"] boolValue]));
        NSURL *preview = [folder URLByAppendingPathComponent:@"contact-sheet.png"];
        CGImageSourceRef images = CGImageSourceCreateWithURL((__bridge CFURLRef)preview, NULL);
        assert(images);
        CGImageRef sheet = CGImageSourceCreateImageAtIndex(images, 0, NULL);
        assert(sheet && CGImageGetWidth(sheet) == 872 && CGImageGetHeight(sheet) >= 282);
        CGImageRelease(sheet);
        CFRelease(images);
        NSPasteboard *board = [NSPasteboard pasteboardWithUniqueName];
        NSString *markdown = @"# Screen annotation\n\n## Narration\nReview the chart.\n";
        NSData *png = [NSData dataWithContentsOfURL:preview];
        BOOL copiedImage = NO;
        assert(JardCopyAnnotationHandoff(board, board.changeCount, markdown, png, &copiedImage) && copiedImage);
        assert([[board stringForType:NSPasteboardTypeString] isEqual:markdown]);
        NSData *rtfd = [board dataForType:NSPasteboardTypeRTFD];
        NSAttributedString *rich = [[NSAttributedString alloc] initWithRTFD:rtfd documentAttributes:nil];
        NSTextAttachment *attachment = [rich attribute:NSAttachmentAttributeName atIndex:0 effectiveRange:nil];
        assert([attachment.fileWrapper.preferredFilename isEqual:@"contact-sheet.png"]);
        assert([attachment.fileWrapper.regularFileContents isEqual:png] && [rich.string containsString:markdown]);
        NSTextView *destination = [[NSTextView alloc] initWithFrame:NSMakeRect(0, 0, 800, 500)];
        destination.richText = destination.importsGraphics = YES;
        assert([destination readSelectionFromPasteboard:board type:NSPasteboardTypeRTFD]);
        assert([destination.string containsString:markdown] && [destination.textStorage attribute:NSAttachmentAttributeName atIndex:0 effectiveRange:nil]);
        assert(JardCopyAnnotationHandoff(board, board.changeCount, markdown, nil, &copiedImage) && !copiedImage);
        assert([[board stringForType:NSPasteboardTypeString] isEqual:markdown] && ![board dataForType:NSPasteboardTypeRTFD]);
        NSInteger staleClipboard = board.changeCount;
        [board clearContents];
        [board setString:@"New clipboard content" forType:NSPasteboardTypeString];
        assert(!JardCopyAnnotationHandoff(board, staleClipboard, markdown, png, &copiedImage));
        assert(!copiedImage && [[board stringForType:NSPasteboardTypeString] isEqual:@"New clipboard content"]);
        [board releaseGlobally];
        puts("Annotation clipboard handoff preserved Markdown and the contact sheet in a private pasteboard fixture.");
        puts("Live ink retired while strokes stayed in the manifest. The video and contact sheet were generated.");
        if (verifyFade) puts("Rendered frames verified full ink, the fade, expiry, Clear Ink, and the next stroke.");
    }
}
