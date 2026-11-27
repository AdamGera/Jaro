#import "jard_annotation.h"
#import "jard_annotation_ink.h"
#import "jard_annotation_keys.h"
#import "jard_annotation_region.h"
#import "jard_interface_language.h"
#import "jard_overlay_dock.h"
#import "jard_phone_annotation.h"
#import <AVFoundation/AVFoundation.h>
#import <Carbon/Carbon.h>
#import <ImageIO/ImageIO.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>

static NSData *screenshotJPEG(CGImageRef image) {
    if (!image || CGImageGetWidth(image) > 1600 || CGImageGetHeight(image) > 1600) return nil;
    for (NSNumber *quality in @[@.90, @.68, @.46, @.28]) {
        NSMutableData *bytes = NSMutableData.data;
        CGImageDestinationRef destination = CGImageDestinationCreateWithData((__bridge CFMutableDataRef)bytes,
            CFSTR("public.jpeg"), 1, NULL);
        if (!destination) return nil;
        NSDictionary *options = @{(__bridge NSString *)kCGImageDestinationLossyCompressionQuality: quality};
        CGImageDestinationAddImage(destination, image, (__bridge CFDictionaryRef)options);
        BOOL encoded = CGImageDestinationFinalize(destination);
        CFRelease(destination);
        if (encoded && bytes.length <= 512 * 1024) return bytes;
    }
    return nil;
}

BOOL JardCopyAnnotationHandoff(NSPasteboard *board, NSInteger expectedChangeCount, NSString *markdown, NSData *png, BOOL *includedImage) {
    if (includedImage) *includedImage = NO;
    if (!markdown.length) return NO;
    NSPasteboardItem *item = NSPasteboardItem.new;
    if (![item setString:markdown forType:NSPasteboardTypeString]) return NO;
    BOOL rich = NO;
    if (png.length && [[NSImage alloc] initWithData:png]) {
        NSFileWrapper *file = [[NSFileWrapper alloc] initRegularFileWithContents:png];
        file.preferredFilename = @"contact-sheet.png";
        NSTextAttachment *attachment = [[NSTextAttachment alloc] initWithFileWrapper:file];
        NSMutableAttributedString *contents = [[NSMutableAttributedString alloc]
            initWithAttributedString:[NSAttributedString attributedStringWithAttachment:attachment]];
        [contents appendAttributedString:[[NSAttributedString alloc] initWithString:[@"\n\n" stringByAppendingString:markdown]]];
        NSData *rtfd = [contents RTFDFromRange:NSMakeRange(0, contents.length) documentAttributes:@{}];
        if (rtfd) rich = [item setData:rtfd forType:NSPasteboardTypeRTFD];
    }
    if (board.changeCount != expectedChangeCount) return NO;
    [board clearContents];
    BOOL copied = [board writeObjects:@[item]];
    if (includedImage) *includedImage = copied && rich;
    return copied;
}

@interface JardLiveInkStroke : NSObject
@property(nonatomic, strong) NSBezierPath *path;
@property(nonatomic) NSTimeInterval liftedAt;
@end

@implementation JardLiveInkStroke
@end

@interface JardInkView : NSView
@property(nonatomic, strong) NSMutableArray<JardLiveInkStroke *> *strokes;
@property(nonatomic, strong) NSMutableArray<NSDictionary *> *events;
@property(nonatomic, strong) NSTimer *inkTimer;
@property(nonatomic) BOOL renderedInk;
@property(nonatomic) NSUInteger stroke;
@property(nonatomic) NSPoint lastPoint;
@property(nonatomic) NSTimeInterval startTime;
@property(nonatomic) NSUInteger visibleFrom;
@property(nonatomic) BOOL strokeActive, followsPointer;
- (void)beginStrokeAt:(NSPoint)point;
- (void)continueStrokeAt:(NSPoint)point;
- (void)endStroke;
- (void)finishInk;
- (void)clear;
@end
@implementation JardInkView
- (instancetype)initWithFrame:(NSRect)frame {
    if (!(self = [super initWithFrame:frame])) return nil;
    _strokes = NSMutableArray.array;
    _events = NSMutableArray.array;
    return self;
}
- (BOOL)acceptsFirstMouse:(NSEvent *)event { return YES; }
- (BOOL)isOpaque { return NO; }
- (void)mouseDown:(NSEvent *)event {
    if (!self.followsPointer) [self beginStrokeAt:[self convertPoint:event.locationInWindow fromView:nil]];
}
- (void)mouseDragged:(NSEvent *)event {
    if (!self.followsPointer) [self continueStrokeAt:[self convertPoint:event.locationInWindow fromView:nil]];
}
- (void)mouseUp:(NSEvent *)event { if (!self.followsPointer) [self endStroke]; }
- (void)beginStrokeAt:(NSPoint)point {
    [self endStroke];
    self.strokeActive = YES;
    self.lastPoint = JardAnnotationUnitPoint(point, self.bounds.size);
    self.stroke++;
    JardLiveInkStroke *stroke = JardLiveInkStroke.new;
    stroke.path = NSBezierPath.bezierPath;
    [stroke.path moveToPoint:self.lastPoint];
    [self.strokes addObject:stroke];
    if (self.strokes.count > 120) [self.strokes removeObjectAtIndex:0];
}
- (void)continueStrokeAt:(NSPoint)point {
    if (!self.strokeActive) return;
    NSPoint normalized = JardAnnotationUnitPoint(point, self.bounds.size);
    if (self.startTime > 0 && self.events.count < 100000) {
        NSMutableDictionary *edge = [JardAnnotationEdge(self.lastPoint, normalized,
            MAX(0, NSProcessInfo.processInfo.systemUptime - self.startTime)) mutableCopy];
        edge[@"stroke"] = @(self.stroke);
        [self.events addObject:edge];
    }
    self.lastPoint = normalized;
    NSBezierPath *path = self.strokes.lastObject.path;
    if (path.elementCount < 100000) [path lineToPoint:normalized];
    self.needsDisplay = YES;
}

- (void)advanceInkAt:(NSTimeInterval)now {
    BOOL animating = NO;
    for (JardLiveInkStroke *stroke in self.strokes.copy) {
        if (!stroke.liftedAt) continue;
        NSTimeInterval age = now - stroke.liftedAt;
        if (age >= JardAnnotationInkLifetime) [self.strokes removeObjectIdenticalTo:stroke];
        else animating = YES;
        if (age > JardAnnotationInkHold) self.needsDisplay = YES;
    }
    if (!animating) {
        [self.inkTimer invalidate];
        self.inkTimer = nil;
    }
}

- (void)endStroke {
    if (!self.strokeActive) return;
    self.strokeActive = NO;
    JardLiveInkStroke *stroke = self.strokes.lastObject;
    if (stroke.path.elementCount < 2) {
        if (stroke) [self.strokes removeLastObject];
        return;
    }
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    stroke.liftedAt = now;
    NSDictionary *last = self.events.lastObject;
    if ([last[@"stroke"] unsignedIntegerValue] == self.stroke) {
        NSMutableDictionary *released = last.mutableCopy;
        released[@"liftedAt"] = @(MAX([last[@"time"] doubleValue], now - self.startTime));
        self.events[self.events.count - 1] = released;
    }
    if (!self.inkTimer) {
        __weak typeof(self) weak = self;
        self.inkTimer = [NSTimer timerWithTimeInterval:1.0 / 30 repeats:YES block:^(NSTimer *timer) {
            [weak advanceInkAt:NSProcessInfo.processInfo.systemUptime];
        }];
        [NSRunLoop.mainRunLoop addTimer:self.inkTimer forMode:NSRunLoopCommonModes];
    }
}

- (void)finishInk {
    [self endStroke];
    [self.inkTimer invalidate];
    self.inkTimer = nil;
    [self.strokes removeAllObjects];
    self.needsDisplay = YES;
}

- (void)dealloc { [_inkTimer invalidate]; }

- (void)clear {
    [self finishInk];
    if (self.events.count > self.visibleFrom) {
        if (self.events.count < 100000) {
            [self.events addObject:@{@"clear": @YES, @"time": @(MAX(0, NSProcessInfo.processInfo.systemUptime - self.startTime))}];
        }
        self.visibleFrom = self.events.count;
    }
}
- (void)drawRect:(NSRect)dirtyRect {
    CGContextRef context = NSGraphicsContext.currentContext.CGContext;
    if (!context) return;
    CGContextClearRect(context, dirtyRect);
    if (!self.strokes.count) return;
    CGFloat width = NSWidth(self.bounds), height = NSHeight(self.bounds);
    CGAffineTransform scale = CGAffineTransformMakeScale(width, height);
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    CGContextSaveGState(context);
    CGContextSetLineCap(context, kCGLineCapRound);
    CGContextSetLineJoin(context, kCGLineJoinRound);
    CGContextSetLineWidth(context, self.renderedInk ? MAX(3, MIN(width, height) * .007) : 5);
    for (JardLiveInkStroke *stroke in self.strokes) {
        CGFloat alpha = stroke.liftedAt ? JardAnnotationInkAlpha(now - stroke.liftedAt) : 1;
        if (alpha <= 0 || stroke.path.elementCount < 2) continue;
        CGPathRef projected = CGPathCreateCopyByTransformingPath(stroke.path.CGPath, &scale);
        if (!projected) continue;
        CGContextAddPath(context, projected);
        CGPathRelease(projected);
        CGContextSetRGBStrokeColor(context, 1, .4, .22, alpha);
        CGContextStrokePath(context);
    }
    CGContextRestoreGState(context);
}
- (void)resetCursorRects { [self addCursorRect:self.bounds cursor:NSCursor.crosshairCursor]; }
@end

@interface JardAnnotationPanel : NSPanel
@end
@implementation JardAnnotationPanel
- (BOOL)canBecomeKeyWindow { return NO; }
- (BOOL)canBecomeMainWindow { return NO; }
@end

static NSVisualEffectView *annotationSurface(NSRect frame) {
    NSVisualEffectView *surface = [[NSVisualEffectView alloc] initWithFrame:frame];
    surface.material = NSVisualEffectMaterialHUDWindow;
    surface.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    surface.state = NSVisualEffectStateActive;
    surface.wantsLayer = YES;
    surface.layer.cornerRadius = 13;
    surface.layer.masksToBounds = YES;
    return surface;
}

@interface JardAnnotationDrawButton : NSButton
@property(nonatomic) BOOL drawing;
@end

@implementation JardAnnotationDrawButton
- (id)accessibilityValue { return self.drawing ? @1 : @0; }

- (BOOL)accessibilityPerformPress {
    return self.enabled && [self sendAction:self.action to:self.target];
}

- (void)setDrawing:(BOOL)drawing {
    self.state = drawing ? NSControlStateValueOn : NSControlStateValueOff;
    if (_drawing == drawing) return;
    _drawing = drawing;
    self.title = JardLocalized(drawing ? @"Done" : @"Draw");
    NSAccessibilityPostNotification(self, NSAccessibilityValueChangedNotification);
}
@end

@interface JardAnnotationPicker : NSObject <SCContentSharingPickerObserver>
@property(nonatomic, copy) void (^select)(SCContentFilter *filter);
@property(nonatomic, copy) void (^finish)(NSError *error);
@end

@implementation JardAnnotationPicker
- (void)contentSharingPicker:(SCContentSharingPicker *)picker didCancelForStream:(SCStream *)stream { self.finish(nil); }
- (void)contentSharingPickerStartDidFailWithError:(NSError *)error { self.finish(error); }
- (void)contentSharingPicker:(SCContentSharingPicker *)picker didUpdateWithFilter:(SCContentFilter *)filter
    forStream:(SCStream *)stream { self.select(filter); }
@end

@interface JardAnnotation () <SCStreamDelegate, SCRecordingOutputDelegate>
@property(nonatomic, copy) JardDictationSend send;
@property(nonatomic, copy) JardDictationReply screenshotReply;
@property(nonatomic, copy) void (^changed)(NSDictionary *);
@property(nonatomic, copy) NSString *phase, *message, *identifier, *sessionID;
@property(nonatomic, strong) NSImage *savedPreview;
@property(nonatomic, copy) NSString *source, *narrationError;
@property(nonatomic, copy) NSString *missingVideo;
@property(nonatomic, copy) NSDictionary *originSnapshot, *routingOrigin, *microphone;
@property(nonatomic, strong) JardPhoneAnnotation *phone;
@property(nonatomic, copy) NSArray<NSString *> *languages;
@property(nonatomic, copy) NSDictionary *lastOutcome;
@property(nonatomic, strong) NSNumber *stoppedAfter;
@property(nonatomic, strong) NSURL *directory, *movie, *recovery, *narration;
@property(nonatomic, strong) SCStream *stream;
@property(nonatomic, strong) SCRecordingOutput *recording;
@property(nonatomic, strong) SCContentFilter *displaySelection, *displayWithoutOverlays, *activeDisplayFilter;
@property(nonatomic, strong) NSMutableArray<NSValue *> *displayInkRanges;
@property(nonatomic, strong) NSNumber *displayInkStart;
@property(nonatomic) BOOL displayFilterPending;
@property(nonatomic, strong) JardAnnotationPicker *pickerObserver;
@property(nonatomic, strong) JardAnnotationPanel *overlay, *controls;
@property(nonatomic, strong) JardAnnotationPanel *feedbackPanel;
@property(nonatomic, strong) NSTimer *feedbackTimer;
@property(nonatomic, strong) NSURL *feedbackRecovery;
@property(nonatomic) NSTimeInterval feedbackDeadline;
@property(nonatomic, strong) JardSelectionPanel *selectionPanel;
@property(nonatomic) NSRect captureArea;
@property(nonatomic, strong) JardInkView *ink;
@property(nonatomic, strong) JardAnnotationDrawButton *penButton;
@property(nonatomic, strong) NSView *recordingTools, *discardPrompt;
@property(nonatomic, strong) NSTimer *discardTimer;
@property(nonatomic) NSTimeInterval discardDeadline;
@property(nonatomic, strong) NSTimer *windowTracker;
@property(nonatomic) NSRect trackedWindowFrame;
@property(nonatomic, strong) NSTimer *holdTimer;
@property(nonatomic) CGWindowID windowID;
@property(nonatomic) pid_t ownerPID;
@property(nonatomic) BOOL acquiringLease, leased, closed, penLatched;
@property(nonatomic) BOOL compositeInk;
@property(nonatomic) BOOL recordingMicrophone, recordingComputerAudio;
@property(nonatomic) BOOL screenStartRequested, narrationStarted, narrationRequested, narrationStopping, narrationFinished, awaitingNarration;
@property(nonatomic) JardAnnotationKeys keys;
@property(nonatomic) NSUInteger keyboardGeneration;
@end

@implementation JardAnnotation

static NSString *const annotationAudioPreference = @"JaroAnnotationAudio";

- (instancetype)initWithSend:(JardDictationSend)send stateChanged:(void (^)(NSDictionary *))changed {
    if (!(self = [super init])) return nil;
    _send = [send copy];
    _changed = [changed copy];
    _phase = @"idle";
    NSDictionary *audio = [NSUserDefaults.standardUserDefaults dictionaryForKey:annotationAudioPreference];
    _recordingMicrophone = ![audio[@"microphone"] isKindOfClass:NSNumber.class] || [audio[@"microphone"] boolValue];
    _recordingComputerAudio = [audio[@"computerAudio"] isKindOfClass:NSNumber.class] && [audio[@"computerAudio"] boolValue];
    return self;
}

- (BOOL)busy { return self.identifier != nil; }

- (NSDictionary *)recordingAudio {
    return @{@"microphone": @(self.recordingMicrophone), @"computerAudio": @(self.recordingComputerAudio)};
}

- (BOOL)setRecordingMicrophone:(BOOL)microphone computerAudio:(BOOL)computerAudio {
    if (self.busy || self.closed) return NO;
    self.recordingMicrophone = microphone;
    self.recordingComputerAudio = computerAudio;
    [NSUserDefaults.standardUserDefaults setObject:self.recordingAudio forKey:annotationAudioPreference];
    if (self.changed) self.changed(self.status);
    return YES;
}

- (void)refreshOverlaySharing {
    NSWindowSharingType sharing = self.showOverlaysInRecordings && !self.screenshotReply ? NSWindowSharingReadOnly : NSWindowSharingNone;
    self.controls.sharingType = self.feedbackPanel.sharingType = self.selectionPanel.sharingType = sharing;
    // A display stream with visible overlays needs the live pen, even while a filter change is pending.
    BOOL liveInk = self.stream && (!self.compositeInk ||
        (self.displaySelection && self.activeDisplayFilter == self.displaySelection));
    self.overlay.sharingType = liveInk ? NSWindowSharingReadOnly : sharing;
}

- (void)setShowOverlaysInRecordings:(BOOL)show {
    if (_showOverlaysInRecordings == show) return;
    _showOverlaysInRecordings = show;
    [self refreshOverlaySharing];
    [self updateDisplayFilter];
}

- (NSNumber *)elapsed {
    if (self.stoppedAfter) return self.stoppedAfter;
    return self.ink.startTime > 0 ? @(MAX(0, NSProcessInfo.processInfo.systemUptime - self.ink.startTime)) : nil;
}

- (BOOL)sharesMicrophone { return self.narrationStarted; }

- (NSDictionary *)status {
    return @{@"phase": self.phase, @"message": self.message ?: @"",
        @"session": self.sessionID ?: @"", @"recoverable": @(self.recovery != nil), @"busy": @(self.busy),
        @"capture": self.identifier ?: (id)NSNull.null, @"source": (self.busy ? self.source : nil) ?: (id)NSNull.null,
        @"elapsedSeconds": self.elapsed ?: (id)NSNull.null, @"last": self.lastOutcome ?: (id)NSNull.null,
        @"sharedMicrophone": @(self.sharesMicrophone), @"audio": self.recordingAudio};
}

- (void)state:(NSString *)phase message:(NSString *)message {
    self.phase = phase;
    self.message = message;
    if (![@[@"saving", @"error"] containsObject:phase]) [self dismissFeedback:nil];
    if (self.changed) self.changed([self status]);
}

- (void)conclude:(NSString *)phase message:(NSString *)message {
    if (self.screenshotReply) {
        [self finishScreenshot:nil error:[phase isEqual:@"error"] ? message : nil];
        return;
    }
    if (self.identifier) {
        self.lastOutcome = @{@"id": self.identifier, @"phase": phase, @"source": self.source ?: (id)NSNull.null,
            @"elapsedSeconds": self.elapsed ?: (id)NSNull.null, @"session": self.sessionID ?: (id)NSNull.null,
            @"message": message};
    }
    [self reset];
    [self state:phase message:message];
}

- (void)takeScreenshot:(NSString *)source reply:(JardDictationReply)reply {
    if (self.busy || self.closed) { reply(nil, @"Finish the current screen capture first."); return; }
    if (![@[@"area", @"window", @"display"] containsObject:source]) {
        reply(nil, @"Choose an area, window, or display to capture.");
        return;
    }
    self.screenshotReply = reply;
    self.identifier = NSUUID.UUID.UUIDString.lowercaseString;
    self.source = source;
    [self beginSource:self.identifier quartzPointer:nil reply:nil];
}

- (void)finishScreenshot:(NSString *)jpeg error:(NSString *)error {
    JardDictationReply reply = self.screenshotReply;
    [self reset];
    [self state:@"idle" message:@""];
    if (reply) reply(jpeg ? @{@"jpeg": jpeg} : @{}, error);
}

- (void)start:(NSArray<NSString *> *)languages source:(NSString *)source microphone:(NSDictionary *)microphone
    origin:(NSDictionary *)origin quartzPointer:(NSValue *)pointer reply:(JardDictationReply)reply {
    if (self.busy || self.closed) { reply(nil, @"An annotation is already active."); return; }
    self.identifier = NSUUID.UUID.UUIDString.lowercaseString;
    self.languages = languages;
    self.source = source;
    self.originSnapshot = origin;
    self.microphone = microphone;
    self.sessionID = nil;
    self.acquiringLease = YES;
    NSString *identifier = self.identifier;
    [self state:@"starting" message:@"Preparing screen annotation…"];
    self.send(@"jard.annotationLease", @{@"id": identifier, @"active": @YES}, ^(id result, NSString *error) {
        if (![self.identifier isEqual:identifier]) {
            if (!error) self.send(@"jard.annotationLease", @{@"id": identifier, @"active": @NO}, nil);
            reply(nil, @"Annotation was cancelled.");
            return;
        }
        self.acquiringLease = NO;
        if ([self.phase isEqual:@"cancelling"]) {
            self.leased = !error;
            [self conclude:@"cancelled" message:@"Annotation discarded."];
            reply(nil, @"Annotation was cancelled.");
            return;
        }
        if (error) { [self fail:error keepMovie:NO]; reply(nil, error); return; }
        self.leased = YES;
        if ([source isEqual:@"phone"] && !JardHasWiredAnnotationDevice()) {
            NSString *message = @"Connect an unlocked iPhone or iPad by USB to start an annotation.";
            [self fail:message keepMovie:NO];
            reply(nil, message);
            return;
        }
        if (self.recordingMicrophone &&
            [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio] == AVAuthorizationStatusNotDetermined) {
            reply([self status], nil);
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL allowed) {
                dispatch_async(dispatch_get_main_queue(), ^{ [self beginSource:identifier quartzPointer:pointer reply:nil]; });
            }];
        } else {
            [self beginSource:identifier quartzPointer:pointer reply:reply];
        }
    });
}

- (void)beginSource:(NSString *)identifier quartzPointer:(NSValue *)pointer reply:(JardDictationReply)reply {
    if (![self.identifier isEqual:identifier]) { if (reply) reply(nil, @"Annotation was cancelled."); return; }
    if (!self.screenshotReply && self.recordingMicrophone &&
        [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio] != AVAuthorizationStatusAuthorized) {
        NSString *error = @"Allow microphone access in System Settings to record narration.";
        [self fail:error keepMovie:NO];
        if (reply) reply(nil, error);
        return;
    }
    if ([self.source isEqual:@"phone"]) {
        [self authorizePhone:identifier reply:reply];
        return;
    }
    if (pointer && [self.source isEqual:@"display"]) {
        [self beginDisplayAtPoint:pointer.pointValue identifier:identifier reply:reply];
    } else if ([self.source isEqual:@"window"]) {
        [self beginWindowSelection:identifier reply:reply];
    } else [self presentPicker:identifier reply:reply];
}

- (void)presentPicker:(NSString *)identifier reply:(JardDictationReply)reply {
    SCContentSharingPicker *picker = SCContentSharingPicker.sharedPicker;
    SCContentSharingPickerConfiguration *configuration = SCContentSharingPickerConfiguration.new;
    BOOL window = [self.source isEqual:@"window"];
    configuration.allowedPickerModes = window ? SCContentSharingPickerModeSingleWindow : SCContentSharingPickerModeSingleDisplay;
    if (window) {
        NSArray *windows = CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID));
        configuration.excludedWindowIDs = [self floatingPickerWindowIDs:windows];
    }
    configuration.allowsChangingSelectedContent = NO;
    picker.defaultConfiguration = configuration;
    picker.active = YES;
    __weak typeof(self) weak = self;
    JardAnnotationPicker *observer = JardAnnotationPicker.new;
    observer.select = ^(SCContentFilter *filter) {
        dispatch_async(dispatch_get_main_queue(), ^{
            JardAnnotation *owner = weak;
            if ([owner.identifier isEqual:identifier] && [owner.phase isEqual:@"picking"]) [owner beginRecording:filter content:nil];
        });
    };
    observer.finish = ^(NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            JardAnnotation *owner = weak;
            if (![owner.identifier isEqual:identifier] || ![owner.phase isEqual:@"picking"]) return;
            if (error) [owner fail:error.localizedDescription keepMovie:NO];
            else [owner cancel];
        });
    };
    self.pickerObserver = observer;
    [picker addObserver:observer];
    NSString *message = window ? @"Choose the window you want to annotate." : @"Choose the display you want to annotate.";
    if ([self.source isEqual:@"area"]) message = @"Choose the display that contains the area you want to annotate.";
    if (self.screenshotReply) message = window ? @"Choose the window you want to capture." : @"Choose a display for the screenshot.";
    [self state:@"picking" message:message];
    [picker presentPickerUsingContentStyle:window ? SCShareableContentStyleWindow : SCShareableContentStyleDisplay];
    if (reply) reply([self status], nil);
}

- (NSArray<NSNumber *> *)floatingPickerWindowIDs:(NSArray<NSDictionary *> *)windows {
    pid_t pid = NSProcessInfo.processInfo.processIdentifier;
    NSMutableArray<NSNumber *> *excluded = NSMutableArray.array;
    for (NSDictionary *window in windows) {
        NSNumber *number = window[(__bridge NSString *)kCGWindowNumber];
        if (number.unsignedIntValue && [window[(__bridge NSString *)kCGWindowOwnerPID] intValue] == pid &&
            [window[(__bridge NSString *)kCGWindowLayer] integerValue] != NSNormalWindowLevel) [excluded addObject:number];
    }
    return excluded;
}

- (void)endPicker {
    if (!self.pickerObserver) return;
    SCContentSharingPicker *picker = SCContentSharingPicker.sharedPicker;
    [picker removeObserver:self.pickerObserver];
    self.pickerObserver = nil;
    picker.active = NO;
}

- (void)selectRegionOnScreen:(NSScreen *)screen filter:(SCContentFilter *)filter {
    NSString *identifier = self.identifier;
    __weak typeof(self) weak = self;
    self.selectionPanel = JardAnnotationRegionPanel(screen, self.screenshotReply != nil, ^(JardRegionChoice choice, NSRect area) {
        JardAnnotation *owner = weak;
        if (![owner.identifier isEqual:identifier] || ![owner.phase isEqual:@"selecting"]) return;
        if (choice == JardRegionCancelled) { [owner stop]; return; }
        if (choice == JardRegionDisplay) {
            owner.source = @"display";
            owner.captureArea = NSZeroRect;
        } else owner.captureArea = area;
        [owner.selectionPanel orderOut:nil];
        owner.selectionPanel = nil;
        [owner beginRecording:filter content:nil];
    });
    [self refreshOverlaySharing];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(regionScreenChanged:)
        name:NSApplicationDidChangeScreenParametersNotification object:NSApp];
    [self state:@"selecting" message:self.screenshotReply ? @"Drag an area or click to capture the display. Press Escape to cancel."
        : @"Drag an area or click to record the display. Press Escape to cancel."];
    [self.selectionPanel makeKeyAndOrderFront:nil];
}

- (void)regionScreenChanged:(NSNotification *)notification {
    if ([self.phase isEqual:@"selecting"]) {
        [self conclude:@"cancelled" message:@"The display layout changed. Choose an area again."];
    } else if ([self.phase isEqual:@"starting"] || [self.phase isEqual:@"recording"]) {
        [self stop];
    }
}

- (BOOL)prepareMovie:(NSString *)name {
    NSURL *directory = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:
        [@"jard-annotation-" stringByAppendingString:self.identifier]] isDirectory:YES];
    NSError *error = nil;
    if (![NSFileManager.defaultManager createDirectoryAtURL:directory withIntermediateDirectories:NO
        attributes:@{NSFilePosixPermissions: @0700} error:&error]) {
        [self fail:error.localizedDescription keepMovie:NO];
        return NO;
    }
    self.directory = directory;
    self.movie = [directory URLByAppendingPathComponent:name];
    return YES;
}

static NSTimeInterval annotationHostTime(void) {
    return CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()));
}

- (void)prepareNarration:(dispatch_block_t)begin {
    if (!self.recordingMicrophone) { begin(); return; }
    NSString *identifier = self.identifier;
    self.narration = [self.directory URLByAppendingPathComponent:@"Narration.wav"];
    NSMutableDictionary *payload = [self.microphone mutableCopy] ?: NSMutableDictionary.dictionary;
    [payload addEntriesFromDictionary:@{@"action": @"prepare", @"id": identifier, @"path": self.narration.path}];
    self.send(@"jard.annotationAudio", payload, ^(id result, NSString *error) {
        if (![self.identifier isEqual:identifier] || ![self.phase isEqual:@"starting"]) return;
        if (error) { [self fail:error keepMovie:NO]; return; }
        begin();
    });
}

- (void)beginNarrationAt:(NSTimeInterval)uptime ready:(dispatch_block_t)ready {
    if (!self.recordingMicrophone) { ready(); return; }
    if (self.narrationRequested) return;
    NSString *identifier = self.identifier;
    NSTimeInterval age = fmax(0, NSProcessInfo.processInfo.systemUptime - uptime);
    self.narrationRequested = YES;
    self.send(@"jard.annotationAudio", @{@"action": @"start", @"id": identifier,
        @"at": @(annotationHostTime() - age)}, ^(id result, NSString *error) {
        if (![self.identifier isEqual:identifier]) return;
        if (error) self.narrationError = error;
        else self.narrationStarted = YES;
        if (![self.phase isEqual:@"starting"]) return;
        if (error) { [self stop]; return; }
        ready();
    });
}

- (void)endNarration {
    if (self.narrationStopping || self.narrationFinished) return;
    if (!self.recordingMicrophone) { self.narrationFinished = YES; return; }
    if (!self.narrationRequested) {
        self.narrationFinished = YES;
        self.narrationError = @"The screen stopped before microphone narration started.";
        if (self.awaitingNarration) [self submitAnnotation];
        return;
    }
    self.narrationStopping = YES;
    NSString *identifier = self.identifier;
    self.send(@"jard.annotationAudio", @{@"action": @"stop", @"id": identifier, @"at": @(annotationHostTime())},
        ^(id result, NSString *error) {
        if (![self.identifier isEqual:identifier]) return;
        self.narrationFinished = YES;
        if (error) self.narrationError = error;
        if (self.awaitingNarration) [self submitAnnotation];
    });
}

- (void)authorizePhone:(NSString *)identifier reply:(JardDictationReply)reply {
    void (^resumed)(BOOL) = ^(BOOL allowed) {
        if (![self.identifier isEqual:identifier]) return;
        if (allowed) [self beginPhoneRecording];
        else [self fail:@"Allow Jaro in System Settings › Privacy & Security › Camera, then try again." keepMovie:NO];
    };
    AVAuthorizationStatus permission = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
    if (permission == AVAuthorizationStatusNotDetermined) {
        [self state:@"starting" message:@"Allow Camera access to mirror the wired screen."];
        if (reply) reply([self status], nil);
        [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL allowed) {
            dispatch_async(dispatch_get_main_queue(), ^{ resumed(allowed); });
        }];
        return;
    }
    resumed(permission == AVAuthorizationStatusAuthorized);
    if (reply) reply(self.busy ? [self status] : nil, self.busy ? nil : self.message);
}

- (void)beginPhoneRecording {
    if (![self prepareMovie:@"Wired capture.mov"]) return;
    [self state:@"starting" message:@"Opening the wired screen mirror…"];
    JardPhoneAnnotation *phone = [[JardPhoneAnnotation alloc] initWithMovie:self.movie];
    self.phone = phone;
    NSString *identifier = self.identifier;
    __weak typeof(self) weak = self;
    phone.started = ^(NSTimeInterval uptime) {
        JardAnnotation *owner = weak;
        if (![owner.identifier isEqual:identifier] || ![owner.phase isEqual:@"starting"]) return;
        NSRect target = owner.phone.pictureOnScreen;
        NSScreen *screen = owner.phone.panel.screen ?: NSScreen.mainScreen;
        if (!screen || NSIsEmptyRect(target)) {
            [owner fail:@"The wired screen preview is unavailable." keepMovie:YES];
            return;
        }
        [owner preparePanels:screen target:target];
        owner.ink.startTime = uptime;
        [owner.phone.panel addChildWindow:owner.overlay ordered:NSWindowAbove];
        [owner.controls orderFrontRegardless];
        [owner beginNarrationAt:uptime ready:^{
            [owner state:@"recording" message:owner.recordingMicrophone
                ? @"Recording the wired screen and your Mac microphone." : @"Recording the wired screen."];
        }];
    };
    phone.pictureChanged = ^{
        JardAnnotation *owner = weak;
        if ([owner.identifier isEqual:identifier] && [owner.phase isEqual:@"recording"]) [owner updatePhoneOverlay];
    };
    phone.stopRequested = ^{
        JardAnnotation *owner = weak;
        if ([owner.identifier isEqual:identifier]) [owner stop];
    };
    phone.finished = ^(BOOL usable, NSString *error) {
        JardAnnotation *owner = weak;
        if (![owner.identifier isEqual:identifier] || owner.closed) return;
        owner.phone = nil;
        if ([owner.phase isEqual:@"cancelling"]) {
            [owner conclude:@"cancelled" message:@"Annotation discarded."];
        } else if (usable || owner.narrationRequested) {
            owner.compositeInk = YES;
            [owner finishMovieWithMissingReason:usable ? @"noFrames" : @"screenInterrupted" stopStream:NO];
        } else {
            [owner fail:error ?: @"No recording was saved from the wired screen." keepMovie:YES];
        }
    };
    [self prepareNarration:^{ [phone start]; }];
}

- (void)updatePhoneOverlay {
    NSRect target = self.phone.pictureOnScreen;
    if (NSIsEmptyRect(target)) {
        [self.ink endStroke];
        [self.overlay orderOut:nil];
        return;
    }
    if (!NSEqualRects(self.overlay.frame, target)) {
        [self.ink endStroke];
        [self.overlay setFrame:target display:YES];
    }
    if (!self.overlay.visible) [self.overlay orderFrontRegardless];
    NSScreen *screen = self.phone.panel.screen;
    if (screen && screen != self.controls.screen) {
        [self placeControlsOnScreen:screen target:NSZeroRect];
    }
}

- (void)placeControlsOnScreen:(NSScreen *)screen target:(NSRect)target {
    if (!screen || !self.controls) return;
    NSRect desired = JardAnnotationControlsRect(screen.visibleFrame, target);
    JardOverlayEdge edge = NSMidY(desired) >= NSMidY(screen.visibleFrame) ? JardOverlayEdgeTop : JardOverlayEdgeBottom;
    CGDirectDisplayID displayID = JardDisplayIDForScreen(screen);
    NSRect resolved = [JardOverlayDock.shared resolvedFrameForSurface:JardOverlaySurfaceAnnotate
                                                              desired:desired
                                                                 edge:edge
                                                            displayID:displayID
                                                              visible:screen.visibleFrame];
    if (!NSEqualRects(self.controls.frame, resolved)) [self.controls setFrame:resolved display:YES];
    if (self.controls.visible) {
        [JardOverlayDock.shared claimSurface:JardOverlaySurfaceAnnotate frame:self.controls.frame
                                        edge:edge displayID:displayID];
    }
}

static NSScreen *screenForAnnotationWindow(NSRect target) {
    NSScreen *screen = nil;
    CGFloat area = 0;
    for (NSScreen *item in NSScreen.screens) {
        NSRect visible = NSIntersectionRect(target, item.frame);
        CGFloat overlap = NSWidth(visible) * NSHeight(visible);
        if (overlap > area) {
            area = overlap;
            screen = item;
        }
    }
    return screen;
}

- (SCContentFilter *)filterForDisplay:(SCDisplay *)display excluding:(SCRunningApplication *)application
    keepingWindows:(NSArray<SCWindow *> *)windows menuBar:(BOOL)menuBar {
    SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:display
        excludingApplications:@[application] exceptingWindows:windows];
    filter.includeMenuBar = menuBar;
    return filter;
}

- (SCContentFilter *)displayFilterExcludingOverlays:(SCContentFilter *)selection content:(SCShareableContent *)content {
    SCDisplay *display = selection.includedDisplays.firstObject;
    if (!display) return selection;
    pid_t pid = NSProcessInfo.processInfo.processIdentifier;
    SCRunningApplication *owner = nil;
    for (SCRunningApplication *application in content.applications) {
        if (application.processID == pid) { owner = application; break; }
    }
    NSMutableArray<SCWindow *> *workspace = NSMutableArray.array;
    for (SCWindow *window in content.windows) {
        if (window.owningApplication.processID != pid) continue;
        owner = owner ?: window.owningApplication;
        if (window.windowLayer == NSNormalWindowLevel) [workspace addObject:window];
    }
    if (!owner) return selection;
    // Excluding the process also covers floating windows that open after capture starts.
    return [self filterForDisplay:display excluding:owner keepingWindows:workspace menuBar:selection.includeMenuBar] ?: selection;
}

- (void)loadScreenContent:(void (^)(SCShareableContent *))completion {
    if (!CGPreflightScreenCaptureAccess()) { completion(nil); return; }
    __block BOOL finished = NO;
    [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:NO
        completionHandler:^(SCShareableContent *content, NSError *error) {
            dispatch_async(dispatch_get_main_queue(), ^{
                if (finished) return;
                finished = YES;
                completion(error ? nil : content);
            });
        }];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 1500 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        if (finished) return;
        finished = YES;
        completion(nil);
    });
}

- (SCDisplay *)displayAtPoint:(CGPoint)pointer fromDisplays:(NSArray<SCDisplay *> *)displays {
    NSValue *bounds = self.originSnapshot[@"bounds"];
    CGRect focused = bounds ? bounds.rectValue : CGRectZero;
    CGPoint center = CGPointMake(CGRectGetMidX(focused), CGRectGetMidY(focused));
    SCDisplay *focusedDisplay = nil, *primary = nil;
    for (SCDisplay *display in displays) {
        if (CGRectContainsPoint(display.frame, pointer)) return display;
        if (!CGRectIsEmpty(focused) && CGRectContainsPoint(display.frame, center)) focusedDisplay = display;
        if (display.displayID == CGMainDisplayID()) primary = display;
    }
    return focusedDisplay ?: primary ?: displays.firstObject;
}

- (SCContentFilter *)filterForPointerDisplay:(SCDisplay *)display {
    return [[SCContentFilter alloc] initWithDisplay:display excludingWindows:@[]];
}

- (void)beginDisplayAtPoint:(CGPoint)pointer identifier:(NSString *)identifier reply:(JardDictationReply)reply {
    [self state:@"picking" message:@"Finding the display under the pointer…"];
    [self loadScreenContent:^(SCShareableContent *content) {
        if (![self.identifier isEqual:identifier] || ![self.phase isEqual:@"picking"]) {
            if (reply) reply(nil, @"Annotation was cancelled.");
            return;
        }
        SCDisplay *display = [self displayAtPoint:pointer fromDisplays:content.displays];
        SCContentFilter *selection = display ? [self filterForPointerDisplay:display] : nil;
        if (!selection) { [self presentPicker:identifier reply:reply]; return; }
        [self beginRecording:selection content:content];
        if (reply) {
            if (self.busy) reply(self.status, nil);
            else reply(nil, self.message);
        }
    }];
}

- (void)beginWindowSelection:(NSString *)identifier reply:(JardDictationReply)reply {
    [self state:@"picking" message:@"Finding windows you can record…"];
    [self loadScreenContent:^(SCShareableContent *content) {
        if (![self.identifier isEqual:identifier] || ![self.phase isEqual:@"picking"]) {
            if (reply) reply(nil, @"Annotation was cancelled.");
            return;
        }
        NSString *hint = self.screenshotReply ? @"Click a window to capture it. Press Escape to cancel."
            : @"Click a window to record it. Press Escape to cancel.";
        if (![self showWindowSelector:content message:hint]) {
            [self presentPicker:identifier reply:reply];
        } else if (reply) reply(self.status, nil);
    }];
}

- (BOOL)showWindowSelector:(SCShareableContent *)content message:(NSString *)message {
    if (!content.displays.count) return NO;
    NSMutableDictionary<NSNumber *, NSNumber *> *owners = NSMutableDictionary.dictionary;
    for (SCWindow *window in content.windows) {
        pid_t owner = window.owningApplication.processID;
        if (owner && window.windowID && window.windowLayer == NSNormalWindowLevel) owners[@(window.windowID)] = @(owner);
    }
    if (self.screenshotReply && !owners.count) return NO;
    NSString *identifier = self.identifier;
    __weak typeof(self) weak = self;
    self.selectionPanel = JardAnnotationWindowPanel(owners, self.screenshotReply != nil, ^(BOOL cancelled, CGWindowID window, pid_t pid, CGPoint pointer) {
        JardAnnotation *annotation = weak;
        if (![annotation.identifier isEqual:identifier] || ![annotation.phase isEqual:@"selecting"]) return;
        if (cancelled) { [annotation cancel]; return; }
        [annotation.selectionPanel orderOut:nil];
        annotation.selectionPanel = nil;
        [annotation resolveWindowSelection:window owner:pid pointer:pointer identifier:identifier];
    });
    if (!self.selectionPanel) return NO;
    [self refreshOverlaySharing];
    [self state:@"selecting" message:message];
    [self.selectionPanel makeKeyAndOrderFront:nil];
    return YES;
}

- (SCWindow *)selectedWindow:(CGWindowID)window owner:(pid_t)pid fromContent:(SCShareableContent *)content {
    for (SCWindow *candidate in content.windows) {
        if (candidate.windowID == window && candidate.owningApplication.processID == pid &&
            candidate.windowLayer == NSNormalWindowLevel && candidate.isOnScreen) return candidate;
    }
    return nil;
}

- (SCContentFilter *)filterForSelectedWindow:(SCWindow *)window {
    return [[SCContentFilter alloc] initWithDesktopIndependentWindow:window];
}

- (void)resolveWindowSelection:(CGWindowID)window owner:(pid_t)pid pointer:(CGPoint)pointer identifier:(NSString *)identifier {
    if (!window) {
        if (self.screenshotReply) { [self presentPicker:identifier reply:nil]; return; }
        self.source = @"display";
        [self beginDisplayAtPoint:pointer identifier:identifier reply:nil];
        return;
    }
    [self state:@"picking" message:@"Opening the highlighted window…"];
    [self loadScreenContent:^(SCShareableContent *content) {
        if (![self.identifier isEqual:identifier] || ![self.phase isEqual:@"picking"]) return;
        SCWindow *picked = [self selectedWindow:window owner:pid fromContent:content];
        SCContentFilter *filter = picked ? [self filterForSelectedWindow:picked] : nil;
        if (filter) [self beginRecording:filter content:content];
        else if (![self showWindowSelector:content message:self.screenshotReply ? @"That window is unavailable. Choose another window."
            : @"That window is unavailable. Choose another window or display."])
            [self presentPicker:identifier reply:nil];
    }];
}

- (void)updateDisplayFilter {
    if (!self.displayWithoutOverlays || self.displayFilterPending || ![self.phase isEqual:@"recording"]) return;
    SCContentFilter *desired = self.showOverlaysInRecordings ? self.displaySelection : self.displayWithoutOverlays;
    if (desired == self.activeDisplayFilter) return;
    self.displayFilterPending = YES;
    SCStream *stream = self.stream;
    __weak typeof(self) weak = self;
    [stream updateContentFilter:desired completionHandler:^(NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            JardAnnotation *owner = weak;
            if (!owner || owner.stream != stream || !owner.recording) return;
            owner.displayFilterPending = NO;
            if (error) {
                if ([owner.phase isEqual:@"recording"] && owner.showOverlaysInRecordings == (desired == owner.displaySelection))
                    [owner state:@"recording" message:@"Could not change which controls appear in this recording."];
                return;
            }
            NSTimeInterval at = CMTimeGetSeconds(owner.recording.recordedDuration);
            if (!isfinite(at) || at < 0) at = MAX(0, NSProcessInfo.processInfo.systemUptime - owner.ink.startTime);
            if (owner.displayInkStart) {
                NSTimeInterval start = owner.displayInkStart.doubleValue;
                if (at > start) [owner.displayInkRanges addObject:[NSValue valueWithCMTimeRange:
                    CMTimeRangeFromTimeToTime(CMTimeMakeWithSeconds(start, 60000), CMTimeMakeWithSeconds(at, 60000))]];
            }
            owner.displayInkStart = desired == owner.displayWithoutOverlays ? @(at) : nil;
            owner.activeDisplayFilter = desired;
            [owner refreshOverlaySharing];
            if ([owner.phase isEqual:@"recording"]) [owner state:@"recording" message:[owner recordingScreenMessage]];
            [owner updateDisplayFilter];
        });
    }];
}

- (void)beginRecording:(SCContentFilter *)filter content:(SCShareableContent *)content {
    BOOL choosingWindow = [self.source isEqual:@"window"];
    BOOL choosingArea = [self.source isEqual:@"area"];
    SCWindow *window = choosingWindow && filter.style == SCShareableContentStyleWindow && filter.includedWindows.count == 1
        ? filter.includedWindows.firstObject : nil;
    SCDisplay *display = !choosingWindow && filter.style == SCShareableContentStyleDisplay && filter.includedDisplays.count == 1
        ? filter.includedDisplays.firstObject : nil;
    if (window.owningApplication.processID == NSProcessInfo.processInfo.processIdentifier &&
        window.windowLayer != NSNormalWindowLevel) {
        [self fail:@"Choose a workspace window instead of a floating Jaro control." keepMovie:NO];
        return;
    }
    NSRect target = window ? JardAnnotationAppKitRect(window.frame, CGRectGetHeight(CGDisplayBounds(CGMainDisplayID()))) : NSZeroRect;
    NSScreen *screen = window ? screenForAnnotationWindow(target) : nil;
    if (display) {
        for (NSScreen *item in NSScreen.screens) {
            if ([item.deviceDescription[@"NSScreenNumber"] unsignedIntValue] == display.displayID) {
                screen = item;
                break;
            }
        }
    }
    if (!screen) { [self fail:choosingWindow ? @"The selected window is unavailable." : @"The selected display is unavailable." keepMovie:NO]; return; }
    CGRect sourceRect = CGRectZero;
    if (choosingArea) {
        if (NSIsEmptyRect(self.captureArea)) { [self selectRegionOnScreen:screen filter:filter]; return; }
        target = NSIntersectionRect(self.captureArea, screen.frame);
        sourceRect = JardAnnotationDisplaySourceRect(target, screen.frame);
        if (CGRectIsEmpty(sourceRect)) {
            [self fail:@"The selected area is no longer on the chosen display." keepMovie:NO];
            return;
        }
    }
    if (self.screenshotReply) {
        [self captureScreenshot:filter window:window != nil crop:sourceRect content:content];
        return;
    }
    self.windowID = window.windowID;
    self.ownerPID = window.owningApplication.processID;
    NSString *bundle = window.owningApplication.bundleIdentifier;
    NSDictionary *selectedApp = bundle.length ? @{@"app": bundle,
        @"name": window.owningApplication.applicationName ?: bundle} : nil;
    CGFloat heightOfPrimary = CGRectGetHeight(CGDisplayBounds(CGMainDisplayID()));
    CGRect captured = window ? window.frame : JardAnnotationAppKitRect(choosingArea ? target : screen.frame, heightOfPrimary);
    if (!window || selectedApp)
        self.routingOrigin = JardAnnotationRoutingOrigin(self.originSnapshot, selectedApp, self.ownerPID, window.title, captured);
    [self state:@"starting" message:@"Starting screen capture…"];
    NSString *identifier = self.identifier;
    NSRect panelTarget = window || choosingArea ? target : screen.frame;
    void (^begin)(SCContentFilter *) = ^(SCContentFilter *resolved) {
        if (![self.identifier isEqual:identifier] || ![self.phase isEqual:@"starting"]) return;
        self.compositeInk = window != nil || resolved != filter;
        SCContentFilter *capture = resolved;
        if (resolved != filter) {
            self.displaySelection = filter;
            self.displayWithoutOverlays = resolved;
            capture = self.showOverlaysInRecordings ? filter : resolved;
            self.activeDisplayFilter = capture;
            self.displayInkRanges = NSMutableArray.array;
            self.displayInkStart = capture == resolved ? @0 : nil;
        }
        [self startScreenStream:capture screen:screen target:panelTarget crop:sourceRect sizingFilter:filter];
    };
    if (window) begin(filter);
    else if (content) begin([self displayFilterExcludingOverlays:filter content:content]);
    else [self loadScreenContent:^(SCShareableContent *available) {
        if (![self.identifier isEqual:identifier] || ![self.phase isEqual:@"starting"]) return;
        begin([self displayFilterExcludingOverlays:filter content:available]);
    }];
}

- (void)captureScreenshot:(SCContentFilter *)filter window:(BOOL)window crop:(CGRect)crop content:(SCShareableContent *)content {
    CGRect bounds = CGRectIsEmpty(crop) ? filter.contentRect : crop;
    double width = CGRectGetWidth(bounds) * fmax(1, filter.pointPixelScale);
    double height = CGRectGetHeight(bounds) * fmax(1, filter.pointPixelScale);
    if (!isfinite(width) || !isfinite(height) || fmin(width, height) < 1) {
        [self finishScreenshot:nil error:@"The selected source has no visible image."];
        return;
    }
    double scale = fmin(1, 1600 / fmax(width, height));
    SCStreamConfiguration *configuration = SCStreamConfiguration.new;
    configuration.width = (size_t)fmax(1, round(width * scale));
    configuration.height = (size_t)fmax(1, round(height * scale));
    configuration.showsCursor = NO;
    if (window) configuration.ignoreShadowsSingleWindow = YES;
    if (!CGRectIsEmpty(crop)) configuration.sourceRect = crop;
    [self state:@"starting" message:@"Capturing screenshot…"];
    NSString *identifier = self.identifier;
    void (^capture)(SCContentFilter *) = ^(SCContentFilter *selection) {
        if (![self.identifier isEqual:identifier]) return;
        [SCScreenshotManager captureImageWithFilter:selection configuration:configuration completionHandler:^(CGImageRef frame, NSError *error) {
            NSData *image = error ? nil : screenshotJPEG(frame);
            NSString *failure = error.localizedDescription ?: @"Could not prepare the screenshot.";
            NSString *jpeg = [image base64EncodedStringWithOptions:0];
            dispatch_async(dispatch_get_main_queue(), ^{
                if ([self.identifier isEqual:identifier]) [self finishScreenshot:jpeg error:jpeg ? nil : failure];
            });
        }];
    };
    if (window) capture(filter);
    else if (content) capture([self displayFilterExcludingOverlays:filter content:content]);
    else [self loadScreenContent:^(SCShareableContent *available) {
        capture([self displayFilterExcludingOverlays:filter content:available]);
    }];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 8 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        if ([self.identifier isEqual:identifier]) [self finishScreenshot:nil error:@"The screenshot did not finish. Try again."];
    });
}

- (void)startScreenStream:(SCContentFilter *)filter screen:(NSScreen *)screen target:(NSRect)target
    crop:(CGRect)sourceRect sizingFilter:(SCContentFilter *)sizingFilter {
    if (![self prepareMovie:self.compositeInk ? @"Screen capture.mp4" : @"Screen annotation.mp4"]) return;
    NSError *error = nil;
    SCStreamConfiguration *configuration = SCStreamConfiguration.new;
    if (!CGRectIsEmpty(sourceRect)) configuration.sourceRect = sourceRect;
    CGRect sourceBounds = CGRectIsEmpty(sourceRect) ? sizingFilter.contentRect : sourceRect;
    double width = CGRectGetWidth(sourceBounds) * sizingFilter.pointPixelScale;
    double height = CGRectGetHeight(sourceBounds) * sizingFilter.pointPixelScale;
    double scale = fmin(1, 2560 / fmax(width, height));
    configuration.width = (size_t)fmax(2, floor(width * scale / 2) * 2);
    configuration.height = (size_t)fmax(2, floor(height * scale / 2) * 2);
    configuration.minimumFrameInterval = CMTimeMake(1, 30);
    configuration.queueDepth = 5;
    configuration.showsCursor = YES;
    configuration.captureMicrophone = NO;
    configuration.capturesAudio = self.recordingComputerAudio;
    configuration.excludesCurrentProcessAudio = YES;
    if (self.windowID) {
        configuration.ignoreShadowsSingleWindow = YES;
        configuration.preservesAspectRatio = NO;
        configuration.scalesToFit = YES;
    }
    SCRecordingOutputConfiguration *file = SCRecordingOutputConfiguration.new;
    file.outputURL = self.movie;
    file.outputFileType = AVFileTypeMPEG4;
    self.recording = [[SCRecordingOutput alloc] initWithConfiguration:file delegate:self];
    self.stream = [[SCStream alloc] initWithFilter:filter configuration:configuration delegate:self];
    if (![self.stream addRecordingOutput:self.recording error:&error]) {
        [self fail:error.localizedDescription keepMovie:NO];
        return;
    }
    [self preparePanels:screen target:target];
    NSString *identifier = self.identifier;
    [self prepareNarration:^{
        self.screenStartRequested = YES;
        [self.stream startCaptureWithCompletionHandler:^(NSError *failure) {
            dispatch_async(dispatch_get_main_queue(), ^{
                if (![self.identifier isEqual:identifier]) return;
                if (failure) [self fail:failure.localizedDescription keepMovie:NO];
            });
        }];
    }];
}

- (void)preparePanels:(NSScreen *)screen target:(NSRect)target {
    NSWindowStyleMask style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel;
    JardAnnotationPanel *overlay = [[JardAnnotationPanel alloc] initWithContentRect:target styleMask:style
        backing:NSBackingStoreBuffered defer:NO];
    overlay.opaque = NO;
    overlay.backgroundColor = NSColor.clearColor;
    overlay.ignoresMouseEvents = YES;
    overlay.level = NSStatusWindowLevel - 1;
    overlay.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    self.ink = [[JardInkView alloc] initWithFrame:overlay.contentView.bounds];
    self.ink.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.ink.renderedInk = self.compositeInk || self.windowID != 0 || self.phone != nil;
    if (self.windowID) self.trackedWindowFrame = target;
    overlay.contentView = self.ink;
    self.overlay = overlay;

    JardAnnotationPanel *controls = [[JardAnnotationPanel alloc] initWithContentRect:
        JardAnnotationControlsRect(screen.visibleFrame, self.windowID ? target : self.captureArea)
        styleMask:style backing:NSBackingStoreBuffered defer:NO];
    controls.level = NSStatusWindowLevel;
    controls.opaque = NO;
    controls.backgroundColor = NSColor.clearColor;
    controls.collectionBehavior = overlay.collectionBehavior;
    controls.title = JardLocalized(@"Annotate");
    [controls setAccessibilityLabel:controls.title];
    NSVisualEffectView *surface = annotationSurface(controls.contentView.bounds);
    controls.contentView = surface;
    self.recordingTools = [[NSView alloc] initWithFrame:surface.bounds];
    [surface addSubview:self.recordingTools];
    self.penButton = [[JardAnnotationDrawButton alloc] initWithFrame:NSMakeRect(10, 8, 74, 30)];
    self.penButton.title = JardLocalized(@"Draw");
    self.penButton.target = self;
    self.penButton.action = @selector(togglePen:);
    [self.penButton setButtonType:NSButtonTypePushOnPushOff];
    [self.penButton setAccessibilityRole:NSAccessibilityCheckBoxRole];
    [self.penButton setAccessibilityLabel:self.penButton.title];
    self.penButton.toolTip = JardLocalized(@"Drag to draw. Hold the Annotate shortcut to draw without clicking.");
    [self.penButton setAccessibilityHelp:self.penButton.toolTip];
    NSButton *clear = [NSButton buttonWithTitle:JardLocalized(@"Clear Ink") target:self action:@selector(clearInk:)];
    NSButton *stop = [NSButton buttonWithTitle:JardLocalized(@"Stop & Save") target:self action:@selector(stopButton:)];
    NSButton *discard = [NSButton buttonWithImage:[NSImage imageWithSystemSymbolName:@"xmark" accessibilityDescription:nil]
        target:self action:@selector(askToDiscard:)];
    discard.frame = NSMakeRect(288, 8, 34, 30);
    discard.toolTip = JardLocalized(@"Discard…");
    discard.accessibilityLabel = discard.toolTip;
    self.penButton.bezelStyle = clear.bezelStyle;
    clear.frame = NSMakeRect(88, 8, 84, 30);
    stop.frame = NSMakeRect(174, 8, 108, 30);
    [self.recordingTools addSubview:self.penButton];
    [self.recordingTools addSubview:clear];
    [self.recordingTools addSubview:stop];
    [self.recordingTools addSubview:discard];
    self.discardPrompt = [[NSView alloc] initWithFrame:surface.bounds];
    self.discardPrompt.hidden = YES;
    [surface addSubview:self.discardPrompt];

    NSButton *confirm = [NSButton buttonWithTitle:JardLocalized(@"Discard") target:self action:@selector(confirmDiscard:)];
    NSButton *keep = [NSButton buttonWithTitle:JardLocalized(@"Keep recording") target:self action:@selector(keepRecording:)];
    confirm.controlSize = keep.controlSize = NSControlSizeSmall;
    confirm.contentTintColor = NSColor.systemRedColor;
    [confirm sizeToFit];
    [keep sizeToFit];
    CGFloat right = NSWidth(surface.bounds) - 10;
    confirm.frame = NSMakeRect(right - NSWidth(confirm.frame), 8, NSWidth(confirm.frame), 30);
    keep.frame = NSMakeRect(NSMinX(confirm.frame) - 5 - NSWidth(keep.frame), 8, NSWidth(keep.frame), 30);
    NSTextField *question = [NSTextField wrappingLabelWithString:JardLocalized(@"Discard this annotation?")];
    question.font = [NSFont systemFontOfSize:12 weight:NSFontWeightSemibold];
    question.maximumNumberOfLines = 2;
    question.frame = NSMakeRect(12, 7, NSMinX(keep.frame) - 17, 32);
    [self.discardPrompt addSubview:question];
    [self.discardPrompt addSubview:keep];
    [self.discardPrompt addSubview:confirm];
    self.controls = controls;
    [self refreshOverlaySharing];
}

- (void)closeDiscardPrompt {
    [self.discardTimer invalidate];
    self.discardTimer = nil;
    self.discardPrompt.hidden = YES;
    self.recordingTools.hidden = NO;
}

- (void)expireDiscardPromptAt:(NSTimeInterval)now pointer:(NSPoint)pointer {
    if (now >= self.discardDeadline && !NSPointInRect(pointer, self.controls.frame)) [self closeDiscardPrompt];
}

- (void)askToDiscard:(NSButton *)sender {
    if (sender.window != self.controls || !self.controls.visible || !self.discardPrompt.hidden ||
        ![self.phase isEqual:@"recording"]) return;
    self.recordingTools.hidden = YES;
    self.discardPrompt.hidden = NO;
    self.discardDeadline = NSProcessInfo.processInfo.systemUptime + 4;
    __weak typeof(self) weak = self;
    self.discardTimer = [NSTimer timerWithTimeInterval:.25 repeats:YES block:^(NSTimer *timer) {
        [weak expireDiscardPromptAt:NSProcessInfo.processInfo.systemUptime pointer:NSEvent.mouseLocation];
    }];
    [NSRunLoop.mainRunLoop addTimer:self.discardTimer forMode:NSRunLoopCommonModes];
    NSAccessibilityPostNotificationWithUserInfo(self.controls, NSAccessibilityAnnouncementRequestedNotification,
        @{NSAccessibilityAnnouncementKey: JardLocalized(@"Discard this annotation?"),
          NSAccessibilityPriorityKey: @(NSAccessibilityPriorityMedium)});
}

- (void)confirmDiscard:(NSButton *)sender {
    if (sender.window == self.controls && self.controls.visible && !self.discardPrompt.hidden &&
        [self.phase isEqual:@"recording"]) [self cancel];
}

- (void)keepRecording:(NSButton *)sender {
    if (sender.window == self.controls) [self closeDiscardPrompt];
}

- (void)togglePen:(id)sender {
    if (![self.phase isEqual:@"recording"]) return;
    self.penLatched = !self.penLatched;
    [self syncPen];
}

- (void)syncPen {
    BOOL drawing = self.penLatched || _keys.shiftPen || _keys.heldPen;
    if (!drawing || self.ink.followsPointer != _keys.heldPen) [self.ink endStroke];
    self.ink.followsPointer = _keys.heldPen;
    self.overlay.ignoresMouseEvents = !drawing;
    self.penButton.drawing = drawing;
}

- (void)drawPointerAtScreenPoint:(NSPoint)point {
    if (!self.overlay.visible || !NSPointInRect(point, self.overlay.frame) ||
        (self.controls.visible && NSPointInRect(point, self.controls.frame))) {
        [self.ink endStroke];
        return;
    }
    NSPoint local = [self.ink convertPoint:[self.overlay convertPointFromScreen:point] fromView:nil];
    if (self.ink.strokeActive) [self.ink continueStrokeAt:local];
    else [self.ink beginStrokeAt:local];
}

- (void)armKeyboardPen {
    [self.holdTimer invalidate];
    __weak typeof(self) weak = self;
    self.holdTimer = [NSTimer timerWithTimeInterval:JardAnnotationHoldDelay repeats:NO block:^(NSTimer *timer) {
        JardAnnotation *owner = weak;
        if (!owner) return;
        owner.holdTimer = nil;
        if (![owner.phase isEqual:@"recording"]) return;
        JardAnnotationKey(&owner->_keys, JardAnnotationRecording, JardAnnotationHoldTimer,
            NSProcessInfo.processInfo.systemUptime, owner.ink.startTime, 0, 0, false, owner.penLatched);
        [owner syncPen];
        if (owner->_keys.heldPen) [owner drawPointerAtScreenPoint:NSEvent.mouseLocation];
    }];
    [NSRunLoop.mainRunLoop addTimer:self.holdTimer forMode:NSRunLoopCommonModes];
}

static unsigned annotationModifiers(CGEventFlags flags) {
    return (flags & kCGEventFlagMaskAlternate ? JardAnnotationOption : 0) |
        (flags & kCGEventFlagMaskShift ? JardAnnotationShift : 0) |
        (flags & kCGEventFlagMaskControl ? JardAnnotationControl : 0) |
        (flags & kCGEventFlagMaskCommand ? JardAnnotationCommand : 0) |
        (flags & kCGEventFlagMaskSecondaryFn ? JardAnnotationFn : 0);
}

- (BOOL)handleEvent:(CGEventType)type event:(CGEventRef)event shortcut:(JardAnnotationShortcut)shortcut
           allowStart:(BOOL)allowStart start:(void (^)(CGPoint))start {
    if (self.closed) return NO;
    if (!event || type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) {
        self.keyboardGeneration++;
        _keys = (JardAnnotationKeys){0};
        [self.holdTimer invalidate];
        self.holdTimer = nil;
        self.penLatched = NO;
        [self syncPen];
        return NO;
    }
    if (type == kCGEventMouseMoved || type == kCGEventLeftMouseDragged) {
        if (_keys.heldPen && [self.phase isEqual:@"recording"]) {
            CGPoint location = CGEventGetLocation(event);
            CGFloat height = CGRectGetHeight(CGDisplayBounds(CGMainDisplayID()));
            [self drawPointerAtScreenPoint:NSMakePoint(location.x, height - location.y)];
        }
        return NO;
    }
    JardAnnotationInputMode mode = JardAnnotationUnavailable;
    if ([self.phase isEqual:@"recording"]) mode = JardAnnotationRecording;
    else if ([self.phase isEqual:@"selecting"]) mode = JardAnnotationPickingTarget;
    else if ([self.phase isEqual:@"picking"]) mode = JardAnnotationPicking;
    else if (!self.busy && allowStart) mode = JardAnnotationReady;
    BOOL down = type == kCGEventKeyDown;
    CGKeyCode code = (CGKeyCode)CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode);
    JardAnnotationKeyEvent kind;
    if (type == kCGEventFlagsChanged) kind = JardAnnotationModifiers;
    else if (type != kCGEventKeyUp && !down) return NO;
    else if (code == shortcut.code) kind = down ? JardAnnotationChordDown : JardAnnotationChordUp;
    else if (code == kVK_Escape) kind = down ? JardAnnotationEscapeDown : JardAnnotationEscapeUp;
    else if (down) kind = JardAnnotationOtherDown;
    else return NO;

    BOOL wasDrawing = _keys.heldPen || _keys.shiftPen;
    unsigned action = JardAnnotationKey(&_keys, mode, kind, NSProcessInfo.processInfo.systemUptime,
        self.ink.startTime, annotationModifiers(shortcut.required), annotationModifiers(shortcut.pressed),
        down && CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat), self.penLatched);
    if (action & JardAnnotationArmDraw) [self armKeyboardPen];
    else if (_keys.press != JardAnnotationDrawPress && self.holdTimer) {
        [self.holdTimer invalidate];
        self.holdTimer = nil;
    }
    if (action & JardAnnotationEndDrawing) self.penLatched = NO;
    if (wasDrawing != (_keys.heldPen || _keys.shiftPen) || (action & JardAnnotationEndDrawing)) [self syncPen];
    NSUInteger generation = self.keyboardGeneration;
    if ((action & JardAnnotationBegin) && start) {
        CGPoint pointer = CGEventGetLocation(event);
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!self.closed && self.keyboardGeneration == generation) start(pointer);
        });
    }
    if (action & (JardAnnotationSave | JardAnnotationCancelPicker | JardAnnotationConfirmTarget)) {
        NSString *identifier = self.identifier;
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.keyboardGeneration != generation || ![self.identifier isEqual:identifier]) return;
            if ((action & JardAnnotationSave) && [self.phase isEqual:@"recording"]) [self stop];
            if ((action & JardAnnotationCancelPicker) && ([self.phase isEqual:@"picking"] || [self.phase isEqual:@"selecting"])) [self stop];
            if ((action & JardAnnotationConfirmTarget) && [self.phase isEqual:@"selecting"]) [self.selectionPanel confirmSelection];
        });
    }
    return (action & JardAnnotationTake) != 0;
}

- (void)clearInk:(id)sender {
    [self.ink clear];
}

- (void)stopButton:(id)sender { [self stop]; }

- (NSString *)recordingScreenMessage {
    if (self.recordingComputerAudio)
        return self.recordingMicrophone ? @"Recording the screen, microphone, and Mac audio." : @"Recording the screen and Mac audio.";
    if (!self.recordingMicrophone) return @"Recording the screen.";
    if (self.windowID) return @"Recording the selected window and your microphone.";
    if ([self.source isEqual:@"area"]) return @"Recording the selected area and your microphone.";
    return @"Recording the selected display and your microphone.";
}

- (void)recordingOutputDidStartRecording:(SCRecordingOutput *)recordingOutput {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (recordingOutput != self.recording || ![self.phase isEqual:@"starting"]) return;
        NSTimeInterval elapsed = CMTimeGetSeconds(recordingOutput.recordedDuration);
        self.ink.startTime = NSProcessInfo.processInfo.systemUptime - (isfinite(elapsed) && elapsed > 0 ? elapsed : 0);
        [self.controls orderFrontRegardless];
        NSScreen *controlScreen = self.controls.screen ?: NSScreen.mainScreen;
        if (controlScreen) {
            [self placeControlsOnScreen:controlScreen target:self.overlay.frame];
            __weak typeof(self) weakDock = self;
            [JardOverlayDock.shared setObserver:^{
                if (weakDock.controls.visible) {
                    [weakDock placeControlsOnScreen:weakDock.controls.screen ?: NSScreen.mainScreen
                                             target:weakDock.overlay.frame];
                }
            } forSurface:JardOverlaySurfaceAnnotate];
        }
        [self beginNarrationAt:self.ink.startTime ready:^{
            [self state:@"recording" message:[self recordingScreenMessage]];
            [self updateDisplayFilter];
        }];
        if (!self.windowID) [self.overlay orderFrontRegardless];
        else {
            [[NSRunningApplication runningApplicationWithProcessIdentifier:self.ownerPID] activateWithOptions:0];
            __weak typeof(self) weak = self;
            self.windowTracker = [NSTimer timerWithTimeInterval:.12 repeats:YES block:^(NSTimer *timer) { [weak updateWindowOverlay]; }];
            [NSRunLoop.mainRunLoop addTimer:self.windowTracker forMode:NSRunLoopCommonModes];
            [self updateWindowOverlay];
        }
    });
}

- (void)updateWindowOverlay {
    if (!self.windowID || ![self.phase isEqual:@"recording"]) return;
    NSArray *info = CFBridgingRelease(CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, self.windowID));
    [self updateWindowOverlayWithInfo:info frontmostPID:NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier];
}

- (void)updateWindowOverlayWithInfo:(NSArray<NSDictionary *> *)info frontmostPID:(pid_t)frontmostPID {
    NSDictionary *window = nil;
    for (NSDictionary *candidate in info) {
        if ([candidate[(__bridge NSString *)kCGWindowNumber] unsignedIntValue] != self.windowID) continue;
        pid_t owner = [candidate[(__bridge NSString *)kCGWindowOwnerPID] intValue];
        if (self.ownerPID && owner != self.ownerPID) continue;
        // ScreenCaptureKit can omit the owner in the picker result.
        self.ownerPID = owner;
        window = candidate;
        break;
    }
    // An empty result means the target is gone. A failed WindowServer query is retried.
    if (info && !window) { [self stop]; return; }

    CGRect bounds;
    NSDictionary *position = window[(__bridge NSString *)kCGWindowBounds];
    NSRect target = NSZeroRect;
    if (self.ownerPID && frontmostPID == self.ownerPID && [window[(__bridge NSString *)kCGWindowIsOnscreen] boolValue] &&
        position && CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)position, &bounds) &&
        !CGRectIsEmpty(bounds) && !CGRectIsInfinite(bounds)) {
        target = JardAnnotationAppKitRect(bounds, CGRectGetHeight(CGDisplayBounds(CGMainDisplayID())));
    }
    NSScreen *screen = NSIsEmptyRect(target) ? nil : screenForAnnotationWindow(target);
    if (!screen) {
        [self.ink endStroke];
        [self.overlay orderOut:nil];
        return;
    }
    // AppKit can round a fractional window frame when it places the panel.
    if (!NSEqualRects(self.trackedWindowFrame, target)) {
        [self.ink endStroke];
        self.trackedWindowFrame = target;
        [self.overlay setFrame:target display:YES];
    }
    if (!self.overlay.visible) [self.overlay orderFrontRegardless];
    if (screen != self.controls.screen) {
        [self placeControlsOnScreen:screen target:target];
    }
}

- (void)hideRecordingControls {
    [self closeDiscardPrompt];
    self.stoppedAfter = self.elapsed;
    [self.ink finishInk];
    [self.windowTracker invalidate];
    self.windowTracker = nil;
    [self.controls orderOut:nil];
    [JardOverlayDock.shared releaseSurface:JardOverlaySurfaceAnnotate];
    [self.overlay orderOut:nil];
}

- (void)dismissFeedback:(NSButton *)sender {
    if (sender && sender.window != self.feedbackPanel) return;
    [self.feedbackTimer invalidate];
    self.feedbackTimer = nil;
    self.feedbackRecovery = nil;
    [self.feedbackPanel orderOut:nil];
    self.feedbackPanel = nil;
}

- (NSVisualEffectView *)feedbackSurfaceForScreen:(NSScreen *)screen anchor:(NSRect)anchor height:(CGFloat)height {
    NSRect visible = screen.visibleFrame;
    if (NSIsEmptyRect(anchor)) anchor = JardAnnotationControlsRect(visible, NSZeroRect);
    CGFloat width = MIN(NSWidth(anchor), NSWidth(visible));
    CGFloat x = MIN(MAX(NSMidX(anchor) - width / 2, NSMinX(visible)), NSMaxX(visible) - width);
    CGFloat y = MIN(MAX(NSMinY(anchor), NSMinY(visible)), NSMaxY(visible) - height);
    NSRect frame = NSMakeRect(x, y, width, height);
    if (!self.feedbackPanel) {
        NSWindowStyleMask style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel;
        self.feedbackPanel = [[JardAnnotationPanel alloc] initWithContentRect:frame styleMask:style
            backing:NSBackingStoreBuffered defer:NO];
        self.feedbackPanel.level = NSStatusWindowLevel;
        [self refreshOverlaySharing];
        self.feedbackPanel.opaque = NO;
        self.feedbackPanel.backgroundColor = NSColor.clearColor;
        self.feedbackPanel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
        self.feedbackPanel.title = JardLocalized(@"Annotate");
        self.feedbackPanel.accessibilityLabel = self.feedbackPanel.title;
    } else [self.feedbackPanel setFrame:frame display:NO];
    NSVisualEffectView *surface = annotationSurface(NSMakeRect(0, 0, width, height));
    self.feedbackPanel.contentView = surface;
    return surface;
}

- (void)presentFeedbackAnnouncement:(NSString *)message {
    [self.feedbackPanel orderFrontRegardless];
    NSAccessibilityPostNotificationWithUserInfo(self.feedbackPanel, NSAccessibilityAnnouncementRequestedNotification,
        @{NSAccessibilityAnnouncementKey: message, NSAccessibilityPriorityKey: @(NSAccessibilityPriorityMedium)});
}

- (void)showSavingFeedback {
    NSScreen *screen = self.controls.screen ?: NSScreen.mainScreen;
    if (self.closed || self.feedbackPanel || !screen || ![self.phase isEqual:@"saving"]) return;
    NSVisualEffectView *surface = [self feedbackSurfaceForScreen:screen anchor:self.controls.frame height:46];
    NSString *message = JardLocalized(@"Processing…");
    NSTextField *title = [NSTextField labelWithString:message];
    title.font = [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold];
    BOOL animate = !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    title.frame = NSMakeRect(animate ? 39 : 14, 14, NSWidth(surface.bounds) - 53, 19);
    [surface addSubview:title];
    if (animate) {
        NSProgressIndicator *indicator = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(14, 15, 16, 16)];
        indicator.style = NSProgressIndicatorStyleSpinning;
        indicator.controlSize = NSControlSizeSmall;
        indicator.accessibilityHidden = YES;
        [surface addSubview:indicator];
        [indicator startAnimation:nil];
    }
    [self presentFeedbackAnnouncement:[NSString stringWithFormat:@"%@. %@", self.feedbackPanel.title, message]];
}

- (void)revealRecording:(NSURL *)recording {
    if (recording) [NSWorkspace.sharedWorkspace activateFileViewerSelectingURLs:@[recording]];
}

- (void)revealFeedbackRecording:(NSButton *)sender {
    if (sender.window != self.feedbackPanel || !self.feedbackRecovery) return;
    [self revealRecording:self.feedbackRecovery];
    [self dismissFeedback:sender];
}

- (void)expireFeedbackAt:(NSTimeInterval)now pointer:(NSPoint)pointer {
    if (self.feedbackTimer && now >= self.feedbackDeadline && !NSPointInRect(pointer, self.feedbackPanel.frame))
        [self dismissFeedback:nil];
}

- (void)showFailureFeedback:(NSString *)reason recovery:(NSURL *)recovery screen:(NSScreen *)screen anchor:(NSRect)anchor {
    if (self.closed || !screen || ![self.phase isEqual:@"error"]) return;
    self.feedbackRecovery = recovery;
    CGFloat height = recovery ? 118 : 86;
    NSVisualEffectView *surface = [self feedbackSurfaceForScreen:screen anchor:anchor height:height];
    CGFloat width = NSWidth(surface.bounds);
    NSString *heading = JardLocalized(@"Annotation failed");
    NSTextField *title = [NSTextField labelWithString:heading];
    title.font = [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold];
    title.frame = NSMakeRect(14, height - 27, width - 54, 19);
    [surface addSubview:title];
    NSTextField *detail = [NSTextField wrappingLabelWithString:JardLocalized(reason)];
    detail.maximumNumberOfLines = 3;
    detail.font = [NSFont systemFontOfSize:12];
    detail.frame = NSMakeRect(14, height - 75, width - 28, 43);
    detail.toolTip = detail.stringValue;
    [surface addSubview:detail];
    NSButton *close = [NSButton buttonWithImage:[NSImage imageWithSystemSymbolName:@"xmark" accessibilityDescription:nil]
        target:self action:@selector(dismissFeedback:)];
    close.frame = NSMakeRect(width - 38, height - 32, 28, 25);
    close.accessibilityLabel = JardLocalized(@"Dismiss");
    [surface addSubview:close];
    if (recovery) {
        NSButton *reveal = [NSButton buttonWithTitle:JardLocalized(@"Show Recording in Finder")
            target:self action:@selector(revealFeedbackRecording:)];
        [reveal sizeToFit];
        reveal.frame = NSMakeRect(11, 9, MIN(width - 22, NSWidth(reveal.frame)), 25);
        [surface addSubview:reveal];
    }
    self.feedbackDeadline = NSProcessInfo.processInfo.systemUptime + 10;
    [self.feedbackTimer invalidate];
    __weak typeof(self) weak = self;
    self.feedbackTimer = [NSTimer timerWithTimeInterval:.25 repeats:YES block:^(NSTimer *timer) {
        [weak expireFeedbackAt:NSProcessInfo.processInfo.systemUptime pointer:NSEvent.mouseLocation];
    }];
    [NSRunLoop.mainRunLoop addTimer:self.feedbackTimer forMode:NSRunLoopCommonModes];
    [self presentFeedbackAnnouncement:[NSString stringWithFormat:@"%@. %@", heading, detail.stringValue]];
}

- (BOOL)stop {
    if ([self.phase isEqual:@"picking"] || [self.phase isEqual:@"selecting"] ||
        ([self.phase isEqual:@"starting"] && !self.phone && (!self.stream || !self.screenStartRequested))) {
        return [self cancel];
    }
    if (![self.phase isEqual:@"recording"] && ![self.phase isEqual:@"starting"]) return NO;
    [self endNarration];
    [self hideRecordingControls];
    [self state:@"saving" message:self.recordingMicrophone
        ? @"Saving the screen recording and transcribing narration…" : @"Saving the screen recording…"];
    if (self.phone) {
        [self.phone stopDiscarding:NO];
        return YES;
    }
    SCStream *stream = self.stream;
    if (!stream) {
        [self fail:@"The screen recording stopped unexpectedly." keepMovie:YES];
        return YES;
    }
    [stream stopCaptureWithCompletionHandler:^(NSError *error) {
        if (error) dispatch_async(dispatch_get_main_queue(), ^{
            if (stream == self.stream && self.recording) [self screenRecordingFailed:error];
        });
    }];
    return YES;
}

- (BOOL)cancel {
    if (!self.busy || [self.phase isEqual:@"saving"] || [self.phase isEqual:@"cancelling"]) return NO;
    if (self.acquiringLease) {
        [self state:@"cancelling" message:@"Discarding the annotation…"];
        return YES;
    }
    if (self.narrationRequested) [self endNarration];
    if (self.phone) {
        [self hideRecordingControls];
        [self state:@"cancelling" message:@"Discarding the annotation…"];
        [self.phone stopDiscarding:YES];
        return YES;
    }
    SCStream *stream = self.stream;
    if (!stream || !self.screenStartRequested) {
        [self conclude:@"cancelled" message:@"Annotation discarded."];
        return YES;
    }
    [self hideRecordingControls];
    [self state:@"cancelling" message:@"Discarding the annotation…"];
    [stream stopCaptureWithCompletionHandler:^(NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.stream != stream || ![self.phase isEqual:@"cancelling"]) return;
            if (error) { [self fail:error.localizedDescription keepMovie:NO]; return; }
            self.stream = nil;
            [self conclude:@"cancelled" message:@"Annotation discarded."];
        });
    }];
    return YES;
}

- (void)recordingOutputDidFinishRecording:(SCRecordingOutput *)recordingOutput {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (recordingOutput != self.recording || !self.busy || self.closed) return;
        if ([self.phase isEqual:@"cancelling"]) return;
        self.recording = nil;
        [self finishMovieWithMissingReason:@"noFrames" stopStream:NO];
    });
}

- (void)finishMovieWithMissingReason:(NSString *)reason stopStream:(BOOL)stopStream {
    [self endNarration];
    [self hideRecordingControls];
    [self state:@"saving" message:@"Checking the captured video…"];
    NSString *identifier = self.identifier;
    __block BOOL inspected = NO;
    dispatch_block_t inspect = ^{
        if (inspected || ![self.identifier isEqual:identifier] || self.closed) return;
        inspected = YES;
        JardAnnotationMovieHasFrame(self.movie, ^(BOOL playable) {
            if (![self.identifier isEqual:identifier] || self.closed) return;
            if (playable) [self finishPlayableMovie];
            else if (!self.narrationRequested) [self fail:@"The screen recording produced no video frames." keepMovie:YES];
            else {
                self.missingVideo = reason;
                [self importMovie];
            }
        });
    };
    if (!stopStream || !self.stream) { inspect(); return; }
    [self.stream stopCaptureWithCompletionHandler:^(NSError *error) { dispatch_async(dispatch_get_main_queue(), inspect); }];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), dispatch_get_main_queue(), inspect);
}

- (void)finishPlayableMovie {
    if (!self.compositeInk) { [self importMovie]; return; }
    NSMutableArray<NSValue *> *ranges = self.displayInkRanges;
    if (self.displayInkStart) [ranges addObject:[NSValue valueWithCMTimeRange:
        CMTimeRangeMake(CMTimeMakeWithSeconds(self.displayInkStart.doubleValue, 60000), kCMTimePositiveInfinity)]];
    NSURL *output = [self.directory URLByAppendingPathComponent:@"Screen annotation.mp4"];
    if (!self.ink.events.count || (ranges && !ranges.count)) {
        NSError *error = nil;
        if (![NSFileManager.defaultManager moveItemAtURL:self.movie toURL:output error:&error]) {
            [self fail:error.localizedDescription keepMovie:YES]; return;
        }
        self.movie = output;
        [self importMovie];
        return;
    }
    [self renderAnnotation:@"Rendering drawings over the screen recording…" ranges:ranges.copy];
}

- (void)renderAnnotation:(NSString *)message ranges:(NSArray<NSValue *> *)ranges {
    NSURL *output = [self.directory URLByAppendingPathComponent:@"Screen annotation.mp4"];
    NSString *identifier = self.identifier;
    [self state:@"saving" message:message];
    [self showSavingFeedback];
    JardRenderAnnotationInRanges(self.movie, output, self.ink.events.copy, ranges, ^(NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (![self.identifier isEqual:identifier] || self.closed) return;
            if (error) { [self fail:@"Could not render the annotation. The original recording is available in Finder." keepMovie:YES]; return; }
            self.movie = output;
            [self importMovie];
        });
    });
}

- (void)importMovie {
    [self state:@"saving" message:self.missingVideo ? @"Saving the microphone narration…" : @"Preparing the annotation and contact sheet…"];
    [self showSavingFeedback];
    NSString *identifier = self.identifier;
    NSURL *media = self.missingVideo ? self.narration : self.movie;
    JardWriteAnnotationAssets(media, self.source, self.routingOrigin, self.ink.events.copy, self.missingVideo, ^(NSImage *preview, NSError *error) {
        if (![self.identifier isEqual:identifier] || self.closed) return;
        if (error) { [self fail:@"Could not save the drawings. The recording is available in Finder." keepMovie:YES]; return; }
        self.savedPreview = preview;
        [self submitAnnotation];
    });
}

- (void)submitAnnotation {
    if (!self.narrationFinished) {
        self.awaitingNarration = YES;
        [self endNarration];
        return;
    }
    self.awaitingNarration = NO;
    if (self.narrationError) {
        [self fail:self.narrationError keepMovie:YES];
        return;
    }
    [self state:@"saving" message:self.recordingMicrophone
        ? @"Transcribing narration and saving the annotation…" : @"Saving the annotation…"];
    NSString *identifier = self.identifier;
    NSURL *media = self.missingVideo ? self.narration : self.movie;
    NSMutableDictionary *payload = [@{@"id": identifier, @"path": media.path, @"languages": self.languages ?: @[],
        @"assets": @YES, @"routeOrigin": self.routingOrigin ?: @{}} mutableCopy];
    if (self.missingVideo) payload[@"narrationOnly"] = @YES;
    else if (self.narration) payload[@"narration"] = self.narration.path;
    self.send(@"jard.importAnnotation", payload, ^(id result, NSString *error) {
        if (![self.identifier isEqual:identifier]) return;
        if (error) { [self fail:error keepMovie:YES]; return; }
        self.sessionID = result[@"id"];
        NSImage *preview = self.savedPreview;
        NSDictionary *routing = [result[@"routing"] isKindOfClass:NSDictionary.class] ? result[@"routing"] : nil;
        [self conclude:@"finished" message:self.missingVideo ? @"Annotation saved with narration and no video." : @"Annotation saved to Sessions."];
        if (self.saved) self.saved(self.sessionID, preview, routing);
    });
}

- (void)screenRecordingFailed:(NSError *)error {
    if (!self.narrationRequested) { [self fail:error.localizedDescription keepMovie:YES]; return; }
    self.recording = nil;
    NSString *reason = [error.domain isEqual:SCStreamErrorDomain] && error.code == SCStreamErrorUserDeclined
        ? @"permissionDenied" : @"screenInterrupted";
    [self finishMovieWithMissingReason:reason stopStream:YES];
}

- (void)recordingOutput:(SCRecordingOutput *)recordingOutput didFailWithError:(NSError *)error {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (recordingOutput == self.recording && ![self.phase isEqual:@"cancelling"])
            [self screenRecordingFailed:error];
    });
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    dispatch_async(dispatch_get_main_queue(), ^{
        if (stream != self.stream || !self.recording) return;
        if ([self.phase isEqual:@"cancelling"]) {
            self.stream = nil;
            [self conclude:@"cancelled" message:@"Annotation discarded."];
        } else [self screenRecordingFailed:error];
    });
}

- (void)fail:(NSString *)message keepMovie:(BOOL)keep {
    if (self.screenshotReply) {
        [self finishScreenshot:nil error:message ?: @"Could not capture the screenshot."];
        return;
    }
    NSNumber *size = nil;
    NSURL *recovery = nil;
    NSURL *media = self.missingVideo ? self.narration : self.movie;
    if (keep && [media getResourceValue:&size forKey:NSURLFileSizeKey error:nil] && size.longLongValue > 0) {
        recovery = self.recovery = media;
        self.directory = nil;
    }
    JardAnnotationPanel *anchor = self.feedbackPanel ?: self.controls;
    NSScreen *screen = anchor.screen ?: NSScreen.mainScreen;
    NSRect frame = anchor.frame;
    NSString *reason = message ?: @"The screen annotation could not be saved.";
    [self conclude:@"error" message:reason];
    [self showFailureFeedback:reason recovery:recovery screen:screen anchor:frame];
}

- (void)reset {
    self.screenshotReply = nil;
    self.keyboardGeneration++;
    [self closeDiscardPrompt];
    // Keep key-up ownership if the picker or recorder stopped while its shortcut was down.
    _keys.press = JardAnnotationNoPress;
    _keys.shiftPen = _keys.heldPen = NO;
    self.penLatched = NO;
    [self.holdTimer invalidate];
    self.holdTimer = nil;
    [self endPicker];
    [NSNotificationCenter.defaultCenter removeObserver:self name:NSApplicationDidChangeScreenParametersNotification object:NSApp];
    [self.selectionPanel orderOut:nil];
    self.selectionPanel = nil;
    self.captureArea = NSZeroRect;
    [self.windowTracker invalidate];
    self.windowTracker = nil;
    self.windowID = 0;
    self.ownerPID = 0;
    self.trackedWindowFrame = NSZeroRect;
    self.compositeInk = NO;
    self.displaySelection = self.displayWithoutOverlays = self.activeDisplayFilter = nil;
    self.displayInkRanges = nil;
    self.displayInkStart = nil;
    self.displayFilterPending = NO;
    if (self.overlay) [self.phone.panel removeChildWindow:self.overlay];
    [self.phone stopDiscarding:YES];
    self.phone = nil;
    self.microphone = nil;
    self.narrationError = nil;
    self.missingVideo = nil;
    self.screenStartRequested = self.narrationRequested = self.narrationStarted = NO;
    self.narrationStopping = self.narrationFinished = self.awaitingNarration = NO;
    self.originSnapshot = self.routingOrigin = nil;
    [self.overlay orderOut:nil];
    [self.controls orderOut:nil];
    [JardOverlayDock.shared releaseSurface:JardOverlaySurfaceAnnotate];
    self.ink = nil;
    self.savedPreview = nil;
    self.stoppedAfter = nil;
    self.overlay = self.controls = nil;
    self.recordingTools = self.discardPrompt = nil;
    SCStream *stream = self.stream;
    self.stream = nil;
    self.recording = nil;
    [stream stopCaptureWithCompletionHandler:nil];
    if (self.leased) self.send(@"jard.annotationLease", @{@"id": self.identifier, @"active": @NO}, nil);
    self.leased = NO;
    self.acquiringLease = NO;
    self.identifier = nil;
    if (self.directory) [NSFileManager.defaultManager removeItemAtURL:self.directory error:nil];
    self.directory = self.movie = self.narration = nil;
}

- (void)reveal {
    [self revealRecording:self.recovery];
}

- (void)shutdown {
    self.closed = YES;
    [self dismissFeedback:nil];
    if ([self.phase isEqual:@"cancelling"]) [self conclude:@"cancelled" message:@"Annotation discarded."];
    else if (self.busy) [self fail:@"Annotation was interrupted." keepMovie:YES];
}
@end
