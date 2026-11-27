#import "system_dictation.h"
#import "jard_dictation_browser.h"
#import "dictation_shortcut.h"
#import "jard_annotation_terminal.h"
#import "jard_dictation_media.h"
#import "jard_dictation_typing.h"
#import "jard_interface_language.h"
#import "jard_permission_return.h"
#import "jard_recording_environment.h"
#import "jard_overlay_dock.h"
#import "jard_secure_hotkeys.h"
#import "indicator/JardDictationMeter.h"
#import "indicator/JardHUDPlacement.h"
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#import <ApplicationServices/ApplicationServices.h>
#import <AVFoundation/AVFoundation.h>
#import <Carbon/Carbon.h>
#import <CommonCrypto/CommonDigest.h>
#import <IOKit/hid/IOHIDLib.h>
#import <IOKit/hidsystem/IOLLEvent.h>
#import <errno.h>
#import <fcntl.h>
#import <mach/mach_time.h>
#import <math.h>
#import <stdio.h>
#import <stdatomic.h>
#import <stdlib.h>
#import <string.h>
#import <sys/file.h>
#import <sys/stat.h>
#import <unistd.h>

static NSString *const preferencesKey = @"JardSystemDictation";
NSString *const JardInterfaceLanguageSettingKey = @"ui_language";
NSString *const JardShowDockIconSettingKey = @"show_dock_icon";
NSString *const JardShowDockIconPreferenceKey = @"JardShowDockIcon";

BOOL JardShowDockIconEnabled(void) {
    id preference = [NSUserDefaults.standardUserDefaults objectForKey:JardShowDockIconPreferenceKey];
    return ![preference isKindOfClass:NSNumber.class] || [preference boolValue];
}

static NSString *const repairedPreferencesWarning = @"Jard used defaults for some invalid dictation settings. Saving will preserve the original values.";
static NSString *const historyKey = @"JardSystemDictationHistory";
static const NSTimeInterval historyAudioLifetime = 24 * 60 * 60;
static const NSUInteger historyEntryLimit = 10000;
static const NSUInteger historyTextLimit = 16 * 1024;
static const NSUInteger historyTotalTextLimit = 16 * 1024 * 1024;
static NSString *const screenDidLock = @"com.apple.screenIsLocked";
static NSString *const screenDidUnlock = @"com.apple.screenIsUnlocked";
static const int64_t pasteMarker = 0x4a415244;
static const int64_t modifierClaimMarker = 0x4a4152444d4f44;
static const CGKeyCode modifierClaimKey = 0x44;
static const CGKeyCode globeKey = 179;
static const NSWindowCollectionBehavior dictationHUDBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
    NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorStationary |
    NSWindowCollectionBehaviorIgnoresCycle | NSWindowCollectionBehaviorCanJoinAllApplications;
static const NSUInteger hudRehomeLimit = 8;
static const CGEventFlags shortcutModifiers = kCGEventFlagMaskAlternate | kCGEventFlagMaskControl |
    kCGEventFlagMaskCommand | kCGEventFlagMaskShift | kCGEventFlagMaskSecondaryFn;
static const CGEventFlags defaultAnnotationFlags = kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
static const CGEventFlags meetingPasteFlags = kCGEventFlagMaskAlternate | kCGEventFlagMaskControl;
static const NSUInteger clipboardLimit = 16 * 1024 * 1024;
static const int64_t caretPasteKeyGap = 15 * NSEC_PER_MSEC;
static const int64_t pasteSettle = 100 * NSEC_PER_MSEC;
static const int64_t pasteRestoreDelay = 500 * NSEC_PER_MSEC;
static const PasteboardFlavorFlags writableClipboardFlags = kPasteboardFlavorSenderOnly | kPasteboardFlavorSenderTranslated |
    kPasteboardFlavorNotSaved | kPasteboardFlavorRequestOnly;

static NSArray<NSString *> *JardDictationSoundStyles(void) {
    return @[@"classic", @"signature", @"click", @"soft-tone", @"wood", @"analog", @"thock", @"soft", @"glass"];
}

static BOOL JardDictationSoundHasDone(NSString *style) {
    return [@[@"signature", @"click", @"soft-tone", @"wood", @"analog"] containsObject:style ?: @""];
}

static NSDictionary<NSString *, NSDictionary<NSString *, NSURL *> *> *JardDictationSoundSources(void) {
    static NSDictionary *sources;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSMutableDictionary *styles = NSMutableDictionary.dictionary;
        NSURL *systemSounds = [NSURL fileURLWithPath:@"/System/Library/Sounds" isDirectory:YES];
        NSURL *basso = [systemSounds URLByAppendingPathComponent:@"Basso.aiff"];
        for (NSString *style in JardDictationSoundStyles()) {
            if ([style isEqual:@"soft"] || [style isEqual:@"glass"]) {
                BOOL soft = [style isEqual:@"soft"];
                NSURL *start = [systemSounds URLByAppendingPathComponent:soft ? @"Purr.aiff" : @"Glass.aiff"];
                NSURL *stop = [systemSounds URLByAppendingPathComponent:soft ? @"Morse.aiff" : @"Ping.aiff"];
                styles[style] = @{@"start": start, @"stop": stop, @"cancel": stop, @"success": stop, @"error": basso};
                continue;
            }
            BOOL classic = [style isEqual:@"classic"];
            NSString *directory = classic ? @"dictation-sounds" : [@"dictation-sounds" stringByAppendingPathComponent:
                [style isEqual:@"soft-tone"] ? @"soft" : style];
            NSMutableDictionary *cues = NSMutableDictionary.dictionary;
            for (NSString *cue in @[@"start", @"stop", @"cancel", @"error", @"done", @"success"]) {
                NSURL *clip = [NSBundle.mainBundle URLForResource:cue withExtension:@"wav" subdirectory:directory];
                if (clip) cues[cue] = clip;
            }
            if (classic) {
                cues[@"error"] = basso;
                cues[@"success"] = cues[@"stop"];
            }
            styles[style] = cues.copy;
        }
        sources = styles.copy;
    });
    return sources;
}

static NSDictionary *JardSoundPreviewSchedule(void) {
    static NSDictionary *schedule;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSURL *url = [NSBundle.mainBundle URLForResource:@"preview" withExtension:@"json" subdirectory:@"dictation-sounds"];
        NSData *data = url ? [NSData dataWithContentsOfURL:url] : nil;
        id value = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
        if ([value isKindOfClass:NSDictionary.class]) schedule = value;
    });
    return schedule;
}

@interface JardClipboardFlavor : NSObject
@property(nonatomic, copy) NSString *type;
@property(nonatomic, strong) NSData *data;
@property(nonatomic) PasteboardFlavorFlags flags;
@end

@implementation JardClipboardFlavor
@end

typedef NSArray<NSArray<JardClipboardFlavor *> *> JardClipboardSnapshot;

static uint16_t physicalModifierBit(uint32_t usage) {
    if (usage == kHIDUsage_KeyboardCapsLock) return 1;
    if (usage >= kHIDUsage_KeyboardLeftControl && usage <= kHIDUsage_KeyboardRightGUI)
        return (uint16_t)(2 << (usage - kHIDUsage_KeyboardLeftControl));
    return 0;
}

// Keep only the live state of Caps Lock and the eight sided modifiers.
// The HID manager runs off the main thread and never seizes a keyboard.
@interface JardPhysicalModifiers : NSObject {
    IOHIDManagerRef _manager;
    _Atomic(uint16_t) _held;
}
@property(nonatomic, strong) dispatch_queue_t queue;
@property(nonatomic, strong) NSMutableDictionary<NSValue *, NSNumber *> *devices;
- (uint16_t)held;
- (void)stop;
@end

@implementation JardPhysicalModifiers
- (void)publish {
    uint16_t held = 0;
    for (NSNumber *state in self.devices.objectEnumerator) held |= state.unsignedShortValue;
    atomic_store_explicit(&_held, held, memory_order_relaxed);
}

static void modifierValueChanged(void *context, IOReturn status, void *sender, IOHIDValueRef value) {
    if (status != kIOReturnSuccess || !value) return;
    IOHIDElementRef element = IOHIDValueGetElement(value);
    if (!element || IOHIDElementGetUsagePage(element) != kHIDPage_KeyboardOrKeypad) return;
    uint16_t bit = physicalModifierBit(IOHIDElementGetUsage(element));
    IOHIDDeviceRef device = IOHIDElementGetDevice(element);
    if (!bit || !device) return;
    JardPhysicalModifiers *keys = (__bridge JardPhysicalModifiers *)context;
    NSValue *key = [NSValue valueWithPointer:device];
    uint16_t held = keys.devices[key].unsignedShortValue;
    if (IOHIDValueGetIntegerValue(value)) held |= bit;
    else held &= ~bit;
    if (held) keys.devices[key] = @(held);
    else [keys.devices removeObjectForKey:key];
    [keys publish];
}

static void modifierDeviceRemoved(void *context, IOReturn status, void *sender, IOHIDDeviceRef device) {
    if (!device) return;
    JardPhysicalModifiers *keys = (__bridge JardPhysicalModifiers *)context;
    [keys.devices removeObjectForKey:[NSValue valueWithPointer:device]];
    [keys publish];
}

- (instancetype)init {
    if (!(self = [super init])) return nil;
    atomic_init(&_held, 0);
    _queue = dispatch_queue_create("local.jardaudio.physical-modifiers", DISPATCH_QUEUE_SERIAL);
    _devices = NSMutableDictionary.dictionary;
    dispatch_async(_queue, ^{
        if (!CGPreflightListenEventAccess()) return;
        IOHIDManagerRef manager = IOHIDManagerCreate(kCFAllocatorDefault, kIOHIDOptionsTypeNone);
        if (!manager) return;
        NSDictionary *keyboards = @{@(kIOHIDDeviceUsagePageKey): @(kHIDPage_GenericDesktop),
            @(kIOHIDDeviceUsageKey): @(kHIDUsage_GD_Keyboard)};
        IOHIDManagerSetDeviceMatching(manager, (__bridge CFDictionaryRef)keyboards);
        NSMutableArray *tracked = NSMutableArray.array;
        for (uint32_t usage = kHIDUsage_KeyboardCapsLock; usage <= kHIDUsage_KeyboardRightGUI; usage++) {
            if (physicalModifierBit(usage))
                [tracked addObject:@{@(kIOHIDElementUsagePageKey): @(kHIDPage_KeyboardOrKeypad), @(kIOHIDElementUsageKey): @(usage)}];
        }
        IOHIDManagerSetInputValueMatchingMultiple(manager, (__bridge CFArrayRef)tracked);
        if (IOHIDManagerOpen(manager, kIOHIDOptionsTypeNone) != kIOReturnSuccess) {
            CFRelease(manager);
            return;
        }
        void *context = (__bridge_retained void *)self;
        IOHIDManagerRegisterInputValueCallback(manager, modifierValueChanged, context);
        IOHIDManagerRegisterDeviceRemovalCallback(manager, modifierDeviceRemoved, context);
        IOHIDManagerSetDispatchQueue(manager, self.queue);
        IOHIDManagerSetCancelHandler(manager, ^{
            IOHIDManagerClose(manager, kIOHIDOptionsTypeNone);
            CFRelease(manager);
            (void)CFBridgingRelease(context);
        });
        self->_manager = manager;
        IOHIDManagerActivate(manager);
    });
    return self;
}

- (uint16_t)held { return atomic_load_explicit(&_held, memory_order_relaxed); }

- (void)stop {
    dispatch_async(self.queue, ^{
        if (self->_manager) IOHIDManagerCancel(self->_manager);
        self->_manager = NULL;
    });
}
@end

static NSString *microphoneStatus(void) {
    switch ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio]) {
        case AVAuthorizationStatusAuthorized: return @"granted";
        case AVAuthorizationStatusNotDetermined: return @"request";
        case AVAuthorizationStatusRestricted: return @"restricted";
        default: return @"denied";
    }
}

static BOOL accessibilityAllowed(void) {
    return AXIsProcessTrusted() || CGPreflightPostEventAccess();
}

static BOOL JardOnboardingAccessRecoveryStep(NSString *step) {
    return [step isEqual:@"access"] || [step isEqual:@"practice"] || [step isEqual:@"ready"];
}

static NSScreen *activeScreen(void) {
    NSPoint mouse = NSEvent.mouseLocation;
    for (NSScreen *screen in NSScreen.screens) {
        if (NSPointInRect(mouse, screen.frame)) return screen;
    }
    return NSScreen.mainScreen;
}

@interface JardDictationPanel : NSPanel
@end
@implementation JardDictationPanel
- (BOOL)canBecomeKeyWindow { return NO; }
- (BOOL)canBecomeMainWindow { return NO; }
@end

static BOOL JardIsRTLLetter(unichar ch) {
    return (ch >= 0x0590 && ch <= 0x08FF) || (ch >= 0xFB1D && ch <= 0xFDFF) || (ch >= 0xFE70 && ch <= 0xFEFF);
}

static BOOL JardTextStartsRTL(NSString *stable, NSString *tentative) {
    NSString *combined = stable.length ? stable : tentative;
    for (NSUInteger i = 0; i < combined.length; i++) {
        unichar ch = [combined characterAtIndex:i];
        if (JardIsRTLLetter(ch)) return YES;
        if ([[NSCharacterSet letterCharacterSet] characterIsMember:ch]) return NO;
    }
    return NO;
}

@interface JardLiveTextView : NSView
@property(nonatomic, copy) NSString *stableText;
@property(nonatomic, copy) NSString *tentativeText;
- (void)setStable:(NSString *)stable tentative:(NSString *)tentative;
@end

@implementation JardLiveTextView
- (BOOL)isFlipped { return YES; }

- (void)setStable:(NSString *)stable tentative:(NSString *)tentative {
    NSString *nextStable = stable ?: @"";
    NSString *nextTentative = tentative ?: @"";
    if ([self.stableText isEqual:nextStable] && [self.tentativeText isEqual:nextTentative]) return;
    self.stableText = nextStable;
    self.tentativeText = nextTentative;
    self.needsDisplay = YES;
}

- (void)drawRect:(NSRect)dirtyRect {
    NSString *stable = [self.stableText stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet] ?: @"";
    NSString *tentative = [self.tentativeText stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet] ?: @"";
    if (!stable.length && !tentative.length) return;

    BOOL rtl = JardTextStartsRTL(stable, tentative);
    NSFont *font = [NSFont systemFontOfSize:13.0 weight:NSFontWeightMedium];
    NSMutableParagraphStyle *para = [NSMutableParagraphStyle new];
    para.lineBreakMode = NSLineBreakByTruncatingHead;
    para.alignment = rtl ? NSTextAlignmentRight : NSTextAlignmentLeft;
    para.baseWritingDirection = rtl ? NSWritingDirectionRightToLeft : NSWritingDirectionLeftToRight;

    NSMutableAttributedString *attr = [NSMutableAttributedString new];
    NSDictionary *stableAttrs = @{
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: [NSColor colorWithSRGBRed:0.96 green:0.97 blue:0.99 alpha:0.96],
        NSParagraphStyleAttributeName: para,
    };
    NSDictionary *tentativeAttrs = @{
        NSFontAttributeName: font,
        NSForegroundColorAttributeName: [NSColor colorWithSRGBRed:0.78 green:0.81 blue:0.86 alpha:0.60],
        NSParagraphStyleAttributeName: para,
    };
    if (stable.length) [attr appendAttributedString:[[NSAttributedString alloc] initWithString:stable attributes:stableAttrs]];
    if (tentative.length) {
        NSString *gap = stable.length ? @" " : @"";
        [attr appendAttributedString:[[NSAttributedString alloc] initWithString:[gap stringByAppendingString:tentative] attributes:tentativeAttrs]];
    }

    CGFloat maxPlateWidth = NSWidth(self.bounds) - 12.0;
    CGFloat textMaxWidth = maxPlateWidth - 28.0;
    CGFloat measured = ceil([attr boundingRectWithSize:NSMakeSize(CGFLOAT_MAX, 24.0) options:0].size.width);
    CGFloat plateWidth = MIN(maxPlateWidth, MAX(96.0, measured + 28.0));
    CGFloat plateHeight = 32.0;
    NSRect plateRect = NSMakeRect(floor((NSWidth(self.bounds) - plateWidth) * 0.5),
                                  floor((NSHeight(self.bounds) - plateHeight) * 0.5),
                                  plateWidth, plateHeight);
    NSBezierPath *pill = [NSBezierPath bezierPathWithRoundedRect:plateRect xRadius:plateHeight * 0.5 yRadius:plateHeight * 0.5];

    [NSGraphicsContext saveGraphicsState];
    NSShadow *shadow = [NSShadow new];
    shadow.shadowColor = [NSColor colorWithSRGBRed:0 green:0 blue:0 alpha:0.35];
    shadow.shadowBlurRadius = 6.0;
    shadow.shadowOffset = NSMakeSize(0, -2.0);
    [shadow set];
    [[NSColor colorWithSRGBRed:0.08 green:0.085 blue:0.095 alpha:0.92] setFill];
    [pill fill];
    [NSGraphicsContext restoreGraphicsState];

    [[NSColor colorWithSRGBRed:1.0 green:1.0 blue:1.0 alpha:0.14] setStroke];
    pill.lineWidth = 0.75;
    [pill stroke];

    CGFloat textWidth = MIN(textMaxWidth, plateWidth - 28.0);
    NSRect textRect = NSMakeRect(NSMinX(plateRect) + 14.0, NSMinY(plateRect) + 7.0, textWidth, 20.0);
    [attr drawWithRect:textRect options:NSStringDrawingUsesLineFragmentOrigin | NSStringDrawingTruncatesLastVisibleLine];
}
@end

@interface JardDictationStartCue : NSObject
- (void)playURL:(NSURL *)url volume:(float)volume at:(NSTimeInterval)deadline;
- (void)afterPlayback:(dispatch_block_t)completion;
- (void)withdraw;
@end

@implementation JardDictationStartCue {
    AVAudioPlayer *_player;
    atomic_bool _withdrawn;
    BOOL _finished;
    dispatch_block_t _completion;
}

- (instancetype)init {
    self = [super init];
    if (self) atomic_init(&_withdrawn, false);
    return self;
}

static dispatch_queue_t startCueQueue(void) {
    static dispatch_queue_t queue;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        queue = dispatch_queue_create("com.jard.dictation.start-cue",
            dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL, QOS_CLASS_USER_INITIATED, 0));
    });
    return queue;
}

- (AVAudioPlayer *)openSound:(NSURL *)url {
    return [[AVAudioPlayer alloc] initWithContentsOfURL:url error:NULL];
}

- (void)finishPlayback {
    _player = nil;
    _finished = YES;
    if (_completion) dispatch_async(dispatch_get_main_queue(), _completion);
    _completion = nil;
}

- (void)afterPlayback:(dispatch_block_t)completion {
    dispatch_async(startCueQueue(), ^{
        if (self->_finished) dispatch_async(dispatch_get_main_queue(), completion);
        else self->_completion = completion;
    });
}

- (void)playURL:(NSURL *)url volume:(float)volume at:(NSTimeInterval)deadline {
    dispatch_async(startCueQueue(), ^{
        if (atomic_load(&self->_withdrawn)) return;
        self->_player = [self openSound:url];
        self->_player.volume = volume;
        [self->_player prepareToPlay];
        NSTimeInterval wait = MAX(0, deadline - NSProcessInfo.processInfo.systemUptime);
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(wait * NSEC_PER_SEC)), startCueQueue(), ^{
            if (atomic_load(&self->_withdrawn) || ![self->_player play]) {
                [self finishPlayback];
                return;
            }
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((self->_player.duration + .04) * NSEC_PER_SEC)),
                startCueQueue(), ^{ [self finishPlayback]; });
        });
    });
}

- (void)withdraw {
    if (atomic_exchange(&_withdrawn, true)) return;
    dispatch_async(startCueQueue(), ^{
        if (![self->_player isPlaying]) {
            [self finishPlayback];
            return;
        }
        [self->_player setVolume:0 fadeDuration:.020];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_MSEC), startCueQueue(), ^{
            [self->_player stop];
            [self finishPlayback];
        });
    });
}
@end

@interface JardPermissionIcon : NSImageView <NSDraggingSource>
@end
@implementation JardPermissionIcon
- (void)mouseDown:(NSEvent *)event {
    NSURL *url = NSBundle.mainBundle.bundleURL;
    NSDraggingItem *item = [[NSDraggingItem alloc] initWithPasteboardWriter:url];
    [item setDraggingFrame:self.bounds contents:self.image];
    [self beginDraggingSessionWithItems:@[item] event:event source:self];
}
- (NSDragOperation)draggingSession:(NSDraggingSession *)session
    sourceOperationMaskForDraggingContext:(NSDraggingContext)context {
    return NSDragOperationCopy;
}
@end

@interface JardTranscriptReply : NSObject
@property(nonatomic, copy) JardDictationReply reply;
+ (instancetype)waitFor:(JardDictationReply)reply seconds:(int64_t)seconds;
- (void)finish:(NSDictionary *)result error:(NSString *)error;
@end

@implementation JardTranscriptReply
+ (instancetype)waitFor:(JardDictationReply)reply seconds:(int64_t)seconds {
    JardTranscriptReply *request = [self new];
    request.reply = reply;
    __weak JardTranscriptReply *waiting = request;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, seconds * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        [waiting finish:nil error:@"Timed out waiting for the dictation transcript."];
    });
    return request;
}

- (void)finish:(NSDictionary *)result error:(NSString *)error {
    JardDictationReply reply = self.reply;
    self.reply = nil;
    if (reply) reply(result, error);
}
@end

@interface JardOutputContext : NSObject
@property(nonatomic, copy) NSString *application, *bundle, *site;
@property(nonatomic, copy) NSString *browserURL, *windowTitle;
@property(nonatomic, strong) NSURL *bundleURL;
@property(nonatomic) pid_t pid;
@property(nonatomic) NSUInteger inputRevision;
@property(atomic) BOOL abandoned;
@property(nonatomic) BOOL ready;
@property(nonatomic, copy) dispatch_block_t resolved;
@end

@implementation JardOutputContext
@end

@interface JardDictationJob : NSObject
@property(nonatomic, strong) NSRunningApplication *destination;
@property(nonatomic) AXUIElementRef focus;
@property(nonatomic) BOOL focusedWindowOnly;
@property(nonatomic, strong) NSValue *selection;
@property(nonatomic, copy) NSDictionary *preferences;
@property(nonatomic, copy) NSString *mode, *outputProfile, *raw, *processed, *error, *warning;
@property(nonatomic, copy) NSString *historyID;
@property(nonatomic, copy) NSString *stageID;
@property(nonatomic, strong) NSURL *pendingHistoryAudio;
@property(nonatomic) NSTimeInterval captureEndedAt;
@property(nonatomic, strong) NSNumber *inferenceMs;
@property(nonatomic, copy) NSDictionary *processing;
@property(nonatomic) NSTimeInterval submittedAt;
@property(nonatomic) NSTimeInterval stopRequestedAt, speechRequestedAt, responseReadyAt, deliveryStartedAt, deliveryFinishedAt;
@property(nonatomic, strong) NSNumber *speechRequestMs, *clipboardSnapshotMs;
@property(nonatomic, copy) NSDictionary *speechTiming;
@property(nonatomic, copy) void (^pasteCompletion)(BOOL);
@property(nonatomic, strong) JardTranscriptReply *printRequest;
@property(nonatomic, strong) JardOutputContext *outputContext;
@property(nonatomic) BOOL waitingToSubmit;
@property(nonatomic) BOOL practice, interrupted, secureInterrupted, ready, cancelled, rawOutput;
@property(nonatomic, copy) NSString *microphoneLossReason;
@property(nonatomic) NSInteger clipboardAtLoss;
@property(nonatomic) BOOL meetingExcerpt, pasteLastRecovery, playsCompletionCue;
@property(nonatomic) double duration;
@property(nonatomic) NSUInteger inputRevision;
@property(nonatomic) NSUInteger takeSequence;
@end

@implementation JardDictationJob
- (void)discardPendingHistoryAudio {
    if (!self.pendingHistoryAudio) return;
    unlink(self.pendingHistoryAudio.fileSystemRepresentation);
    self.pendingHistoryAudio = nil;
}
- (void)dealloc {
    [self discardPendingHistoryAudio];
    if (_focus) CFRelease(_focus);
}
@end

@interface JardPressContext : NSObject
@property(nonatomic, strong) NSRunningApplication *destination;
@property(nonatomic) AXUIElementRef focus;
@property(nonatomic) BOOL focusedWindowOnly;
@property(nonatomic, strong) NSValue *selection;
@property(nonatomic, copy) NSString *site;
@property(nonatomic, copy) NSDictionary *preferences;
@property(nonatomic, strong) JardOutputContext *outputContext;
@property(nonatomic) NSUInteger inputRevision;
@property(nonatomic) NSTimeInterval pressedAt, releasedAt;
@property(nonatomic) BOOL focusResolved;
@end

@implementation JardPressContext
- (void)dealloc { if (_focus) CFRelease(_focus); }
@end

@interface JardTypingDelivery : NSObject
@property(nonatomic, copy) NSString *text, *warning;
@property(nonatomic, copy) NSDictionary *layout;
@property(nonatomic, copy) NSArray *events;
@property(nonatomic) NSUInteger cursor, edge, entered, generation;
@property(nonatomic) JardInsertionStyle style;
@end
@implementation JardTypingDelivery
@end

typedef struct {
    NSTimeInterval pressedAt, lastSeenAt;
} JardTrackedShortcutKey;

typedef struct {
    CFMachPortRef port;
    CFRunLoopSourceRef source;
    NSTimeInterval retryAt;
    BOOL interruptionReported;
} JardEventTap;

static BOOL JardEventTapEnabled(const JardEventTap *tap) {
    return tap->port && CFMachPortIsValid(tap->port) && CGEventTapIsEnabled(tap->port);
}

static void JardDetachEventTap(JardEventTap *tap) {
    if (tap->source) {
        CFRunLoopRemoveSource(CFRunLoopGetMain(), tap->source, kCFRunLoopCommonModes);
        CFRelease(tap->source);
    }
    if (tap->port) {
        CFMachPortInvalidate(tap->port);
        CFRelease(tap->port);
    }
    *tap = (JardEventTap){0};
}

static void JardEnableEventTap(JardEventTap *tap) {
    if (tap->port && CFMachPortIsValid(tap->port) && !CGEventTapIsEnabled(tap->port))
        CGEventTapEnable(tap->port, true);
}

// Returns whether a previously attached tap had a gap, even if it recovered.
static BOOL JardMaintainEventTap(JardEventTap *tap, CGEventTapLocation location, CGEventTapOptions options,
                                CGEventMask mask, CGEventTapCallBack callback, void *context, NSString *label) {
    if (JardEventTapEnabled(tap)) return NO;
    BOOL attached = tap->port != NULL;
    BOOL interrupted = attached && !tap->interruptionReported;
    JardEnableEventTap(tap);
    if (JardEventTapEnabled(tap)) {
        tap->interruptionReported = NO;
        NSLog(@"Jaro re-enabled its %@ event tap.", label);
        return interrupted;
    }
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    if (now < tap->retryAt) return interrupted;
    BOOL recovering = attached || tap->retryAt > 0;
    JardDetachEventTap(tap);
    tap->port = CGEventTapCreate(location, kCGHeadInsertEventTap, options, mask, callback, context);
    if (tap->port) tap->source = CFMachPortCreateRunLoopSource(NULL, tap->port, 0);
    if (tap->source) {
        CFRunLoopAddSource(CFRunLoopGetMain(), tap->source, kCFRunLoopCommonModes);
        JardEnableEventTap(tap);
    }
    if (!tap->source || !JardEventTapEnabled(tap)) {
        if (!recovering || attached) NSLog(@"Jaro could not attach its %@ event tap. It will retry.", label);
        JardDetachEventTap(tap);
        tap->retryAt = now + 5;
    } else if (recovering) {
        NSLog(@"Jaro recreated its %@ event tap.", label);
    }
    return interrupted;
}

@interface JardSystemDictation () <AVAudioPlayerDelegate> {
    JardEventTap _sessionTap, _physicalKeyTap, _modifierFilter;
    NSTimeInterval _physicalChordTimes[8];
    NSTimeInterval _modifierSidePress[4][2];
    NSUInteger _nextPhysicalChord;
    JardTrackedShortcutKey _trackedShortcutKeys[128];
    NSTimeInterval _shortcutRecoveryFence, _modifierNeutralAt, _modifierChangedAt;
    CGEventFlags _modifiersSinceNeutral;
    BOOL _functionReleaseObserved;
    JardShortcutLayout _shortcutLayout, _pendingShortcutLayout;
    BOOL _hasShortcutLayout, _shortcutLayoutPending;
}
@property(nonatomic, strong) JardRecordingEnvironment *recordingEnvironment;
@property(nonatomic, strong) JardDictationJob *activeOutput;
@property(nonatomic, strong) JardTranscriptReply *printRequest;
@property(nonatomic) BOOL commandRawTake;
@property(nonatomic) NSUInteger recordingGeneration;
@property(nonatomic) NSUInteger takeSequence, lastOutcomeSequence;
@property(nonatomic, copy) NSString *lastFailure;
@property(nonatomic) BOOL mediaAccessPending;
@property(nonatomic, copy) NSString *mediaAccessMessage;
@property(nonatomic) BOOL browserAccessPending;
@property(nonatomic, copy) NSString *browserAccessMessage;
@property(nonatomic, strong) NSRunningApplication *lastWebsiteBrowser;
@property(nonatomic, copy) JardDictationSend send;
@property(nonatomic, copy) void (^meetingAction)(void);
@property(nonatomic, copy) void (^activeMeetingPaste)(BOOL);
@property(nonatomic, copy) JardAnnotationInput annotationEvents;
@property(nonatomic) BOOL meetingAvailable, suppressedMeetingPaste, dictationKeyEnabled;
@property(nonatomic) BOOL annotationActive;
@property(nonatomic) BOOL annotationMicrophoneShared;
@property(nonatomic) BOOL annotationPreservesDictation;
@property(nonatomic) BOOL systemAudioRecordingActive;
@property(nonatomic, copy) NSString *annotationPhase;
@property(nonatomic) NSUInteger annotationCueGeneration;
@property(nonatomic) BOOL captureUsesSharedMicrophone;
@property(nonatomic) BOOL annotationKeyDown;
@property(nonatomic, strong) NSRunningApplication *lastExternalApp;
@property(nonatomic, copy) JardDictationReply annotationPasteReply;
@property(nonatomic) NSUInteger annotationPasteGeneration;
@property(nonatomic) NSUInteger annotationItemsPosted;
@property(nonatomic, strong) NSMutableDictionary *settings;
@property(nonatomic, copy) NSDictionary *loadedSettings;
@property(nonatomic, copy) NSString *preferencesWarning;
@property(nonatomic) BOOL preferencesUnreadable, deferHistoryExpiry;
@property(nonatomic) NSUInteger preferencesRevision;
@property(nonatomic) NSTimeInterval nextPreferencesRead;
@property(nonatomic) BOOL preferencesReadInFlight;
@property(nonatomic, strong) NSMutableArray<NSDictionary *> *history;
@property(nonatomic, strong) NSURL *historyAudioDirectory;
@property(nonatomic, strong) NSTimer *historyAudioTimer;
@property(nonatomic) NSUInteger historyAudioRevision;
@property(nonatomic, strong) AVAudioPlayer *historyAudioPlayer;
@property(nonatomic, copy) NSString *playingHistoryID;
@property(nonatomic, strong) NSMutableArray<JardDictationJob *> *pendingJobs;
@property(nonatomic, strong) JardDictationJob *lastInsertion;
@property(nonatomic) NSUInteger inputRevision, takeInputRevision;
@property(nonatomic) NSUInteger typedOutputGeneration;
@property(nonatomic, copy) NSDictionary *capturePreferences;
@property(nonatomic, strong) JardOutputContext *captureOutputContext;
@property(nonatomic, strong) JardOutputContext *captureOutputCandidate;
@property(nonatomic) BOOL dispatchingOutput, usingRawBinding, lastTapWasRaw;
@property(nonatomic, copy) NSString *lastText, *captureID, *phase, *message, *lastHistoryID, *websiteHost, *preview, *previewStable, *previewTentative;
@property(nonatomic, strong) NSRunningApplication *destination;
@property(nonatomic) AXUIElementRef focusedElement;
@property(nonatomic) BOOL focusedWindowOnly;
@property(nonatomic, strong) NSValue *selectedRange;
@property(nonatomic, strong) JardDictationPanel *hud, *guide, *liveTextPanel;
@property(nonatomic, strong) JardDictationMeter *meter;
@property(nonatomic, strong) JardLiveTextView *liveTextView;
@property(nonatomic) NSUInteger hudRehomeAttempts;
@property(nonatomic) NSTimeInterval hudRehomeDeadline, nextHUDPlacementAt;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSSound *> *cues;
@property(nonatomic, strong) NSDictionary<NSString *, NSDictionary<NSString *, NSURL *> *> *soundSources;
@property(nonatomic, strong) NSMutableArray<AVAudioPlayer *> *previewSounds;
@property(nonatomic, copy) NSDictionary *soundPreviewAnimation;
@property(nonatomic) NSUInteger soundPreviewGeneration, pendingStopCueGeneration;
@property(nonatomic) NSTimeInterval lastStopCueAt;
@property(nonatomic, strong) JardDictationStartCue *shortcutStartCue, *captureStartCue;
@property(nonatomic) NSTimeInterval shortcutStartCueAt;
@property(nonatomic, strong) NSTimer *monitor, *animation;
@property(nonatomic, strong) JardSecureHotkeys *secureHotkeys;
@property(nonatomic, strong) JardPhysicalModifiers *physicalModifiers;
@property(nonatomic, copy) NSDictionary *outputScriptReview;
@property(nonatomic) NSUInteger outputScriptRevision;
@property(nonatomic) BOOL secureInput;
@property(nonatomic, strong) id<NSObject> listenerActivity;
@property(nonatomic) NSUInteger shortcutActionRevision;
@property(nonatomic) NSTimeInterval neutralShortcutSince;
@property(nonatomic) NSTimeInterval modifierClaimAt;
@property(nonatomic) JardShortcut shortcut;
@property(nonatomic, strong) NSDictionary *warmRequest;
@property(nonatomic, copy) NSString *warmRequestID;
@property(nonatomic, copy) NSDictionary *warmInput;
@property(nonatomic) BOOL systemAsleep, displayAsleep, screenLocked, inactiveSession, updatePaused;
@property(nonatomic) BOOL sdkRecording;
@property(nonatomic) BOOL workspace, started, stopping, cancelled, transcribing, practice;
@property(nonatomic) BOOL discardingShortcut, deferredShortcutStart;
@property(nonatomic) BOOL closingReleaseTail, captureRawBinding;
@property(nonatomic) NSTimeInterval queuedCaptureStart;
@property(nonatomic, strong) JardPressContext *tailPress;
@property(nonatomic) BOOL accessibilityVisitedSettings, accessibilityStale, accessibilityRepairing, listenerStartFailed;
@property(nonatomic) NSTimeInterval accessibilityAskedAt, accessibilityReturnedAt, accessibilityReturnDeadline;
@property(nonatomic) BOOL secureTakeInterrupted;
@property(nonatomic) NSTimeInterval shortcutReleasedAt;
@property(nonatomic) NSTimeInterval captureIntentAt;
@property(nonatomic) NSTimeInterval captureOpenedAt;
@property(nonatomic) NSTimeInterval captureStopRequestedAt;
@property(nonatomic) BOOL retriedColdCapture, captureStartAnnounced, captureIntentional, captureMicReady, captureFromCommand;
@property(nonatomic) BOOL keyDown, suppressedKey, suppressedPaste, choosingShortcut, closed;
@property(nonatomic) CGEventFlags lastModifierFlags;
@property(nonatomic) CGEventFlags lastRemappedSides;
@property(nonatomic) NSTimeInterval alternateModifierTap;
@property(nonatomic) NSUInteger armGeneration, hudGeneration;
@property(nonatomic) NSInteger shortcutCandidate, ignoredRelease, cancelRelease;
@property(nonatomic) CGEventFlags shortcutCandidateFlags, shortcutCandidateBoth;
@property(nonatomic) BOOL shortcutCandidateUsedByKey, shortcutCandidateEither;
@property(nonatomic, copy) NSString *shortcutTarget;
@property(nonatomic) double inputRMS;
@property(nonatomic, strong) JardClipboardSnapshot *clipboard;
@property(nonatomic) NSInteger clipboardCount;
@property(nonatomic) NSUInteger clipboardGeneration;
@property(nonatomic) BOOL clipboardPending, pasteSettling, reportedActivity;
@property(nonatomic, copy) void (^releaseCaretPasteKeys)(void);
@property(nonatomic) BOOL caretPasteSent;
@end

static AXUIElementRef copyAppElement(pid_t pid, CFStringRef attribute, AXError *status) CF_RETURNS_RETAINED {
    if (status) *status = kAXErrorFailure;
    if (pid <= 0) return NULL;
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    AXUIElementSetMessagingTimeout(app, 0.10);
    CFTypeRef element = NULL;
    AXError error = AXUIElementCopyAttributeValue(app, attribute, &element);
    CFRelease(app);
    if (status) *status = error;
    if (element && (error != kAXErrorSuccess || CFGetTypeID(element) != AXUIElementGetTypeID())) {
        CFRelease(element);
        return NULL;
    }
    return (AXUIElementRef)element;
}

static AXUIElementRef copyFocus(pid_t pid) CF_RETURNS_RETAINED {
    return copyAppElement(pid, kAXFocusedUIElementAttribute, NULL);
}

static AXUIElementRef copyDictationTarget(pid_t pid, BOOL allowWindow, BOOL *windowOnly) CF_RETURNS_RETAINED {
    *windowOnly = NO;
    AXError status;
    AXUIElementRef target = copyAppElement(pid, kAXFocusedUIElementAttribute, &status);
    // Some editors report their focused window but expose no focused text element.
    // A timeout or other AX error cannot establish that the element is absent.
    if (!target && allowWindow && (status == kAXErrorNoValue || status == kAXErrorAttributeUnsupported) &&
        !IsSecureEventInputEnabled()) {
        target = copyAppElement(pid, kAXFocusedWindowAttribute, NULL);
        *windowOnly = target != NULL;
    }
    return target;
}

static BOOL secureField(AXUIElementRef element) {
    if (IsSecureEventInputEnabled()) return YES;
    if (!element) return NO;
    CFTypeRef value = NULL;
    AXUIElementCopyAttributeValue(element, kAXSubroleAttribute, &value);
    BOOL secure = value && CFEqual(value, kAXSecureTextFieldSubrole);
    if (value) CFRelease(value);
    return secure;
}

static NSValue *selectedRange(AXUIElementRef element) {
    CFTypeRef value = NULL;
    AXUIElementCopyAttributeValue(element, kAXSelectedTextRangeAttribute, &value);
    CFRange range;
    BOOL readable = value && CFGetTypeID(value) == AXValueGetTypeID() &&
        AXValueGetValue((AXValueRef)value, kAXValueCFRangeType, &range) && range.location >= 0 && range.length >= 0;
    if (value) CFRelease(value);
    return readable ? [NSValue valueWithRange:NSMakeRange((NSUInteger)range.location, (NSUInteger)range.length)] : nil;
}

static NSURL *browserURL(AXUIElementRef focus, NSTimeInterval deadline) {
    if (!focus) return nil;
    AXUIElementRef element = (AXUIElementRef)CFRetain(focus);
    NSURL *address = nil;
    // Only ancestors of this focused element can describe its active page.
    for (NSUInteger depth = 0; element && depth < 12; ++depth) {
        if (deadline && NSProcessInfo.processInfo.systemUptime >= deadline) break;
        if (deadline) AXUIElementSetMessagingTimeout(element, .10);
        CFTypeRef role = NULL;
        AXUIElementCopyAttributeValue(element, kAXRoleAttribute, &role);
        BOOL webArea = role && CFEqual(role, CFSTR("AXWebArea"));
        if (role) CFRelease(role);
        if (webArea) {
            CFTypeRef value = NULL;
            AXUIElementCopyAttributeValue(element, kAXURLAttribute, &value);
            address = JardWebContextURL(CFBridgingRelease(value));
            break;
        }
        CFTypeRef parent = NULL;
        AXUIElementCopyAttributeValue(element, kAXParentAttribute, &parent);
        CFRelease(element);
        element = parent && CFGetTypeID(parent) == AXUIElementGetTypeID() ? (AXUIElementRef)parent : NULL;
        if (parent && !element) CFRelease(parent);
    }
    if (element) CFRelease(element);
    return address;
}

static NSString *browserHost(AXUIElementRef focus, NSTimeInterval deadline) {
    return browserURL(focus, deadline).host.lowercaseString;
}

static CGEventRef dictationTap(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *context);
static CGEventRef dictationPhysicalKeyTap(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *context);
static CGEventRef dictationModifierTap(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *context);
static CGEventFlags modifierFlag(CGKeyCode code);
static CGEventFlags JardEventModifiers(CGEventRef event, CGKeyCode code);
static NSTimeInterval JardShortcutEventTime(CGEventRef event, BOOL physical);
static NSString *JardShortcutSide(CGKeyCode code, BOOL either);
static NSDictionary *JardDefaultPreferences(void);
static NSDictionary *JardDecodePreferences(NSDictionary *saved, NSMutableArray<NSString *> *invalid);
static NSDictionary *JardCommitPreferences(id saved, NSDictionary *base, NSDictionary *edited,
                                            NSDictionary *application, NSString **error);
static NSArray<NSArray<NSString *> *> *JardVocabularyAliases(NSDictionary *preferences);
static NSArray *JardVocabularyEntries(NSDictionary *settings);
static NSString *JardCorrectVocabulary(NSString *text, NSDictionary *preferences);

static id JardReadSavedPreferences(BOOL *readable) {
    *readable = CFPreferencesSynchronize(kCFPreferencesCurrentApplication, kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    if (!*readable) return nil;
    return CFBridgingRelease(CFPreferencesCopyValue((__bridge CFStringRef)preferencesKey,
        kCFPreferencesCurrentApplication, kCFPreferencesCurrentUser, kCFPreferencesAnyHost));
}

static NSString *JardOutputAgentPrompt(void) {
    NSString *cli = [[NSBundle.mainBundle.bundlePath stringByAppendingPathComponent:@"Contents/Helpers"]
        stringByAppendingPathComponent:@"jardctl"];
    return [NSString stringWithFormat:
        @"Help me change how Jaro formats my dictation before insertion. Ask what output I want if I have not specified it.\n\n"
        @"Use jardctl on PATH or the bundled executable at %@. Read the current values with "
        @"`jardctl settings get dictation.outputProfiles`, `jardctl settings get dictation.replacements`, "
        @"`jardctl settings get dictation.vocabulary`, and `jardctl settings get dictation.rewriteInstructions`. "
        @"Use `jardctl settings list` to inspect the supported keys. Preserve rules unrelated to my request.\n\n"
        @"`dictation.outputProfiles` is a string containing a JSON array. Each profile has a `target` "
        @"(`default`, `app:App Name`, or `site:example.com`), ordered `steps`, `vocabulary` (an array of strings), "
        @"`instruction`, `deadline` (seconds), and `corrections`. Website rules take precedence over app rules. "
        @"Supported steps are trim, replace, fillers, format, symbols, sentence, compact, lowercase, spongebob-case, "
        @"no-trailing-punctuation, rewrite, and script. Named local steps use `local:<id>`. "
        @"If I request a JavaScript or TypeScript step, use `jardctl transforms init` to locate the config "
        @"and `jardctl transforms check` to validate it. Leave version and capability approvals to me in Output.\n\n"
        @"Use `jardctl vocabulary list --json` and `jardctl vocabulary add` or `remove` for names and alternate spellings. "
        @"Use one `phrase => replacement` per line for other corrections. "
        @"Rewrite uses Jaro's local Qwen. Speech recognition uses Rumble2 Flash. "
        @"Apply only the changed keys with `jardctl settings apply -` and a JSON object on stdin. "
        @"Keep the profiles value encoded as a string inside that object. Confirm the saved values, then show me what changed.\n", cli];
}

static unichar JardShortcutLetter(NSString *label) {
    if (![label isKindOfClass:NSString.class]) return 0;
    NSUInteger start = 0;
    while (start < label.length) {
        unichar prefix = [label characterAtIndex:start];
        if (prefix != 0x2303 && prefix != 0x2325 && prefix != 0x21e7 && prefix != 0x2318) break; // ⌃⌥⇧⌘
        start++;
    }
    if (label.length - start == 4 && [label compare:@"Fn " options:0 range:NSMakeRange(start, 3)] == NSOrderedSame)
        start += 3;
    return label.length - start == 1 ? [label characterAtIndex:start] : 0;
}

static NSInteger JardPreferenceShortcutCode(NSDictionary *settings, NSString *slot, const JardShortcutLayout *layout) {
    NSInteger recorded = [settings[[slot stringByAppendingString:@"Code"]] integerValue];
    if (!layout || recorded < 0 || recorded >= 128 || modifierFlag((CGKeyCode)recorded)) return recorded;
    CGEventFlags flags = [settings[[slot stringByAppendingString:@"Flags"]] unsignedLongLongValue];
    return JardShortcutLetterCode(layout, JardShortcutLetter(settings[[slot stringByAppendingString:@"Label"]]),
        (flags & kCGEventFlagMaskCommand) != 0, recorded);
}

static BOOL JardAnnotationChordConflicts(NSDictionary *settings, const JardShortcutLayout *layout) {
    NSInteger code = JardPreferenceShortcutCode(settings, @"annotate", layout);
    CGEventFlags flags = [settings[@"annotateFlags"] unsignedLongLongValue];
    if (flags == meetingPasteFlags && code == JardShortcutLetterCode(layout, 'v', NO, kVK_ANSI_V)) return YES;
    for (NSString *slot in @[@"shortcut", @"raw", @"paste", @"cancel"]) {
        if (flags == [settings[[slot stringByAppendingString:@"Flags"]] unsignedLongLongValue] &&
            code == JardPreferenceShortcutCode(settings, slot, layout)) return YES;
    }
    return NO;
}

static NSDictionary *JardDefaultPreferences(void) {
    return @{@"enabled": @NO, @"onboarding": @"welcome", @"mode": @"holdDouble",
                  @"shortcutCode": @(kVK_RightOption), @"shortcutFlags": @(kCGEventFlagMaskAlternate),
                  @"shortcutLabel": @"Right ⌥", @"shortcutEitherSide": @NO,
                  @"shortcutBothSides": @0, @"rawBothSides": @0,
                  @"rawCode": @(-1), @"rawFlags": @0, @"rawLabel": @"",
                  @"pasteCode": @(kVK_ANSI_V), @"pasteFlags": @(kCGEventFlagMaskAlternate | kCGEventFlagMaskShift),
                  @"pasteLabel": @"⌥⇧V", @"cancelCode": @(kVK_Escape), @"cancelFlags": @0, @"cancelLabel": @"Escape",
                  @"annotateCode": @(kVK_ANSI_S), @"annotateFlags": @(defaultAnnotationFlags), @"annotateLabel": @"⌥⇧S",
                  @"annotationEnabled": @YES, @"annotationShowOverlays": @NO,
                  @"retention": @7, @"previousRetention": @7, @"showHistoryStats": @NO, @"sound": @YES,
                  @"placement": @"top", @"output": @"paste", @"fillerWords": @NO,
                  @"spokenFormatting": @NO, @"replacements": @"", @"appRules": @"",
                  @"languages": @[], @"inputDevice": @"", @"avoidBluetooth": @YES, @"vocabulary": @"", @"vocabularySoundsLike": @{}, @"vocabularyMenu": @NO, @"livePreview": @NO, @"keepMicrophoneReady": @NO, @"noiseCleanup": @NO, @"boostMicrophone": @NO,
                  @"outputProfiles": @"", @"outputScript": @"", @"outputScriptApproval": @{}, @"rewriteInstructions": @"",
                  @"soundSet": @"classic", @"soundVolume": @70, @"audioWhileDictating": @"none", @"lowerAudioPercent": @20};
}

@implementation JardSystemDictation

+ (NSURL *)audioDirectoryForLibrary:(NSString *)configured defaultLibrary:(NSString *)defaultLibrary bundleID:(NSString *)bundleID {
    NSString *library = configured.length ? configured.stringByExpandingTildeInPath : defaultLibrary;
    // A different preferences domain has a different index. Its cleanup must
    // never treat the installed app’s audio as orphaned.
    if (![bundleID isEqual:@"local.jardaudio.desktop"]) {
        if (!configured.length || [library.stringByResolvingSymlinksInPath isEqual:defaultLibrary.stringByResolvingSymlinksInPath]) return nil;
        struct stat source, installed;
        if (stat(library.fileSystemRepresentation, &source) == 0 && stat(defaultLibrary.fileSystemRepresentation, &installed) == 0 &&
            source.st_dev == installed.st_dev && source.st_ino == installed.st_ino) return nil;
    }
    return [[NSURL fileURLWithPath:library isDirectory:YES] URLByAppendingPathComponent:@"dictation-history-audio" isDirectory:YES];
}

- (instancetype)initWithSend:(JardDictationSend)send {
    if (!(self = [super init])) return nil;
    _send = [send copy];
    _soundSources = JardDictationSoundSources();
    id saved = [NSUserDefaults.standardUserDefaults objectForKey:preferencesKey];
    _preferencesUnreadable = saved && ![saved isKindOfClass:NSDictionary.class];
    NSMutableArray<NSString *> *invalid = NSMutableArray.array;
    _settings = [JardDecodePreferences(_preferencesUnreadable ? nil : saved, invalid) mutableCopy];
    if (_preferencesUnreadable) {
        _settings[@"onboarding"] = @"later";
        _preferencesWarning = @"Jard loaded default dictation settings. The saved preferences could not be read.";
        NSLog(@"Jard dictation preferences are not a dictionary.");
    } else if (invalid.count) {
        _preferencesWarning = repairedPreferencesWarning;
        NSLog(@"Jard used defaults for invalid dictation settings: %@", [invalid componentsJoinedByString:@", "]);
    }
    _loadedSettings = [_settings copy];
    _deferHistoryExpiry = _preferencesUnreadable || [invalid containsObject:@"retention"] || [invalid containsObject:@"previousRetention"];
    _pendingJobs = NSMutableArray.array;
    _history = [[NSUserDefaults.standardUserDefaults arrayForKey:historyKey] mutableCopy] ?: NSMutableArray.array;
    NSString *defaultLibrary = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Rumble"];
    const char *configuredLibrary = getenv("RUMBLE_DATA_DIR");
    _historyAudioDirectory = [JardSystemDictation audioDirectoryForLibrary:
        configuredLibrary && *configuredLibrary ? [NSString stringWithUTF8String:configuredLibrary] : nil
        defaultLibrary:defaultLibrary bundleID:NSBundle.mainBundle.bundleIdentifier];
    _shortcutCandidate = _ignoredRelease = _cancelRelease = -1;
    _phase = @"idle";
    _message = @"";
    [self applyShortcutMode];
    if (!_preferencesWarning) [self trimHistory];
    [self sweepHistoryAudioAt:NSDate.date.timeIntervalSince1970];
    for (NSDictionary *entry in _history) {
        if ([entry[@"kind"] isEqual:@"printed"]) continue;
        _lastText = entry[@"text"];
        _lastHistoryID = entry[@"id"];
        break;
    }
    return self;
}

- (void)applyShortcutMode {
    _shortcut.mode = [_settings[@"mode"] isEqual:@"tap"] ? JardToggle :
        [_settings[@"mode"] isEqual:@"hold"] ? JardHold : [_settings[@"mode"] isEqual:@"doubleTap"] ? JardDoubleTap : JardHoldOrDoubleTap;
}

static BOOL JardValidScriptDigest(id value) {
    return [value isKindOfClass:NSString.class] && [value length] == 64 &&
        [value rangeOfCharacterFromSet:[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdef"].invertedSet].location == NSNotFound;
}

static BOOL JardValidScriptGrants(id value) {
    if (![value isKindOfClass:NSDictionary.class] || [value count] > 3) return NO;
    for (id key in value)
        if (![@[@"network", @"programs", @"pageContext"] containsObject:key] ||
            CFGetTypeID((__bridge CFTypeRef)value[key]) != CFBooleanGetTypeID())
            return NO;
    return YES;
}

static NSString *JardApprovedScriptDigest(NSDictionary *settings) {
    NSDictionary *approval = settings[@"outputScriptApproval"];
    return [approval[@"path"] isEqual:settings[@"outputScript"]] ? approval[@"digest"] : @"";
}

static NSDictionary *JardApprovedScriptGrants(NSDictionary *settings) {
    if (!JardApprovedScriptDigest(settings).length) return @{};
    id grants = settings[@"outputScriptApproval"][@"grants"];
    return JardValidScriptGrants(grants) ? grants : @{};
}

typedef NS_OPTIONS(NSUInteger, JardOutputHostUse) {
    JardOutputHostForSelection = 1 << 0,
    JardOutputHostForProcessing = 1 << 1,
    JardOutputScriptPageContext = 1 << 2,
};

static JardOutputHostUse JardOutputRuleHostUse(id target, id steps, BOOL rewriteConfigured, JardOutputHostUse scriptUse) {
    NSCharacterSet *spaces = NSCharacterSet.whitespaceCharacterSet;
    if (![target isKindOfClass:NSString.class]) return 0;
    NSString *scope = [[target stringByTrimmingCharactersInSet:spaces] lowercaseString];
    JardOutputHostUse use = [scope hasPrefix:@"site:"] ? JardOutputHostForSelection : 0;
    if (!use && ![scope isEqual:@"default"] && ![scope hasPrefix:@"app:"]) return 0;
    if (![steps isKindOfClass:NSArray.class]) return use;
    for (id item in steps) {
        if (![item isKindOfClass:NSString.class]) continue;
        NSString *step = [[item stringByTrimmingCharactersInSet:spaces] lowercaseString];
        if (rewriteConfigured && [step isEqual:@"rewrite"]) use |= JardOutputHostForProcessing;
        if ([step isEqual:@"script"] || [step hasPrefix:@"local:"]) use |= scriptUse;
    }
    return use;
}

static JardOutputHostUse JardOutputProfilesHostUse(NSDictionary *settings) {
    NSCharacterSet *spaces = NSCharacterSet.whitespaceAndNewlineCharacterSet;
    NSString *rules = [settings[@"outputProfiles"] stringByTrimmingCharactersInSet:spaces];
    if (!rules.length) return 0;
    BOOL sharedRewrite = [settings[@"rewriteInstructions"] length] > 0;
    JardOutputHostUse scriptUse = JardApprovedScriptDigest(settings).length ? JardOutputHostForProcessing : 0;
    if ([JardApprovedScriptGrants(settings)[@"pageContext"] boolValue]) scriptUse |= JardOutputScriptPageContext;
    JardOutputHostUse use = 0;
    if ([rules hasPrefix:@"["]) {
        id rows = [NSJSONSerialization JSONObjectWithData:[rules dataUsingEncoding:NSUTF8StringEncoding] options:0 error:nil];
        if (![rows isKindOfClass:NSArray.class]) return 0;
        for (id row in rows) {
            if (![row isKindOfClass:NSDictionary.class]) continue;
            id instruction = row[@"instruction"];
            BOOL rewrite = sharedRewrite || ([instruction isKindOfClass:NSString.class] &&
                [instruction stringByTrimmingCharactersInSet:spaces].length > 0);
            use |= JardOutputRuleHostUse(row[@"target"], row[@"steps"], rewrite, scriptUse);
        }
    } else {
        for (NSString *line in [rules componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]) {
            NSRange separator = [line rangeOfString:@"|"];
            if (separator.location == NSNotFound) continue;
            NSArray *steps = [[line substringFromIndex:NSMaxRange(separator)] componentsSeparatedByString:@","];
            use |= JardOutputRuleHostUse([line substringToIndex:separator.location], steps, sharedRewrite, scriptUse);
        }
    }
    return use;
}

- (void)publishOutputScriptApproval {
    if (self.workspace)
        self.send(@"jard.outputScriptApproval", @{@"path": self.settings[@"outputScript"],
            @"digest": JardApprovedScriptDigest(self.settings), @"grants": JardApprovedScriptGrants(self.settings)}, nil);
}

- (void)installPreferencesSnapshot:(NSDictionary *)saved {
    NSDictionary *previous = self.settings;
    self.loadedSettings = saved;
    self.settings = [saved mutableCopy];
    self.preferencesRevision++;
    if (![previous[@"annotationEnabled"] isEqual:saved[@"annotationEnabled"]] ||
        ![previous[@"annotateCode"] isEqual:saved[@"annotateCode"]] ||
        ![previous[@"annotateFlags"] isEqual:saved[@"annotateFlags"]] ||
        ![previous[@"annotateLabel"] isEqual:saved[@"annotateLabel"]]) {
        [self handleAnnotationEvent:kCGEventTapDisabledByUserInput event:NULL chordFree:NO];
    }
    BOOL scriptChanged = ![previous[@"outputScript"] isEqual:saved[@"outputScript"]];
    if (scriptChanged || ![previous[@"outputScriptApproval"] isEqual:saved[@"outputScriptApproval"]]) {
        self.outputScriptRevision++;
        if (scriptChanged) self.outputScriptReview = nil;
        [self publishOutputScriptApproval];
    }
    if (![previous[@"mode"] isEqual:saved[@"mode"]]) {
        [self withdrawShortcutStartCue];
        self.modifierClaimAt = 0;
        [self act:jardShortcutCancel(&_shortcut)];
        if ([self.shortcutTarget isEqual:@"cancel"]) self.choosingShortcut = NO;
        [self applyShortcutMode];
    }
    if (![saved[@"sound"] boolValue] || [saved[@"soundVolume"] floatValue] <= 0) {
        [self withdrawShortcutStartCue];
        [self.captureStartCue withdraw];
    }
    for (NSString *key in @[@"sound", @"soundSet", @"soundVolume"]) {
        if (![previous[key] isEqual:saved[key]]) { [self stopSoundPreview]; break; }
    }
    for (NSString *key in saved) {
        if (([key hasPrefix:@"shortcut"] || [key hasPrefix:@"raw"]) && ![previous[key] isEqual:saved[key]]) {
            _shortcut.lastTap = 0;
            self.alternateModifierTap = 0;
            break;
        }
    }
    if (!self.preferencesWarning && (![previous[@"retention"] isEqual:saved[@"retention"]] ||
        ![previous[@"previousRetention"] isEqual:saved[@"previousRetention"]])) [self trimHistory];
    if ([saved[@"placement"] isEqual:@"off"]) [self hideHUD];
    if (self.microphonePreferenceChanged && ![previous[@"inputDevice"] isEqual:saved[@"inputDevice"]]) {
        self.microphonePreferenceChanged(saved[@"inputDevice"]);
    }
    if (self.preferencesChanged && (![previous[@"annotationShowOverlays"] isEqual:saved[@"annotationShowOverlays"]] ||
        ![previous[@"vocabularyMenu"] isEqual:saved[@"vocabularyMenu"]]))
        self.preferencesChanged(self.status);
}

- (BOOL)saveSettings:(NSDictionary *)edited error:(NSString **)error {
    return [self saveSettings:edited application:nil error:error];
}

- (BOOL)saveSettings:(NSDictionary *)edited application:(NSDictionary *)application error:(NSString **)error {
    BOOL readable = NO;
    id current = self.preferencesUnreadable ? nil : JardReadSavedPreferences(&readable);
    if (!readable) {
        *error = @"Jard could not read its saved dictation preferences. The existing value was preserved.";
        return NO;
    }
    NSDictionary *saved = JardCommitPreferences(current, self.loadedSettings, edited, application, error);
    if (!saved) return NO;
    self.preferencesWarning = nil;
    self.deferHistoryExpiry = NO;
    [self installPreferencesSnapshot:saved];
    return YES;
}

static NSUInteger JardHistoryTextBytes(NSDictionary *entry) {
    id raw = entry[@"raw"], final = entry[@"text"];
    NSUInteger rawBytes = [raw isKindOfClass:NSString.class] ? [raw lengthOfBytesUsingEncoding:NSUTF8StringEncoding] : 0;
    NSUInteger finalBytes = [final isKindOfClass:NSString.class] ? [final lengthOfBytesUsingEncoding:NSUTF8StringEncoding] : 0;
    return rawBytes + finalBytes;
}

static NSUInteger JardHistoryWords(NSString *text) {
    if (![text isKindOfClass:NSString.class]) return 0;
    __block NSUInteger count = 0;
    [text enumerateSubstringsInRange:NSMakeRange(0, text.length) options:NSStringEnumerationByWords | NSStringEnumerationSubstringNotRequired
        usingBlock:^(NSString *word, NSRange wordRange, NSRange enclosing, BOOL *stop) { count++; }];
    return count;
}

static NSDictionary *JardHistoryStats(NSArray<NSDictionary *> *entries, NSDate *now, NSCalendar *calendar) {
    NSUInteger words = 0, timedWords = 0, hours[24] = {0}, streak = 0;
    double seconds = 0;
    NSMutableSet<NSDate *> *days = NSMutableSet.set;
    for (NSDictionary *entry in entries) {
        NSUInteger count = JardHistoryWords(entry[@"text"]);
        words += count;
        double duration = [entry[@"seconds"] doubleValue];
        if (isfinite(duration) && duration > 0) {
            timedWords += count;
            seconds += duration;
        }
        NSDate *date = [NSDate dateWithTimeIntervalSince1970:[entry[@"time"] doubleValue]];
        NSInteger hour = [calendar component:NSCalendarUnitHour fromDate:date];
        if (hour >= 0 && hour < 24) hours[hour]++;
        [days addObject:[calendar startOfDayForDate:date]];
    }
    NSDate *day = [calendar startOfDayForDate:now];
    if (![days containsObject:day]) day = [calendar dateByAddingUnit:NSCalendarUnitDay value:-1 toDate:day options:0];
    while (day && [days containsObject:day]) {
        streak++;
        day = [calendar dateByAddingUnit:NSCalendarUnitDay value:-1 toDate:day options:0];
    }
    NSUInteger busiest = 0;
    for (NSUInteger hour = 1; hour < 24; hour++) if (hours[hour] > hours[busiest]) busiest = hour;
    return @{@"dictations": @(entries.count), @"words": @(words), @"streakDays": @(streak),
             @"busiestHour": hours[busiest] ? @(busiest) : NSNull.null,
             @"wordsPerMinute": seconds > 0 ? @(round(timedWords * 60.0 / seconds)) : NSNull.null,
             @"savedSeconds": seconds > 0 ? @(MAX(0., timedWords * 60.0 / 40.0 - seconds)) : NSNull.null};
}

static NSString *JardBoundHistoryText(NSString *text) {
    if ([text lengthOfBytesUsingEncoding:NSUTF8StringEncoding] <= historyTextLimit) return text;
    UInt8 buffer[16 * 1024];
    CFIndex converted = CFStringGetBytes((__bridge CFStringRef)text, CFRangeMake(0, text.length),
        kCFStringEncodingUTF8, 0, false, buffer, sizeof buffer, NULL);
    return [text substringToIndex:(NSUInteger)converted];
}

static BOOL JardHistoryAudioID(id value) {
    return [value isKindOfClass:NSString.class] && [value length] == 36 &&
        [[NSUUID alloc] initWithUUIDString:value] != nil;
}

static BOOL JardPrivateHistoryFile(NSURL *url) {
    struct stat info;
    return url && lstat(url.fileSystemRepresentation, &info) == 0 && S_ISREG(info.st_mode) &&
        info.st_uid == geteuid() && !(info.st_mode & 077) && info.st_size > 44;
}

- (BOOL)historyAudioDirectoryAvailableCreating:(BOOL)create {
    const char *path = self.historyAudioDirectory.fileSystemRepresentation;
    if (!path || (create && mkdir(path, 0700) != 0 && errno != EEXIST)) return NO;
    struct stat info;
    return lstat(path, &info) == 0 && S_ISDIR(info.st_mode) && info.st_uid == geteuid() && !(info.st_mode & 077);
}

- (NSURL *)historyAudioURLForID:(NSString *)identifier {
    return JardHistoryAudioID(identifier) ? [[self.historyAudioDirectory URLByAppendingPathComponent:identifier]
        URLByAppendingPathExtension:@"wav"] : nil;
}

- (void)holdHistoryAudioForJob:(JardDictationJob *)job {
    if (job.practice || [self.settings[@"retention"] integerValue] <= 0 || !JardHistoryAudioID(job.stageID) ||
        ![self historyAudioDirectoryAvailableCreating:YES]) return;
    NSURL *staging = [[self.historyAudioDirectory URLByDeletingLastPathComponent]
        URLByAppendingPathComponent:@"staging" isDirectory:YES];
    NSURL *original = [staging URLByAppendingPathComponent:job.stageID];
    NSURL *pending = [[self.historyAudioDirectory URLByAppendingPathComponent:job.stageID]
        URLByAppendingPathExtension:@"pending"];
    if (JardPrivateHistoryFile(original) && link(original.fileSystemRepresentation, pending.fileSystemRepresentation) == 0)
        job.pendingHistoryAudio = pending;
}

- (void)scheduleHistoryAudioSweepAt:(NSTimeInterval)deadline {
    if (self.closed || (self.historyAudioTimer.valid && self.historyAudioTimer.fireDate.timeIntervalSince1970 <= deadline)) return;
    [self.historyAudioTimer invalidate];
    __weak typeof(self) weakSelf = self;
    self.historyAudioTimer = [[NSTimer alloc] initWithFireDate:[NSDate dateWithTimeIntervalSince1970:deadline]
        interval:0 repeats:NO block:^(NSTimer *timer) {
            [weakSelf sweepHistoryAudioAt:NSDate.date.timeIntervalSince1970];
        }];
    [NSRunLoop.mainRunLoop addTimer:self.historyAudioTimer forMode:NSRunLoopCommonModes];
}

- (void)sweepHistoryAudioAt:(NSTimeInterval)now {
    [self.historyAudioTimer invalidate];
    self.historyAudioTimer = nil;
    if (![self historyAudioDirectoryAvailableCreating:NO]) return;
    NSMutableDictionary<NSString *, NSNumber *> *deadlines = NSMutableDictionary.dictionary;
    for (NSDictionary *entry in self.history) {
        id expiry = entry[@"audioExpiresAt"], identifier = entry[@"id"];
        if ([expiry isKindOfClass:NSNumber.class] && JardHistoryAudioID(identifier))
            deadlines[identifier] = expiry;
    }
    NSTimeInterval next = INFINITY;
    NSArray *files = [NSFileManager.defaultManager contentsOfDirectoryAtURL:self.historyAudioDirectory
        includingPropertiesForKeys:@[] options:0 error:nil];
    if (!files) next = now + 60;
    NSMutableSet<NSString *> *pending = NSMutableSet.set;
    for (JardDictationJob *job in self.pendingJobs)
        if (job.pendingHistoryAudio) [pending addObject:job.pendingHistoryAudio.lastPathComponent];
    BOOL playingAvailable = NO;
    for (NSURL *file in files) {
        NSString *identifier = file.lastPathComponent.stringByDeletingPathExtension;
        BOOL clip = [file.pathExtension isEqual:@"wav"];
        if (!JardHistoryAudioID(identifier) || !(clip || [file.pathExtension isEqual:@"pending"])) continue;
        if (!clip && [pending containsObject:file.lastPathComponent]) continue;
        NSTimeInterval deadline = clip ? deadlines[identifier].doubleValue : 0;
        if (isfinite(deadline) && deadline > now) {
            next = MIN(next, deadline);
            if ([self.playingHistoryID isEqual:identifier]) playingAvailable = YES;
            continue;
        }
        if (clip) self.historyAudioRevision++;
        if (unlink(file.fileSystemRepresentation) != 0 && errno != ENOENT) next = MIN(next, now + 60);
    }
    if (self.playingHistoryID && !playingAvailable) [self stopHistoryAudio];
    if (isfinite(next)) [self scheduleHistoryAudioSweepAt:next];
}

- (NSURL *)availableHistoryAudio:(NSDictionary *)entry {
    id expiry = entry[@"audioExpiresAt"];
    if (![expiry isKindOfClass:NSNumber.class] || !isfinite([expiry doubleValue]) ||
        [expiry doubleValue] <= NSDate.date.timeIntervalSince1970 || ![self historyAudioDirectoryAvailableCreating:NO]) return nil;
    NSURL *url = [self historyAudioURLForID:entry[@"id"]];
    return JardPrivateHistoryFile(url) ? url : nil;
}

- (void)stopHistoryAudio {
    self.historyAudioPlayer.delegate = nil;
    [self.historyAudioPlayer stop];
    self.historyAudioPlayer = nil;
    self.playingHistoryID = nil;
}

- (void)audioPlayerDidFinishPlaying:(AVAudioPlayer *)player successfully:(BOOL)flag {
    if (player == self.historyAudioPlayer) [self stopHistoryAudio];
}

- (void)audioPlayerDecodeErrorDidOccur:(AVAudioPlayer *)player error:(NSError *)error {
    if (player == self.historyAudioPlayer) [self stopHistoryAudio];
}

- (BOOL)pruneHistory {
    NSInteger days = [self.settings[@"retention"] integerValue] ?: [self.settings[@"previousRetention"] integerValue];
    NSTimeInterval since = NSDate.date.timeIntervalSince1970 - days * 86400;
    NSMutableIndexSet *old = NSMutableIndexSet.indexSet;
    NSUInteger retained = 0, total = 0;
    for (NSUInteger index = 0; index < self.history.count; index++) {
        NSDictionary *entry = self.history[index];
        if (!self.deferHistoryExpiry && [entry[@"time"] doubleValue] < since) {
            [old addIndex:index];
            continue;
        }
        NSUInteger bytes = JardHistoryTextBytes(entry);
        if (retained == historyEntryLimit || bytes > historyTotalTextLimit - total) {
            [old addIndexesInRange:NSMakeRange(index, self.history.count - index)];
            break;
        }
        retained++;
        total += bytes;
    }
    if (!old.count) return NO;
    [self.history removeObjectsAtIndexes:old];
    [self sweepHistoryAudioAt:NSDate.date.timeIntervalSince1970];
    return YES;
}

- (void)trimHistory {
    if ([self pruneHistory]) [self persistHistory:self.history];
}

- (void)saveHistory {
    [self pruneHistory];
    [self persistHistory:self.history];
}

- (void)persistHistory:(NSArray *)entries {
    [NSUserDefaults.standardUserDefaults setObject:entries forKey:historyKey];
}

- (void)forgetLastDictation {
    self.lastText = nil;
    self.lastHistoryID = nil;
    if (self.secureInput) [self updateSecureHotkeys:YES];
}

- (NSDictionary *)clearHistoryWithError:(NSString **)error {
    if (self.closed || self.updatePaused) { *error = @"Jaro is preparing to close."; return nil; }
    NSUInteger removed = self.history.count;
    BOOL changed = removed > 0 || self.lastText.length > 0;
    [self persistHistory:@[]];
    if (![NSUserDefaults.standardUserDefaults synchronize]) {
        [self persistHistory:self.history];
        *error = @"macOS could not clear the saved dictation history.";
        return nil;
    }
    [self.history removeAllObjects];
    [self sweepHistoryAudioAt:NSDate.date.timeIntervalSince1970];
    [self forgetLastDictation];
    return @{@"changed": @(changed), @"removed": @(removed)};
}

- (BOOL)keepsRunning {
    return [self.settings[@"enabled"] boolValue];
}

- (BOOL)quietAtLogin {
    return self.keepsRunning && [self.settings[@"onboarding"] isEqual:@"done"] &&
        [microphoneStatus() isEqual:@"granted"] && accessibilityAllowed() && !self.listenerStartFailed;
}

- (BOOL)hasActivity {
    return _shortcut.phase != JardIdle || self.captureID || self.transcribing || self.pendingJobs.count ||
        self.clipboardPending || self.pasteSettling || self.annotationPasteReply;
}

- (BOOL)sharesMicrophone {
    return self.captureID != nil && self.captureMicReady && self.captureUsesSharedMicrophone;
}

- (void)reportActivity {
    BOOL active = self.hasActivity;
    if (active == self.reportedActivity) return;
    self.reportedActivity = active;
    if (self.activityChanged) self.activityChanged();
}

- (NSDictionary *)sdkMicrophoneSettings {
    return @{ @"inputDevice": self.settings[@"inputDevice"], @"avoidBluetooth": self.settings[@"avoidBluetooth"],
              @"languages": self.settings[@"languages"] };
}

- (void)setSDKCaptureActive:(BOOL)active {
    self.sdkRecording = active;
    if (active) {
        [self stopSoundPreview];
        [self stopHistoryAudio];
    }
    [self refresh];
}

- (void)setMeetingPasteAction:(void (^)(void))action {
    self.meetingAction = action;
    if (self.workspace) [self refresh];
}

- (void)setMeetingPasteAvailable:(BOOL)available {
    if (self.meetingAvailable == available) return;
    self.meetingAvailable = available;
    [self refresh];
}

- (void)setAnnotationInput:(JardAnnotationInput)input {
    self.annotationEvents = input;
    self.annotationKeyDown = NO;
    if (self.workspace) [self refresh];
}

- (BOOL)annotationEnabled { return self.annotationEvents != nil && [self.settings[@"annotationEnabled"] boolValue]; }

- (void)setSystemAudioRecordingActive:(BOOL)active {
    if (_systemAudioRecordingActive == active) return;
    _systemAudioRecordingActive = active;
    if (active && self.captureIntentional && self.recordingEnvironment) {
        [self.recordingEnvironment setDictationMode:nil percent:0];
    }
}

- (void)setAnnotationActive:(BOOL)active {
    [self setAnnotationActive:active microphoneShared:NO];
}

- (void)setAnnotationActive:(BOOL)active microphoneShared:(BOOL)shared {
    if (_annotationActive == active && _annotationMicrophoneShared == shared) return;
    if (!active || !_annotationActive) self.annotationPreservesDictation = active && self.sharesMicrophone;
    _annotationActive = active;
    _annotationMicrophoneShared = shared;
    if (active) {
        if ([self.shortcutTarget isEqual:@"annotate"]) self.choosingShortcut = NO;
        [self stopSoundPreview];
        [self stopHistoryAudio];
    }
    [self refresh];
}

- (void)setAnnotationPhase:(NSString *)phase {
    NSString *previous = _annotationPhase;
    if (self.closed || [previous isEqual:phase]) return;
    _annotationPhase = [phase copy];
    if ([phase isEqual:@"starting"]) self.annotationCueGeneration++;
    BOOL wasRecording = [previous isEqual:@"recording"];
    BOOL recording = [phase isEqual:@"recording"];
    if (recording && !self.recordingEnvironment) self.recordingEnvironment = JardRecordingEnvironment.new;
    if (recording) [self playAnnotationCue:@"start"];

    NSString *name = nil;
    if (wasRecording && [phase isEqual:@"saving"]) name = @"stop";
    else if ([phase isEqual:@"cancelled"]) name = @"cancel";
    else if ([phase isEqual:@"error"]) name = @"error";
    dispatch_block_t cue = nil;
    if (name) {
        NSUInteger generation = ++self.annotationCueGeneration;
        __weak typeof(self) weak = self;
        cue = ^{
            JardSystemDictation *owner = weak;
            if (owner.annotationCueGeneration == generation) [owner playAnnotationCue:name];
        };
    }
    if (!self.recordingEnvironment) {
        if (cue) cue();
        return;
    }
    [self.recordingEnvironment setAnnotationRecording:recording
        mode:self.systemAudioRecordingActive ? nil : self.settings[@"audioWhileDictating"]
        percent:[self.settings[@"lowerAudioPercent"] doubleValue] completion:cue];
}

- (void)discardPendingOutput:(BOOL)includingMeetings {
    for (JardDictationJob *job in self.pendingJobs.copy) {
        if (job.meetingExcerpt && !includingMeetings) continue;
        job.cancelled = YES;
        [job discardPendingHistoryAudio];
        [self.pendingJobs removeObject:job];
        if (job.waitingToSubmit) [self discardUnsubmittedStage:job];
        if (job.pasteCompletion) job.pasteCompletion(NO);
        [job.printRequest finish:nil error:@"Dictation was cancelled."];
    }
}

- (void)pauseForUpdate:(BOOL)paused {
    self.updatePaused = paused;
    if (paused) {
        [self stopSoundPreview];
        [self stopHistoryAudio];
    }
    if (paused && self.annotationPasteReply) [self finishAnnotationPaste:@"Jard is preparing to restart."];
    [self refresh];
}

- (void)helperDisconnected {
    self.warmRequest = nil;
    self.warmRequestID = nil;
    [self setWarmInputAndNotify:nil];
    [self withdrawShortcutStartCue];
    if (self.captureID && !self.transcribing) [self fail:@"The local helper stopped. Try dictating again."];
}

- (BOOL)shortcutInputIsIdle {
    return _shortcut.phase == JardIdle && !self.keyDown && !self.suppressedKey && !self.suppressedPaste &&
        !self.suppressedMeetingPaste && !self.annotationKeyDown && !self.choosingShortcut &&
        self.ignoredRelease < 0 && self.cancelRelease < 0;
}

- (void)adoptShortcutLayout {
    if (!_shortcutLayoutPending || ![self shortcutInputIsIdle]) return;
    _shortcutLayout = _pendingShortcutLayout;
    _hasShortcutLayout = YES;
    _shortcutLayoutPending = NO;
    _shortcut.lastTap = 0;
    self.alternateModifierTap = 0;
}

- (void)stageShortcutLayout:(JardShortcutLayout)layout {
    _pendingShortcutLayout = layout;
    _shortcutLayoutPending = !_hasShortcutLayout || memcmp(&_shortcutLayout, &layout, sizeof layout) != 0;
    [self adoptShortcutLayout];
}

- (void)shortcutInputSourceChanged:(NSNotification *)notification {
    __weak typeof(self) weakSelf = self;
    dispatch_async(dispatch_get_main_queue(), ^{
        JardSystemDictation *owner = weakSelf;
        if (owner.workspace && !owner.closed) {
            [owner stageShortcutLayout:JardCurrentShortcutLayout()];
            if (owner.secureInput) [owner updateSecureHotkeys:YES];
        }
    });
}

- (NSInteger)shortcutCodeForSlot:(NSString *)slot layout:(const JardShortcutLayout *)layout {
    return JardPreferenceShortcutCode(self.settings, slot, layout);
}

- (NSInteger)shortcutCodeForSlot:(NSString *)slot {
    return [self shortcutCodeForSlot:slot layout:_hasShortcutLayout ? &_shortcutLayout : NULL];
}

- (CGKeyCode)annotationShortcutCode {
    return (CGKeyCode)[self shortcutCodeForSlot:@"annotate"];
}

- (CGEventFlags)annotationShortcutFlags { return [self.settings[@"annotateFlags"] unsignedLongLongValue]; }

- (CGKeyCode)meetingPasteShortcutCode {
    return (CGKeyCode)JardShortcutLetterCode(_hasShortcutLayout ? &_shortcutLayout : NULL, 'v', NO, kVK_ANSI_V);
}

- (BOOL)canReloadPreferences {
    return self.workspace && !self.closed && !self.updatePaused && !self.sdkRecording &&
        !self.annotationActive && !self.hasActivity && [self shortcutInputIsIdle];
}

- (void)acceptSavedPreferences:(id)saved readable:(BOOL)readable revision:(NSUInteger)revision {
    if (self.closed || revision != self.preferencesRevision) return;
    if (![self canReloadPreferences]) {
        self.nextPreferencesRead = 0;
        return;
    }
    if (!readable || (saved && ![saved isKindOfClass:NSDictionary.class])) {
        if (self.preferencesUnreadable) return;
        self.preferencesUnreadable = YES;
        self.preferencesWarning = @"Jard kept the current dictation settings. The saved preferences could not be read.";
        self.preferencesRevision++;
    } else {
        NSMutableArray<NSString *> *invalid = NSMutableArray.array;
        NSDictionary *decoded = JardDecodePreferences(saved, invalid);
        BOOL deferExpiry = [invalid containsObject:@"retention"] || [invalid containsObject:@"previousRetention"];
        NSString *warning = invalid.count ? repairedPreferencesWarning : nil;
        if (!self.preferencesUnreadable && [self.loadedSettings isEqual:decoded] &&
            deferExpiry == self.deferHistoryExpiry && (warning == self.preferencesWarning || [warning isEqual:self.preferencesWarning])) return;
        self.preferencesUnreadable = NO;
        self.preferencesWarning = warning;
        self.deferHistoryExpiry = deferExpiry;
        [self installPreferencesSnapshot:decoded];
        [self refresh];
    }
    if (self.preferencesChanged) self.preferencesChanged(self.status);
}

- (void)checkSavedPreferences {
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    if (self.preferencesReadInFlight || now < self.nextPreferencesRead || ![self canReloadPreferences]) return;
    self.nextPreferencesRead = now + 2;
    self.preferencesReadInFlight = YES;
    NSUInteger revision = self.preferencesRevision;
    __weak typeof(self) weakSelf = self;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        BOOL readable;
        id saved = JardReadSavedPreferences(&readable);
        dispatch_async(dispatch_get_main_queue(), ^{
            JardSystemDictation *owner = weakSelf;
            owner.preferencesReadInFlight = NO;
            [owner acceptSavedPreferences:saved readable:readable revision:revision];
        });
    });
}

- (void)workspaceReady {
    if (self.workspace || self.closed) return;
    self.workspace = YES;
    if (JardApprovedScriptDigest(self.settings).length) [self publishOutputScriptApproval];
    [self rememberExternalApp:NSWorkspace.sharedWorkspace.frontmostApplication];
    NSArray *events = @[NSWorkspaceWillSleepNotification, NSWorkspaceDidWakeNotification,
        NSWorkspaceScreensDidSleepNotification, NSWorkspaceScreensDidWakeNotification,
        NSWorkspaceSessionDidResignActiveNotification, NSWorkspaceSessionDidBecomeActiveNotification];
    for (NSString *name in events) {
        [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(powerChanged:) name:name object:nil];
    }
    [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(activationChanged:)
        name:NSWorkspaceDidActivateApplicationNotification object:nil];
    for (NSString *name in @[screenDidLock, screenDidUnlock]) {
        [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(powerChanged:)
            name:name object:nil suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
    }
    [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(shortcutInputSourceChanged:)
        name:(__bridge NSString *)kTISNotifySelectedKeyboardInputSourceChanged object:nil
        suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
    // The observers deliver changes. Seed the current state before anything can open the microphone.
    self.displayAsleep = CGDisplayIsAsleep(CGMainDisplayID());
    CFDictionaryRef session = CGSessionCopyCurrentDictionary();
    CFTypeRef locked = session ? CFDictionaryGetValue(session, CFSTR("CGSSessionScreenIsLocked")) : NULL;
    self.screenLocked = locked && CFGetTypeID(locked) == CFBooleanGetTypeID() && CFBooleanGetValue(locked);
    if (session) CFRelease(session);
    [self stageShortcutLayout:JardCurrentShortcutLayout()];
    [self refresh];
    self.nextPreferencesRead = NSProcessInfo.processInfo.systemUptime + 2;
    __weak typeof(self) weakSelf = self;
    self.monitor = [NSTimer timerWithTimeInterval:0.25 repeats:YES block:^(NSTimer *timer) {
        [weakSelf checkSavedPreferences];
        [weakSelf refresh];
    }];
    self.monitor.tolerance = 0.04;
    [NSRunLoop.mainRunLoop addTimer:self.monitor forMode:NSRunLoopCommonModes];
}

- (void)powerChanged:(NSNotification *)notification {
    NSString *name = notification.name;
    if ([name isEqual:NSWorkspaceWillSleepNotification]) self.systemAsleep = YES;
    if ([name isEqual:NSWorkspaceDidWakeNotification]) self.systemAsleep = NO;
    if ([name isEqual:NSWorkspaceScreensDidSleepNotification]) self.displayAsleep = YES;
    if ([name isEqual:NSWorkspaceScreensDidWakeNotification]) self.displayAsleep = NO;
    if ([name isEqual:screenDidLock]) self.screenLocked = YES;
    if ([name isEqual:screenDidUnlock]) self.screenLocked = NO;
    if ([name isEqual:NSWorkspaceSessionDidResignActiveNotification]) self.inactiveSession = YES;
    if ([name isEqual:NSWorkspaceSessionDidBecomeActiveNotification]) self.inactiveSession = NO;
    [self activationChanged:nil];
    [self refresh];
}

- (BOOL)sessionUnavailable {
    return self.systemAsleep || self.displayAsleep || self.screenLocked || self.inactiveSession;
}

- (void)activationChanged:(NSNotification *)notification {
    self.inputRevision++;
    self.lastInsertion = nil;
    if (notification) {
        NSRunningApplication *app = notification.userInfo[NSWorkspaceApplicationKey];
        if (self.choosingShortcut && [self.shortcutTarget isEqual:@"annotate"] &&
            app.processIdentifier != NSProcessInfo.processInfo.processIdentifier) self.choosingShortcut = NO;
        [self rememberExternalApp:app];
    }
}

- (void)rememberExternalApp:(NSRunningApplication *)app {
    if (app && app.processIdentifier > 0 && app.activationPolicy == NSApplicationActivationPolicyRegular &&
        app.processIdentifier != NSProcessInfo.processInfo.processIdentifier) {
        self.lastExternalApp = app;
        if (JardSupportsBrowserTabAccess(app.bundleIdentifier)) {
            if (![self.lastWebsiteBrowser.bundleIdentifier isEqual:app.bundleIdentifier]) self.browserAccessMessage = nil;
            self.lastWebsiteBrowser = app;
        }
    }
}

- (pid_t)annotationForegroundPID { return NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier; }
- (BOOL)annotationAccessibilityAvailable { return accessibilityAllowed(); }
- (BOOL)activateAnnotationApp:(NSRunningApplication *)app { return [app activateWithOptions:0]; }
- (BOOL)annotationModifiersReleased {
    return !(CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState) & shortcutModifiers);
}

- (NSRunningApplication *)annotationPasteDestinationFromPanel:(BOOL)fromPanel {
    pid_t front = [self annotationForegroundPID];
    if (front == NSProcessInfo.processInfo.processIdentifier)
        return fromPanel || self.lastExternalApp.terminated ? nil : self.lastExternalApp;
    if (!fromPanel || front <= 0) return nil;
    NSRunningApplication *app = [self foregroundOutputApplication];
    return app.processIdentifier == front && !app.terminated && app.activationPolicy == NSApplicationActivationPolicyRegular ? app : nil;
}

static NSString *annotationTerminalDirectory(AXUIElementRef element) {
    for (NSString *attribute in @[@"AXDocument", @"AXURL"]) {
        CFTypeRef raw = NULL;
        AXUIElementCopyAttributeValue(element, (__bridge CFStringRef)attribute, &raw);
        id value = CFBridgingRelease(raw);
        NSURL *url = [value isKindOfClass:NSURL.class] ? value :
            ([value isKindOfClass:NSString.class] ? [NSURL URLWithString:value] : nil);
        if (url.isFileURL && (!url.host.length || [url.host isEqual:@"localhost"])) {
            BOOL directory = NO;
            if ([NSFileManager.defaultManager fileExistsAtPath:url.path isDirectory:&directory] && directory) return url.path;
        }
    }
    return nil;
}

- (NSDictionary *)annotationOriginSnapshotAllowingPrevious:(BOOL)allowPrevious {
    NSRunningApplication *app = NSWorkspace.sharedWorkspace.frontmostApplication;
    pid_t ownPID = NSProcessInfo.processInfo.processIdentifier;
    if (app.processIdentifier == ownPID && allowPrevious) app = self.lastExternalApp;
    if (app.processIdentifier == ownPID || app.terminated || app.activationPolicy != NSApplicationActivationPolicyRegular || !app.bundleIdentifier.length ||
        !accessibilityAllowed()) return nil;
    AXUIElementRef focus = copyFocus(app.processIdentifier);
    if (focus) AXUIElementSetMessagingTimeout(focus, .10);
    if (!focus || secureField(focus)) { if (focus) CFRelease(focus); return nil; }
    CFTypeRef window = NULL;
    AXUIElementCopyAttributeValue(focus, kAXWindowAttribute, &window);
    BOOL terminal = JardIsAnnotationTerminal(app.bundleIdentifier);
    NSURL *annotationPage = terminal ? nil : browserURL(focus, NSProcessInfo.processInfo.systemUptime + .10);
    NSString *directory = terminal ? annotationTerminalDirectory(focus) : nil;
    CFRelease(focus);
    if (!window || CFGetTypeID(window) != AXUIElementGetTypeID()) {
        if (window) CFRelease(window);
        return nil;
    }
    AXUIElementSetMessagingTimeout((AXUIElementRef)window, .10);
    if (terminal && !directory) directory = annotationTerminalDirectory((AXUIElementRef)window);
    CFTypeRef position = NULL, size = NULL, title = NULL;
    AXUIElementCopyAttributeValue(window, kAXPositionAttribute, &position);
    AXUIElementCopyAttributeValue(window, kAXSizeAttribute, &size);
    AXUIElementCopyAttributeValue(window, kAXTitleAttribute, &title);
    CFRelease(window);
    CGPoint origin;
    CGSize dimensions;
    BOOL geometry = position && size && CFGetTypeID(position) == AXValueGetTypeID() && CFGetTypeID(size) == AXValueGetTypeID() &&
        AXValueGetValue((AXValueRef)position, kAXValueCGPointType, &origin) &&
        AXValueGetValue((AXValueRef)size, kAXValueCGSizeType, &dimensions);
    if (position) CFRelease(position);
    if (size) CFRelease(size);
    id caption = CFBridgingRelease(title);
    if (!geometry) return nil;
    NSMutableDictionary *identity = [@{@"app": app.bundleIdentifier, @"name": app.localizedName ?: app.bundleIdentifier} mutableCopy];
    if (annotationPage.host.length) {
        identity[@"site"] = annotationPage.host.lowercaseString;
        NSURLComponents *location = [NSURLComponents componentsWithURL:annotationPage resolvingAgainstBaseURL:NO];
        location.query = nil;
        location.fragment = nil;
        if (location.string.length) identity[@"siteUrl"] = location.string;
    }
    NSString *windowTitle = [caption isKindOfClass:NSString.class] ? caption : @"";
    if (terminal) {
        NSDictionary *context = JardAnnotationTerminalContext(app.processIdentifier, windowTitle, directory);
        if (context) identity[@"terminal"] = context;
    }
    NSRect bounds = NSMakeRect(origin.x, origin.y, dimensions.width, dimensions.height);
    return @{@"app": identity, @"pid": @(app.processIdentifier), @"bounds": [NSValue valueWithRect:bounds],
             @"title": windowTitle};
}

- (NSUInteger)annotationPreparationRevision { return self.inputRevision; }
- (BOOL)annotationRoutingDisplayAvailable {
    return !self.closed && ![self sessionUnavailable] && !self.updatePaused;
}


- (void)observeUnmatchedInput:(CGEventType)type event:(CGEventRef)event {
    if (event && JardShortcutEventTime(event, NO) < _shortcutRecoveryFence) return;
    if (type == kCGEventKeyDown &&
        [self isOwnGlobeCompanion:(CGKeyCode)CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)]) return;
    if (type == kCGEventKeyDown || type == kCGEventLeftMouseDown || type == kCGEventRightMouseDown ||
        type == kCGEventOtherMouseDown || type == kCGEventScrollWheel) [self activationChanged:nil];
}

- (void)updateWarm:(BOOL)wanted {
    BOOL keep = wanted && [self.settings[@"keepMicrophoneReady"] boolValue];
    NSDictionary *request = keep ? @{@"keep": @YES,
        @"inputDevice": self.settings[@"inputDevice"], @"avoidBluetooth": self.settings[@"avoidBluetooth"]} : @{@"keep": @NO};
    if ([request isEqual:self.warmRequest] || (!self.warmRequest && !keep)) return;
    self.warmRequest = request;
    self.warmRequestID = keep ? NSUUID.UUID.UUIDString : nil;
    [self setWarmInputAndNotify:nil];
    NSMutableDictionary *payload = [request mutableCopy];
    payload[@"request"] = self.warmRequestID;
    self.send(@"jard.warm", payload, nil);
}

- (void)setWarmInputAndNotify:(NSDictionary *)input {
    if (self.warmInput == input || [self.warmInput isEqual:input]) return;
    self.warmInput = input;
    if (self.preferencesChanged) self.preferencesChanged(self.status);
}

- (void)updateWarmInputStatus:(NSDictionary *)event {
    if (!self.warmRequestID || ![self.warmRequestID isEqual:event[@"request"]]) return;
    NSString *phase = event[@"phase"];
    NSString *microphone = [event[@"microphone"] isKindOfClass:NSString.class] ? event[@"microphone"] : @"";
    NSString *message = [event[@"message"] isKindOfClass:NSString.class] ? event[@"message"] : @"";
    if ([phase isEqual:@"fallback"] && microphone.length) {
        [self setWarmInputAndNotify:@{@"phase": phase, @"microphone": microphone}];
    } else if ([phase isEqual:@"unavailable"] && message.length) {
        [self setWarmInputAndNotify:@{@"phase": phase, @"message": message}];
    } else if ([phase isEqual:@"idle"] || [phase isEqual:@"ready"]) {
        [self setWarmInputAndNotify:nil];
    }
}

- (void)updatePhysicalKeyTap:(BOOL)wanted {
    BOOL listening = wanted && CGPreflightListenEventAccess();
    if (!listening) {
        [self.physicalModifiers stop];
        self.physicalModifiers = nil;
        JardDetachEventTap(&_physicalKeyTap);
        memset(_physicalChordTimes, 0, sizeof _physicalChordTimes);
        return;
    } else if (!self.physicalModifiers) {
        self.physicalModifiers = [JardPhysicalModifiers new];
    }
    if (JardMaintainEventTap(&_physicalKeyTap, kCGHIDEventTap, kCGEventTapOptionListenOnly,
            CGEventMaskBit(kCGEventKeyDown), dictationPhysicalKeyTap, (__bridge void *)self, @"HID chord"))
        memset(_physicalChordTimes, 0, sizeof _physicalChordTimes);
}

- (void)removeTap {
    self.modifierClaimAt = 0;
    [self updatePhysicalKeyTap:NO];
    BOOL attached = _sessionTap.port != NULL;
    JardDetachEventTap(&_sessionTap);
    if (!attached) return;
    [self resetShortcutEvidenceAt:NSProcessInfo.processInfo.systemUptime];
    [self handleAnnotationEvent:kCGEventTapDisabledByUserInput event:NULL chordFree:NO];
    self.keyDown = NO;
    self.lastModifierFlags = self.lastRemappedSides = 0;
    self.alternateModifierTap = 0;
    memset(_modifierSidePress, 0, sizeof _modifierSidePress);
    self.suppressedKey = self.suppressedPaste = self.suppressedMeetingPaste = NO;
    self.ignoredRelease = self.cancelRelease = -1;
}

- (void)installModifierFilter {
    // Keep an installed filter through listener toggles, in case a posted pair is still in flight.
    if (!JardEventTapEnabled(&_modifierFilter) && CGPreflightPostEventAccess())
        JardMaintainEventTap(&_modifierFilter, kCGAnnotatedSessionEventTap, kCGEventTapOptionDefault,
            CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp), dictationModifierTap, (__bridge void *)self, @"modifier filter");
}

- (void)keepShortcutListenerAwake:(BOOL)wanted {
    if (wanted && !self.listenerActivity) {
        self.listenerActivity = [NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiatedAllowingIdleSystemSleep
            reason:@"Jaro listens for its global shortcuts"];
    } else if (!wanted && self.listenerActivity) {
        [NSProcessInfo.processInfo endActivity:self.listenerActivity];
        self.listenerActivity = nil;
    }
}

- (BOOL)annotationChordFreeIncludingInactive:(BOOL)includeInactive {
    if (includeInactive) return !JardAnnotationChordConflicts(self.settings, _hasShortcutLayout ? &_shortcutLayout : NULL);
    CGEventFlags flags = [self annotationShortcutFlags];
    CGKeyCode code = [self annotationShortcutCode];
    if ([self canUseMeetingPaste] && code == [self meetingPasteShortcutCode] && flags == meetingPasteFlags) return NO;
    if (!self.dictationKeyEnabled) return YES;
    for (NSString *binding in @[@"shortcut", @"raw", @"paste", @"cancel"]) {
        if ([binding isEqual:@"cancel"] && _shortcut.mode != JardToggle) continue;
        if ([self shortcutCodeForSlot:binding] == code &&
            [self.settings[[binding stringByAppendingString:@"Flags"]] unsignedLongLongValue] == flags) return NO;
    }
    return YES;
}

- (BOOL)annotationChordFree { return [self annotationChordFreeIncludingInactive:NO]; }

- (BOOL)handleAnnotationEvent:(CGEventType)type event:(CGEventRef)event chordFree:(BOOL)chordFree {
    CGKeyCode shortcutCode = [self annotationShortcutCode];
    if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) self.annotationKeyDown = NO;
    if (!self.annotationEvents) return NO;
    CGKeyCode eventCode = event ? (CGKeyCode)CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) : 0;
    JardAnnotationShortcut binding = {shortcutCode, [self annotationShortcutFlags], event ? JardEventModifiers(event, eventCode) : 0};
    BOOL taken = self.annotationEvents(type, event, binding, chordFree);
    if (event && (type == kCGEventKeyDown || type == kCGEventKeyUp) &&
        CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == shortcutCode) {
        if (type == kCGEventKeyUp) self.annotationKeyDown = NO;
        else if (taken) self.annotationKeyDown = YES;
    }
    return taken;
}

- (BOOL)canUseMeetingPaste {
    return (!self.annotationActive || self.annotationMicrophoneShared) &&
        self.meetingAvailable && self.meetingAction != nil;
}

- (void)showSecurePasteFeedback {
    if (!self.captureID && !self.transcribing) {
        [self update:@"saved" message:@"Secure keyboard input is active. Return to an editable field to paste."
            hud:@"Secure input"];
    }
}

- (void)routeSecureChord:(JardSecureChord)chord edge:(JardSecureEdge)edge {
    if (self.closed) return;
    if (edge == JardSecureCancelled || !IsSecureEventInputEnabled()) {
        if (chord.action == JardSecureAnnotate)
            [self handleAnnotationEvent:kCGEventTapDisabledByUserInput event:NULL chordFree:NO];
        return;
    }
    if (chord.action != JardSecureAnnotate) {
        BOOL available = chord.action == JardSecureMeetingPaste ? [self canUseMeetingPaste] :
            self.dictationKeyEnabled && self.lastText.length;
        if (edge == JardSecurePressed && available && !self.choosingShortcut) [self showSecurePasteFeedback];
        return;
    }
    if (!self.annotationEnabled || self.choosingShortcut || chord.code != [self annotationShortcutCode] ||
        chord.flags != [self annotationShortcutFlags]) return;
    BOOL down = edge == JardSecurePressed;
    CGEventFlags current = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState);
    CGEventRef edgeEvent = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)chord.code, down);
    if (!edgeEvent) return;
    CGEventSetFlags(edgeEvent, down ? chord.flags : current & ~chord.flags);
    CGEventRef pointer = CGEventCreate(NULL);
    if (pointer) { CGEventSetLocation(edgeEvent, CGEventGetLocation(pointer)); CFRelease(pointer); }
    // Deliver only to Jard's existing reducer. These events never enter the OS event stream.
    if ([self handleAnnotationEvent:down ? kCGEventKeyDown : kCGEventKeyUp event:edgeEvent chordFree:[self annotationChordFree]] && down)
        [self activationChanged:nil];
    if (down && (current & chord.flags) != chord.flags) {
        CGEventSetFlags(edgeEvent, current);
        [self handleAnnotationEvent:kCGEventFlagsChanged event:edgeEvent chordFree:YES];
    }
    CFRelease(edgeEvent);
}

- (void)updateSecureHotkeys:(BOOL)available {
    BOOL secure = available && IsSecureEventInputEnabled();
    if (self.secureInput != secure) {
        self.secureInput = secure;
        [self activationChanged:nil];
        if (secure) {
            [self resetShortcutEvidenceAt:NSProcessInfo.processInfo.systemUptime];
            [self abandonTailPress];
            [self withdrawShortcutStartCue];
            [self.captureStartCue withdraw];
            self.modifierClaimAt = 0;
            [self handleAnnotationEvent:kCGEventTapDisabledByUserInput event:NULL chordFree:NO];
            self.keyDown = self.suppressedKey = self.suppressedPaste = self.suppressedMeetingPaste = NO;
            self.lastModifierFlags = self.lastRemappedSides = 0;
            self.alternateModifierTap = 0;
            memset(_modifierSidePress, 0, sizeof _modifierSidePress);
            self.ignoredRelease = self.cancelRelease = -1;
            _shortcut.lastTap = 0;
            if (_shortcut.phase == JardArmed) jardShortcutCancel(&_shortcut);
            if (self.captureID) {
                self.secureTakeInterrupted = YES;
                if (!self.stopping) {
                    _shortcut.phase = JardFinishing;
                    [self stopCapture];
                }
            }
        }
    }
    if (!secure || self.choosingShortcut) {
        [self.secureHotkeys update:NULL count:0];
        return;
    }
    JardSecureChord chords[3];
    NSUInteger count = 0;
    if (self.annotationEnabled && (self.annotationActive || [self annotationChordFree]))
        chords[count++] = (JardSecureChord){JardSecureAnnotate, [self annotationShortcutCode], [self annotationShortcutFlags]};
    if ([self canUseMeetingPaste])
        chords[count++] = (JardSecureChord){JardSecureMeetingPaste, [self meetingPasteShortcutCode], meetingPasteFlags};
    NSInteger pasteCode = [self shortcutCodeForSlot:@"paste"];
    if (self.dictationKeyEnabled && self.lastText.length && pasteCode >= 0 && pasteCode <= 127 && !modifierFlag((CGKeyCode)pasteCode))
        chords[count++] = (JardSecureChord){JardSecurePasteLast, pasteCode, [self.settings[@"pasteFlags"] unsignedLongLongValue]};
    if (!self.secureHotkeys && count) {
        __weak typeof(self) weakSelf = self;
        self.secureHotkeys = [[JardSecureHotkeys alloc] initWithHandler:^(JardSecureChord chord, JardSecureEdge edge) {
            [weakSelf routeSecureChord:chord edge:edge];
        }];
    }
    [self.secureHotkeys update:chords count:count];
}

- (void)noteAccessibilityRequestAt:(NSTimeInterval)now {
    self.accessibilityAskedAt = now;
    self.accessibilityReturnedAt = 0;
    self.accessibilityReturnDeadline = 0;
    self.accessibilityVisitedSettings = NO;
    self.accessibilityStale = NO;
}

- (BOOL)observeAccessibilityAt:(NSTimeInterval)now trusted:(BOOL)trusted
                  settingsOpen:(BOOL)settingsOpen settingsFront:(BOOL)settingsFront active:(BOOL)active {
    if (self.closed || self.updatePaused || [self sessionUnavailable] ||
        !JardOnboardingAccessRecoveryStep(self.settings[@"onboarding"])) {
        [self noteAccessibilityRequestAt:0];
        return NO;
    }
    if (trusted) {
        if (self.accessibilityAskedAt) {
            [self noteAccessibilityRequestAt:0];
            // Give an authentication sheet time to return focus to System Settings.
            self.accessibilityReturnDeadline = now + JardPermissionReturnGrace;
        }
        return JardTakePermissionReturn(&_accessibilityReturnDeadline, now, settingsOpen, settingsFront, active);
    }
    self.accessibilityReturnDeadline = 0;
    if (!self.accessibilityAskedAt || self.accessibilityStale) return NO;
    if (settingsOpen && (settingsFront || !active)) {
        self.accessibilityVisitedSettings = YES;
        self.accessibilityReturnedAt = 0;
    } else if (self.accessibilityVisitedSettings) {
        if (!self.accessibilityReturnedAt) self.accessibilityReturnedAt = now;
        self.accessibilityStale = now - self.accessibilityReturnedAt >= 2;
    } else if (active) {
        self.accessibilityStale = now - self.accessibilityAskedAt >= 6;
    }
    return NO;
}

- (void)maintainEventTapsForDictation:(BOOL)dictation {
    [self keepShortcutListenerAwake:YES];
    BOOL wasAttached = _sessionTap.port != NULL;
    CGEventMask mask = CGEventMaskBit(kCGEventFlagsChanged) | CGEventMaskBit(kCGEventKeyDown) | CGEventMaskBit(kCGEventKeyUp) |
        CGEventMaskBit(kCGEventOtherMouseDown) | CGEventMaskBit(kCGEventOtherMouseUp) | CGEventMaskBit(kCGEventOtherMouseDragged) |
        CGEventMaskBit(kCGEventRightMouseDown) | CGEventMaskBit(kCGEventRightMouseUp) | CGEventMaskBit(kCGEventRightMouseDragged) |
        CGEventMaskBit(kCGEventLeftMouseDown) | CGEventMaskBit(kCGEventScrollWheel) |
        CGEventMaskBit(kCGEventMouseMoved) | CGEventMaskBit(kCGEventLeftMouseDragged);
    BOOL interrupted = JardMaintainEventTap(&_sessionTap, kCGSessionEventTap, kCGEventTapOptionDefault,
        mask, dictationTap, (__bridge void *)self, @"session keyboard");
    if (interrupted) [self interruptShortcutInput:kCGEventTapDisabledByUserInput];
    BOOL ready = JardEventTapEnabled(&_sessionTap);
    if (ready && (!wasAttached || interrupted)) {
        self.lastModifierFlags = CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState);
        self.lastRemappedSides = 0;
    }
    self.listenerStartFailed = dictation && !ready;
    if (dictation && !self.secureInput) {
        [self installModifierFilter];
        [self updatePhysicalKeyTap:YES];
    }
    if (!self.secureInput && ready) [self sampleShortcutRelease];
}

- (void)refresh {
    [self reportActivity];
    [self adoptShortcutLayout];
    BOOL trusted = accessibilityAllowed();
    if (trusted || !JardOnboardingAccessRecoveryStep(self.settings[@"onboarding"])) {
        [self.guide orderOut:nil];
        self.guide = nil;
    }
    if (self.accessibilityAskedAt || self.accessibilityReturnDeadline) {
        BOOL settingsOpen = [NSRunningApplication runningApplicationsWithBundleIdentifier:JardSystemSettingsBundle].count > 0;
        BOOL settingsFront = [NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier isEqual:JardSystemSettingsBundle];
        if ([self observeAccessibilityAt:NSProcessInfo.processInfo.systemUptime trusted:trusted
            settingsOpen:settingsOpen settingsFront:settingsFront active:NSApp.active] && self.accessibilityGrantedInSettings)
            self.accessibilityGrantedInSettings();
    }
    BOOL available = self.workspace && trusted && !self.closed && ![self sessionUnavailable] && !self.updatePaused;
    BOOL dictation = available && !self.annotationPasteReply && !self.sdkRecording &&
        [self.settings[@"enabled"] boolValue] && [microphoneStatus() isEqual:@"granted"];
    if (self.dictationKeyEnabled != dictation) [self resetShortcutEvidenceAt:NSProcessInfo.processInfo.systemUptime];
    if (self.dictationKeyEnabled && !dictation) {
        [self abandonTailPress];
        [self withdrawShortcutStartCue];
        [self.captureStartCue withdraw];
        self.modifierClaimAt = 0;
        self.keyDown = NO;
        [self discardPendingOutput:NO];
        if (!self.activeMeetingPaste) [self act:jardShortcutCancel(&_shortcut)];
    }
    self.dictationKeyEnabled = dictation;
    [self updateSecureHotkeys:available];
    [self updateWarm:dictation && !self.secureInput];
    if (!dictation || self.secureInput) [self updatePhysicalKeyTap:NO];
    BOOL annotationEdit = self.annotationEvents && [self.shortcutTarget isEqual:@"annotate"] &&
        (self.choosingShortcut || self.ignoredRelease >= 0);
    BOOL wanted = available && (dictation || self.annotationEnabled || annotationEdit || self.meetingAvailable ||
        self.suppressedMeetingPaste || self.activeMeetingPaste);
    if (wanted) {
        [self maintainEventTapsForDictation:dictation];
    } else {
        BOOL attached = _sessionTap.port != NULL;
        [self removeTap];
        [self keepShortcutListenerAwake:NO];
        self.listenerStartFailed = NO;
        if ([self.shortcutTarget isEqual:@"annotate"]) self.choosingShortcut = NO;
        if (attached) {
            [self discardPendingOutput:YES];
            [self act:jardShortcutCancel(&_shortcut)];
        }
    }
}

- (NSDictionary *)status {
    NSMutableDictionary *visible = [self.settings mutableCopy];
    visible[@"microphone"] = microphoneStatus();
    visible[@"accessibility"] = @(accessibilityAllowed());
    visible[@"accessibilityRequested"] = @(self.accessibilityAskedAt > 0);
    visible[@"accessibilityStale"] = @(self.accessibilityStale);
    visible[@"listenerNeedsRestart"] = @(self.listenerStartFailed);
    visible[@"inputMonitoring"] = @(CGPreflightListenEventAccess());
    visible[@"listener"] = @(self.dictationKeyEnabled && JardEventTapEnabled(&_sessionTap));
    visible[@"annotationEnabled"] = @(self.annotationEnabled);
    visible[@"annotationListener"] = @(self.annotationEnabled && JardEventTapEnabled(&_sessionTap));
    visible[@"annotationRecorderAvailable"] = @(self.annotationEvents != nil && accessibilityAllowed() &&
        (!self.annotationEnabled || JardEventTapEnabled(&_sessionTap)));
    visible[@"annotationShortcutConflict"] = @(self.annotationEvents != nil &&
        ![self annotationChordFreeIncludingInactive:!self.annotationEnabled]);
    visible[@"phase"] = self.phase;
    visible[@"pendingJobs"] = @(self.pendingJobs.count);
    visible[@"soundPreview"] = @(self.previewSounds != nil);
    visible[@"soundPreviewRevision"] = @(self.soundPreviewGeneration);
    visible[@"soundPreviewAnimation"] = self.soundPreviewAnimation;
    visible[@"message"] = self.message ?: @"";
    visible[@"lastFailure"] = self.lastFailure ?: @"";
    visible[@"warmInput"] = self.warmInput;
    visible[@"preferencesWarning"] = self.preferencesWarning ?: @"";
    visible[@"preferencesRevision"] = @(self.preferencesRevision);
    visible[@"mediaAccessMessage"] = self.mediaAccessMessage ?: @"";
    visible[@"browserAccessMessage"] = self.browserAccessMessage ?: @"";
    if (!self.lastWebsiteBrowser.terminated) visible[@"browserAccessTarget"] = self.lastWebsiteBrowser.localizedName;
    BOOL liveVisible = self.practice || [self.settings[@"livePreview"] boolValue];
    visible[@"preview"] = liveVisible ? (self.preview ?: @"") : @"";
    [visible removeObjectForKey:@"onboardingEnabledBefore"];
    visible[@"outputScriptName"] = [self.settings[@"outputScript"] lastPathComponent] ?: @"";
    [visible removeObjectForKey:@"outputScript"];
    [visible removeObjectForKey:@"outputScriptApproval"];
    [visible removeObjectForKey:@"vocabularySoundsLike"];
    visible[@"vocabularyEntries"] = JardVocabularyEntries(self.settings);
    visible[@"choosingShortcut"] = @(self.choosingShortcut);
    visible[@"shortcutTarget"] = self.choosingShortcut ? self.shortcutTarget : @"";
    visible[@"shortcutSide"] = JardShortcutSide([self.settings[@"shortcutCode"] unsignedShortValue],
        [self.settings[@"shortcutEitherSide"] boolValue]);
    visible[@"hasLast"] = @(self.lastText.length > 0);
    NSDictionary *latest = self.history.firstObject;
    visible[@"historyRevision"] = latest[@"totalMs"]
        ? [NSString stringWithFormat:@"%@:%@", latest[@"id"], latest[@"totalMs"]] : latest[@"id"] ?: @"";
    if (latest) visible[@"historyRevision"] = [NSString stringWithFormat:@"%@:%lu", visible[@"historyRevision"], (unsigned long)self.history.count];
    if (self.historyAudioRevision) visible[@"historyRevision"] = [NSString stringWithFormat:@"%@:audio:%lu",
        visible[@"historyRevision"], (unsigned long)self.historyAudioRevision];
    visible[@"playingHistoryID"] = self.playingHistoryID;
    return visible;
}

- (void)permissionGuide {
    if (accessibilityAllowed()) return;
    [self noteAccessibilityRequestAt:NSProcessInfo.processInfo.systemUptime];
    NSDictionary *options = @{(__bridge NSString *)kAXTrustedCheckOptionPrompt: @YES};
    AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)options);
    NSURL *settings = [NSURL URLWithString:@"x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"];
    [NSWorkspace.sharedWorkspace openURL:settings];
    if (!self.guide) {
        JardDictationPanel *panel = [[JardDictationPanel alloc] initWithContentRect:NSMakeRect(0, 0, 350, 156)
            styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskNonactivatingPanel
            backing:NSBackingStoreBuffered defer:NO];
        panel.releasedWhenClosed = NO;
        panel.title = @"Allow Jard in Accessibility";
        panel.level = NSFloatingWindowLevel;
        panel.hidesOnDeactivate = NO;
        panel.collectionBehavior = NSWindowCollectionBehaviorMoveToActiveSpace | NSWindowCollectionBehaviorFullScreenAuxiliary;
        JardPermissionIcon *icon = [[JardPermissionIcon alloc] initWithFrame:NSMakeRect(18, 91, 44, 44)];
        icon.image = NSApp.applicationIconImage;
        icon.toolTip = @"Drag Jard into the Accessibility list";
        [panel.contentView addSubview:icon];
        NSTextField *instructions = [NSTextField wrappingLabelWithString:
            @"Turn on Jard in the list. If it’s missing, drag the icon into the list or click + and choose Jard."];
        instructions.font = [NSFont systemFontOfSize:13];
        instructions.frame = NSMakeRect(74, 71, 251, 65);
        [panel.contentView addSubview:instructions];
        NSButton *reveal = [NSButton buttonWithTitle:@"Show Jard in Finder" target:self action:@selector(revealInFinder:)];
        reveal.frame = NSMakeRect(17, 20, 163, 32);
        [panel.contentView addSubview:reveal];
        NSRect screen = activeScreen().visibleFrame;
        [panel setFrameTopLeftPoint:NSMakePoint(NSMaxX(screen) - 374, NSMinY(screen) + 192)];
        self.guide = panel;
    }
    [self.guide orderFrontRegardless];
}

- (void)runAccessibilityResetForBundle:(NSString *)bundle completion:(void (^)(NSString *))completion {
    NSTask *task = NSTask.new;
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/tccutil"];
    task.arguments = @[@"reset", @"Accessibility", bundle];
    task.standardOutput = NSFileHandle.fileHandleWithNullDevice;
    task.standardError = NSFileHandle.fileHandleWithNullDevice;
    task.terminationHandler = ^(NSTask *process) {
        NSString *failure = process.terminationStatus == 0 ? nil :
            @"Could not reset Accessibility. In System Settings, remove Jard and add this copy again.";
        dispatch_async(dispatch_get_main_queue(), ^{ completion(failure); });
    };
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) completion(error.localizedDescription);
}

- (void)repairAccessibility:(JardDictationReply)reply {
    NSString *bundle = NSBundle.mainBundle.bundleIdentifier;
    if (!self.accessibilityStale || self.accessibilityRepairing || accessibilityAllowed() ||
        !JardOnboardingAccessRecoveryStep(self.settings[@"onboarding"]) || !bundle.length) {
        reply(nil, @"Accessibility repair is available after returning from System Settings with access still off.");
        return;
    }
    self.accessibilityRepairing = YES;
    [self runAccessibilityResetForBundle:bundle completion:^(NSString *error) {
        self.accessibilityRepairing = NO;
        if (error) { reply(nil, error); return; }
        [self noteAccessibilityRequestAt:0];
        if (!self.closed && JardOnboardingAccessRecoveryStep(self.settings[@"onboarding"])) [self permissionGuide];
        [self refresh];
        reply([self status], nil);
    }];
}

- (void)requestMediaAccess:(JardDictationReply)reply {
    if (![self.settings[@"audioWhileDictating"] isEqual:@"pause"]) {
        reply(nil, @"Choose Pause media before allowing playback control.");
        return;
    }
    if (self.mediaAccessPending || self.browserAccessPending || self.hasActivity) {
        reply(nil, @"Finish the current dictation or permission request first.");
        return;
    }
    self.mediaAccessPending = YES;
    [JardDictationMedia.new requestAccess:^(NSArray<NSString *> *allowed, NSArray<NSString *> *unavailable) {
        self.mediaAccessPending = NO;
        NSMutableArray<NSString *> *messages = NSMutableArray.array;
        if (allowed.count) [messages addObject:[NSString stringWithFormat:@"Allowed for %@.", [allowed componentsJoinedByString:@", "]]];
        if (unavailable.count) [messages addObject:[NSString stringWithFormat:@"Check Automation in System Settings for %@.",
            [unavailable componentsJoinedByString:@", "]]];
        self.mediaAccessMessage = messages.count ? [messages componentsJoinedByString:@" "] :
            @"Open Music, Spotify, or VLC, then allow playback control.";
        reply([self status], nil);
    }];
}

- (void)requestBrowserAccess:(JardDictationReply)reply {
    NSRunningApplication *browser = self.lastWebsiteBrowser;
    if (!browser || browser.terminated) {
        reply(nil, @"Switch to a supported browser, then return to Dictation settings.");
        return;
    }
    if (self.browserAccessPending || self.mediaAccessPending || self.hasActivity) {
        reply(nil, @"Finish the current dictation or permission request first.");
        return;
    }
    self.browserAccessPending = YES;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        BOOL allowed = JardRequestBrowserTabAccess(browser);
        dispatch_async(dispatch_get_main_queue(), ^{
            self.browserAccessPending = NO;
            NSString *name = browser.localizedName ?: @"the browser";
            self.browserAccessMessage = [NSString stringWithFormat:allowed ? @"Allowed for %@." : @"Check Automation in System Settings for %@.", name];
            reply([self status], nil);
        });
    });
}

- (void)revealInFinder:(id)sender {
    [NSWorkspace.sharedWorkspace activateFileViewerSelectingURLs:@[NSBundle.mainBundle.bundleURL]];
}

static void reapplyHUDWindowBehavior(NSPanel *panel, BOOL reorder) {
    if (reorder) {
        [panel orderOut:nil];
        // Pull a panel stranded on another full-screen Space onto the active Space without
        // clearing FullScreenAuxiliary while shown, then restore CanJoinAllSpaces.
        panel.collectionBehavior = (dictationHUDBehavior & ~NSWindowCollectionBehaviorCanJoinAllSpaces) |
            NSWindowCollectionBehaviorMoveToActiveSpace;
        [panel orderFrontRegardless];
        panel.collectionBehavior = dictationHUDBehavior;
    } else {
        panel.collectionBehavior = dictationHUDBehavior;
    }
}

- (void)rehomeHUDAt:(NSTimeInterval)now {
    reapplyHUDWindowBehavior(self.hud, YES);
    if (self.liveTextPanel.visible && !self.liveTextPanel.onActiveSpace) {
        reapplyHUDWindowBehavior(self.liveTextPanel, YES);
    }
    NSTimeInterval wait = fmin(3, 0.15 * (1u << self.hudRehomeAttempts));
    self.hudRehomeDeadline = now + wait;
    self.hudRehomeAttempts++;
}

- (void)checkHUDSpaceAt:(NSTimeInterval)now {
    if (!self.hud.visible) return;
    if (self.liveTextPanel.visible && !self.liveTextPanel.onActiveSpace && self.hud.onActiveSpace) {
        reapplyHUDWindowBehavior(self.liveTextPanel, YES);
    }
    if (self.hud.onActiveSpace) {
        self.hudRehomeAttempts = 0;
        self.hudRehomeDeadline = 0;
    } else if (now >= self.hudRehomeDeadline) {
        if (self.hudRehomeAttempts < hudRehomeLimit) {
            [self rehomeHUDAt:now];
        } else if (self.hudRehomeDeadline) {
            NSLog(@"Jaro could not move its dictation indicator to the active Space after %lu attempts.",
                  (unsigned long)self.hudRehomeAttempts);
            self.hudRehomeDeadline = 0;
        }
    }
}

- (void)applicationActivationPolicyChanged {
    if (!self.hud) return;
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.closed) return;
        self.hudRehomeAttempts = 0;
        self.hudRehomeDeadline = 0;
        self.nextHUDPlacementAt = 0;
        if (self.hud.visible) [self rehomeHUDAt:NSProcessInfo.processInfo.systemUptime];
        else reapplyHUDWindowBehavior(self.hud, NO);
    });
}

- (NSPoint)hudEdgeOrigin:(NSString *)placement {
    NSScreen *screen = activeScreen();
    if (!screen) return self.hud.frame.origin;
    BOOL bottom = [placement isEqual:@"bottom"];
    NSPoint origin = JardDictationHUDOrigin(screen.frame, screen.visibleFrame, screen.safeAreaInsets.top,
                                            self.hud.frame.size, bottom);
    NSRect desired = NSMakeRect(origin.x, origin.y, NSWidth(self.hud.frame), NSHeight(self.hud.frame));
    JardOverlayEdge edge = bottom ? JardOverlayEdgeBottom : JardOverlayEdgeTop;
    NSRect resolved = [JardOverlayDock.shared resolvedFrameForSurface:JardOverlaySurfaceDictationHUD
                                                              desired:desired
                                                                 edge:edge
                                                            displayID:JardDisplayIDForScreen(screen)
                                                              visible:screen.visibleFrame];
    return resolved.origin;
}

- (void)repositionHUDForOverlayDock {
    if (!self.hud.visible) return;
    NSString *placement = self.settings[@"placement"];
    if ([placement isEqual:@"cursor"] || [placement isEqual:@"off"]) return;
    NSPoint origin = [self hudEdgeOrigin:placement];
    if (!NSEqualPoints(self.hud.frame.origin, origin)) {
        [self.hud setFrameOrigin:origin];
        [self positionLiveTextPlate];
    }
    NSScreen *screen = self.hud.screen ?: activeScreen();
    if (screen) {
        JardOverlayEdge edge = [placement isEqual:@"bottom"] ? JardOverlayEdgeBottom : JardOverlayEdgeTop;
        [JardOverlayDock.shared claimSurface:JardOverlaySurfaceDictationHUD frame:self.hud.frame
                                        edge:edge displayID:JardDisplayIDForScreen(screen)];
    }
}

- (void)trackHUDAt:(NSTimeInterval)now {
    [self checkHUDSpaceAt:now];
    if (!self.hud.visible || now < self.nextHUDPlacementAt) return;
    self.nextHUDPlacementAt = now + 0.1;
    [self repositionHUDForOverlayDock];
}

- (void)showHUD:(NSString *)label stage:(JardIndicatorStage)stage {
    NSUInteger generation = ++self.hudGeneration;
    NSString *placement = self.settings[@"placement"];
    if ([placement isEqual:@"off"]) return;
    if (!self.hud) {
        JardDictationPanel *panel = [[JardDictationPanel alloc] initWithContentRect:NSMakeRect(0, 0, 142, 50)
             styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
        panel.opaque = NO;
        panel.backgroundColor = NSColor.clearColor;
        panel.hasShadow = NO;
        panel.ignoresMouseEvents = YES;
        panel.hidesOnDeactivate = NO;
        panel.level = NSStatusWindowLevel;
        panel.collectionBehavior = dictationHUDBehavior;
        self.meter = [[JardDictationMeter alloc] initWithFrame:panel.contentView.bounds];
        panel.contentView = self.meter;
        self.hud = panel;
    }
    if (stage == JardIndicatorRecording) label = [self.phase isEqual:@"starting"] ? @"" : @"Listening…";
    [self.meter setAccessibilityHelp:JardLocalized(@"Dictation")];
    [self.meter setStage:stage label:JardLocalized(label)];
    if (!self.hud.visible) {
        NSPoint point = [self hudEdgeOrigin:placement];
        if ([placement isEqual:@"cursor"] && self.focusedElement && self.selectedRange) {
            NSRange range = self.selectedRange.rangeValue;
            CFRange caret = CFRangeMake(range.location, 0);
            AXValueRef parameter = AXValueCreate(kAXValueCFRangeType, &caret);
            CFTypeRef result = NULL;
            if (parameter) {
                AXUIElementCopyParameterizedAttributeValue(self.focusedElement, kAXBoundsForRangeParameterizedAttribute, parameter, &result);
                CFRelease(parameter);
            }
            CGRect bounds;
            if (result && CFGetTypeID(result) == AXValueGetTypeID() &&
                AXValueGetValue((AXValueRef)result, kAXValueCGRectType, &bounds)) {
                double top = NSMaxY(NSScreen.screens.firstObject.frame);
                NSPoint caretPoint = NSMakePoint(CGRectGetMinX(bounds), top - CGRectGetMaxY(bounds));
                for (NSScreen *display in NSScreen.screens) {
                    if (!NSPointInRect(caretPoint, display.frame)) continue;
                    NSRect visible = display.visibleFrame;
                    point.x = fmax(NSMinX(visible) + 5, fmin(caretPoint.x, NSMaxX(visible) - NSWidth(self.hud.frame) - 5));
                    point.y = caretPoint.y - NSHeight(self.hud.frame) - 8;
                    if (point.y < NSMinY(visible) + 5) point.y = fmin(NSMaxY(visible) - NSHeight(self.hud.frame) - 5,
                        caretPoint.y + CGRectGetHeight(bounds) + 8);
                    break;
                }
            }
            if (result) CFRelease(result);
        }
        [self.hud setFrameOrigin:point];
        reapplyHUDWindowBehavior(self.hud, NO);
        [self.hud orderFrontRegardless];
        __weak typeof(self) weakDock = self;
        [JardOverlayDock.shared setObserver:^{ [weakDock repositionHUDForOverlayDock]; } forSurface:JardOverlaySurfaceDictationHUD];
        if (![placement isEqual:@"cursor"]) {
            NSScreen *dockScreen = self.hud.screen ?: activeScreen();
            if (dockScreen) {
                JardOverlayEdge edge = [placement isEqual:@"bottom"] ? JardOverlayEdgeBottom : JardOverlayEdgeTop;
                [JardOverlayDock.shared claimSurface:JardOverlaySurfaceDictationHUD frame:self.hud.frame
                                                edge:edge displayID:JardDisplayIDForScreen(dockScreen)];
            }
        }
    }
    if (self.hud.visible) [self.meter announceStatus];
    if (!self.animation) {
        __weak typeof(self) weakSelf = self;
        self.animation = [NSTimer timerWithTimeInterval:1.0 / 60 repeats:YES block:^(NSTimer *timer) {
            JardSystemDictation *owner = weakSelf;
            NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
            [owner trackHUDAt:now];
            [owner.meter advanceAt:now rms:owner.inputRMS];
        }];
        [NSRunLoop.mainRunLoop addTimer:self.animation forMode:NSRunLoopCommonModes];
    }
    if (stage == JardIndicatorDone || stage == JardIndicatorFailed) {
        double displayTime = stage == JardIndicatorFailed ? 0.60 : 0.10;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(displayTime * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            if (generation != self.hudGeneration) return;
            [self.meter beginDismissal];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.38 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                if (generation == self.hudGeneration) [self hideHUD];
            });
        });
    }
}

- (void)hideLiveTextPlate {
    self.previewStable = nil;
    self.previewTentative = nil;
    [self.liveTextView setStable:@"" tentative:@""];
    [self.liveTextPanel orderOut:nil];
}

- (void)positionLiveTextPlate {
    if (!self.liveTextPanel || !self.hud || !self.hud.visible) return;
    NSRect hudFrame = self.hud.frame;
    NSSize plateSize = self.liveTextPanel.frame.size;
    NSScreen *screen = self.hud.screen ?: activeScreen();
    NSRect visible = screen ? screen.visibleFrame : NSMakeRect(0, 0, 1440, 900);
    NSString *placement = self.settings[@"placement"];
    BOOL placeAbove = [placement isEqual:@"bottom"] || (NSMinY(hudFrame) - plateSize.height - 6.0 < NSMinY(visible));
    CGFloat x = round(NSMidX(hudFrame) - plateSize.width * 0.5);
    x = fmax(NSMinX(visible) + 8.0, fmin(x, NSMaxX(visible) - plateSize.width - 8.0));
    CGFloat y = placeAbove ? (NSMaxY(hudFrame) + 4.0) : (NSMinY(hudFrame) - plateSize.height - 4.0);
    y = fmax(NSMinY(visible) + 6.0, fmin(y, NSMaxY(visible) - plateSize.height - 6.0));
    [self.liveTextPanel setFrameOrigin:NSMakePoint(x, y)];
}

- (void)updateLiveTextPlate {
    BOOL enabled = [self.capturePreferences[@"livePreview"] boolValue] || [self.settings[@"livePreview"] boolValue];
    if (!enabled || self.practice || !self.hud || !self.hud.visible || self.cancelled || self.stopping ||
        (![self.phase isEqual:@"listening"] && ![self.phase isEqual:@"starting"])) {
        [self hideLiveTextPlate];
        return;
    }
    NSString *stable = self.previewStable ?: @"";
    NSString *tentative = self.previewTentative ?: @"";
    if (!stable.length && !tentative.length) {
        [self.liveTextPanel orderOut:nil];
        return;
    }
    if (!self.liveTextPanel) {
        JardDictationPanel *panel = [[JardDictationPanel alloc] initWithContentRect:NSMakeRect(0, 0, 560, 44)
            styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
        panel.opaque = NO;
        panel.backgroundColor = NSColor.clearColor;
        panel.hasShadow = NO;
        panel.ignoresMouseEvents = YES;
        panel.hidesOnDeactivate = NO;
        panel.level = NSStatusWindowLevel;
        panel.collectionBehavior = dictationHUDBehavior;
        self.liveTextView = [[JardLiveTextView alloc] initWithFrame:panel.contentView.bounds];
        panel.contentView = self.liveTextView;
        self.liveTextPanel = panel;
    }
    [self.liveTextView setStable:stable tentative:tentative];
    [self positionLiveTextPlate];
    if (!self.liveTextPanel.visible) {
        reapplyHUDWindowBehavior(self.liveTextPanel, NO);
        [self.liveTextPanel orderFrontRegardless];
    }
}

- (void)hideHUD {
    [self.animation invalidate];
    self.animation = nil;
    self.hudRehomeAttempts = 0;
    self.hudRehomeDeadline = self.nextHUDPlacementAt = 0;
    self.inputRMS = 0;
    [self hideLiveTextPlate];
    [self.hud orderOut:nil];
    [JardOverlayDock.shared releaseSurface:JardOverlaySurfaceDictationHUD];
    [self.meter dismiss];
}

- (void)update:(NSString *)phase message:(NSString *)message hud:(NSString *)label {
    BOOL changed = ![self.phase isEqual:phase];
    self.phase = phase;
    self.message = message;
    [self reportActivity];
    if (changed && self.phaseChanged) self.phaseChanged(phase);
    if (!label) return;
    BOOL failed = [phase isEqual:@"error"] || [phase isEqual:@"empty"];
    BOOL cancelled = [phase isEqual:@"cancelled"];
    if (failed || cancelled) {
        NSString *cue = failed ? @"error" : @"cancel";
        if (self.captureID && self.recordingEnvironment) [self finishRecordingEnvironmentWithSound:cue];
        else [self playSound:cue];
    }
    if (cancelled) { [self hideHUD]; return; }
    JardIndicatorStage stage = failed ? JardIndicatorFailed : [phase isEqual:@"processing"] ? JardIndicatorProcessing :
        [@[@"starting", @"listening"] containsObject:phase] ? JardIndicatorRecording : JardIndicatorDone;
    if (stage != JardIndicatorRecording) [self hideLiveTextPlate];
    [self showHUD:label stage:stage];
}

- (float)soundLevel {
    return [self.settings[@"sound"] boolValue] ? [self.settings[@"soundVolume"] floatValue] / 100 : 0;
}

- (NSURL *)soundURL:(NSString *)cue style:(NSString *)style {
    NSDictionary *sounds = self.soundSources[style ?: @"classic"];
    if ([cue isEqual:@"annotationSent"]) return sounds[@"done"] ?:
        [NSBundle.mainBundle URLForResource:cue withExtension:@"mp3" subdirectory:@"dictation-sounds"];
    if ([cue isEqual:@"annotationUndo"]) return
        [NSBundle.mainBundle URLForResource:cue withExtension:@"mp3" subdirectory:@"dictation-sounds"];
    return sounds[cue];
}

- (NSSound *)soundForCue:(NSString *)name style:(NSString *)style {
    NSURL *clip = [self soundURL:name style:style];
    if (!clip) return nil;
    if (!self.cues) self.cues = NSMutableDictionary.dictionary;
    NSSound *sound = self.cues[clip.path];
    if (!sound) {
        sound = [[NSSound alloc] initWithContentsOfURL:clip byReference:NO];
        if (sound) self.cues[clip.path] = sound;
    }
    return sound;
}

- (void)playSound:(NSString *)name {
    float volume = [self soundLevel];
    if (volume <= 0) return;
    NSSound *sound = [self soundForCue:name style:self.settings[@"soundSet"]];
    if (sound.playing) {
        sound = [sound copy];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((sound.duration + .1) * NSEC_PER_SEC)),
            dispatch_get_main_queue(), ^{ (void)sound; });
    }
    sound.volume = volume;
    [sound play];
}

- (void)playAnnotationCue:(NSString *)name {
    if (!self.closed) [self playSound:name];
}
- (AVAudioPlayer *)previewPlayerForCue:(NSString *)cue style:(NSString *)style {
    NSURL *url = [self soundURL:cue style:style];
    return url ? [[AVAudioPlayer alloc] initWithContentsOfURL:url error:NULL] : nil;
}

- (void)stopSoundPreview {
    self.soundPreviewGeneration++;
    BOOL wasPreviewing = self.previewSounds != nil;
    for (AVAudioPlayer *clip in self.previewSounds) {
        if (!clip.playing) continue;
        NSTimeInterval fade = .020;
        [clip setVolume:0 fadeDuration:fade];
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(fade * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [clip stop];
        });
    }
    self.previewSounds = nil;
    self.soundPreviewAnimation = nil;
    if (wasPreviewing && !self.closed && self.preferencesChanged) self.preferencesChanged(self.status);
}

- (BOOL)previewSound:(NSString *)cue style:(NSString *)style {
    if (self.closed || self.updatePaused || self.hasActivity || self.sdkRecording || self.annotationActive) return NO;
    NSDictionary *schedule = JardSoundPreviewSchedule();
    NSDictionary *preview = nil;
    for (NSDictionary *candidate in schedule[@"previews"]) {
        if ([candidate[@"cue"] isEqual:cue]) { preview = candidate; break; }
    }
    double fps = [schedule[@"fps"] doubleValue];
    if (!preview || fps <= 0) return NO;
    [self stopSoundPreview];
    float volume = [self soundLevel];
    if (volume <= 0) volume = .5f;
    NSUInteger generation = self.soundPreviewGeneration;
    self.previewSounds = NSMutableArray.array;
    self.soundPreviewAnimation = @{ @"id": @(generation), @"cue": cue, @"style": style,
        @"startedAt": @(NSDate.date.timeIntervalSince1970 * 1000)};
    dispatch_time_t start = dispatch_time(DISPATCH_TIME_NOW, 0);
    __weak typeof(self) weakSelf = self;
    for (NSDictionary *beat in preview[@"beats"]) {
        NSInteger frame = [beat[@"frame"] integerValue];
        NSString *name = beat[@"cue"];
        if (frame < 0 || [name isEqual:@"leave"]) continue;
        int64_t delay = (int64_t)(frame / fps * NSEC_PER_SEC);
        dispatch_after(dispatch_time(start, delay), dispatch_get_main_queue(), ^{
            JardSystemDictation *owner = weakSelf;
            if (owner.soundPreviewGeneration != generation || !owner.previewSounds || owner.closed) return;
            AVAudioPlayer *clip = [owner previewPlayerForCue:name style:style];
            if (!clip) return;
            clip.volume = volume;
            [owner.previewSounds addObject:clip];
            [clip play];
        });
    }
    int64_t duration = (int64_t)([preview[@"frames"] doubleValue] / fps * NSEC_PER_SEC);
    dispatch_after(dispatch_time(start, duration), dispatch_get_main_queue(), ^{
        JardSystemDictation *owner = weakSelf;
        if (owner.soundPreviewGeneration == generation) [owner stopSoundPreview];
    });
    return YES;
}

- (JardDictationStartCue *)newShortcutStartCueAt:(NSTimeInterval)deadline preferences:(NSDictionary *)preferences {
    NSURL *url = [self soundURL:@"start" style:preferences[@"soundSet"]];
    if (!url) return nil;
    JardDictationStartCue *cue = JardDictationStartCue.new;
    [cue playURL:url volume:[preferences[@"soundVolume"] floatValue] / 100 at:deadline];
    return cue;
}

- (void)withdrawShortcutStartCue {
    [self.shortcutStartCue withdraw];
    self.shortcutStartCue = nil;
    self.shortcutStartCueAt = 0;
}

- (void)queueShortcutStartCue:(JardShortcutAction)action at:(NSTimeInterval)time {
    if ((_shortcut.phase == JardIdle || action == JardCancel) && (!time || self.shortcutStartCueAt <= time)) {
        [self withdrawShortcutStartCue];
        return;
    }
    if (action == JardFinish && self.shortcutStartCueAt == _shortcut.pressedAt) {
        self.shortcutStartCue = nil;
        self.shortcutStartCueAt = 0;
        return;
    }
    if ((action != JardArm && action != JardStart) || _shortcut.pressedAt != time ||
        _shortcut.mode == JardDoubleTap || (self.captureID && !self.closingReleaseTail && !self.discardingShortcut)) return;
    if (self.previewSounds) {
        NSUInteger generation = self.soundPreviewGeneration;
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.soundPreviewGeneration == generation) [self stopSoundPreview];
        });
    }
    NSDictionary *preferences = self.tailPress.preferences ?: self.settings;
    if (![preferences[@"keepMicrophoneReady"] boolValue] || ![preferences[@"sound"] boolValue] ||
        [preferences[@"soundVolume"] floatValue] <= 0) return;
    [self withdrawShortcutStartCue];
    self.shortcutStartCue = [self newShortcutStartCueAt:time + .040 preferences:preferences];
    if (self.shortcutStartCue) self.shortcutStartCueAt = time;
}

- (BOOL)captureWaitsForStartCue {
    NSDictionary *preferences = self.capturePreferences;
    NSString *mode = preferences[@"audioWhileDictating"];
    return ([mode isEqual:@"mute"] || [mode isEqual:@"lower"]) && [preferences[@"sound"] boolValue] &&
        [preferences[@"soundVolume"] floatValue] > 0;
}

- (void)announceCaptureStartIfReady {
    if (!self.captureIntentional || !self.captureMicReady || self.captureStartAnnounced ||
        self.cancelled || self.stopping) return;
    self.captureStartAnnounced = YES;
    if (![self captureWaitsForStartCue]) {
        if (!self.captureStartCue) [self playSound:@"start"];
        return;
    }
    NSDictionary *preferences = self.capturePreferences;
    if (!self.captureStartCue) self.captureStartCue =
        [self newShortcutStartCueAt:NSProcessInfo.processInfo.systemUptime preferences:preferences];
    NSUInteger sequence = self.takeSequence;
    __weak typeof(self) weakSelf = self;
    dispatch_block_t suppressOutput = ^{
        JardSystemDictation *owner = weakSelf;
        if (!owner || owner.closed || owner.takeSequence != sequence || owner.cancelled || owner.stopping) return;
        [owner.recordingEnvironment setDictationMode:owner.systemAudioRecordingActive ? nil : preferences[@"audioWhileDictating"]
            percent:[preferences[@"lowerAudioPercent"] doubleValue]];
    };
    if (self.captureStartCue) [self.captureStartCue afterPlayback:suppressOutput];
    else suppressOutput();
}

- (void)admitCaptureIntentionAt:(NSTimeInterval)now {
    if (self.captureIntentional || !self.capturePreferences || self.cancelled || self.stopping ||
        now < self.captureIntentAt) return;
    if (self.captureIntentAt && (_shortcut.phase != JardHeld ||
        _shortcut.pressedAt + JardHoldThreshold != self.captureIntentAt)) return;
    self.captureIntentional = YES;
    [self prepareCaptureOutputContext];
    if (!self.recordingEnvironment) self.recordingEnvironment = JardRecordingEnvironment.new;
    NSString *audioMode = (self.systemAudioRecordingActive || [self captureWaitsForStartCue])
        ? nil : self.capturePreferences[@"audioWhileDictating"];
    [self.recordingEnvironment beginWithMode:audioMode
        percent:[self.capturePreferences[@"lowerAudioPercent"] doubleValue]];
    [self announceCaptureStartIfReady];
}

- (void)playRestoredCue:(NSString *)name {
    if ([name isEqual:@"stop"] && [self soundLevel] > 0) self.lastStopCueAt = NSProcessInfo.processInfo.systemUptime;
    [self playSound:name];
}

- (void)playCompletionCueAt:(NSTimeInterval)now {
    if (!self.activeOutput.playsCompletionCue || self.cancelled || self.pendingStopCueGeneration ||
        (self.lastStopCueAt && now - self.lastStopCueAt < 2) ||
        !JardDictationSoundHasDone(self.settings[@"soundSet"])) return;
    [self playSound:@"done"];
}

- (void)finishRecordingEnvironmentWithSound:(NSString *)name {
    if (!self.captureID) return;
    if (!self.captureIntentional || !self.recordingEnvironment) {
        if (name) [self playRestoredCue:name];
        return;
    }
    NSUInteger generation = self.recordingGeneration;
    BOOL stop = [name isEqual:@"stop"];
    if (stop) self.pendingStopCueGeneration = generation;
    __weak typeof(self) weakSelf = self;
    dispatch_block_t cue = nil;
    if (name) cue = ^{
        JardSystemDictation *owner = weakSelf;
        if (stop && owner.pendingStopCueGeneration == generation) owner.pendingStopCueGeneration = 0;
        if (!owner.closed && owner.recordingGeneration == generation) [owner playRestoredCue:name];
    };
    [self.recordingEnvironment finish:cue];
}

- (void)clearDestination {
    self.destination = nil;
    self.selectedRange = nil;
    self.websiteHost = nil;
    if (self.focusedElement) CFRelease(self.focusedElement);
    self.focusedElement = NULL;
    self.focusedWindowOnly = NO;
}

- (BOOL)targetStillFocusedCheckingSelection:(BOOL)checkSelection {
    if (!self.destination || self.destination.terminated || !accessibilityAllowed() ||
        self.inputRevision != self.takeInputRevision || [self sessionUnavailable] || self.updatePaused) return NO;
    pid_t pid = self.destination.processIdentifier;
    if ([self foregroundOutputApplication].processIdentifier != pid) return NO;
    BOOL windowOnly;
    AXUIElementRef element = copyDictationTarget(pid, self.focusedWindowOnly, &windowOnly);
    BOOL same = self.focusedElement && element && windowOnly == self.focusedWindowOnly &&
        CFEqual(self.focusedElement, element) && (windowOnly || !secureField(element));
    if (same && checkSelection && self.selectedRange) same = [self.selectedRange isEqual:selectedRange(element)];
    if (element) CFRelease(element);
    return same && !IsSecureEventInputEnabled() && [self foregroundOutputApplication].processIdentifier == pid;
}

- (BOOL)dictationPasteAppStillActive {
    if (!self.destination || self.destination.terminated || !accessibilityAllowed() ||
        [self sessionUnavailable] || self.updatePaused || IsSecureEventInputEnabled()) return NO;
    pid_t pid = self.destination.processIdentifier;
    if (pid <= 0 || [self foregroundOutputApplication].processIdentifier != pid) return NO;
    AXUIElementRef element = copyFocus(pid);
    BOOL secure = secureField(element);
    if (element) CFRelease(element);
    return !secure && !IsSecureEventInputEnabled() && [self foregroundOutputApplication].processIdentifier == pid;
}

- (BOOL)freshDictationFollowsCaret {
    JardDictationJob *job = self.activeOutput;
    return [job.mode isEqual:@"paste"] && !job.practice && !job.meetingExcerpt && !job.pasteLastRecovery;
}

- (JardPressContext *)snapshotPressContext {
    JardPressContext *context = JardPressContext.new;
    context.destination = [self foregroundOutputApplication];
    context.preferences = self.settings;
    context.inputRevision = self.inputRevision;
    return context;
}

- (void)resolvePressFocus:(JardPressContext *)context {
    if (context.focusResolved) return;
    context.focusResolved = YES;
    pid_t pid = context.destination.processIdentifier;
    if (context.inputRevision != self.inputRevision || [self foregroundOutputApplication].processIdentifier != pid) return;
    BOOL windowOnly;
    context.focus = copyDictationTarget(pid, YES, &windowOnly);
    context.focusedWindowOnly = windowOnly;
    if (context.focus && !windowOnly && !secureField(context.focus)) {
        context.selection = selectedRange(context.focus);
        if ([context.preferences[@"appRules"] rangeOfString:@"site:" options:NSCaseInsensitiveSearch].location != NSNotFound) {
            context.site = browserHost(context.focus, 0);
        }
    }
}

- (void)usePressContext:(JardPressContext *)context {
    [self resolvePressFocus:context];
    [self clearDestination];
    self.destination = context.destination;
    self.focusedElement = context.focus ? (AXUIElementRef)CFRetain(context.focus) : NULL;
    self.focusedWindowOnly = context.focusedWindowOnly;
    self.selectedRange = context.selection;
    self.websiteHost = context.site;
}

- (void)rememberDestination {
    [self usePressContext:[self snapshotPressContext]];
}

- (NSRunningApplication *)foregroundOutputApplication {
    return NSWorkspace.sharedWorkspace.frontmostApplication;
}

- (NSDictionary *)readOutputContextForPID:(pid_t)pid deadline:(NSTimeInterval)deadline detailed:(BOOL)detailed {
    NSRunningApplication *application = [self foregroundOutputApplication];
    if (IsSecureEventInputEnabled() || application.processIdentifier != pid) return nil;
    AXUIElementRef focus = copyFocus(pid);
    if (focus) AXUIElementSetMessagingTimeout(focus, .10);
    NSMutableDictionary *result = NSMutableDictionary.dictionary;
    AXUIElementRef window = NULL;
    if (!secureField(focus)) {
        if (detailed && focus) window = copyAppElement(pid, kAXFocusedWindowAttribute, NULL);
        NSTimeInterval remaining = deadline - NSProcessInfo.processInfo.systemUptime;
        NSURL *address = JardBrowserTabURL(application, MIN(.25, remaining), NULL);
        if (!address && !IsSecureEventInputEnabled()) address = browserURL(focus, deadline);
        result[@"site"] = address.host.lowercaseString;
        if (detailed && focus && [address.absoluteString lengthOfBytesUsingEncoding:NSUTF8StringEncoding] <= 4096)
            result[@"browserUrl"] = address.absoluteString;
        if (window && !IsSecureEventInputEnabled() && NSProcessInfo.processInfo.systemUptime < deadline) {
            AXUIElementSetMessagingTimeout(window, .10);
            CFTypeRef titleValue = NULL;
            AXUIElementCopyAttributeValue(window, kAXTitleAttribute, &titleValue);
            id title = CFBridgingRelease(titleValue);
            if ([title isKindOfClass:NSString.class] && [title lengthOfBytesUsingEncoding:NSUTF8StringEncoding] <= 1024)
                result[@"windowTitle"] = title;
        }
    }
    if (result.count && !IsSecureEventInputEnabled() && NSProcessInfo.processInfo.systemUptime < deadline) {
        AXUIElementRef currentFocus = copyFocus(pid);
        BOOL sameFocus = currentFocus == focus || (currentFocus && focus && CFEqual(currentFocus, focus));
        if (!sameFocus || secureField(currentFocus)) [result removeAllObjects];
        if (currentFocus) CFRelease(currentFocus);
        if (window && result.count && NSProcessInfo.processInfo.systemUptime < deadline) {
            AXUIElementRef currentWindow = copyAppElement(pid, kAXFocusedWindowAttribute, NULL);
            if (!currentWindow || !CFEqual(currentWindow, window)) [result removeAllObjects];
            if (currentWindow) CFRelease(currentWindow);
        }
    }
    if (window) CFRelease(window);
    if (focus) CFRelease(focus);
    return !IsSecureEventInputEnabled() && [self foregroundOutputApplication].processIdentifier == pid &&
        NSProcessInfo.processInfo.systemUptime < deadline ? result : nil;
}

- (JardOutputContext *)snapshotOutputContext:(NSDictionary *)preferences application:(NSRunningApplication *)application
                                   hostUse:(JardOutputHostUse)hostUse {
    NSString *rules = preferences[@"outputProfiles"];
    if (!rules.length) return nil;
    JardOutputContext *context = JardOutputContext.new;
    context.application = application.localizedName;
    context.bundle = application.bundleIdentifier;
    context.bundleURL = application.bundleURL;
    pid_t pid = context.pid = application.processIdentifier;
    context.inputRevision = self.inputRevision;
    if (!hostUse || pid <= 0 || pid == NSProcessInfo.processInfo.processIdentifier ||
        self.secureInput || IsSecureEventInputEnabled() || [self sessionUnavailable]) {
        context.ready = YES;
        return context;
    }

    static dispatch_queue_t pageQueue;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        pageQueue = dispatch_queue_create("local.jardaudio.dictation-page", DISPATCH_QUEUE_SERIAL);
    });
    NSUInteger revision = context.inputRevision;
    NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + 1;
    __weak typeof(self) weakSelf = self;
    void (^complete)(NSDictionary *) = ^(NSDictionary *details) {
        if (context.ready) return;
        context.ready = YES;
        context.site = details[@"site"];
        context.browserURL = details[@"browserUrl"];
        context.windowTitle = details[@"windowTitle"];
        dispatch_block_t resolved = context.resolved;
        context.resolved = nil;
        if (resolved) resolved();
    };
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), dispatch_get_main_queue(), ^{ complete(nil); });
    dispatch_async(pageQueue, ^{
        JardSystemDictation *owner = weakSelf;
        NSDictionary *details = !context.abandoned && NSProcessInfo.processInfo.systemUptime < deadline
            ? [owner readOutputContextForPID:pid deadline:deadline detailed:(hostUse & JardOutputScriptPageContext) != 0] : nil;
        dispatch_async(dispatch_get_main_queue(), ^{
            JardSystemDictation *current = weakSelf;
            BOOL stillCurrent = current && !context.abandoned && !current.closed && current.inputRevision == revision &&
                [current foregroundOutputApplication].processIdentifier == pid && !IsSecureEventInputEnabled() &&
                NSProcessInfo.processInfo.systemUptime < deadline;
            complete(stillCurrent ? details : nil);
        });
    });
    return context;
}

- (void)prepareCaptureOutputContext {
    if (self.captureOutputContext || self.captureOutputCandidate || self.practice || self.captureRawBinding ||
        self.commandRawTake || self.secureTakeInterrupted) return;
    // Site selectors and detailed context use the window at release. A host
    // hint can be fetched during the take while the input revision agrees.
    if (JardOutputProfilesHostUse(self.capturePreferences) != JardOutputHostForProcessing) return;
    self.captureOutputCandidate = [self snapshotOutputContext:self.capturePreferences
        application:[self foregroundOutputApplication] ?: self.destination hostUse:JardOutputHostForProcessing];
}

- (void)pinCaptureOutputContext {
    if (self.captureOutputContext || self.cancelled || self.practice || self.captureRawBinding || self.commandRawTake) return;
    if (![self.capturePreferences[@"outputProfiles"] length]) return;
    NSRunningApplication *application = [self foregroundOutputApplication] ?: self.destination;
    JardOutputContext *candidate = self.captureOutputCandidate;
    self.captureOutputCandidate = nil;
    BOOL canReadPage = !self.secureTakeInterrupted && !self.secureInput && !IsSecureEventInputEnabled() &&
        ![self sessionUnavailable];
    if (canReadPage && candidate && candidate.pid == application.processIdentifier && candidate.inputRevision == self.inputRevision) {
        self.captureOutputContext = candidate;
        return;
    }
    candidate.abandoned = YES;
    self.captureOutputContext = [self snapshotOutputContext:self.capturePreferences
        application:application hostUse:canReadPage ? JardOutputProfilesHostUse(self.capturePreferences) : 0];
}

- (void)begin {
    if (self.captureID || self.transcribing || self.sdkRecording || self.annotationPasteReply) return;
    [self stopSoundPreview];
    [self stopHistoryAudio];
    JardPressContext *queued = self.tailPress;
    self.tailPress = nil;
    NSDictionary *preferences = queued.preferences ?: self.settings;
    if (queued.releasedAt && (![preferences[@"keepMicrophoneReady"] boolValue] ||
                              ![preferences[@"audioWhileDictating"] isEqual:@"none"])) {
        [self withdrawShortcutStartCue];
        jardShortcutCancel(&_shortcut);
        [self drainOutput];
        return;
    }
    self.takeSequence = ++self.recordingGeneration;
    NSTimeInterval press = queued ? queued.pressedAt : _shortcut.pressedAt;
    if (self.shortcutStartCueAt && self.shortcutStartCueAt == press) self.captureStartCue = self.shortcutStartCue;
    if (self.pendingJobs.count >= 8) { [self fail:@"Finish the queued dictations before starting another."]; return; }
    if (queued) [self usePressContext:queued];
    else [self rememberDestination];
    if (secureField(self.focusedElement)) {
        [self fail:@"Dictation is unavailable in secure fields."];
        return;
    }
    self.practice = !self.printRequest && [preferences[@"onboarding"] isEqual:@"practice"] &&
        self.destination.processIdentifier == NSProcessInfo.processInfo.processIdentifier;
    self.takeInputRevision = queued ? queued.inputRevision : self.inputRevision;
    self.capturePreferences = preferences;
    self.captureOutputContext = queued.outputContext;
    self.captureRawBinding = self.usingRawBinding;
    self.queuedCaptureStart = queued.pressedAt;
    self.started = self.stopping = self.cancelled = self.secureTakeInterrupted = NO;
    self.retriedColdCapture = self.captureIntentional = self.captureMicReady = NO;
    self.captureStartAnnounced = NO;
    self.shortcutReleasedAt = queued.releasedAt;
    self.stopping = queued.releasedAt > 0;
    self.captureIntentAt = _shortcut.phase == JardHeld ? _shortcut.pressedAt + JardHoldThreshold : 0;
    self.inputRMS = 0;
    self.preview = nil;
    [self update:@"starting" message:@"Opening the microphone…" hud:@"Starting…"];
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    [self admitCaptureIntentionAt:now];
    if (self.captureIntentAt > now) {
        NSUInteger sequence = self.takeSequence;
        __weak typeof(self) weakSelf = self;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((self.captureIntentAt - now) * NSEC_PER_SEC)),
            dispatch_get_main_queue(), ^{
                JardSystemDictation *owner = weakSelf;
                if (!owner.closed && owner.takeSequence == sequence)
                    [owner admitCaptureIntentionAt:NSProcessInfo.processInfo.systemUptime];
            });
    }
    [self openCaptureAttempt];
}

- (void)openCaptureAttempt {
    NSString *capture = NSUUID.UUID.UUIDString.lowercaseString;
    self.captureID = capture;
    self.captureOpenedAt = 0;
    self.started = self.captureMicReady = NO;
    self.captureUsesSharedMicrophone = NO;
    NSDictionary *preferences = self.capturePreferences;
    self.send(@"jard.record", @{@"dictation": capture, @"global": @YES,
                                @"languages": preferences[@"languages"], @"mode": @"mic",
                                @"inputDevice": preferences[@"inputDevice"], @"avoidBluetooth": preferences[@"avoidBluetooth"],
                                @"live": (self.practice || [preferences[@"livePreview"] boolValue]) ? @YES : @NO,
                                @"preRoll": [preferences[@"audioWhileDictating"] isEqual:@"none"] ? @YES : @NO,
                                @"keep": preferences[@"keepMicrophoneReady"],
                                @"queuedAt": @(self.queuedCaptureStart)}, ^(id result, NSString *error) {
        if (![self.captureID isEqual:capture]) return;
        if (error) {
            if (self.cancelled) [self resetCapture];
            else [self fail:error];
            return;
        }
        self.started = YES;
        if (self.stopping) [self stopCapture];
    });
}

- (void)stopCapture {
    if (!self.captureID || self.transcribing) return;
    if (!self.captureStopRequestedAt) self.captureStopRequestedAt = NSProcessInfo.processInfo.systemUptime;
    [self pinCaptureOutputContext];
    BOOL wasStopping = self.stopping;
    self.stopping = YES;
    if (!self.cancelled && !wasStopping) [self update:@"processing" message:@"Transcribing on this Mac…" hud:@"Transcribing…"];
    if (!self.started) return;
    NSString *capture = self.captureID;
    BOOL releaseTail = !self.cancelled && !self.secureTakeInterrupted && self.shortcutReleasedAt > 0;
    self.send(@"jard.dictation", @{@"action": self.cancelled ? @"cancel" : @"stop", @"id": capture,
        @"releaseTail": @(releaseTail), @"releaseAt": @(releaseTail ? self.shortcutReleasedAt : 0)}, ^(id result, NSString *error) {
        if (![self.captureID isEqual:capture] || self.transcribing) return;
        if (self.cancelled) [self resetCapture];
        else if (error) [self fail:error];
    });
}

- (void)cancelCapture {
    [self abandonTailPress];
    [self withdrawShortcutStartCue];
    [self.captureStartCue withdraw];
    if (self.caretPasteSent || (!self.captureID && !self.transcribing)) return;
    if (self.discardingShortcut) {
        self.discardingShortcut = self.deferredShortcutStart = NO;
        return;
    }
    self.cancelled = YES;
    self.captureOutputCandidate.abandoned = YES;
    self.captureOutputContext.abandoned = YES;
    if (self.activeMeetingPaste) return;
    [self update:@"cancelled" message:@"Dictation cancelled." hud:@"Cancelled"];
    if (!self.transcribing) [self stopCapture];
}

- (void)discardShortcutTake {
    self.deferredShortcutStart = NO;
    if (!self.captureID || self.stopping || self.transcribing) return;
    [self.captureStartCue withdraw];
    self.discardingShortcut = self.cancelled = YES;
    self.captureOutputCandidate.abandoned = YES;
    self.captureOutputContext.abandoned = YES;
    [self update:@"idle" message:@"" hud:nil];
    [self hideHUD];
    [self stopCapture];
}

- (void)resetCapture {
    if (self.releaseCaretPasteKeys) {
        self.releaseCaretPasteKeys();
        self.releaseCaretPasteKeys = nil;
        [self restoreClipboard];
    }
    self.caretPasteSent = NO;
    // The recorder can acknowledge a short take after the next key gesture has started.
    // Clear only the old recorder state in that case.
    BOOL releasingTail = self.closingReleaseTail;
    BOOL preserveGesture = self.discardingShortcut || releasingTail;
    BOOL deferredStart = self.discardingShortcut && self.deferredShortcutStart;
    self.closingReleaseTail = NO;
    self.discardingShortcut = self.deferredShortcutStart = NO;
    if (self.activeOutput) [self finishDictationTiming:self.activeOutput];
    self.activeOutput = nil;
    [self finishRecordingEnvironmentWithSound:nil];
    void (^cancelMeetingPaste)(BOOL) = self.activeMeetingPaste;
    self.activeMeetingPaste = nil;
    [self.printRequest finish:nil error:@"Dictation was cancelled."];
    self.printRequest = nil;
    self.commandRawTake = NO;
    self.captureFromCommand = NO;
    self.captureRawBinding = NO;
    self.queuedCaptureStart = 0;
    self.captureID = nil;
    self.captureUsesSharedMicrophone = NO;
    self.captureIntentAt = 0;
    self.captureOpenedAt = 0;
    self.captureStopRequestedAt = 0;
    if (self.shortcutStartCue == self.captureStartCue) {
        self.shortcutStartCue = nil;
        self.shortcutStartCueAt = 0;
    }
    self.captureStartCue = nil;
    self.retriedColdCapture = self.captureStartAnnounced = self.captureIntentional = self.captureMicReady = NO;
    self.takeSequence = 0;
    self.capturePreferences = nil;
    self.captureOutputContext = nil;
    self.captureOutputCandidate.abandoned = YES;
    self.captureOutputCandidate = nil;
    self.preview = nil;
    self.practice = NO;
    self.transcribing = self.started = self.stopping = self.cancelled = self.secureTakeInterrupted = NO;
    self.inputRMS = 0;
    self.typedOutputGeneration++;
    if (!preserveGesture) {
        self.modifierClaimAt = 0;
        jardShortcutCancel(&_shortcut);
    }
    [self clearDestination];
    if (cancelMeetingPaste) cancelMeetingPaste(NO);
    BOOL canContinue = self.dictationKeyEnabled && !self.closed && !self.secureInput && !self.updatePaused;
    if (releasingTail && !canContinue) {
        self.tailPress = nil;
        jardShortcutCancel(&_shortcut);
    } else if (releasingTail && self.tailPress &&
               (_shortcut.phase == JardHeld || _shortcut.phase == JardLocked || _shortcut.phase == JardFinishing)) {
        [self begin];
    } else if (deferredStart && canContinue) {
        if (_shortcut.phase == JardHeld || _shortcut.phase == JardLocked) [self begin];
        else if (_shortcut.phase == JardFinishing) {
            self.takeSequence = ++self.recordingGeneration;
            [self fail:@"The microphone was still closing the previous take. Try dictating again."];
        }
    }
    dispatch_async(dispatch_get_main_queue(), ^{ [self drainOutput]; });
}

- (void)recordOutcomeForTake:(NSUInteger)sequence failure:(NSString *)failure {
    if (sequence <= self.lastOutcomeSequence) return;
    self.lastOutcomeSequence = sequence;
    if ([failure isEqual:@"The selected microphone is unavailable."] || [failure isEqual:@"The microphone is unavailable."] ||
        [failure isEqual:@"The selected microphone could not be opened."]) {
        self.lastFailure = [failure stringByAppendingString:@" Reconnect it or choose another microphone in Voice input."];
    } else if ([failure isEqual:@"macOS has not allowed microphone access."]) {
        self.lastFailure = @"Allow Jard in System Settings under Privacy & Security → Microphone, then try again.";
    } else self.lastFailure = failure;
}

- (void)fail:(NSString *)message {
    [self.captureStartCue withdraw];
    [self.printRequest finish:nil error:message];
    [self recordOutcomeForTake:self.takeSequence failure:message];
    [self update:@"error" message:message hud:message];
    [self resetCapture];
}

- (void)act:(JardShortcutAction)action {
    switch (action) {
        case JardArm: {
            if (_shortcut.phase != JardArmed || _shortcut.mode == JardDoubleTap) break;
            NSUInteger generation = ++self.armGeneration;
            NSTimeInterval delay = MAX(0, _shortcut.pressedAt + .175 - NSProcessInfo.processInfo.systemUptime);
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(delay * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                if (!self.closed && generation == self.armGeneration) {
                    JardShortcutAction started = jardShortcutArm(&self->_shortcut);
                    if (started == JardNoAction) [self drainOutput];
                    else [self act:started];
                }
            });
            break;
        }
        case JardStart:
            if (!self.closingReleaseTail) {
                if (self.discardingShortcut) self.deferredShortcutStart = YES;
                else [self begin];
            }
            break;
        case JardLock:
            if (_shortcut.phase == JardLocked && self.captureID && !self.stopping) {
                self.captureIntentAt = 0;
                [self admitCaptureIntentionAt:NSProcessInfo.processInfo.systemUptime];
                if ([self.phase isEqual:@"listening"]) [self update:@"listening" message:@"Listening…" hud:@"Listening · locked"];
            }
            break;
        case JardFinish:
            if (!self.discardingShortcut) [self stopCapture];
            break;
        case JardDiscard: [self discardShortcutTake]; break;
        case JardCancel: [self cancelCapture]; break;
        default: break;
    }
}

- (NSString *)format:(NSString *)raw preferences:(NSDictionary *)preferences {
    NSString *text = raw;
    if ([preferences[@"fillerWords"] boolValue]) {
        text = [text stringByReplacingOccurrencesOfString:@"(?i)\\b(?:um+|uh+|erm+)\\b[,]?\\s*"
            withString:@"" options:NSRegularExpressionSearch range:NSMakeRange(0, text.length)];
    }
    if ([preferences[@"spokenFormatting"] boolValue]) {
        text = [text stringByReplacingOccurrencesOfString:@"(?i)\\s*\\bnew paragraph\\b[.,]?\\s*"
            withString:@"\n\n" options:NSRegularExpressionSearch range:NSMakeRange(0, text.length)];
        text = [text stringByReplacingOccurrencesOfString:@"(?i)\\s*\\bnew line\\b[.,]?\\s*"
            withString:@"\n" options:NSRegularExpressionSearch range:NSMakeRange(0, text.length)];
    }
    text = JardCorrectVocabulary(text, preferences);
    for (NSString *rule in [preferences[@"replacements"] componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]) {
        NSRange separator = [rule rangeOfString:@"=>"];
        if (separator.location == NSNotFound) continue;
        NSString *word = [[rule substringToIndex:separator.location] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        if (!word.length) continue;
        NSString *replacement = [[rule substringFromIndex:NSMaxRange(separator)] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        NSString *pattern = [NSString stringWithFormat:@"(?<![\\p{L}\\p{N}_])%@(?![\\p{L}\\p{N}_])",
            [NSRegularExpression escapedPatternForString:word]];
        NSString *escaped = [NSRegularExpression escapedTemplateForString:replacement];
        text = [text stringByReplacingOccurrencesOfString:pattern withString:escaped options:
            NSRegularExpressionSearch | NSCaseInsensitiveSearch range:NSMakeRange(0, text.length)];
    }
    return [text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
}

- (NSString *)outputMode:(NSDictionary *)preferences {
    NSString *appMode = nil;
    for (NSString *rule in [preferences[@"appRules"] componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]) {
        NSArray *parts = [rule componentsSeparatedByString:@"=>"];
        if (parts.count != 2) continue;
        NSString *target = [[parts[0] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet] lowercaseString];
        NSString *mode = [[parts[1] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet] lowercaseString];
        if (!target.length || ![@[@"paste", @"copy", @"type"] containsObject:mode]) continue;
        if ([target hasPrefix:@"site:"]) {
            NSString *host = [target substringFromIndex:5];
            if ([self.websiteHost isEqual:host] ||
                ([host hasPrefix:@"*."] && [self.websiteHost hasSuffix:[host substringFromIndex:1]])) return mode;
        } else if (!appMode && ([target isEqual:self.destination.bundleIdentifier.lowercaseString] ||
                                [target isEqual:self.destination.localizedName.lowercaseString])) {
            appMode = mode;
        }
    }
    return appMode ?: preferences[@"output"];
}

- (NSPasteboard *)pasteboard { return NSPasteboard.generalPasteboard; }

static JardClipboardSnapshot *captureClipboardItems(PasteboardRef pasteboard) {
    ItemCount count = 0;
    if (PasteboardGetItemCount(pasteboard, &count) != noErr) return nil;
    NSMutableArray *items = NSMutableArray.array;
    NSUInteger total = 0;
    for (ItemCount index = 0; index < count; index++) {
        PasteboardItemID item = NULL;
        CFArrayRef names = NULL;
        if (PasteboardGetItemIdentifier(pasteboard, index + 1, &item) != noErr ||
            PasteboardCopyItemFlavors(pasteboard, item, &names) != noErr) return nil;
        NSArray<NSString *> *types = CFBridgingRelease(names);
        if (!types.count) return nil;
        NSMutableArray<JardClipboardFlavor *> *formats = NSMutableArray.array;
        BOOL hasOriginal = NO;
        for (NSString *type in types) {
            PasteboardFlavorFlags flags = 0;
            if (PasteboardGetItemFlavorFlags(pasteboard, item, (__bridge CFStringRef)type, &flags) != noErr) return nil;
            JardClipboardFlavor *flavor = JardClipboardFlavor.new;
            flavor.type = type;
            flavor.flags = flags;
            [formats addObject:flavor];
            if (!(flags & kPasteboardFlavorSystemTranslated)) hasOriginal = YES;
        }
        NSMutableArray *preserved = NSMutableArray.array;
        for (JardClipboardFlavor *flavor in formats) {
            // macOS regenerates these alternatives from the original item on demand.
            if (hasOriginal && (flavor.flags & kPasteboardFlavorSystemTranslated)) continue;
            CFDataRef value = NULL;
            OSStatus status = PasteboardCopyItemFlavorData(pasteboard, item, (__bridge CFStringRef)flavor.type, &value);
            NSData *data = CFBridgingRelease(value);
            if (status != noErr || !data || data.length > clipboardLimit - total) return nil;
            flavor.data = data;
            flavor.flags &= writableClipboardFlags;
            [preserved addObject:flavor];
            total += data.length;
        }
        [items addObject:preserved];
    }
    return items;
}

static JardClipboardSnapshot *snapshotClipboard(NSPasteboard *board, NSInteger revision) {
    if (board.changeCount != revision) return nil;
    PasteboardRef pasteboard = NULL;
    if (PasteboardCreate((__bridge CFStringRef)board.name, &pasteboard) != noErr) return nil;
    PasteboardSynchronize(pasteboard);
    JardClipboardSnapshot *items = captureClipboardItems(pasteboard);
    CFRelease(pasteboard);
    return board.changeCount == revision ? items : nil;
}

static void restoreClipboardItems(NSPasteboard *board, NSInteger revision, JardClipboardSnapshot *items) {
    if (board.changeCount != revision) return;
    PasteboardRef pasteboard = NULL;
    if (PasteboardCreate((__bridge CFStringRef)board.name, &pasteboard) != noErr) return;
    PasteboardSynchronize(pasteboard);
    if (board.changeCount == revision && PasteboardClear(pasteboard) == noErr) {
        uintptr_t index = 0;
        for (NSArray<JardClipboardFlavor *> *formats in items) {
            PasteboardItemID item = (PasteboardItemID)++index;
            for (JardClipboardFlavor *flavor in formats) {
                OSStatus status = PasteboardPutItemFlavor(pasteboard, item, (__bridge CFStringRef)flavor.type,
                    (__bridge CFDataRef)flavor.data, flavor.flags);
                if (status != noErr && status != duplicatePasteboardFlavorErr)
                    NSLog(@"Jard could not restore a clipboard format (%d).", (int)status);
            }
        }
    }
    CFRelease(pasteboard);
}

static NSPasteboardItem *transientPasteboardItem(void) {
    NSPasteboardItem *item = NSPasteboardItem.new;
    [item setData:NSData.data forType:@"org.nspasteboard.TransientType"];
    [item setData:NSData.data forType:@"org.nspasteboard.ConcealedType"];
    [item setData:NSData.data forType:@"org.nspasteboard.AutoGeneratedType"];
    return item;
}

- (void)restoreClipboard {
    if (!self.clipboardPending) return;
    self.clipboardGeneration++;
    restoreClipboardItems([self pasteboard], self.clipboardCount, self.clipboard);
    self.clipboardPending = NO;
    self.clipboard = nil;
    [self reportActivity];
}

- (BOOL)copyText:(NSString *)text {
    self.lastInsertion = nil;
    self.clipboardGeneration++;
    self.clipboardPending = NO;
    self.clipboard = nil;
    [self reportActivity];
    NSPasteboard *board = [self pasteboard];
    [board clearContents];
    return [board setString:text forType:NSPasteboardTypeString];
}

static NSNumber *JardTotalMilliseconds(JardDictationJob *job) {
    return @(llround((NSProcessInfo.processInfo.systemUptime - job.submittedAt) * 1000));
}

static NSNumber *JardElapsedMilliseconds(NSTimeInterval start, NSTimeInterval end) {
    return start > 0 && end >= start ? @(llround((end - start) * 1000)) : nil;
}

static NSDictionary *JardHistoryProcessing(id value) {
    if (![value isKindOfClass:NSDictionary.class]) return nil;
    id latency = value[@"latencyMs"];
    if (![latency isKindOfClass:NSNumber.class] || !isfinite([latency doubleValue]) || [latency doubleValue] < 0)
        return nil;
    NSMutableDictionary *processing = [@{@"latencyMs": latency} mutableCopy];
    id model = value[@"model"];
    if ([model isKindOfClass:NSString.class] && [model length] && [model length] <= 80)
        processing[@"model"] = model;
    id fallbacks = value[@"fallbacks"];
    if ([fallbacks isKindOfClass:NSArray.class]) {
        NSMutableArray *allowed = NSMutableArray.array;
        for (NSString *kind in @[@"rewrite", @"transformations"])
            if ([fallbacks containsObject:kind]) [allowed addObject:kind];
        if (allowed.count) processing[@"fallbacks"] = allowed;
    }
    return processing;
}

- (void)finishDictationTiming:(JardDictationJob *)job {
    if (job.speechRequestMs && job.deliveryStartedAt) {
        // Paste history waits for the receiving app to settle. Time delivery through the
        // shortcut itself so the fixed settle interval does not look like a slow insertion.
        NSTimeInterval finished = job.deliveryFinishedAt ?: NSProcessInfo.processInfo.systemUptime;
        NSMutableDictionary *timing = NSMutableDictionary.dictionary;
        for (NSString *field in @[@"audioReadMs", @"speechQueueMs", @"audioPriorityMs", @"voiceCleanupMs"]) {
            id value = job.speechTiming[field];
            if ([value isKindOfClass:NSNumber.class]) timing[field] = value;
        }
        timing[@"captureFinalizeMs"] = JardElapsedMilliseconds(job.stopRequestedAt, job.submittedAt);
        timing[@"submissionWaitMs"] = JardElapsedMilliseconds(job.submittedAt, job.speechRequestedAt);
        timing[@"speechRequestMs"] = job.speechRequestMs;
        timing[@"inferenceMs"] = job.inferenceMs;
        timing[@"bridgeRoundTripMs"] = JardElapsedMilliseconds(job.speechRequestedAt, job.responseReadyAt);
        timing[@"outputWaitMs"] = JardElapsedMilliseconds(job.responseReadyAt, job.deliveryStartedAt);
        timing[@"clipboardSnapshotMs"] = job.clipboardSnapshotMs;
        timing[@"deliveryMs"] = JardElapsedMilliseconds(job.deliveryStartedAt, finished);
        timing[@"totalMs"] = JardElapsedMilliseconds(job.stopRequestedAt ?: job.submittedAt, finished);
        self.send(@"jard.diagnosticTiming", timing, nil);
    }
    if (!job.historyID || !job.inferenceMs) return;
    NSUInteger index = [self.history indexOfObjectPassingTest:^BOOL(NSDictionary *entry, NSUInteger i, BOOL *stop) {
        return [entry[@"id"] isEqual:job.historyID];
    }];
    if (index == NSNotFound) return;
    NSMutableDictionary *entry = [self.history[index] mutableCopy];
    entry[@"totalMs"] = JardTotalMilliseconds(job);
    self.history[index] = entry;
    [self saveHistory];
}

- (void)keepText:(NSString *)text forJob:(JardDictationJob *)job awaitingOutput:(BOOL)awaitingOutput {
    if (job.practice) return;
    BOOL printed = job.printRequest != nil;
    if (!printed) {
        self.lastText = text;
        if (self.secureInput) [self updateSecureHotkeys:YES];
        self.lastHistoryID = nil;
    }
    if ([self.settings[@"retention"] integerValue] > 0) {
        job.historyID = NSUUID.UUID.UUIDString;
        if (!printed) self.lastHistoryID = job.historyID;
        NSMutableDictionary *entry = [@{@"id": job.historyID, @"time": @(NSDate.date.timeIntervalSince1970),
            @"text": JardBoundHistoryText(text), @"raw": JardBoundHistoryText(job.raw), @"app": job.destination.localizedName ?: @"Mac",
            @"seconds": @(job.duration),
            @"kind": printed ? @"printed" : job.rawOutput ? @"raw" : @"dictation"} mutableCopy];
        if (job.outputProfile.length) entry[@"mode"] = job.outputProfile;
        if (job.processing) entry[@"processing"] = job.processing;
        if (job.inferenceMs) {
            entry[@"inferenceMs"] = job.inferenceMs;
            if (!awaitingOutput) entry[@"totalMs"] = JardTotalMilliseconds(job);
        }
        NSTimeInterval expires = job.captureEndedAt + historyAudioLifetime;
        if (job.pendingHistoryAudio && expires > NSDate.date.timeIntervalSince1970) {
            NSURL *clip = [self historyAudioURLForID:job.historyID];
            if (clip && renamex_np(job.pendingHistoryAudio.fileSystemRepresentation, clip.fileSystemRepresentation, RENAME_EXCL) == 0) {
                job.pendingHistoryAudio = nil;
                entry[@"audioExpiresAt"] = @(expires);
                [self scheduleHistoryAudioSweepAt:expires];
            }
        }
        [self.history insertObject:entry atIndex:0];
        [self saveHistory];
    }
    [job discardPendingHistoryAudio];
}

static BOOL postKeyPair(CGKeyCode key, CGEventFlags flags, int64_t marker, void (^post)(CGEventRef)) {
    CGEventRef down = CGEventCreateKeyboardEvent(NULL, key, true);
    CGEventRef up = CGEventCreateKeyboardEvent(NULL, key, false);
    if (!down || !up) {
        if (down) CFRelease(down);
        if (up) CFRelease(up);
        return NO;
    }
    for (unsigned int index = 0; index < 2; ++index) {
        CGEventRef event = index == 0 ? down : up;
        CGEventSetFlags(event, flags);
        CGEventSetIntegerValueField(event, kCGEventSourceUserData, marker);
        post(event);
        CFRelease(event);
    }
    return YES;
}

static BOOL sendKey(pid_t pid, CGKeyCode key, CGEventFlags flags) {
    return postKeyPair(key, flags, pasteMarker, ^(CGEventRef event) { CGEventPostToPid(pid, event); });
}

static BOOL annotationEditable(AXUIElementRef element) {
    if (!element || secureField(element)) return NO;
    CFTypeRef editable = NULL;
    AXUIElementCopyAttributeValue(element, CFSTR("AXEditable"), &editable);
    if (editable) {
        BOOL known = CFGetTypeID(editable) == CFBooleanGetTypeID();
        BOOL allowed = known && CFBooleanGetValue((CFBooleanRef)editable);
        CFRelease(editable);
        if (known) return allowed;
    }
    CFTypeRef role = NULL;
    AXUIElementCopyAttributeValue(element, kAXRoleAttribute, &role);
    BOOL editor = role && (CFEqual(role, kAXTextFieldRole) || CFEqual(role, kAXTextAreaRole) || CFEqual(role, kAXComboBoxRole));
    if (role) CFRelease(role);
    Boolean writable = false;
    return editor || (AXUIElementIsAttributeSettable(element, kAXSelectedTextAttribute, &writable) == kAXErrorSuccess && writable);
}

static NSArray<NSPasteboardItem *> *annotationPasteItems(NSString *markdown, NSData *image, NSString *path, NSString *bundleID) {
    NSMutableArray *items = NSMutableArray.array;
    if (image.length) {
        NSPasteboardItem *sheet = transientPasteboardItem();
        BOOL ready = JardIsAnnotationTerminal(bundleID)
            ? (path.isAbsolutePath && [sheet setString:path forType:NSPasteboardTypeString])
            : [sheet setData:image forType:NSPasteboardTypePNG];
        if (ready) [items addObject:sheet];
    }
    NSPasteboardItem *context = transientPasteboardItem();
    if ([context setString:markdown forType:NSPasteboardTypeString]) [items addObject:context];
    return items;
}

- (void)finishAnnotationPaste:(NSString *)error {
    JardDictationReply reply = self.annotationPasteReply;
    if (!reply) return;
    NSString *name = self.destination.localizedName ?: @"the previous app";
    NSUInteger posted = self.annotationItemsPosted;
    self.annotationPasteReply = nil;
    self.annotationPasteGeneration++;
    [self restoreClipboard];
    self.clipboard = nil;
    [self clearDestination];
    if (!self.closed) { [self refresh]; [self drainOutput]; }
    NSString *failure = error && posted ? [@"Part of the annotation was sent. " stringByAppendingString:error] : error;
    reply(failure ? nil : @{ @"destination": name, @"includedSheet": @(posted > 1) }, failure);
}

- (BOOL)annotationPasteFocusMatches:(NSUInteger)revision {
    return self.inputRevision == revision && JardEventTapEnabled(&_sessionTap) &&
        [self annotationModifiersReleased] && [self targetStillFocusedCheckingSelection:YES];
}

- (BOOL)postPasteShortcut {
    if ([self pasteboard].changeCount != self.clipboardCount) return NO;
    return sendKey(self.destination.processIdentifier, JardCurrentPasteKeyCode(), kCGEventFlagMaskCommand);
}

- (void)postCaretPasteChecking:(BOOL (^)(void))canSend completion:(void (^)(BOOL))completion {
    // WebKit and other clients need the physical modifier edges even when
    // they don't expose a focused accessibility element.
    CGKeyCode key = JardCurrentPasteKeyCode();
    CGKeyCode codes[] = {kVK_Command, key, key, kVK_Command};
    NSMutableArray *events = [NSMutableArray arrayWithCapacity:4];
    for (NSUInteger index = 0; index < 4; index++) {
        CGEventRef event = CGEventCreateKeyboardEvent(NULL, codes[index], index < 2);
        if (!event) { completion(NO); return; }
        if (index == 0 || index == 3) CGEventSetType(event, kCGEventFlagsChanged);
        CGEventSetFlags(event, index < 3 ? kCGEventFlagMaskCommand : 0);
        CGEventSetIntegerValueField(event, kCGEventSourceUserData, pasteMarker);
        [events addObject:CFBridgingRelease(event)];
    }
    __block BOOL released = NO;
    __block BOOL keyHeld = NO;
    void (^release)(void) = ^{
        released = YES;
        if (keyHeld) CGEventPost(kCGHIDEventTap, (__bridge CGEventRef)events[2]);
        CGEventPost(kCGHIDEventTap, (__bridge CGEventRef)events[3]);
    };
    self.releaseCaretPasteKeys = release;
    CGEventPost(kCGHIDEventTap, (__bridge CGEventRef)events[0]);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, caretPasteKeyGap), dispatch_get_main_queue(), ^{
        if (released) return;
        if (!canSend()) {
            release();
            self.releaseCaretPasteKeys = nil;
            completion(NO);
            return;
        }
        keyHeld = self.caretPasteSent = YES;
        CGEventPost(kCGHIDEventTap, (__bridge CGEventRef)events[1]);
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, caretPasteKeyGap), dispatch_get_main_queue(), ^{
            if (released) return;
            keyHeld = NO;
            CGEventPost(kCGHIDEventTap, (__bridge CGEventRef)events[2]);
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, caretPasteKeyGap), dispatch_get_main_queue(), ^{
                if (released) return;
                release();
                self.releaseCaretPasteKeys = nil;
                completion(YES);
            });
        });
    });
}

- (BOOL)postAnnotationPaste { return [self postPasteShortcut]; }

- (BOOL)prepareAnnotationFocus:(pid_t)pid {
    self.focusedElement = copyFocus(pid);
    if (!annotationEditable(self.focusedElement)) return NO;
    self.selectedRange = selectedRange(self.focusedElement);
    return YES;
}

- (void)pasteAnnotationItems:(NSArray<NSPasteboardItem *> *)items index:(NSUInteger)index generation:(NSUInteger)generation
    focusRevision:(NSUInteger)focusRevision {
    if (generation != self.annotationPasteGeneration || !self.annotationPasteReply) return;
    NSPasteboard *board = [self pasteboard];
    if (board.changeCount != self.clipboardCount || ![self annotationPasteFocusMatches:focusRevision] ||
        board.changeCount != self.clipboardCount) {
        [self finishAnnotationPaste:@"The insertion point or clipboard changed. Copy the annotation from Jard to resume."];
        return;
    }
    [board clearContents];
    self.clipboardPending = YES;
    BOOL written = [board writeObjects:@[items[index]]];
    self.clipboardCount = board.changeCount;
    if (!written) { [self finishAnnotationPaste:@"Could not prepare the clipboard."]; return; }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 70 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        if (generation != self.annotationPasteGeneration) return;
        if (board.changeCount != self.clipboardCount || ![self annotationPasteFocusMatches:focusRevision] ||
            board.changeCount != self.clipboardCount || ![self postAnnotationPaste]) {
            [self finishAnnotationPaste:@"The insertion point or clipboard changed. Copy the annotation from Jard to resume."];
            return;
        }
        self.annotationItemsPosted++;
        self.selectedRange = nil; // The preceding paste may legitimately move the caret in the same field.
        BOOL more = index + 1 < items.count;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (more ? 230 : 650) * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
            if (generation != self.annotationPasteGeneration) return;
            if (more) [self pasteAnnotationItems:items index:index + 1 generation:generation focusRevision:focusRevision];
            else [self finishAnnotationPaste:nil];
        });
    });
}

- (void)waitForAnnotationDestination:(NSArray<NSPasteboardItem *> *)items generation:(NSUInteger)generation
    deadline:(NSTimeInterval)deadline panelRevision:(NSNumber *)panelRevision {
    if (generation != self.annotationPasteGeneration || !self.annotationPasteReply) return;
    pid_t front = [self annotationForegroundPID];
    if (panelRevision && (front != self.destination.processIdentifier || self.inputRevision != panelRevision.unsignedIntegerValue)) {
        [self finishAnnotationPaste:@"The focused app changed. Focus a field and click Paste again."];
        return;
    }
    if (![self annotationAccessibilityAvailable] || self.destination.terminated ||
        (front != self.destination.processIdentifier && front != NSProcessInfo.processInfo.processIdentifier)) {
        [self finishAnnotationPaste:@"The previous app is unavailable. Use Copy for Agent to paste manually."];
        return;
    }
    if (front == self.destination.processIdentifier && [self annotationModifiersReleased]) {
        NSUInteger revision = self.inputRevision;
        if (![self prepareAnnotationFocus:front]) {
            [self finishAnnotationPaste:@"Focus an editable field in the previous app and try again."];
            return;
        }
        self.takeInputRevision = revision;
        self.clipboard = snapshotClipboard([self pasteboard], self.clipboardCount);
        if (!self.clipboard) {
            [self finishAnnotationPaste:@"Jard preserved your clipboard. Use Copy for Agent to paste manually."];
            return;
        }
        [self pasteAnnotationItems:items index:0 generation:generation focusRevision:revision];
        return;
    }
    if (NSProcessInfo.processInfo.systemUptime >= deadline) {
        [self finishAnnotationPaste:@"The previous app did not become ready to paste."];
        return;
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [self waitForAnnotationDestination:items generation:generation deadline:deadline panelRevision:panelRevision];
    });
}

- (void)pasteAnnotation:(NSString *)markdown image:(NSData *)image path:(NSString *)path
    destination:(NSRunningApplication *)destination fromPanel:(BOOL)fromPanel preparationRevision:(NSUInteger)revision
    clipboardRevision:(NSInteger)clipboardRevision reply:(JardDictationReply)reply {
    if (self.hasActivity || self.updatePaused || self.annotationActive || self.closed) {
        reply(nil, @"Finish the current dictation, paste, or update first.");
        return;
    }
    if (![self annotationAccessibilityAvailable]) { reply(nil, @"Allow Accessibility access or use Copy for Agent."); return; }
    if (!destination || [self annotationPasteDestinationFromPanel:fromPanel].processIdentifier != destination.processIdentifier ||
        revision != self.inputRevision || [self pasteboard].changeCount != clipboardRevision) {
        reply(nil, @"The previous app or clipboard changed. Try Paste into Last App again.");
        return;
    }
    if (!markdown.length) { reply(nil, @"The annotation context is unavailable."); return; }
    NSArray *items = annotationPasteItems(markdown, image, path, destination.bundleIdentifier);
    if (!items.count) { reply(nil, @"Could not prepare the annotation context."); return; }
    [self clearDestination];
    self.destination = destination;
    self.annotationPasteReply = reply;
    self.annotationItemsPosted = 0;
    self.clipboardCount = clipboardRevision;
    NSUInteger generation = ++self.annotationPasteGeneration;
    [self refresh];
    if (fromPanel) {
        [self waitForAnnotationDestination:items generation:generation
            deadline:NSProcessInfo.processInfo.systemUptime + .65 panelRevision:@(revision)];
        return;
    }
    if ([self annotationForegroundPID] != destination.processIdentifier && ![self activateAnnotationApp:destination]) {
        [self finishAnnotationPaste:@"Could not activate the previous app."];
        return;
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [self waitForAnnotationDestination:items generation:generation deadline:NSProcessInfo.processInfo.systemUptime + .65 panelRevision:nil];
    });
}

- (void)rememberInsertion:(NSString *)text {
    if (!self.focusedElement || self.focusedWindowOnly || self.inputRevision != self.takeInputRevision) {
        self.lastInsertion = nil;
        return;
    }
    JardDictationJob *insertion = JardDictationJob.new;
    insertion.destination = self.destination;
    insertion.focus = (AXUIElementRef)CFRetain(self.focusedElement);
    insertion.selection = self.selectedRange;
    insertion.raw = text;
    insertion.inputRevision = self.inputRevision;
    self.lastInsertion = insertion;
}

static NSString *JardJoinDictation(NSString *previous, NSString *text) {
    static NSCharacterSet *sentenceClosers, *sentenceEnders, *opening, *closing, *wordBoundary;
    static NSSet<NSString *> *sentenceWords;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        sentenceClosers = [NSCharacterSet characterSetWithCharactersInString:@"'\")]}"];
        sentenceEnders = [NSCharacterSet characterSetWithCharactersInString:@".?!"];
        opening = [NSCharacterSet characterSetWithCharactersInString:@"([{"];
        closing = [NSCharacterSet characterSetWithCharactersInString:@",.?!;:)]}"];
        NSMutableCharacterSet *letters = [NSCharacterSet.letterCharacterSet mutableCopy];
        [letters addCharactersInString:@"'"];
        wordBoundary = letters.invertedSet;
        sentenceWords = [NSSet setWithArray:@[@"a", @"an", @"and", @"as", @"because", @"but", @"for", @"if", @"nor",
            @"or", @"so", @"the", @"then", @"though", @"to", @"when", @"while", @"yet"]];
    });
    NSCharacterSet *whitespace = NSCharacterSet.whitespaceAndNewlineCharacterSet;
    NSUInteger end = previous.length;
    while (end && [whitespace characterIsMember:[previous characterAtIndex:end - 1]]) end--;
    while (end && [sentenceClosers characterIsMember:[previous characterAtIndex:end - 1]]) end--;
    NSRange firstLetter = [text rangeOfCharacterFromSet:NSCharacterSet.letterCharacterSet];
    if (firstLetter.location != NSNotFound) {
        NSString *initial = [text substringWithRange:firstLetter];
        if (end && [sentenceEnders characterIsMember:[previous characterAtIndex:end - 1]]) {
            text = [text stringByReplacingCharactersInRange:firstLetter withString:initial.uppercaseString];
        } else {
            NSString *tail = [text substringFromIndex:firstLetter.location];
            NSRange wordEnd = [tail rangeOfCharacterFromSet:wordBoundary];
            NSString *word = wordEnd.location == NSNotFound ? tail : [tail substringToIndex:wordEnd.location];
            if ([sentenceWords containsObject:word.lowercaseString])
                text = [text stringByReplacingCharactersInRange:firstLetter withString:initial.lowercaseString];
        }
    }
    unichar before = [previous characterAtIndex:previous.length - 1], after = [text characterAtIndex:0];
    if (![whitespace characterIsMember:before] && ![opening characterIsMember:before] &&
        ![whitespace characterIsMember:after] && ![closing characterIsMember:after])
        return [@" " stringByAppendingString:text];
    return text;
}

- (NSString *)continueInsertion:(NSString *)text {
    JardDictationJob *previous = self.lastInsertion;
    if (!previous || previous.inputRevision != self.inputRevision || self.takeInputRevision != self.inputRevision ||
        self.destination.processIdentifier != previous.destination.processIdentifier || !self.focusedElement || self.focusedWindowOnly ||
        !CFEqual(self.focusedElement, previous.focus) || !previous.raw.length || !text.length) return text;
    NSValue *selection = self.selectedRange;
    if (previous.selection) {
        NSValue *after = [NSValue valueWithRange:NSMakeRange(previous.selection.rangeValue.location + previous.raw.length, 0)];
        if (![selection isEqual:after] && ![selection isEqual:previous.selection]) return text;
        if (![selection isEqual:after]) {
            // A take can already be queued when the preceding paste advances the caret.
            self.selectedRange = after;
            if (![self targetStillFocusedCheckingSelection:YES]) {
                self.selectedRange = selection;
                return text;
            }
        }
    } else if (selection) {
        return text;
    }
    return JardJoinDictation(previous.raw, text);
}

static NSString *JardDeliveryMessage(NSString *message, NSString *warning) {
    return warning.length ? [NSString stringWithFormat:@"%@ %@", message, warning] : message;
}

- (void)postDictationKeyEvent:(CGEventRef)event toProcess:(pid_t)pid {
    CGEventPostToPid(pid, event);
}

- (void)finishTyping:(JardTypingDelivery *)delivery completed:(BOOL)completed {
    BOOL cancelled = self.cancelled;
    void (^meetingCompletion)(BOOL) = self.activeMeetingPaste;
    self.activeMeetingPaste = nil;
    if (completed && !meetingCompletion) {
        NSString *normalized = [[delivery.text stringByReplacingOccurrencesOfString:@"\r\n" withString:@"\n"]
            stringByReplacingOccurrencesOfString:@"\r" withString:@"\n"];
        [self rememberInsertion:normalized];
    } else self.lastInsertion = nil;
    NSString *message = completed ? @"Sent to the active app." : delivery.entered
        ? @"Typing stopped after entering part of the text. The full dictation is available through Paste Last."
        : @"The insertion point changed. Use Paste Last to recover the text.";
    if (!cancelled) [self recordOutcomeForTake:self.takeSequence failure:completed ? nil : message];
    if (completed) [self playCompletionCueAt:NSProcessInfo.processInfo.systemUptime];
    [self resetCapture];
    if (meetingCompletion) { meetingCompletion(completed); return; }
    if (cancelled && !delivery.entered) return;
    [self update:completed ? @"inserted" : @"saved" message:JardDeliveryMessage(message, delivery.warning)
        hud:completed ? @"Dictation sent" : @"Saved · Paste Last"];
}

- (void)continueTyping:(JardTypingDelivery *)delivery {
    if (self.closed || self.typedOutputGeneration != delivery.generation) return;
    if (!delivery.events) {
        if (delivery.cursor == delivery.text.length) { [self finishTyping:delivery completed:YES]; return; }
        NSUInteger next = delivery.cursor;
        delivery.events = JardTypingEvents(delivery.text, &next, delivery.style, delivery.layout, pasteMarker);
        if (!delivery.events) { [self finishTyping:delivery completed:NO]; return; }
        delivery.cursor = next;
        delivery.edge = 0;
    }
    BOOL shifted = delivery.events.count == 4;
    NSUInteger keyDown = shifted ? 1 : 0;
    pid_t pid = self.destination.processIdentifier;
    if (delivery.edge <= keyDown && (self.cancelled || ![self targetStillFocusedCheckingSelection:delivery.entered == 0])) {
        // Balance a synthetic Shift if focus changed between its press and the text key.
        if (shifted && delivery.edge == 1)
            [self postDictationKeyEvent:(__bridge CGEventRef)delivery.events.lastObject toProcess:pid];
        [self finishTyping:delivery completed:NO];
        return;
    }
    CGEventRef event = (__bridge CGEventRef)delivery.events[delivery.edge];
    [self postDictationKeyEvent:event toProcess:pid];
    if (delivery.edge == keyDown) delivery.entered = delivery.cursor;
    if (++delivery.edge == delivery.events.count) delivery.events = nil;
    if (delivery.style == JardInsertPaced) {
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 4 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
            [self continueTyping:delivery];
        });
    } else if (delivery.events) {
        [self continueTyping:delivery];
    } else {
        dispatch_async(dispatch_get_main_queue(), ^{ [self continueTyping:delivery]; });
    }
}

- (void)deliver:(NSString *)text mode:(NSString *)mode warning:(NSString *)warning {
    if ([mode isEqual:@"copy"]) {
        if (![self copyText:text]) {
            [self fail:@"Could not copy the dictation. Use Paste Last to recover the text."];
            return;
        }
        [self recordOutcomeForTake:self.takeSequence failure:nil];
        [self playCompletionCueAt:NSProcessInfo.processInfo.systemUptime];
        [self resetCapture];
        [self update:@"saved" message:JardDeliveryMessage(@"Copied to the clipboard.", warning) hud:@"Copied"];
        return;
    }
    BOOL followsCaret = [self freshDictationFollowsCaret];
    if (followsCaret) {
        NSRunningApplication *active = [self foregroundOutputApplication];
        if (active.processIdentifier != self.destination.processIdentifier) {
            [self clearDestination];
            self.destination = active;
        }
    }
    JardInsertionStyle style = JardInsertionForApp(mode, self.destination.bundleIdentifier, self.destination.bundleURL);
    BOOL usesCurrentCaret = followsCaret && style == JardInsertPaste;
    if (followsCaret && !usesCurrentCaret) {
        [self rememberDestination];
        self.takeInputRevision = self.inputRevision;
    }
    if (!self.activeMeetingPaste) text = [self continueInsertion:text];
    if (!(usesCurrentCaret ? [self dictationPasteAppStillActive] : [self targetStillFocusedCheckingSelection:YES])) {
        BOOL meeting = self.activeMeetingPaste != nil;
        NSString *message = usesCurrentCaret ? @"The active app is unavailable for paste. Use Paste Last to recover the text." :
            @"The insertion point changed. Use Paste Last to recover the text.";
        [self recordOutcomeForTake:self.takeSequence failure:message];
        [self resetCapture];
        if (!meeting) [self update:@"saved" message:JardDeliveryMessage(message, warning)
            hud:@"Saved · Paste Last"];
        return;
    }
    if (style != JardInsertPaste) {
        JardTypingDelivery *delivery = JardTypingDelivery.new;
        delivery.style = style;
        delivery.text = text;
        delivery.warning = warning;
        if (style == JardInsertPaced) delivery.layout = JardTypingLayout();
        delivery.generation = ++self.typedOutputGeneration;
        self.transcribing = YES;
        [self continueTyping:delivery];
        return;
    }
    NSPasteboard *board = [self pasteboard];
    NSInteger initialCount = board.changeCount;
    NSTimeInterval snapshotStarted = NSProcessInfo.processInfo.systemUptime;
    JardClipboardSnapshot *items = self.clipboardPending && self.clipboardCount == initialCount
        ? self.clipboard : snapshotClipboard(board, initialCount);
    self.activeOutput.clipboardSnapshotMs = JardElapsedMilliseconds(snapshotStarted, NSProcessInfo.processInfo.systemUptime);
    if (!items || board.changeCount != initialCount) {
        BOOL meeting = self.activeMeetingPaste != nil;
        NSString *message = @"Jard preserved your clipboard. Use Paste Last or copy the saved dictation.";
        [self recordOutcomeForTake:self.takeSequence failure:message];
        [self resetCapture];
        if (!meeting) [self update:@"saved" message:message
            hud:@"Saved in Jard"];
        return;
    }
    NSPasteboardItem *item = transientPasteboardItem();
    [item setString:text forType:NSPasteboardTypeString];
    NSUInteger generation = ++self.clipboardGeneration;
    [board clearContents];
    self.clipboard = items;
    self.clipboardPending = YES;
    BOOL written = [board writeObjects:@[item]];
    NSInteger count = board.changeCount;
    self.clipboardCount = count;
    if (!written) {
        [self restoreClipboard];
        if (self.activeMeetingPaste) [self resetCapture];
        else [self fail:@"Could not prepare the clipboard. Use Paste Last to recover the text."];
        return;
    }
    BOOL ready = !self.cancelled && board.changeCount == count &&
        (usesCurrentCaret ? [self dictationPasteAppStillActive] : [self targetStillFocusedCheckingSelection:YES]) &&
        board.changeCount == count;
    if (ready && usesCurrentCaret) {
        pid_t pid = self.destination.processIdentifier;
        NSUInteger revision = self.inputRevision;
        self.transcribing = YES;
        [self postCaretPasteChecking:^BOOL{
            return !self.closed && !self.cancelled && !self.updatePaused && ![self sessionUnavailable] &&
                self.clipboardGeneration == generation && self.inputRevision == revision &&
                accessibilityAllowed() && !IsSecureEventInputEnabled() &&
                [self foregroundOutputApplication].processIdentifier == pid && board.changeCount == count;
        } completion:^(BOOL sent) {
            [self finishClipboardPaste:sent text:text warning:warning generation:generation usesCurrentCaret:YES];
        }];
    } else {
        [self finishClipboardPaste:ready && [self postPasteShortcut] text:text warning:warning generation:generation
                  usesCurrentCaret:usesCurrentCaret];
    }
}

- (void)finishClipboardPaste:(BOOL)sent text:(NSString *)text warning:(NSString *)warning
                  generation:(NSUInteger)generation usesCurrentCaret:(BOOL)usesCurrentCaret {
    if (sent) {
        self.activeOutput.deliveryFinishedAt = NSProcessInfo.processInfo.systemUptime;
        [self playCompletionCueAt:NSProcessInfo.processInfo.systemUptime];
        JardDictationJob *output = self.activeOutput;
        self.activeOutput = nil;
        self.pasteSettling = YES;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, pasteSettle), dispatch_get_main_queue(), ^{
            if (!self.closed && output) [self finishDictationTiming:output];
            self.pasteSettling = NO;
            [self drainOutput];
        });
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, pasteRestoreDelay), dispatch_get_main_queue(), ^{
            if (self.clipboardGeneration == generation) [self restoreClipboard];
        });
    } else if (self.clipboardGeneration == generation) {
        [self restoreClipboard];
    }
    BOOL cancelled = self.cancelled;
    void (^meetingCompletion)(BOOL) = self.activeMeetingPaste;
    self.activeMeetingPaste = nil;
    if (sent) {
        if (!meetingCompletion && (!usesCurrentCaret || [self targetStillFocusedCheckingSelection:NO]))
            [self rememberInsertion:text];
        else self.lastInsertion = nil;
    }
    NSString *blocked = @"The insertion point or clipboard changed. Use Paste Last to recover the text.";
    if (!cancelled) [self recordOutcomeForTake:self.takeSequence failure:sent ? nil : blocked];
    [self resetCapture];
    if (meetingCompletion) {
        meetingCompletion(sent);
    } else if (sent) {
        [self update:@"inserted" message:JardDeliveryMessage(@"Sent to the active app.", warning) hud:@"Dictation sent"];
    } else if (!cancelled) {
        [self update:@"saved" message:JardDeliveryMessage(blocked, warning) hud:@"Saved · Paste Last"];
    }
}

- (BOOL)textRecoveryAvailable {
    if (!IsSecureEventInputEnabled()) return YES;
    [self showSecurePasteFeedback];
    return NO;
}

- (void)enqueueMeetingPaste:(JardPrepareExcerpt)prepare {
    if (!self.meetingAvailable || !JardEventTapEnabled(&_sessionTap) || self.closed || self.updatePaused ||
        self.annotationPasteReply || self.pendingJobs.count >= 8) return;
    if (![self textRecoveryAvailable]) return;
    JardDictationJob *job = JardDictationJob.new;
    job.destination = NSWorkspace.sharedWorkspace.frontmostApplication;
    job.inputRevision = self.inputRevision;
    job.focus = copyFocus(job.destination.processIdentifier);
    if (!job.focus || secureField(job.focus)) return;
    job.selection = selectedRange(job.focus);
    job.meetingExcerpt = YES;
    job.mode = @"paste";
    [self.pendingJobs addObject:job];
    prepare(^(NSString *text, void (^didPaste)(BOOL)) {
        if (self.closed || job.cancelled) {
            didPaste(NO);
            return;
        }
        job.raw = text;
        job.pasteCompletion = didPaste;
        job.ready = YES;
        [self drainOutput];
    });
}

- (NSString *)finalTextForJob:(JardDictationJob *)job {
    if (job.meetingExcerpt || job.rawOutput) return job.raw;
    return job.processed ?: [self format:job.raw preferences:job.preferences];
}

- (void)recoverMicrophoneLoss:(JardDictationJob *)job text:(NSString *)text {
    if (!text.length) {
        [self recordOutcomeForTake:job.takeSequence failure:job.microphoneLossReason];
        [self update:@"error" message:job.microphoneLossReason hud:@"Microphone interrupted"];
        return;
    }
    [self keepText:text forJob:job awaitingOutput:NO];
    BOOL copied = !IsSecureEventInputEnabled() && [self pasteboard].changeCount == job.clipboardAtLoss && [self copyText:text];
    NSString *message = copied ? @"Microphone interrupted. Recovered text copied to the clipboard."
        : @"Microphone interrupted. Use Paste Last to recover the text.";
    [self recordOutcomeForTake:job.takeSequence failure:nil];
    [self update:@"saved" message:JardDeliveryMessage(message, job.warning) hud:copied ? @"Copied" : @"Saved · Paste Last"];
}

- (void)drainOutput {
    if (self.closed || self.captureID || self.transcribing || self.pasteSettling || self.annotationPasteReply ||
        self.dispatchingOutput || _shortcut.phase != JardIdle) return;
    while (self.pendingJobs.firstObject.ready) {
        JardDictationJob *job = self.pendingJobs.firstObject;
        [self.pendingJobs removeObjectAtIndex:0];
        if (job.cancelled) continue;
        if (job.error) {
            [self recordOutcomeForTake:job.takeSequence failure:job.error];
            [self update:@"error" message:job.error hud:job.error];
            continue;
        }
        job.deliveryStartedAt = NSProcessInfo.processInfo.systemUptime;
        NSString *text = [self finalTextForJob:job];
        if (job.microphoneLossReason && !job.practice && !job.secureInterrupted) {
            [self recoverMicrophoneLoss:job text:text];
            continue;
        }
        if (job.meetingExcerpt && !text.length) {
            job.pasteCompletion(NO);
            continue;
        }
        if (job.warning && !job.processed) {
            self.practice = job.practice;
            if (text.length) [self keepText:text forJob:job awaitingOutput:NO];
            self.practice = NO;
            [self recordOutcomeForTake:job.takeSequence failure:job.warning];
            [self update:@"saved" message:job.warning hud:@"Output failed · Saved"];
            continue;
        }
        if (!text.length) {
            NSString *message = @"No speech was detected. Try a slightly longer phrase.";
            [self recordOutcomeForTake:job.takeSequence failure:message];
            [self update:@"empty" message:JardDeliveryMessage(message, job.warning)
                hud:@"No speech heard"];
            continue;
        }
        self.practice = job.practice;
        BOOL interrupted = job.interrupted || job.secureInterrupted;
        if (!job.meetingExcerpt && !job.pasteLastRecovery) [self keepText:text forJob:job awaitingOutput:!interrupted];
        if (interrupted) {
            self.practice = NO;
            NSString *message = job.secureInterrupted ? @"Secure keyboard input interrupted dictation. Use Paste Last to recover the text."
                : @"The microphone stopped early. Use Paste Last to recover the text.";
            [self recordOutcomeForTake:job.takeSequence failure:message];
            [self update:@"saved" message:message
                hud:@"Interrupted · Saved"];
            continue;
        }
        self.takeInputRevision = job.inputRevision;
        self.takeSequence = job.takeSequence;
        self.destination = job.destination;
        self.focusedElement = job.focus ? (AXUIElementRef)CFRetain(job.focus) : NULL;
        self.focusedWindowOnly = job.focusedWindowOnly;
        self.selectedRange = job.selection;
        self.activeMeetingPaste = job.pasteCompletion;
        self.activeOutput = job;
        self.dispatchingOutput = YES;
        _shortcut.phase = JardFinishing;
        [self deliver:text mode:job.mode warning:job.warning];
        self.dispatchingOutput = NO;
        return;
    }
    [self reportActivity];
    if (self.pendingJobs.count && !self.pendingJobs.firstObject.meetingExcerpt && ![self.phase isEqual:@"processing"]) {
        [self update:@"processing" message:@"Transcribing on this Mac…" hud:@"Transcribing…"];
    }
}

static NSString *JardVocabularyKey(NSString *term) {
    return [term stringByFoldingWithOptions:NSCaseInsensitiveSearch locale:nil];
}

static BOOL JardValidVocabularyText(id value) {
    return [value isKindOfClass:NSString.class] && [value length] > 0 && [value length] <= 80 &&
        [value rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location == NSNotFound &&
        [value rangeOfCharacterFromSet:NSCharacterSet.newlineCharacterSet].location == NSNotFound;
}

static NSArray<NSString *> *JardVocabulary(NSString *terms) {
    NSMutableArray *vocabulary = NSMutableArray.array;
    NSMutableSet *seen = NSMutableSet.set;
    for (NSString *line in [terms componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]) {
        NSString *word = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        if (!word.length || word.length > 80) continue;
        NSString *key = JardVocabularyKey(word);
        if ([seen containsObject:key]) continue;
        [seen addObject:key];
        [vocabulary addObject:word];
        if (vocabulary.count == 32) break;
    }
    return vocabulary;
}

static NSArray<NSArray<NSString *> *> *JardVocabularyAliases(NSDictionary *preferences) {
    NSArray *terms = JardVocabulary(preferences[@"vocabulary"]);
    NSDictionary *aliases = preferences[@"vocabularySoundsLike"];
    NSMutableSet *seen = NSMutableSet.set;
    for (NSString *term in terms) [seen addObject:JardVocabularyKey(term)];
    NSMutableArray *pairs = NSMutableArray.array;
    for (NSString *term in terms) {
        for (NSString *alias in aliases[JardVocabularyKey(term)]) {
            NSString *key = JardVocabularyKey(alias);
            if ([seen containsObject:key]) continue;
            [seen addObject:key];
            [pairs addObject:@[alias, term]];
        }
    }
    return pairs;
}

static void JardPruneVocabularyAliases(NSMutableDictionary *preferences) {
    NSDictionary *aliases = preferences[@"vocabularySoundsLike"];
    NSMutableDictionary *remaining = NSMutableDictionary.dictionary;
    for (NSString *term in JardVocabulary(preferences[@"vocabulary"])) {
        NSString *key = JardVocabularyKey(term);
        if (aliases[key]) remaining[key] = aliases[key];
    }
    preferences[@"vocabularySoundsLike"] = remaining;
}

static NSString *JardCorrectVocabulary(NSString *text, NSDictionary *preferences) {
    NSArray *aliases = JardVocabularyAliases(preferences);
    if (!aliases.count) return text;
    aliases = [aliases sortedArrayUsingComparator:^NSComparisonResult(NSArray *left, NSArray *right) {
        return [@([right[0] length]) compare:@([left[0] length])];
    }];
    NSMutableDictionary *terms = NSMutableDictionary.dictionary;
    NSMutableArray *patterns = NSMutableArray.array;
    for (NSArray<NSString *> *pair in aliases) {
        terms[JardVocabularyKey(pair[0])] = pair[1];
        [patterns addObject:[NSRegularExpression escapedPatternForString:pair[0]]];
    }
    NSString *pattern = [NSString stringWithFormat:@"(?<![\\p{L}\\p{N}_])(?:%@)(?![\\p{L}\\p{N}_])",
        [patterns componentsJoinedByString:@"|"]];
    NSRegularExpression *regex = [NSRegularExpression regularExpressionWithPattern:pattern
        options:NSRegularExpressionCaseInsensitive error:nil];
    NSMutableString *output = [text mutableCopy];
    for (NSTextCheckingResult *match in [[regex matchesInString:text options:0 range:NSMakeRange(0, text.length)] reverseObjectEnumerator]) {
        NSString *term = terms[JardVocabularyKey([text substringWithRange:match.range])];
        if (term) [output replaceCharactersInRange:match.range withString:term];
        if (output.length > MAX(65536, text.length)) return text;
    }
    return output;
}

- (void)finishPrintedJob:(JardDictationJob *)job {
    [self.pendingJobs removeObject:job];
    NSString *text = job.error ? @"" : [self finalTextForJob:job];
    if (text.length) [self keepText:text forJob:job awaitingOutput:NO];
    NSString *failure = job.error ?: (text.length ? nil : @"No speech was detected.");
    [self recordOutcomeForTake:job.takeSequence failure:failure];
    if (!self.captureID && !self.transcribing && _shortcut.phase == JardIdle && !self.pendingJobs.count) {
        [self update:job.error ? @"error" : text.length ? @"saved" : @"empty"
              message:failure ?: @"Returned dictation to the command." hud:nil];
    }
    NSDictionary *result = text.length ? @{@"status": @"transcript", @"text": text} : @{@"status": @"no-speech"};
    [job.printRequest finish:result error:job.error];
    [self drainOutput];
}

- (void)submitStage:(NSString *)stage interrupted:(BOOL)interrupted microphoneLoss:(NSString *)microphoneLoss duration:(double)duration {
    JardDictationJob *job = JardDictationJob.new;
    job.stageID = stage;
    job.captureEndedAt = NSDate.date.timeIntervalSince1970;
    job.submittedAt = NSProcessInfo.processInfo.systemUptime;
    job.stopRequestedAt = self.captureStopRequestedAt;
    job.destination = self.destination;
    job.focus = self.focusedElement ? (AXUIElementRef)CFRetain(self.focusedElement) : NULL;
    job.focusedWindowOnly = self.focusedWindowOnly;
    job.selection = self.selectedRange;
    job.preferences = self.capturePreferences;
    job.mode = self.practice ? @"paste" : [self outputMode:job.preferences];
    job.practice = self.practice;
    job.interrupted = interrupted;
    job.microphoneLossReason = microphoneLoss;
    if (microphoneLoss) job.clipboardAtLoss = [self pasteboard].changeCount;
    job.secureInterrupted = self.secureTakeInterrupted;
    job.duration = duration;
    job.inputRevision = self.takeInputRevision;
    job.takeSequence = self.takeSequence;
    job.printRequest = self.printRequest;
    self.printRequest = nil;
    job.playsCompletionCue = !job.practice && !self.captureFromCommand && !job.printRequest;
    job.rawOutput = (self.captureRawBinding || self.commandRawTake) && !job.practice;
    if (!job.practice && !job.rawOutput) {
        job.outputContext = self.captureOutputContext ?:
            [self snapshotOutputContext:job.preferences application:job.destination hostUse:0];
        if (job.outputContext && !job.outputContext.ready) {
            __weak typeof(self) weakSelf = self;
            job.outputContext.resolved = ^{ [weakSelf submitReadyDictations]; };
        }
    }
    job.waitingToSubmit = YES;
    [self.pendingJobs addObject:job];
    [self resetCapture];
    if (!self.captureID) [self update:@"processing" message:@"Transcribing on this Mac…" hud:@"Transcribing…"];
    [self submitReadyDictations];
}

- (void)submitReadyDictations {
    if (self.closed) return;
    for (JardDictationJob *job in self.pendingJobs.copy) {
        if (!job.waitingToSubmit) continue;
        if (job.outputContext && !job.outputContext.ready) break;
        job.waitingToSubmit = NO;
        [self transcribeJob:job];
    }
}

- (void)transcribeJob:(JardDictationJob *)job {
    NSDictionary *profile = @{};
    if (!job.practice && !job.rawOutput) {
        JardOutputContext *context = job.outputContext;
        NSString *rules = job.preferences[@"outputProfiles"], *bundleName = nil;
        if ([rules rangeOfString:@"app:" options:NSCaseInsensitiveSearch].location != NSNotFound) {
            NSURL *bundleURL = context.bundleURL;
            if (bundleURL.isFileURL && bundleURL.path.length)
                bundleName = [NSFileManager.defaultManager displayNameAtPath:bundleURL.path];
        }
        if (JardApprovedScriptDigest(self.settings).length) [self publishOutputScriptApproval];
        NSMutableDictionary *output = [@{@"rules": rules, @"replacements": job.preferences[@"replacements"],
            @"script": job.preferences[@"outputScript"], @"scriptDigest": JardApprovedScriptDigest(job.preferences),
            @"scriptGrants": JardApprovedScriptGrants(job.preferences),
            @"app": context.application ?: @"",
            @"bundle": context.bundle ?: @"", @"bundleName": bundleName ?: @"",
            @"site": context.site ?: @"", @"instruction": job.preferences[@"rewriteInstructions"],
            @"aliases": JardVocabularyAliases(job.preferences)} mutableCopy];
        if ([output[@"scriptDigest"] isEqual:JardApprovedScriptDigest(self.settings)] &&
            [output[@"script"] isEqual:self.settings[@"outputScript"]] &&
            [JardApprovedScriptGrants(self.settings)[@"pageContext"] boolValue]) {
            output[@"browserUrl"] = context.browserURL;
            output[@"windowTitle"] = context.windowTitle;
        }
        profile = output;
    }
    NSArray *vocabulary = JardVocabulary(job.rawOutput ? @"" : job.preferences[@"vocabulary"]);
    [self holdHistoryAudioForJob:job];
    job.speechRequestedAt = NSProcessInfo.processInfo.systemUptime;
    self.send(@"jard.dictation", @{@"action": @"transcribe", @"id": job.stageID, @"cleanupOnFailure": @YES,
        @"languages": job.preferences[@"languages"], @"vocabulary": vocabulary, @"outputProfile": profile,
        @"noiseCleanup": job.preferences[@"noiseCleanup"] ?: @NO, @"boostMicrophone": job.preferences[@"boostMicrophone"] ?: @NO}, ^(id result, NSString *error) {
        if (self.closed || job.cancelled) return;
        job.responseReadyAt = NSProcessInfo.processInfo.systemUptime;
        job.raw = result[@"text"] ?: @"";
        job.processed = result[@"processed"];
        job.outputProfile = result[@"profile"];
        job.processing = JardHistoryProcessing(result[@"processing"]);
        job.warning = result[@"outputWarning"];
        NSNumber *inference = result[@"inferenceMs"];
        if ([inference isKindOfClass:NSNumber.class] && isfinite(inference.doubleValue) && inference.doubleValue >= 0)
            job.inferenceMs = inference;
        NSNumber *requestMs = result[@"speechRequestMs"];
        if ([requestMs isKindOfClass:NSNumber.class] && isfinite(requestMs.doubleValue) && requestMs.doubleValue >= 0)
            job.speechRequestMs = requestMs;
        id speechTiming = result[@"speechTiming"];
        if ([speechTiming isKindOfClass:NSDictionary.class]) job.speechTiming = speechTiming;
        job.error = error;
        if (job.printRequest) {
            [self finishPrintedJob:job];
            return;
        }
        job.ready = YES;
        [self drainOutput];
    });
}

- (void)discardUnsubmittedStage:(JardDictationJob *)job {
    job.waitingToSubmit = NO;
    job.outputContext.abandoned = YES;
    job.outputContext.resolved = nil;
    self.send(@"jard.dictation", @{@"action": @"discard", @"id": job.stageID}, nil);
}

- (void)cancelPendingOutput {
    JardDictationJob *job = self.pendingJobs.lastObject;
    if (!job) return;
    job.cancelled = YES;
    [job discardPendingHistoryAudio];
    [self.pendingJobs removeLastObject];
    if (job.waitingToSubmit) [self discardUnsubmittedStage:job];
    [job.printRequest finish:nil error:@"Dictation was cancelled."];
    if (job.meetingExcerpt) {
        if (job.pasteCompletion) job.pasteCompletion(NO);
    } else {
        [self update:@"cancelled" message:@"Dictation cancelled." hud:@"Cancelled"];
    }
    [self submitReadyDictations];
    [self drainOutput];
}

static NSTimeInterval captureClock(id stamp) {
    NSTimeInterval time = [stamp isKindOfClass:NSNumber.class] ? [stamp doubleValue] : 0;
    return isfinite(time) && time > 0 ? time : 0;
}

static NSTimeInterval microphoneFailureAt(NSDictionary *event) {
    return captureClock(event[@"failedAt"]) ?: captureClock(event[@"capturedThrough"]);
}

- (BOOL)retryColdCaptureForFailure:(NSDictionary *)event {
    if (self.retriedColdCapture || self.stopping || self.cancelled || !self.keyDown || _shortcut.phase != JardHeld ||
        !self.dictationKeyEnabled || self.closed || self.updatePaused || self.secureInput || IsSecureEventInputEnabled() ||
        [self.capturePreferences[@"keepMicrophoneReady"] boolValue] || event[@"permission"] ||
        ![event[@"interruption"] isEqual:@"microphone"]) return NO;
    NSTimeInterval failedAt = microphoneFailureAt(event);
    if (failedAt < _shortcut.pressedAt || !self.captureIntentAt || failedAt >= self.captureIntentAt) return NO;

    NSString *stage = [event[@"id"] isKindOfClass:NSString.class] ? event[@"id"] : nil;
    if (stage.length) self.send(@"jard.dictation", @{@"action": @"discard", @"id": stage}, nil);
    self.retriedColdCapture = YES;
    self.inputRMS = 0;
    self.preview = nil;
    [self update:@"starting" message:@"Reopening the microphone…" hud:@"Starting…"];
    [self openCaptureAttempt];
    return YES;
}

- (BOOL)releasedBeforeColdMicrophoneOpened {
    if (!self.shortcutReleasedAt || [self.capturePreferences[@"keepMicrophoneReady"] boolValue]) return NO;
    if (self.captureOpenedAt) return self.shortcutReleasedAt < self.captureOpenedAt;
    return self.retriedColdCapture;
}

- (BOOL)handleCapture:(NSDictionary *)event {
    NSString *kind = event[@"event"];
    if (![event[@"dictation"] isEqual:self.captureID]) {
        if (![event[@"global"] isEqual:@YES]) return NO;
        NSString *stage = event[@"id"];
        if ([kind isEqual:@"dictationReady"] && stage.length) {
            for (JardDictationJob *job in self.pendingJobs)
                if ([job.stageID isEqual:stage]) return YES;
            self.send(@"jard.dictation", @{@"action": @"discard", @"id": stage}, nil);
        }
        return YES;
    }
    BOOL ready = [kind isEqual:@"dictationReady"];
    BOOL error = [kind isEqual:@"error"];
    if ((ready || error) && !self.cancelled) {
        if ([self retryColdCaptureForFailure:event]) return YES;
        if (([event[@"openingAbandoned"] isEqual:@YES] || [self releasedBeforeColdMicrophoneOpened]) && !event[@"permission"]) {
            if (ready) self.send(@"jard.dictation", @{@"action": @"discard", @"id": event[@"id"]}, nil);
            [self.printRequest finish:@{@"status": @"no-speech"} error:nil];
            [self resetCapture];
            if (!self.captureID) {
                [self update:@"idle" message:@"" hud:nil];
                [self hideHUD];
            }
            return YES;
        }
    }
    if ([kind isEqual:@"started"]) {
        self.captureOpenedAt = captureClock(event[@"openedAt"]);
        self.captureUsesSharedMicrophone = [event[@"warm"] isEqual:@YES];
        self.captureMicReady = YES;
        if (!self.stopping && !self.cancelled) {
            NSString *message = [event[@"usingDefaultMicrophone"] isEqual:@YES] && [event[@"microphone"] isKindOfClass:NSString.class]
                ? [NSString stringWithFormat:@"Listening with %@ as the fallback microphone.", event[@"microphone"]]
                : @"Listening…";
            [self update:@"listening" message:message hud:_shortcut.phase == JardLocked ? @"Listening · locked" : @"Listening"];
            [self admitCaptureIntentionAt:NSProcessInfo.processInfo.systemUptime];
            [self announceCaptureStartIfReady];
        }
    } else if ([kind isEqual:@"levels"]) {
        self.inputRMS = [event[@"micRms"] doubleValue];
    } else if ([kind isEqual:@"sourceChanged"] && !self.stopping && !self.cancelled) {
        self.message = event[@"message"] ?: @"The microphone changed.";
    } else if ([kind isEqual:@"preview"] && !self.cancelled && !self.stopping) {
        NSString *text = [event[@"text"] isKindOfClass:NSString.class] ? event[@"text"] : @"";
        NSString *stable = [event[@"stable"] isKindOfClass:NSString.class] ? event[@"stable"] : text;
        NSString *tentative = [event[@"tentative"] isKindOfClass:NSString.class] ? event[@"tentative"] : @"";
        self.preview = text.length <= 500 ? text : [text substringFromIndex:text.length - 500];
        self.previewStable = stable.length <= 500 ? stable : [stable substringFromIndex:stable.length - 500];
        self.previewTentative = tentative.length <= 200 ? tentative : [tentative substringFromIndex:tentative.length - 200];
        [self updateLiveTextPlate];
    } else if (error && !self.cancelled) {
        if ([event[@"noSpeech"] isEqual:@YES]) [self.printRequest finish:@{@"status": @"no-speech"} error:nil];
        [self fail:event[@"message"] ?: @"The microphone did not deliver audio."];
    } else if (ready) {
        NSString *stage = event[@"id"];
        if (self.cancelled) {
            self.send(@"jard.dictation", @{@"action": @"discard", @"id": stage}, nil);
            [self resetCapture];
            return YES;
        }
        BOOL interrupted = [event[@"interrupted"] boolValue];
        NSString *interruption = event[@"interruption"];
        NSString *microphoneLoss = nil;
        if ([interruption isEqual:@"microphone"] || [interruption isEqual:@"discontinuity"]) {
            NSString *reason = event[@"reason"];
            if (!self.closingReleaseTail) self.keyDown = NO;
            if ([interruption isEqual:@"discontinuity"] || ![event[@"hasLiveAudio"] isEqual:@YES] ||
                microphoneFailureAt(event) < self.captureIntentAt) {
                self.send(@"jard.dictation", @{@"action": @"discard", @"id": stage}, nil);
                [self fail:reason.length ? reason : @"Microphone capture was interrupted."];
                return YES;
            }
            interrupted = YES;
            microphoneLoss = reason.length ? reason : @"Microphone capture was interrupted.";
        }
        [self finishRecordingEnvironmentWithSound:interrupted ? nil : @"stop"];
        [self submitStage:stage interrupted:interrupted microphoneLoss:microphoneLoss duration:[event[@"seconds"] doubleValue]];
    }
    return YES;
}

static CGEventFlags modifierFlag(CGKeyCode code) {
    switch (code) {
        case kVK_Option: case kVK_RightOption: return kCGEventFlagMaskAlternate;
        case kVK_Command: case kVK_RightCommand: return kCGEventFlagMaskCommand;
        case kVK_Control: case kVK_RightControl: return kCGEventFlagMaskControl;
        case kVK_Shift: case kVK_RightShift: return kCGEventFlagMaskShift;
        case kVK_Function: return kCGEventFlagMaskSecondaryFn;
        default: return 0;
    }
}

static CGEventFlags sideFlag(CGKeyCode code) {
    switch (code) {
        case kVK_Option: return NX_DEVICELALTKEYMASK;
        case kVK_RightOption: return NX_DEVICERALTKEYMASK;
        case kVK_Command: return NX_DEVICELCMDKEYMASK;
        case kVK_RightCommand: return NX_DEVICERCMDKEYMASK;
        case kVK_Control: return NX_DEVICELCTLKEYMASK;
        case kVK_RightControl: return NX_DEVICERCTLKEYMASK;
        case kVK_Shift: return NX_DEVICELSHIFTKEYMASK;
        case kVK_RightShift: return NX_DEVICERSHIFTKEYMASK;
        default: return 0;
    }
}

static const CGKeyCode modifierPairs[][2] = {{kVK_Option, kVK_RightOption}, {kVK_Command, kVK_RightCommand},
    {kVK_Control, kVK_RightControl}, {kVK_Shift, kVK_RightShift}};

- (uint16_t)physicalModifierKeys { return self.physicalModifiers.held; }

- (CGEventFlags)normalizedPhysicalModifiers:(CGEventFlags)flags {
    uint16_t held = self.physicalModifierKeys;
    if (!(held & physicalModifierBit(kHIDUsage_KeyboardCapsLock))) return flags;
    const CGKeyCode codes[] = {kVK_Control, kVK_Shift, kVK_Option, kVK_Command,
        kVK_RightControl, kVK_RightShift, kVK_RightOption, kVK_RightCommand};
    for (NSUInteger i = 0; i < sizeof codes / sizeof *codes; i++) {
        CGEventFlags side = sideFlag(codes[i]);
        if ((flags & side) && !(held & physicalModifierBit(kHIDUsage_KeyboardLeftControl + (uint32_t)i)))
            flags = (flags & ~side) | modifierFlag(codes[i]);
    }
    return flags;
}

static BOOL JardModifierPair(CGKeyCode code, CGKeyCode *left, CGKeyCode *right) {
    for (NSUInteger index = 0; index < sizeof modifierPairs / sizeof modifierPairs[0]; index++) {
        if (code == modifierPairs[index][0] || code == modifierPairs[index][1]) {
            *left = modifierPairs[index][0];
            *right = modifierPairs[index][1];
            return YES;
        }
    }
    return NO;
}

static NSString *JardShortcutSide(CGKeyCode code, BOOL either) {
    CGKeyCode left, right;
    if (!JardModifierPair(code, &left, &right)) return @"";
    return either ? @"either" : code == left ? @"left" : @"right";
}

static NSString *modifierLabel(CGKeyCode code, BOOL either) {
    NSString *symbol;
    switch (modifierFlag(code)) {
        case kCGEventFlagMaskAlternate: symbol = @"⌥"; break;
        case kCGEventFlagMaskCommand: symbol = @"⌘"; break;
        case kCGEventFlagMaskControl: symbol = @"⌃"; break;
        case kCGEventFlagMaskShift: symbol = @"⇧"; break;
        case kCGEventFlagMaskSecondaryFn: return @"Fn";
        default: return nil;
    }
    if (either) return symbol;
    NSString *side = [JardShortcutSide(code, NO) isEqual:@"left"] ? @"Left" : @"Right";
    return [NSString stringWithFormat:@"%@ %@", side, symbol];
}

typedef struct {
    NSInteger code;
    CGEventFlags flags, both;
    BOOL either;
} JardShortcutBinding;

static BOOL JardModifierOnly(JardShortcutBinding binding) {
    return binding.flags && (binding.code < 0 || modifierFlag((CGKeyCode)binding.code));
}

static CGEventFlags JardBothModifierSides(CGEventFlags raw) {
    CGEventFlags both = 0;
    for (NSUInteger index = 0; index < sizeof modifierPairs / sizeof modifierPairs[0]; index++) {
        CGKeyCode left = modifierPairs[index][0], right = modifierPairs[index][1];
        CGEventFlags sides = sideFlag(left) | sideFlag(right);
        if ((raw & sides) == sides) both |= modifierFlag(left);
    }
    return both;
}

static BOOL JardModifierBindingHeld(JardShortcutBinding binding, CGEventFlags raw, BOOL fixedSideIfUnknown) {
    if ((raw & binding.flags) != binding.flags) return NO;
    if (binding.code < 0) return (JardBothModifierSides(raw) & binding.both) == binding.both;
    CGKeyCode left, right, code = (CGKeyCode)binding.code;
    if (binding.either || !JardModifierPair(code, &left, &right)) return YES;
    CGEventFlags knownSides = raw & (sideFlag(left) | sideFlag(right));
    return knownSides ? (knownSides & sideFlag(code)) != 0 : fixedSideIfUnknown;
}

static BOOL JardModifierBindingHeldAfterRemap(JardShortcutBinding binding, CGEventFlags raw, CGEventFlags remapped,
                                             BOOL fixedSideIfUnknown) {
    return JardModifierBindingHeld(binding, raw, fixedSideIfUnknown && !(remapped & sideFlag((CGKeyCode)binding.code)));
}

static BOOL JardOwnGlobeKey(CGKeyCode code, JardShortcutBinding binding) {
    return code == globeKey && binding.code != code && (binding.flags & kCGEventFlagMaskSecondaryFn);
}

static BOOL JardModifierBindingExact(JardShortcutBinding binding, CGEventFlags raw) {
    return (raw & shortcutModifiers) == binding.flags &&
        (binding.code < 0 || binding.either || !(JardBothModifierSides(raw) & binding.flags));
}

static NSString *JardModifierSymbols(CGEventFlags flags, CGEventFlags both) {
    NSMutableString *label = NSMutableString.string;
    if (flags & kCGEventFlagMaskControl) [label appendString:both & kCGEventFlagMaskControl ? @"⌃⌃" : @"⌃"];
    if (flags & kCGEventFlagMaskAlternate) [label appendString:both & kCGEventFlagMaskAlternate ? @"⌥⌥" : @"⌥"];
    if (flags & kCGEventFlagMaskShift) [label appendString:both & kCGEventFlagMaskShift ? @"⇧⇧" : @"⇧"];
    if (flags & kCGEventFlagMaskCommand) [label appendString:both & kCGEventFlagMaskCommand ? @"⌘⌘" : @"⌘"];
    if (flags & kCGEventFlagMaskSecondaryFn) [label appendString:label.length ? @" Fn" : @"Fn"];
    return label;
}

static const CGKeyCode mouseBase = 256;

static const CGKeyCode shortcutFunctionKeys[] = {kVK_F1, kVK_F2, kVK_F3, kVK_F4, kVK_F5, kVK_F6, kVK_F7, kVK_F8, kVK_F9, kVK_F10,
    kVK_F11, kVK_F12, kVK_F13, kVK_F14, kVK_F15, kVK_F16, kVK_F17, kVK_F18, kVK_F19, kVK_F20};

static NSInteger functionIndex(CGKeyCode code) {
    for (NSUInteger i = 0; i < sizeof shortcutFunctionKeys / sizeof *shortcutFunctionKeys; ++i) {
        if (code == shortcutFunctionKeys[i]) return (NSInteger)i + 1;
    }
    return 0;
}

static BOOL hasImplicitFn(CGKeyCode code) {
    return functionIndex(code) || code == kVK_LeftArrow || code == kVK_RightArrow || code == kVK_UpArrow ||
        code == kVK_DownArrow || code == kVK_Home || code == kVK_End || code == kVK_PageUp || code == kVK_PageDown ||
        code == kVK_ForwardDelete || code == kVK_Help;
}

static CGEventFlags JardEventModifiers(CGEventRef event, CGKeyCode code) {
    CGEventFlags flags = CGEventGetFlags(event) & shortcutModifiers;
    return hasImplicitFn(code) ? flags & ~kCGEventFlagMaskSecondaryFn : flags;
}

static BOOL JardValidAnnotationBinding(NSInteger code, CGEventFlags flags) {
    if (code < 0 || code > 127 || code == kVK_Escape || modifierFlag((CGKeyCode)code) ||
        (flags & kCGEventFlagMaskSecondaryFn)) return NO;
    return flags || functionIndex((CGKeyCode)code) || code == kVK_Home || code == kVK_End ||
        code == kVK_PageUp || code == kVK_PageDown || code == kVK_Help;
}

static NSTimeInterval JardShortcutEventTime(CGEventRef event, BOOL physical) {
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    CGEventTimestamp stamp = CGEventGetTimestamp(event);
    if (!stamp) return now;
    if (physical) {
        // HID events can carry Mach ticks where session events carry nanoseconds.
        static mach_timebase_info_data_t timebase;
        static dispatch_once_t once;
        dispatch_once(&once, ^{ mach_timebase_info(&timebase); });
        NSTimeInterval ticks = (double)stamp * timebase.numer / timebase.denom / NSEC_PER_SEC;
        if (ticks <= now && now - ticks <= 5) return ticks;
    }
    NSTimeInterval nanos = (double)stamp / NSEC_PER_SEC;
    return nanos <= now && (!physical || now - nanos <= 5) ? nanos : now;
}

static BOOL JardReturnKey(CGKeyCode code) {
    return code == kVK_Return || code == kVK_ANSI_KeypadEnter;
}

static NSString *printableShortcutLabel(NSString *label) {
    if (!label.length || [label rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location != NSNotFound) return nil;
    return label.uppercaseString;
}

- (NSString *)layoutLabelForShortcut:(CGKeyCode)code source:(TISInputSourceRef)source {
    if (!source) return nil;
    CFDataRef data = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData);
    if (!data) return nil;
    const UCKeyboardLayout *layout = (const UCKeyboardLayout *)CFDataGetBytePtr(data);
    UInt32 dead = 0;
    UniCharCount length = 0;
    UniChar chars[4];
    if (UCKeyTranslate(layout, code, kUCKeyActionDown, 0, LMGetKbdType(),
        kUCKeyTranslateNoDeadKeysMask, &dead, 4, &length, chars) != noErr) return nil;
    return printableShortcutLabel([NSString stringWithCharacters:chars length:length]);
}

- (NSString *)shortcutKeyLabel:(CGKeyCode)code event:(CGEventRef)event {
    if (code >= mouseBase) return [NSString stringWithFormat:@"Mouse %u", code - mouseBase + 1];
    NSInteger function = functionIndex(code);
    if (function) return [NSString stringWithFormat:@"F%ld", (long)function];
    if (code == kVK_Space) return @"Space";
    if (code == kVK_Home) return @"Home";
    if (code == kVK_End) return @"End";
    if (code == kVK_PageUp) return @"Page Up";
    if (code == kVK_PageDown) return @"Page Down";
    if (code == kVK_Help) return @"Help";
    if (code == kVK_Return) return @"Return";
    if (code == kVK_Tab) return @"Tab";
    if (code == kVK_Delete) return @"⌫";
    if (code == kVK_ForwardDelete) return @"⌦";
    if (code == kVK_LeftArrow) return @"←";
    if (code == kVK_RightArrow) return @"→";
    if (code == kVK_UpArrow) return @"↑";
    if (code == kVK_DownArrow) return @"↓";
    NSString *label = printableShortcutLabel([NSEvent eventWithCGEvent:event].charactersIgnoringModifiers);
    if (label) return label;
    TISInputSourceRef source = TISCopyCurrentKeyboardLayoutInputSource();
    if (!source) return nil;
    label = [self layoutLabelForShortcut:code source:source];
    CFRelease(source);
    return label;
}

- (JardShortcutBinding)shortcutBinding:(NSString *)target {
    return (JardShortcutBinding){
        .code = [self shortcutCodeForSlot:target],
        .flags = [self.settings[[target stringByAppendingString:@"Flags"]] unsignedLongLongValue],
        .both = [self.settings[[target stringByAppendingString:@"BothSides"]] unsignedLongLongValue],
        .either = [target isEqual:@"shortcut"] && [self.settings[@"shortcutEitherSide"] boolValue]
    };
}

- (BOOL)isOwnGlobeCompanion:(CGKeyCode)code {
    if (code != globeKey || !self.dictationKeyEnabled) return NO;
    if (self.keyDown || _shortcut.phase != JardIdle || self.captureID)
        return JardOwnGlobeKey(code, [self shortcutBinding:self.usingRawBinding ? @"raw" : @"shortcut"]);
    return JardOwnGlobeKey(code, [self shortcutBinding:@"shortcut"]) || JardOwnGlobeKey(code, [self shortcutBinding:@"raw"]);
}

static BOOL JardShortcutBindingsOverlap(JardShortcutBinding binding, JardShortcutBinding other) {
    if (binding.flags != other.flags) return NO;
    if (!JardModifierOnly(binding) || !JardModifierOnly(other)) return binding.code == other.code;
    if (binding.either || other.either) return YES;
    if (binding.code >= 0 && other.code >= 0) return binding.code == other.code;
    // A fixed side is exclusive of a binding that requires both sides.
    if (binding.code >= 0) return !other.both;
    if (other.code >= 0) return !binding.both;
    return YES;
}

- (void)resetShortcutCandidate {
    self.shortcutCandidate = -1;
    self.shortcutCandidateFlags = self.shortcutCandidateBoth = 0;
    self.shortcutCandidateUsedByKey = self.shortcutCandidateEither = NO;
}

- (BOOL)setShortcutBinding:(JardShortcutBinding)binding label:(NSString *)label target:(NSString *)target {
    NSMutableDictionary *edited = [self.settings mutableCopy];
    edited[[target stringByAppendingString:@"Code"]] = @(binding.code);
    edited[[target stringByAppendingString:@"Flags"]] = @(binding.flags);
    edited[[target stringByAppendingString:@"Label"]] = label;
    if ([target isEqual:@"shortcut"]) edited[@"shortcutEitherSide"] = @(binding.either);
    if ([target isEqual:@"shortcut"] || [target isEqual:@"raw"])
        edited[[target stringByAppendingString:@"BothSides"]] = @(binding.both);
    NSString *failure = nil;
    if (![self saveSettings:edited error:&failure]) {
        self.message = failure;
        return NO;
    }
    self.choosingShortcut = NO;
    [self resetShortcutCandidate];
    _shortcut.lastTap = 0;
    self.alternateModifierTap = 0;
    self.modifierClaimAt = 0;
    if (self.secureInput) [self updateSecureHotkeys:YES];
    return YES;
}

- (BOOL)saveRecordedBinding:(JardShortcutBinding)binding label:(NSString *)label {
    NSString *target = self.shortcutTarget ?: @"shortcut";
    const JardShortcutLayout *layout = _shortcutLayoutPending ? &_pendingShortcutLayout : _hasShortcutLayout ? &_shortcutLayout : NULL;
    JardShortcutBinding resolved = binding;
    resolved.code = JardShortcutLetterCode(layout, JardShortcutLetter(label),
        (binding.flags & kCGEventFlagMaskCommand) != 0, binding.code);
    BOOL annotate = [target isEqual:@"annotate"];
    if (annotate && !JardValidAnnotationBinding(binding.code, binding.flags)) {
        self.message = @"Use a key with Command, Option, Control or Shift, or a function key.";
        return NO;
    }
    if ((annotate || [target isEqual:@"cancel"]) && binding.flags == meetingPasteFlags &&
        resolved.code == JardShortcutLetterCode(layout, 'v', NO, kVK_ANSI_V)) {
        self.message = @"That shortcut is already in use by Jard.";
        return NO;
    }
    for (NSString *other in @[@"shortcut", @"raw", @"paste", @"cancel", @"annotate"]) {
        if ([other isEqual:target] || ([other isEqual:@"annotate"] && !self.annotationEnabled)) continue;
        JardShortcutBinding otherBinding = [self shortcutBinding:other];
        otherBinding.code = [self shortcutCodeForSlot:other layout:layout];
        if (JardShortcutBindingsOverlap(resolved, otherBinding)) {
            self.message = @"That shortcut is already in use by Jard.";
            return NO;
        }
    }
    if (![self setShortcutBinding:binding label:label target:target]) return NO;
    self.message = @"";
    return YES;
}

- (BOOL)saveRecordedCode:(CGKeyCode)code flags:(CGEventFlags)flags label:(NSString *)label {
    return [self saveRecordedBinding:(JardShortcutBinding){.code = code, .flags = flags} label:label];
}

- (void)recordModifierChange:(CGKeyCode)code flags:(CGEventFlags)flags raw:(CGEventFlags)raw {
    if (flags) {
        if (self.shortcutCandidateUsedByKey || !modifierFlag(code)) return;
        CGEventFlags normalized = [self normalizedPhysicalModifiers:raw];
        CGEventFlags side = sideFlag(code);
        BOOL remapped = (raw & ~normalized & side) != 0;
        CGEventFlags both = JardBothModifierSides(normalized) & flags;
        NSInteger count = __builtin_popcountll(flags) + __builtin_popcountll(both);
        NSInteger oldCount = __builtin_popcountll(self.shortcutCandidateFlags) + __builtin_popcountll(self.shortcutCandidateBoth);
        if (count < oldCount) return;
        self.shortcutCandidateFlags = flags;
        self.shortcutCandidateBoth = both;
        if (count == 1 && ((normalized & side) || flags == modifierFlag(code))) {
            self.shortcutCandidate = code;
            self.shortcutCandidateEither = remapped;
        }
        return;
    }
    JardShortcutBinding binding = {.code = -1, .flags = self.shortcutCandidateFlags, .both = self.shortcutCandidateBoth};
    if (!binding.both && modifierFlag((CGKeyCode)self.shortcutCandidate) == binding.flags) {
        if (!self.shortcutCandidateEither || [self.shortcutTarget isEqual:@"shortcut"]) binding.code = self.shortcutCandidate;
        binding.either = self.shortcutCandidateEither && binding.code >= 0;
    }
    BOOL used = self.shortcutCandidateUsedByKey;
    [self resetShortcutCandidate];
    if (used || !binding.flags) return;
    if (![self.shortcutTarget isEqual:@"shortcut"] && ![self.shortcutTarget isEqual:@"raw"]) {
        self.message = @"Add a key or mouse button to this shortcut.";
        return;
    }
    NSString *label = binding.code >= 0 ? modifierLabel((CGKeyCode)binding.code, binding.either) : JardModifierSymbols(binding.flags, binding.both);
    [self saveRecordedBinding:binding label:label];
}

- (BOOL)shortcutRecorderIsFrontmost {
    return NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier == NSProcessInfo.processInfo.processIdentifier;
}

- (BOOL)canOpenShortcutRecorder {
    return JardEventTapEnabled(&_sessionTap) && !self.captureID && !self.transcribing && _shortcut.phase == JardIdle;
}

- (BOOL)recordShortcut:(CGEventType)type code:(CGKeyCode)code flags:(CGEventFlags)flags event:(CGEventRef)event {
    if (!self.choosingShortcut) return NO;
    if (![self shortcutRecorderIsFrontmost]) {
        self.choosingShortcut = NO;
        return NO;
    }
    if (type == kCGEventFlagsChanged) {
        [self recordModifierChange:code flags:flags raw:CGEventGetFlags(event)];
        return NO;
    }
    if (type != kCGEventKeyDown) return NO;
    if (code == globeKey) return NO;
    self.ignoredRelease = code;
    [self resetShortcutCandidate];
    self.shortcutCandidateUsedByKey = flags != 0;
    if (code == kVK_Escape) {
        self.choosingShortcut = NO;
        if (self.secureInput) [self updateSecureHotkeys:YES];
        return YES;
    }
    if (!flags && (code == kVK_Delete || code == kVK_ForwardDelete)) {
        if ([self.shortcutTarget isEqual:@"paste"] || [self.shortcutTarget isEqual:@"raw"]) {
            if ([self setShortcutBinding:(JardShortcutBinding){.code = -1} label:@"" target:self.shortcutTarget])
                self.message = @"";
        } else {
            self.message = @"Add a modifier to the Delete key.";
        }
        return YES;
    }
    NSString *key = [self shortcutKeyLabel:code event:event];
    BOOL function = functionIndex(code) > 0 || code == kVK_Home || code == kVK_End || code == kVK_PageUp ||
        code == kVK_PageDown || code == kVK_Help || (code >= mouseBase && code < mouseBase + 8);
    if (!key || (!flags && !function)) {
        self.message = @"Use a modifier, a function key, a mouse button, or a key with a modifier.";
        return YES;
    }
    NSMutableString *label = NSMutableString.string;
    if (flags & kCGEventFlagMaskControl) [label appendString:@"⌃"];
    if (flags & kCGEventFlagMaskAlternate) [label appendString:@"⌥"];
    if (flags & kCGEventFlagMaskShift) [label appendString:@"⇧"];
    if (flags & kCGEventFlagMaskCommand) [label appendString:@"⌘"];
    if (flags & kCGEventFlagMaskSecondaryFn) [label appendString:@"Fn "];
    [label appendString:key];
    [self saveRecordedCode:code flags:flags label:label];
    return YES;
}

- (void)abandonTailPress {
    BOOL hadPress = self.tailPress != nil;
    if (hadPress && self.shortcutStartCueAt == self.tailPress.pressedAt) [self withdrawShortcutStartCue];
    self.tailPress = nil;
    if (!self.closingReleaseTail && !hadPress) return;
    self.closingReleaseTail = NO;
    self.armGeneration++;
    if (self.captureID) _shortcut.phase = JardFinishing;
    else jardShortcutCancel(&_shortcut);
}

- (void)rememberTailPressAt:(NSTimeInterval)time {
    if (!self.closingReleaseTail || _shortcut.phase != JardIdle) return;
    JardPressContext *press = [self snapshotPressContext];
    press.pressedAt = time;
    self.tailPress = press;
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.tailPress == press && !self.closed) [self resolvePressFocus:press];
    });
}

- (void)queueAction:(JardShortcutAction)action at:(NSTimeInterval)time {
    [self queueShortcutStartCue:action at:time];
    if (self.closingReleaseTail) {
        if (action == JardArm) [self act:JardArm];
        else if (action == JardFinish && self.tailPress) {
            JardPressContext *press = self.tailPress;
            press.releasedAt = time;
            if (!self.usingRawBinding && [press.preferences[@"outputProfiles"] length] &&
                !([press.preferences[@"onboarding"] isEqual:@"practice"] &&
                  press.destination.processIdentifier == NSProcessInfo.processInfo.processIdentifier)) {
                press.outputContext = [self snapshotOutputContext:press.preferences
                    application:[self foregroundOutputApplication] ?: press.destination
                    hostUse:JardOutputProfilesHostUse(press.preferences)];
            }
        }
        else if (_shortcut.phase == JardIdle) self.tailPress = nil;
        return;
    }
    if (action == JardFinish && self.captureID && !self.stopping && !self.discardingShortcut) {
        NSString *capture = self.captureID;
        [self pinCaptureOutputContext];
        self.shortcutReleasedAt = time;
        self.closingReleaseTail = YES;
        jardShortcutCancel(&_shortcut);
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!self.closed && [self.captureID isEqual:capture] && !self.stopping) [self stopCapture];
        });
        return;
    }
    if (_shortcut.phase == JardIdle && self.tailPress) self.tailPress = nil;
    if (action == JardNoAction && _shortcut.phase != JardIdle) return;
    NSUInteger revision = self.shortcutActionRevision;
    NSString *capture = (action == JardCancel || action == JardDiscard) ? self.captureID : nil;
    dispatch_async(dispatch_get_main_queue(), ^{
        if (self.closed) return;
        if (revision != self.shortcutActionRevision && ![self.captureID isEqual:capture]) return;
        if (action == JardNoAction) [self drainOutput];
        else [self act:action];
    });
}

- (void)observeDictationChordAt:(NSTimeInterval)time earlyOnly:(BOOL)earlyOnly {
    self.modifierClaimAt = 0;
    JardShortcutPhase before = _shortcut.phase;
    JardShortcutAction action = jardShortcutChord(&_shortcut, time, self.keyDown, earlyOnly);
    if (before == JardArmed && _shortcut.phase == JardIdle) self.armGeneration++;
    if (action != JardNoAction || before != _shortcut.phase) [self queueAction:action at:time];
}

- (BOOL)filterModifierClaim:(CGEventRef)event type:(CGEventType)type {
    return event && (type == kCGEventKeyDown || type == kCGEventKeyUp) &&
        CGEventGetIntegerValueField(event, kCGEventSourceUserData) == modifierClaimMarker &&
        CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == modifierClaimKey;
}

- (void)observePhysicalKeyDown:(CGEventRef)event {
    if (!self.dictationKeyEnabled || self.choosingShortcut || IsSecureEventInputEnabled() ||
        [self filterModifierClaim:event type:kCGEventKeyDown] ||
        CGEventGetIntegerValueField(event, kCGEventSourceUserData) == pasteMarker ||
        CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat)) return;
    [self adoptShortcutLayout];
    CGKeyCode code = (CGKeyCode)CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode);
    if (code == kVK_Escape || modifierFlag(code)) return;
    if (code == globeKey &&
        (JardOwnGlobeKey(code, [self shortcutBinding:@"shortcut"]) || JardOwnGlobeKey(code, [self shortcutBinding:@"raw"]))) return;
    CGEventFlags flags = JardEventModifiers(event, code);
    if ((code == [self annotationShortcutCode] && flags == [self annotationShortcutFlags] && self.annotationEnabled) ||
        (code == [self meetingPasteShortcutCode] && flags == meetingPasteFlags && [self canUseMeetingPaste])) return;
    for (NSString *slot in @[@"shortcut", @"raw", @"paste", @"cancel"]) {
        if ([slot isEqual:@"cancel"] && _shortcut.mode != JardToggle) continue;
        if ([self shortcutCodeForSlot:slot] == code &&
            [self.settings[[slot stringByAppendingString:@"Flags"]] unsignedLongLongValue] == flags) return;
    }
    NSTimeInterval time = JardShortcutEventTime(event, YES);
    if (time < _shortcutRecoveryFence) return;
    self.alternateModifierTap = 0;
    // Timestamp only, to handle a HID callback that overtakes the shortcut's session event.
    _physicalChordTimes[_nextPhysicalChord] = time;
    _nextPhysicalChord = (_nextPhysicalChord + 1) % (sizeof _physicalChordTimes / sizeof _physicalChordTimes[0]);
    BOOL bare = JardModifierOnly([self shortcutBinding:self.usingRawBinding ? @"raw" : @"shortcut"]);
    [self observeDictationChordAt:time earlyOnly:!bare || JardReturnKey(code)];
}

- (BOOL)physicalChordAfterPress:(NSTimeInterval)time {
    for (NSUInteger i = 0; i < sizeof _physicalChordTimes / sizeof _physicalChordTimes[0]; i++) {
        NSTimeInterval next = _physicalChordTimes[i];
        if (next > 0 && next >= time && next < time + JardHoldThreshold) {
            _shortcut.lastTap = 0;
            return YES;
        }
    }
    return NO;
}

- (void)resetShortcutEvidenceAt:(NSTimeInterval)time {
    _shortcutRecoveryFence = MAX(_shortcutRecoveryFence, time);
    _modifierNeutralAt = _modifierChangedAt = 0;
    _modifiersSinceNeutral = 0;
    _functionReleaseObserved = NO;
    self.neutralShortcutSince = 0;
    memset(_trackedShortcutKeys, 0, sizeof _trackedShortcutKeys);
    memset(_physicalChordTimes, 0, sizeof _physicalChordTimes);
}

- (BOOL)canRepairShortcutRelease {
    return !self.choosingShortcut && (self.keyDown || self.suppressedKey || self.suppressedPaste || self.suppressedMeetingPaste ||
        self.annotationKeyDown || self.ignoredRelease >= 0 || self.cancelRelease >= 0 || (self.lastModifierFlags & shortcutModifiers)) &&
        (_shortcut.phase == JardIdle || _shortcut.phase == JardLocked);
}

- (void)repairShortcutReleaseAt:(NSTimeInterval)now inputHeld:(BOOL)inputHeld {
    if (inputHeld || ![self canRepairShortcutRelease]) {
        self.neutralShortcutSince = 0;
        return;
    }
    if (!self.neutralShortcutSince) self.neutralShortcutSince = now;
    if (now < self.neutralShortcutSince + .1) return;
    self.keyDown = self.suppressedKey = NO;
    self.suppressedPaste = self.suppressedMeetingPaste = NO;
    if (self.annotationKeyDown) [self handleAnnotationEvent:kCGEventTapDisabledByUserInput event:NULL chordFree:NO];
    self.ignoredRelease = self.cancelRelease = -1;
    self.modifierClaimAt = 0;
    self.lastModifierFlags = self.lastRemappedSides = 0;
    memset(_modifierSidePress, 0, sizeof _modifierSidePress);
    _shortcut.lastTap = 0;
    self.alternateModifierTap = 0;
    BOOL observedFnRelease = _functionReleaseObserved;
    [self resetShortcutEvidenceAt:now];
    _functionReleaseObserved = observedFnRelease;
}

- (BOOL)shortcutKeyDown:(CGKeyCode)code source:(CGEventSourceStateID)source {
    return CGEventSourceKeyState(source, code);
}

- (BOOL)shortcutMouseButtonDown:(CGMouseButton)button {
    return CGEventSourceButtonState(kCGEventSourceStateCombinedSessionState, button);
}

- (CGEventFlags)shortcutLiveModifiers {
    return CGEventSourceFlagsState(kCGEventSourceStateCombinedSessionState) & shortcutModifiers;
}

- (CGEventFlags)shortcutBlockingModifiers {
    CGEventFlags flags = [self shortcutLiveModifiers];
    return _functionReleaseObserved ? flags & ~kCGEventFlagMaskSecondaryFn : flags;
}

- (BOOL)ordinaryKeyHeldForShortcutAt:(NSTimeInterval)now bindingFlags:(CGEventFlags)bindingFlags {
    for (CGKeyCode code = 0; code < 128; code++) {
        if (code == kVK_CapsLock || modifierFlag(code) ||
            ![self shortcutKeyDown:code source:kCGEventSourceStateCombinedSessionState] ||
            ![self shortcutKeyDown:code source:kCGEventSourceStateHIDSystemState]) continue;
        JardTrackedShortcutKey key = _trackedShortcutKeys[code];
        // The ordinary neutral poll never excuses a key that reports down in both states.
        BOOL unreleasedWithoutRepeats = bindingFlags && key.pressedAt && _modifierNeutralAt > key.pressedAt &&
            !(_modifiersSinceNeutral & ~bindingFlags) && now >= key.lastSeenAt + 5 &&
            !([self shortcutBlockingModifiers] & ~bindingFlags);
        if (!unreleasedWithoutRepeats) return YES;
    }
    for (CGMouseButton button = 0; button < 8; button++) {
        if ([self shortcutMouseButtonDown:button]) return YES;
    }
    return NO;
}

- (void)sampleShortcutReleaseAt:(NSTimeInterval)now {
    if (![self canRepairShortcutRelease]) {
        self.neutralShortcutSince = 0;
        return;
    }
    BOOL inputHeld = [self shortcutBlockingModifiers] || [self ordinaryKeyHeldForShortcutAt:now bindingFlags:0] ||
        [self shortcutBlockingModifiers];
    [self repairShortcutReleaseAt:now inputHeld:inputHeld];
}

- (void)sampleShortcutRelease {
    [self sampleShortcutReleaseAt:NSProcessInfo.processInfo.systemUptime];
}

- (void)releaseShortcutAt:(NSTimeInterval)time {
    if (!self.keyDown) return;
    self.keyDown = NO;
    self.modifierClaimAt = 0;
    self.alternateModifierTap = 0;
    self.neutralShortcutSince = 0;
    if (_shortcut.phase == JardArmed) self.armGeneration++;
    [self queueAction:jardShortcutUp(&_shortcut, time) at:time];
}

- (void)noteModifierChange:(CGKeyCode)code raw:(CGEventFlags)raw at:(NSTimeInterval)time {
    _modifierChangedAt = time;
    if (code == kVK_Function) _functionReleaseObserved = !(raw & kCGEventFlagMaskSecondaryFn);
    CGEventFlags held = raw & shortcutModifiers;
    if (_functionReleaseObserved) held &= ~kCGEventFlagMaskSecondaryFn;
    if (!held) {
        _modifierNeutralAt = time;
        _modifiersSinceNeutral = 0;
    } else {
        _modifiersSinceNeutral |= held;
    }
    for (NSUInteger i = 0; i < sizeof modifierPairs / sizeof modifierPairs[0]; i++) {
        for (NSUInteger side = 0; side < 2; side++) {
            CGKeyCode modifier = modifierPairs[i][side];
            CGEventFlags mask = sideFlag(modifier);
            if (!(raw & mask)) _modifierSidePress[i][side] = 0;
            else if (code == modifier && !(self.lastModifierFlags & mask)) _modifierSidePress[i][side] = time;
        }
    }
    self.lastModifierFlags = raw;
}

- (BOOL)startsModifierBinding:(JardShortcutBinding)binding code:(CGKeyCode)code raw:(CGEventFlags)raw
                    remapped:(CGEventFlags)remapped previous:(CGEventFlags)previous previousRemapped:(CGEventFlags)previousRemapped {
    if (!(modifierFlag(code) & binding.flags) || !JardModifierBindingExact(binding, raw) ||
        !JardModifierBindingHeldAfterRemap(binding, raw, remapped, binding.code == code) ||
        JardModifierBindingHeldAfterRemap(binding, previous, previousRemapped, YES)) return NO;
    for (NSUInteger i = 0; binding.both && i < sizeof modifierPairs / sizeof modifierPairs[0]; i++) {
        if (!(binding.both & modifierFlag(modifierPairs[i][0]))) continue;
        NSTimeInterval left = _modifierSidePress[i][0], right = _modifierSidePress[i][1];
        if (!left || !right || MAX(left, right) > MIN(left, right) + .080) return NO;
    }
    return YES;
}

- (BOOL)modifierClaimAvailable {
    return JardEventTapEnabled(&_sessionTap) && JardEventTapEnabled(&_modifierFilter);
}

- (CGEventFlags)physicalModifierClaimFlags {
    return CGEventSourceFlagsState(kCGEventSourceStateHIDSystemState);
}

- (void)postModifierClaimEvent:(CGEventRef)event {
    CGEventPost(kCGHIDEventTap, event);
}

- (void)sendModifierClaimAt:(NSTimeInterval)when {
    if (!self.modifierClaimAt || when < self.modifierClaimAt) return;
    self.modifierClaimAt = 0;
    if (!self.keyDown || !self.dictationKeyEnabled || self.closed || self.choosingShortcut ||
        self.discardingShortcut || IsSecureEventInputEnabled() || ![self modifierClaimAvailable]) return;
    BOOL toggle = _shortcut.mode == JardToggle && (_shortcut.phase == JardArmed || _shortcut.phase == JardLocked);
    BOOL hold = _shortcut.phase == JardHeld && ((self.captureID && !self.stopping) || self.tailPress);
    BOOL finishing = _shortcut.phase == JardFinishing || (self.closingReleaseTail && _shortcut.phase == JardIdle);
    if (!toggle && !hold && !finishing) return;
    JardShortcutBinding binding = [self shortcutBinding:self.usingRawBinding ? @"raw" : @"shortcut"];
    CGEventFlags flags = [self physicalModifierClaimFlags];
    CGEventFlags normalized = [self normalizedPhysicalModifiers:flags];
    if (!JardModifierBindingExact(binding, normalized) || !JardModifierBindingHeld(binding, normalized, NO)) return;
    postKeyPair(modifierClaimKey, flags, modifierClaimMarker, ^(CGEventRef event) { [self postModifierClaimEvent:event]; });
}

- (void)scheduleModifierClaimForBinding:(JardShortcutBinding)binding action:(JardShortcutAction)action at:(NSTimeInterval)when {
    if (binding.flags & kCGEventFlagMaskSecondaryFn || ![self modifierClaimAvailable]) return;
    if (action != JardFinish && (action != JardArm || _shortcut.mode == JardDoubleTap)) return;
    NSTimeInterval due = when;
    if (action == JardArm) due += _shortcut.mode == JardToggle ? .04 : JardHoldThreshold;
    self.modifierClaimAt = due;
    NSTimeInterval delay = MAX(0, due - NSProcessInfo.processInfo.systemUptime);
    __weak typeof(self) weakSelf = self;
    dispatch_block_t postWhenDue = ^{
        JardSystemDictation *owner = weakSelf;
        if (owner.modifierClaimAt == due) [owner sendModifierClaimAt:NSProcessInfo.processInfo.systemUptime];
    };
    if (delay > 0)
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)ceil(delay * NSEC_PER_SEC)), dispatch_get_main_queue(), postWhenDue);
    else dispatch_async(dispatch_get_main_queue(), postWhenDue);
}

- (void)interruptShortcutInput:(CGEventType)type {
    [self resetShortcutEvidenceAt:NSProcessInfo.processInfo.systemUptime];
    [self withdrawShortcutStartCue];
    self.shortcutActionRevision++;
    self.armGeneration++;
    self.tailPress = nil;
    self.deferredShortcutStart = NO;
    [self activationChanged:nil];
    [self handleAnnotationEvent:type event:NULL chordFree:NO];
    self.keyDown = self.suppressedKey = self.suppressedPaste = self.suppressedMeetingPaste = NO;
    self.lastModifierFlags = self.lastRemappedSides = 0;
    self.alternateModifierTap = 0;
    memset(_modifierSidePress, 0, sizeof _modifierSidePress);
    self.ignoredRelease = self.cancelRelease = -1;
    self.modifierClaimAt = 0;
    [self resetShortcutCandidate];
    JardShortcutAction action = jardShortcutCancel(&_shortcut);
    // Once the actual release was received, a gap can discard the next gesture
    // during the audio tail, but cannot take that audio away from the queue.
    NSString *capture = self.captureID;
    if (action == JardCancel && capture && !self.closingReleaseTail && !self.stopping) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (!self.closed && [self.captureID isEqual:capture] && !self.stopping) [self cancelCapture];
        });
    } else {
        [self queueAction:JardNoAction at:0];
    }
}

- (void)eventTapDisabled:(CGEventTapLocation)location type:(CGEventType)type {
    BOOL sessionGap = !_sessionTap.interruptionReported &&
        (location == kCGSessionEventTap || (_sessionTap.port && !JardEventTapEnabled(&_sessionTap)));
    if (location == kCGHIDEventTap || (_physicalKeyTap.port && !JardEventTapEnabled(&_physicalKeyTap)))
        memset(_physicalChordTimes, 0, sizeof _physicalChordTimes);
    JardEnableEventTap(&_sessionTap);
    JardEnableEventTap(&_modifierFilter);
    JardEnableEventTap(&_physicalKeyTap);
    _sessionTap.interruptionReported = _sessionTap.port && !JardEventTapEnabled(&_sessionTap);
    NSLog(@"macOS interrupted a Jaro event tap (location %u, event %u).", (unsigned)location, (unsigned)type);
    if (sessionGap) [self interruptShortcutInput:type];
}

- (BOOL)handleKey:(CGEventRef)event type:(CGEventType)type {
    if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput) {
        [self eventTapDisabled:kCGSessionEventTap type:type];
        return NO;
    }
    // Let earlier taps and global monitors see the synthetic chord. If the
    // annotated-session filter disappears, keep its marked key out of the app.
    if ([self filterModifierClaim:event type:type]) return ![self modifierClaimAvailable];
    if (CGEventGetIntegerValueField(event, kCGEventSourceUserData) == pasteMarker) return NO;
    NSTimeInterval now = JardShortcutEventTime(event, NO);
    if (now < _shortcutRecoveryFence || (type == kCGEventFlagsChanged && now < _modifierChangedAt)) return NO;
    [self adoptShortcutLayout];
    if (type == kCGEventMouseMoved || type == kCGEventLeftMouseDragged) {
        if (self.annotationActive) [self handleAnnotationEvent:type event:event chordFree:NO];
        return NO;
    }
    BOOL mouse = type == kCGEventOtherMouseDown || type == kCGEventOtherMouseUp || type == kCGEventOtherMouseDragged ||
        type == kCGEventRightMouseDown || type == kCGEventRightMouseUp || type == kCGEventRightMouseDragged;
    NSInteger button = mouse ? (NSInteger)CGEventGetIntegerValueField(event, kCGMouseEventButtonNumber) : 0;
    if (mouse && (button < 1 || button >= 8)) return NO;
    CGKeyCode code = mouse ? mouseBase + (CGKeyCode)button :
        (CGKeyCode)CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode);
    CGEventFlags systemFlags = CGEventGetFlags(event);
    CGEventFlags raw = type == kCGEventFlagsChanged ? [self normalizedPhysicalModifiers:systemFlags] : systemFlags;
    CGEventFlags remapped = systemFlags & ~raw;
    CGEventFlags flags = JardEventModifiers(event, code);
    CGEventFlags previousModifiers = self.lastModifierFlags;
    CGEventFlags previousRemapped = self.lastRemappedSides;
    if (type == kCGEventFlagsChanged) {
        [self noteModifierChange:code raw:raw at:now];
        self.lastRemappedSides = remapped;
    }
    BOOL down = type == kCGEventKeyDown || type == kCGEventOtherMouseDown || type == kCGEventRightMouseDown;
    BOOL up = type == kCGEventKeyUp || type == kCGEventOtherMouseUp || type == kCGEventRightMouseUp;
    if (mouse && !down && !up) {
        // Keep a suppressed press balanced during its drag and release.
        return self.cancelRelease == code || self.ignoredRelease == code ||
            (self.suppressedPaste && code == [self shortcutCodeForSlot:@"paste"]) ||
            (self.suppressedKey && code == [self shortcutCodeForSlot:self.usingRawBinding ? @"raw" : @"shortcut"]);
    }
    BOOL repeat = down && CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat);
    if (down || up || type == kCGEventFlagsChanged || type == kCGEventLeftMouseDown || type == kCGEventScrollWheel)
        self.neutralShortcutSince = 0;
    if (self.dictationKeyEnabled && !self.choosingShortcut && !mouse && code < 128 && !modifierFlag(code)) {
        JardTrackedShortcutKey *key = &_trackedShortcutKeys[code];
        if (type == kCGEventKeyUp && now >= key->lastSeenAt) *key = (JardTrackedShortcutKey){0};
        if (type == kCGEventKeyDown && now >= key->lastSeenAt) {
            if (!repeat || !key->pressedAt) key->pressedAt = now;
            key->lastSeenAt = now;
        }
    }
    if (self.cancelRelease == code && (down || up)) {
        if (up) self.cancelRelease = -1;
        return YES;
    }
    if (self.ignoredRelease == code) {
        if (up) self.ignoredRelease = -1;
        if (up || repeat) return YES;
        self.ignoredRelease = -1;
    }
    if (self.choosingShortcut) return [self recordShortcut:mouse && down ? kCGEventKeyDown : type code:code flags:flags event:event];

    if (!mouse && self.annotationEnabled && (down || up || type == kCGEventFlagsChanged)) {
        BOOL chordFree = !down || code != [self annotationShortcutCode] || flags != [self annotationShortcutFlags] || [self annotationChordFree];
        if ([self handleAnnotationEvent:type event:event chordFree:chordFree]) {
            if (down) [self activationChanged:nil];
            return YES;
        }
    }

    if (!mouse && code == [self meetingPasteShortcutCode] && (down || up)) {
        if (self.suppressedMeetingPaste) {
            if (up) {
                self.suppressedMeetingPaste = NO;
                dispatch_async(dispatch_get_main_queue(), ^{ [self refresh]; });
            }
            return YES;
        }
        if (down && !repeat && [self canUseMeetingPaste] && flags == meetingPasteFlags) {
            self.suppressedMeetingPaste = YES;
            dispatch_async(dispatch_get_main_queue(), self.meetingAction);
            return YES;
        }
    }
    if (!self.dictationKeyEnabled) return NO;
    BOOL matchesCancel = _shortcut.mode == JardToggle
        ? code == [self shortcutCodeForSlot:@"cancel"] && flags == [self.settings[@"cancelFlags"] unsignedLongLongValue]
        : code == kVK_Escape && self.keyDown;
    if (down && !repeat && matchesCancel && jardShortcutTakesCancel(&_shortcut, now)) {
        self.cancelRelease = code;
        [self queueAction:jardShortcutCancel(&_shortcut) at:0];
        return YES;
    }
    if (code == kVK_Escape) return NO;
    if (code == [self shortcutCodeForSlot:@"paste"] && (down || up)) {
        if (up && self.suppressedPaste) { self.suppressedPaste = NO; return YES; }
        if (down && self.suppressedPaste) return YES;
        if (down && flags == [self.settings[@"pasteFlags"] unsignedLongLongValue] && self.lastText.length && !self.captureID && !self.transcribing) {
            self.suppressedPaste = YES;
            dispatch_async(dispatch_get_main_queue(), ^{ [self pasteLast]; });
            return YES;
        }
    }

    JardShortcutBinding rawBinding = [self shortcutBinding:@"raw"];
    JardShortcutBinding primaryBinding = [self shortcutBinding:@"shortcut"];
    BOOL flagsChanged = type == kCGEventFlagsChanged;
    BOOL rawModifierPress = flagsChanged && JardModifierOnly(rawBinding) &&
        [self startsModifierBinding:rawBinding code:code raw:raw remapped:remapped previous:previousModifiers previousRemapped:previousRemapped];
    BOOL primaryModifierPress = flagsChanged && JardModifierOnly(primaryBinding) &&
        [self startsModifierBinding:primaryBinding code:code raw:raw remapped:remapped previous:previousModifiers previousRemapped:previousRemapped];
    BOOL rawMatch = rawModifierPress || (!JardModifierOnly(rawBinding) && rawBinding.code == code && rawBinding.flags == flags);
    BOOL useRaw = self.keyDown || self.suppressedKey || _shortcut.phase != JardIdle ? self.usingRawBinding : rawMatch;
    JardShortcutBinding selected = useRaw ? rawBinding : primaryBinding;
    BOOL bare = JardModifierOnly(selected);
    NSInteger binding = selected.code;
    CGEventFlags expected = selected.flags;
    if (flagsChanged && bare) {
        if (self.keyDown && !JardModifierBindingHeldAfterRemap(selected, raw, remapped, YES)) {
            [self releaseShortcutAt:now];
            return NO;
        }
        BOOL switchedBinding = NO;
        if (self.keyDown && !JardModifierBindingExact(selected, raw)) {
            [self observeDictationChordAt:now earlyOnly:YES];
            BOOL completedOther = useRaw ? primaryModifierPress : rawModifierPress;
            if (_shortcut.phase != JardIdle || !completedOther) {
                self.alternateModifierTap = 0;
                return NO;
            }
            self.keyDown = NO;
            useRaw = !useRaw;
            switchedBinding = YES;
        }
        BOOL matches = useRaw ? rawModifierPress : primaryModifierPress;
        if (self.keyDown || !matches) {
            if (flags & ~expected) [self observeDictationChordAt:now earlyOnly:YES];
            return NO;
        }
        CGEventFlags matchedFlags = useRaw ? rawBinding.flags : primaryBinding.flags;
        if ([self ordinaryKeyHeldForShortcutAt:now bindingFlags:matchedFlags]) {
            _shortcut.lastTap = 0;
            self.alternateModifierTap = 0;
            return NO;
        }
        if (self.lastTapWasRaw != useRaw) {
            NSTimeInterval saved = self.alternateModifierTap;
            self.alternateModifierTap = switchedBinding ? 0 : _shortcut.lastTap;
            _shortcut.lastTap = switchedBinding ? saved : 0;
        } else self.alternateModifierTap = 0;
        self.usingRawBinding = useRaw;
        self.lastTapWasRaw = useRaw;
        self.keyDown = YES;
        self.neutralShortcutSince = 0;
        if (_shortcut.phase != JardIdle || ![self physicalChordAfterPress:now]) {
            _shortcutRecoveryFence = now;
            [self rememberTailPressAt:now];
            JardShortcutAction action = jardShortcutDown(&_shortcut, now, true);
            [self scheduleModifierClaimForBinding:useRaw ? rawBinding : primaryBinding action:action at:now];
            [self queueAction:action at:now];
        }
        return NO;
    }
    if (type == kCGEventLeftMouseDown || (down && !repeat && code != binding && ![self isOwnGlobeCompanion:code])) {
        self.alternateModifierTap = 0;
        [self observeDictationChordAt:now earlyOnly:!bare || mouse || type == kCGEventLeftMouseDown || JardReturnKey(code)];
    } else if (type == kCGEventFlagsChanged && flags != expected) {
        if (!bare && (flags & expected) != expected) [self releaseShortcutAt:now];
        else [self observeDictationChordAt:now earlyOnly:YES];
    }
    if (bare) return NO;
    if (code != binding) return NO;
    if (up && self.suppressedKey) {
        self.suppressedKey = NO;
        [self releaseShortcutAt:now];
        return YES;
    }
    if (down && self.suppressedKey) {
        if (repeat || (_shortcut.phase != JardIdle && _shortcut.phase != JardLocked)) return YES;
        // A fresh press is also evidence that its previous release was lost.
        self.keyDown = self.suppressedKey = NO;
        _shortcut.lastTap = 0;
    }
    if (!down || repeat || flags != expected) return NO;
    if (_shortcut.phase == JardIdle && [self physicalChordAfterPress:now]) return NO;
    if (_shortcut.phase == JardIdle && self.lastTapWasRaw != useRaw) _shortcut.lastTap = 0;
    [self rememberTailPressAt:now];
    JardShortcutAction action = jardShortcutDown(&_shortcut, now, false);
    if (action == JardNoAction) return NO;
    _shortcutRecoveryFence = now;
    self.usingRawBinding = self.lastTapWasRaw = useRaw;
    self.keyDown = self.suppressedKey = YES;
    self.neutralShortcutSince = 0;
    [self queueAction:action at:now];
    return YES;
}

- (BOOL)canInvokePasteLast {
    return self.dictationKeyEnabled && JardEventTapEnabled(&_sessionTap) && self.lastText.length &&
        !self.captureID && !self.transcribing && !self.closed && !self.updatePaused && !self.choosingShortcut;
}

- (BOOL)vocabularyMenuEnabled {
    return [self.settings[@"vocabularyMenu"] boolValue];
}

- (NSArray<NSDictionary *> *)menuVocabularyEntries {
    return JardVocabularyEntries(self.settings);
}

- (void)configurePasteLastMenuItem:(NSMenuItem *)item {
    JardShortcutBinding binding = [self shortcutBinding:@"paste"];
    NSString *key = @"";
    if (binding.code >= 0 && binding.code < mouseBase && !modifierFlag((CGKeyCode)binding.code)) {
        NSInteger function = functionIndex((CGKeyCode)binding.code);
        if (function) key = [NSString stringWithFormat:@"%C", (unichar)(NSF1FunctionKey + function - 1)];
        else if (binding.code == kVK_Space) key = @" ";
        else {
            NSString *label = [self.settings[@"pasteLabel"] stringByTrimmingCharactersInSet:
                [NSCharacterSet characterSetWithCharactersInString:@"⌃⌥⇧⌘ "]];
            if ([label hasPrefix:@"Fn "]) label = [label substringFromIndex:3];
            if (label.length == 1) key = label.lowercaseString;
        }
    }
    // Quartz and AppKit use the same bits for device-independent modifiers.
    item.keyEquivalent = key;
    item.keyEquivalentModifierMask = key.length ? (NSEventModifierFlags)binding.flags : 0;
    item.enabled = [self canInvokePasteLast];
}

- (void)pasteLastFromMenu {
    if (![self canInvokePasteLast]) return;
    pid_t pid = [self foregroundOutputApplication].processIdentifier;
    if (pid <= 0) return;
    __weak typeof(self) weakSelf = self;
    // NSMenu can dispatch an action while still tracking. Wait until the
    // normal run loop resumes before reading Accessibility focus or pasting.
    CFRunLoopPerformBlock(CFRunLoopGetMain(), kCFRunLoopDefaultMode, ^{
        JardSystemDictation *owner = weakSelf;
        if ([owner canInvokePasteLast] && [owner foregroundOutputApplication].processIdentifier == pid) [owner pasteLast];
    });
    CFRunLoopWakeUp(CFRunLoopGetMain());
}

- (void)pasteLast {
    if (self.captureID || self.transcribing || !self.lastText.length || self.closed) return;
    if (self.pasteSettling && self.pendingJobs.count >= 8) return;
    if (![self textRecoveryAvailable]) return;
    [self rememberDestination];
    if (!self.pasteSettling) {
        self.takeInputRevision = self.inputRevision;
        _shortcut.phase = JardFinishing;
        [self deliver:self.lastText mode:@"paste" warning:nil];
        return;
    }
    JardDictationJob *job = JardDictationJob.new;
    job.destination = self.destination;
    job.focus = self.focusedElement ? (AXUIElementRef)CFRetain(self.focusedElement) : NULL;
    job.focusedWindowOnly = self.focusedWindowOnly;
    job.selection = self.selectedRange;
    job.inputRevision = self.inputRevision;
    job.mode = @"paste";
    job.raw = self.lastText;
    job.rawOutput = job.pasteLastRecovery = job.ready = YES;
    [self clearDestination];
    [self.pendingJobs addObject:job];
    [self drainOutput];
}

static NSDictionary *JardPreferenceCatalog(void) {
    static NSDictionary *catalog;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSMutableDictionary *fields = [@{
            @"mode": @{@"type": @"enum", @"choices": @[@"hold", @"holdDouble", @"tap", @"doubleTap"]},
            @"placement": @{@"type": @"enum", @"choices": @[@"top", @"bottom", @"cursor", @"off"]},
            @"output": @{@"type": @"enum", @"choices": @[@"paste", @"copy", @"type"]},
            @"retention": @{@"type": @"integer", @"choices": @[@0, @1, @7, @30]},
            @"soundSet": @{@"type": @"enum", @"choices": JardDictationSoundStyles()},
            @"soundVolume": @{@"type": @"number", @"minimum": @0, @"maximum": @100},
            @"audioWhileDictating": @{@"type": @"enum", @"choices": @[@"none", @"mute", @"lower", @"pause"]},
            @"lowerAudioPercent": @{@"type": @"integer", @"minimum": @0, @"maximum": @100},
            @"languages": @{@"type": @"array", @"maxItems": @25},
        } mutableCopy];
        for (NSString *key in @[@"enabled", @"sound", @"showHistoryStats", @"fillerWords", @"spokenFormatting", @"avoidBluetooth",
                                @"vocabularyMenu", @"livePreview", @"keepMicrophoneReady", @"noiseCleanup", @"boostMicrophone"]) {
            fields[key] = @{@"type": @"boolean"};
        }
        NSDictionary *limits = @{@"inputDevice": @1024, @"rewriteInstructions": @2500, @"outputProfiles": @48000,
                                 @"vocabulary": @3000, @"replacements": @16000, @"appRules": @16000};
        for (NSString *key in limits) fields[key] = @{@"type": @"string", @"maxLength": limits[key]};
        for (NSString *key in @[@"shortcutLabel", @"rawLabel", @"pasteLabel", @"cancelLabel"]) {
            fields[key] = @{@"type": @"string", @"readOnly": @YES};
        }
        catalog = [fields copy];
    });
    return catalog;
}

static BOOL JardBoolean(id value) {
    return value == (__bridge id)kCFBooleanTrue || value == (__bridge id)kCFBooleanFalse;
}

static BOOL JardValidPreference(NSString *key, id value) {
    if (!key || !value) return NO;
    if ([key isEqual:@"onboarding"]) {
        return [@[@"welcome", @"access", @"practice", @"ready", @"done", @"later"] containsObject:value];
    }
    NSDictionary *field = JardPreferenceCatalog()[key];
    if (!field || [field[@"readOnly"] boolValue]) return NO;
    NSString *type = field[@"type"];
    if ([type isEqual:@"boolean"]) return JardBoolean(value);
    if ([type isEqual:@"integer"] || [type isEqual:@"number"]) {
        if (![value isKindOfClass:NSNumber.class] || JardBoolean(value)) return NO;
        if (field[@"choices"]) return [field[@"choices"] containsObject:value];
        double number = [value doubleValue];
        if ([type isEqual:@"integer"] && trunc(number) != number) return NO;
        return number >= [field[@"minimum"] doubleValue] && number <= [field[@"maximum"] doubleValue];
    }
    if ([type isEqual:@"array"]) {
        if (![value isKindOfClass:NSArray.class] || [value count] > [field[@"maxItems"] unsignedIntegerValue]) return NO;
        for (id code in value) if (![code isKindOfClass:NSString.class] || [code length] > 12) return NO;
        return YES;
    }
    if (![value isKindOfClass:NSString.class]) return NO;
    if (field[@"choices"]) return [field[@"choices"] containsObject:value];
    return [value length] <= [field[@"maxLength"] unsignedIntegerValue] &&
        (![key isEqual:@"inputDevice"] ||
         [value rangeOfString:[NSString stringWithFormat:@"%C", (unichar)0]].location == NSNotFound);
}

static BOOL JardValidSavedPreference(NSString *key, id value) {
    if ([key hasSuffix:@"Label"]) return [value isKindOfClass:NSString.class] && [value length] <= 128;
    if ([key hasSuffix:@"Code"]) {
        if (![value isKindOfClass:NSNumber.class] || JardBoolean(value)) return NO;
        double code = [value doubleValue];
        return code >= -1 && code <= UINT16_MAX && trunc(code) == code;
    }
    if ([key hasSuffix:@"Flags"] || [key hasSuffix:@"BothSides"]) {
        if (![value isKindOfClass:NSNumber.class] || JardBoolean(value)) return NO;
        double flags = [value doubleValue];
        return flags >= 0 && flags <= shortcutModifiers && trunc(flags) == flags &&
            !([value unsignedLongLongValue] & ~shortcutModifiers);
    }
    if ([key isEqual:@"shortcutEitherSide"] || [key isEqual:@"onboardingEnabledBefore"] ||
        [key isEqual:@"annotationEnabled"] || [key isEqual:@"annotationShowOverlays"]) return JardBoolean(value);
    if ([key isEqual:@"previousRetention"])
        return [value isKindOfClass:NSNumber.class] && !JardBoolean(value) && [@[@1, @7, @30] containsObject:value];
    if ([key isEqual:@"outputScript"])
        return [value isKindOfClass:NSString.class] && (![value length] || [value isAbsolutePath]);
    if ([key isEqual:@"outputScriptApproval"])
        return [value isKindOfClass:NSDictionary.class] && (![value count] ||
            ([value count] == (value[@"grants"] ? 3 : 2) && [value[@"path"] isKindOfClass:NSString.class] &&
             [value[@"path"] isAbsolutePath] && JardValidScriptDigest(value[@"digest"]) &&
             (!value[@"grants"] || JardValidScriptGrants(value[@"grants"]))));
    if ([key isEqual:@"vocabularySoundsLike"]) {
        if (![value isKindOfClass:NSDictionary.class] || [value count] > 32) return NO;
        for (id term in value) {
            id spellings = value[term];
            if (![term isKindOfClass:NSString.class] || ![term length] || [term length] > 240 ||
                ![spellings isKindOfClass:NSArray.class] || [spellings count] > 8) return NO;
            for (id spelling in spellings) if (!JardValidVocabularyText(spelling)) return NO;
        }
        return YES;
    }
    return JardValidPreference(key, value);
}

static NSDictionary *JardDecodePreferences(NSDictionary *saved, NSMutableArray<NSString *> *invalid) {
    NSDictionary *defaults = JardDefaultPreferences();
    NSMutableDictionary *decoded = [defaults mutableCopy];
    for (NSString *key in [defaults.allKeys arrayByAddingObject:@"onboardingEnabledBefore"]) {
        id value = saved[key];
        if (!value) continue;
        if (JardValidSavedPreference(key, value)) decoded[key] = value;
        else [invalid addObject:key];
    }
    if ((saved[@"annotateCode"] || saved[@"annotateFlags"] || saved[@"annotateLabel"]) &&
        (!saved[@"annotateCode"] || !saved[@"annotateFlags"] || !saved[@"annotateLabel"] ||
         ![decoded[@"annotateLabel"] length] || !JardValidAnnotationBinding([decoded[@"annotateCode"] integerValue],
            [decoded[@"annotateFlags"] unsignedLongLongValue]))) [invalid addObject:@"annotateCode"];
    for (NSString *slot in @[@"shortcut", @"raw", @"paste", @"cancel", @"annotate"]) {
        BOOL invalidBinding = NO;
        for (NSString *key in invalid) if ([key hasPrefix:slot]) { invalidBinding = YES; break; }
        if (!invalidBinding) continue;
        for (NSString *key in defaults) if ([key hasPrefix:slot]) decoded[key] = defaults[key];
    }
    return decoded;
}

static BOOL JardWritePreferences(NSDictionary *writes, NSString **error) {
    CFPreferencesSetMultiple((__bridge CFDictionaryRef)writes, NULL, kCFPreferencesCurrentApplication,
        kCFPreferencesCurrentUser, kCFPreferencesAnyHost);
    if (CFPreferencesSynchronize(kCFPreferencesCurrentApplication, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)) return YES;
    *error = @"macOS could not save the preferences.";
    return NO;
}

static NSDictionary *JardCommitPreferences(id saved, NSDictionary *base, NSDictionary *edited,
                                            NSDictionary *application, NSString **error) {
    if (saved && ![saved isKindOfClass:NSDictionary.class]) {
        *error = @"Jard could not read its saved dictation preferences. The existing value was preserved.";
        return nil;
    }
    NSMutableArray<NSString *> *invalid = NSMutableArray.array;
    NSMutableDictionary *decoded = [JardDecodePreferences(saved, invalid) mutableCopy];
    NSMutableDictionary *stored = [saved mutableCopy] ?: NSMutableDictionary.dictionary;
    for (NSString *key in [JardDefaultPreferences().allKeys arrayByAddingObject:@"onboardingEnabledBefore"]) {
        if (base[key] != edited[key] && ![base[key] isEqual:edited[key]]) decoded[key] = edited[key];
        stored[key] = decoded[key];
    }
    NSMutableDictionary *writes = [@{preferencesKey: stored} mutableCopy];
    if (application.count) [writes addEntriesFromDictionary:application];
    if (invalid.count) {
        NSData *original = [NSPropertyListSerialization dataWithPropertyList:saved
            format:NSPropertyListXMLFormat_v1_0 options:0 error:nil];
        if (!original || original.length > UINT32_MAX) {
            *error = @"Jard could not preserve the original dictation preferences.";
            return nil;
        }
        unsigned char digest[CC_SHA256_DIGEST_LENGTH];
        CC_SHA256(original.bytes, (CC_LONG)original.length, digest);
        NSMutableString *recoveryKey = [@"JardSystemDictationRecovery-" mutableCopy];
        for (NSUInteger index = 0; index < sizeof digest; index++) [recoveryKey appendFormat:@"%02x", digest[index]];
        id existing = CFBridgingRelease(CFPreferencesCopyValue((__bridge CFStringRef)recoveryKey,
            kCFPreferencesCurrentApplication, kCFPreferencesCurrentUser, kCFPreferencesAnyHost));
        if (existing && ![existing isEqual:original]) {
            *error = @"Jard could not preserve the original dictation preferences.";
            return nil;
        }
        writes[recoveryKey] = original;
    }
    return JardWritePreferences(writes, error) ? decoded : nil;
}

static NSString *const captureEnabledKey = @"capture.enabled";
static NSString *const captureHotkeyKey = @"capture.hotkey";
static NSString *const captureOverlayKey = @"capture.show_overlays_in_recordings";

BOOL JardChangesAnnotationInputSettings(id changes) {
    return [changes isKindOfClass:NSDictionary.class] && (changes[captureEnabledKey] || changes[captureHotkeyKey]);
}

BOOL JardChangesAnnotationSettings(id changes) {
    return [changes isKindOfClass:NSDictionary.class] && (JardChangesAnnotationInputSettings(changes) || changes[captureOverlayKey]);
}

static const struct { CGEventFlags flag; NSString *name; NSString *alias; } captureModifiers[] = {
    {kCGEventFlagMaskControl, @"control", @"ctrl"}, {kCGEventFlagMaskAlternate, @"option", @"opt"},
    {kCGEventFlagMaskShift, @"shift", nil}, {kCGEventFlagMaskCommand, @"command", @"cmd"},
};

static const struct { CGKeyCode code; NSString *name; NSString *label; } captureKeys[] = {
    {kVK_Space, @"space", @"Space"}, {kVK_Tab, @"tab", @"Tab"}, {kVK_Return, @"return", @"Return"},
    {kVK_ANSI_KeypadEnter, @"enter", @"Enter"}, {kVK_Delete, @"backspace", @"⌫"},
    {kVK_ForwardDelete, @"delete", @"⌦"}, {kVK_Home, @"home", @"Home"}, {kVK_End, @"end", @"End"},
    {kVK_PageUp, @"pageup", @"Page Up"}, {kVK_PageDown, @"pagedown", @"Page Down"}, {kVK_Help, @"help", @"Help"},
    {kVK_LeftArrow, @"left", @"←"}, {kVK_RightArrow, @"right", @"→"}, {kVK_UpArrow, @"up", @"↑"},
    {kVK_DownArrow, @"down", @"↓"}, {kVK_ANSI_0, @"0", @"0"}, {kVK_ANSI_1, @"1", @"1"},
    {kVK_ANSI_2, @"2", @"2"}, {kVK_ANSI_3, @"3", @"3"}, {kVK_ANSI_4, @"4", @"4"}, {kVK_ANSI_5, @"5", @"5"},
    {kVK_ANSI_6, @"6", @"6"}, {kVK_ANSI_7, @"7", @"7"}, {kVK_ANSI_8, @"8", @"8"}, {kVK_ANSI_9, @"9", @"9"},
};

static BOOL JardASCIIShortcutLetter(unichar letter) {
    return (letter >= 'a' && letter <= 'z') || (letter >= 'A' && letter <= 'Z');
}

static NSInteger JardShortcutNumber(NSString *name, NSString *prefix, NSInteger min, NSInteger max) {
    if (![name hasPrefix:prefix]) return -1;
    NSString *digits = [name substringFromIndex:prefix.length];
    NSInteger value = digits.integerValue;
    return value >= min && value <= max && [digits isEqual:[@(value) stringValue]] ? value : -1;
}

static NSInteger JardParseCaptureKey(NSString *name, CGEventFlags flags, const JardShortcutLayout *layout, NSString **label) {
    const CGKeyCode letters[] = {kVK_ANSI_A, kVK_ANSI_B, kVK_ANSI_C, kVK_ANSI_D, kVK_ANSI_E, kVK_ANSI_F, kVK_ANSI_G,
        kVK_ANSI_H, kVK_ANSI_I, kVK_ANSI_J, kVK_ANSI_K, kVK_ANSI_L, kVK_ANSI_M, kVK_ANSI_N, kVK_ANSI_O, kVK_ANSI_P,
        kVK_ANSI_Q, kVK_ANSI_R, kVK_ANSI_S, kVK_ANSI_T, kVK_ANSI_U, kVK_ANSI_V, kVK_ANSI_W, kVK_ANSI_X, kVK_ANSI_Y, kVK_ANSI_Z};
    unichar letter = name.length == 1 ? [name characterAtIndex:0] : 0;
    if (JardASCIIShortcutLetter(letter)) {
        *label = name.uppercaseString;
        return JardShortcutLetterCode(layout, letter, (flags & kCGEventFlagMaskCommand) != 0, letters[letter - 'a']);
    }
    for (NSUInteger i = 0; i < sizeof captureKeys / sizeof *captureKeys; ++i) {
        if ([name isEqual:captureKeys[i].name]) {
            *label = captureKeys[i].label;
            return captureKeys[i].code;
        }
    }
    NSInteger function = JardShortcutNumber(name, @"f", 1, sizeof shortcutFunctionKeys / sizeof *shortcutFunctionKeys);
    if (function > 0) {
        *label = name.uppercaseString;
        return shortcutFunctionKeys[function - 1];
    }
    NSInteger code = JardShortcutNumber(name, @"keycode-", 0, 127);
    *label = [NSString stringWithFormat:@"Key %ld", (long)code];
    return code;
}

static NSDictionary *JardParseCaptureHotkey(id value, const JardShortcutLayout *layout) {
    if (![value isKindOfClass:NSString.class] || [value length] > 128) return nil;
    NSArray *parts = [[value lowercaseString] componentsSeparatedByString:@"+"];
    NSString *keyName = [parts.lastObject stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    CGEventFlags flags = 0;
    for (NSUInteger i = 0; i + 1 < parts.count; ++i) {
        NSString *name = [parts[i] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        if ([name isEqual:@"alt"]) name = @"option";
        CGEventFlags flag = 0;
        for (NSUInteger m = 0; m < sizeof captureModifiers / sizeof *captureModifiers; ++m) {
            if ([name isEqual:captureModifiers[m].name] || [name isEqual:captureModifiers[m].alias]) {
                flag = captureModifiers[m].flag;
                break;
            }
        }
        if (!flag || (flags & flag)) return nil;
        flags |= flag;
    }
    NSString *label = nil;
    NSInteger code = JardParseCaptureKey(keyName, flags, layout, &label);
    if (!JardValidAnnotationBinding(code, flags)) return nil;
    return @{@"annotateCode": @(code), @"annotateFlags": @(flags),
             @"annotateLabel": [JardModifierSymbols(flags, 0) stringByAppendingString:label]};
}

static NSString *JardFormatCaptureHotkey(NSDictionary *settings) {
    NSMutableArray *parts = NSMutableArray.array;
    CGEventFlags flags = [settings[@"annotateFlags"] unsignedLongLongValue];
    for (NSUInteger m = 0; m < sizeof captureModifiers / sizeof *captureModifiers; ++m) {
        if (flags & captureModifiers[m].flag) [parts addObject:captureModifiers[m].name];
    }
    CGKeyCode code = [settings[@"annotateCode"] unsignedShortValue];
    unichar letter = JardShortcutLetter(settings[@"annotateLabel"]);
    if (JardASCIIShortcutLetter(letter)) {
        [parts addObject:[[NSString stringWithCharacters:&letter length:1] lowercaseString]];
    } else {
        NSString *name = nil;
        for (NSUInteger i = 0; i < sizeof captureKeys / sizeof *captureKeys; ++i) {
            if (code == captureKeys[i].code) { name = captureKeys[i].name; break; }
        }
        NSInteger function = functionIndex(code);
        [parts addObject:name ?: (function ? [NSString stringWithFormat:@"f%ld", (long)function] :
            [NSString stringWithFormat:@"keycode-%u", code])];
    }
    return [parts componentsJoinedByString:@"+"];
}

static NSDictionary *JardValidatePreferenceBatch(id requested, const JardShortcutLayout *layout,
                                                NSDictionary **application, NSString **error) {
    NSDictionary *catalog = JardPreferenceCatalog();
    if (![requested isKindOfClass:NSDictionary.class] || ![requested count] || [requested count] > catalog.count + 4) {
        *error = @"Invalid settings update.";
        return nil;
    }
    NSString *prefix = @"dictation.";
    NSMutableDictionary *changes = NSMutableDictionary.dictionary;
    NSMutableDictionary *app = NSMutableDictionary.dictionary;
    for (NSString *full in requested) {
        id value = requested[full];
        if ([full isEqual:JardInterfaceLanguageSettingKey]) {
            NSString *language = JardInterfaceLanguageChoice(value);
            if (!language) {
                *error = @"Choose an available interface language or system.";
                return nil;
            }
            app[JardInterfaceLanguagePreferenceKey] = language;
            continue;
        }
        if ([full isEqual:JardShowDockIconSettingKey] && JardBoolean(value)) {
            app[JardShowDockIconPreferenceKey] = value;
            continue;
        }
        if ([full isEqual:captureEnabledKey] && JardBoolean(value)) {
            changes[@"annotationEnabled"] = value;
            continue;
        }
        if ([full isEqual:captureOverlayKey] && JardBoolean(value)) {
            changes[@"annotationShowOverlays"] = value;
            continue;
        }
        if ([full isEqual:captureHotkeyKey]) {
            NSDictionary *binding = JardParseCaptureHotkey(value, layout);
            if (!binding) {
                *error = @"Invalid Annotate shortcut. Use a chord such as option+shift+s or a function key.";
                return nil;
            }
            [changes addEntriesFromDictionary:binding];
            continue;
        }
        NSString *key = [full hasPrefix:prefix] ? [full substringFromIndex:prefix.length] : nil;
        if (!key || !catalog[key] || !JardValidPreference(key, value)) {
            *error = [NSString stringWithFormat:@"Invalid, unknown, or read-only setting %@.", full];
            return nil;
        }
        changes[key] = value;
    }
    *application = app;
    return changes;
}

static BOOL JardValidateCapturePreferences(NSDictionary *base, NSDictionary *changes, const JardShortcutLayout *layout, NSString **error) {
    BOOL enabling = [changes[@"annotationEnabled"] boolValue] && ![base[@"annotationEnabled"] boolValue];
    if (!enabling && !changes[@"annotateCode"]) return YES;
    NSMutableDictionary *edited = [base mutableCopy];
    [edited addEntriesFromDictionary:changes];
    if (!JardAnnotationChordConflicts(edited, layout)) return YES;
    *error = @"The Annotate shortcut is already in use by Jaro.";
    return NO;
}

int JardLockDictationPreferences(BOOL exclusive) {
    NSString *identifier = NSBundle.mainBundle.bundleIdentifier;
    if (!identifier.length || [identifier containsString:@"/"]) return -1;
    NSString *folder = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Preferences"];
    NSString *name = [NSString stringWithFormat:@".%@.jard-settings.lock", identifier];
    NSString *path = [folder stringByAppendingPathComponent:name];
    int fd = open(path.fileSystemRepresentation, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
    if (fd < 0) return -1;
    struct stat info;
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != geteuid() || (info.st_mode & 077)) {
        close(fd);
        return -1;
    }
    int type = exclusive ? LOCK_EX : LOCK_SH;
    for (NSUInteger attempt = 0; attempt < (exclusive ? 1 : 100); attempt++) {
        if (flock(fd, type | LOCK_NB) == 0) return fd;
        if (errno != EWOULDBLOCK) break;
        if (!exclusive) usleep(20000);
    }
    close(fd);
    return -1;
}

int JardApplyOfflineDictationSettings(BOOL annotationManaged) {
    unsigned char input[56001];
    size_t count = fread(input, 1, sizeof(input), stdin);
    NSString *error = nil;
    id requested = nil;
    if (ferror(stdin) || count > 56000) error = @"The settings document exceeds 56,000 bytes or could not be read.";
    else requested = [NSJSONSerialization JSONObjectWithData:[NSData dataWithBytes:input length:count]
                                                       options:0 error:nil];
    if (!error && annotationManaged && JardChangesAnnotationSettings(requested))
        error = @"Screen annotation is managed by your organization.";
    JardShortcutLayout layout = {0};
    if (!error && JardChangesAnnotationInputSettings(requested)) layout = JardCurrentShortcutLayout();
    NSDictionary *application = nil;
    NSDictionary *changes = error ? nil : JardValidatePreferenceBatch(requested, &layout, &application, &error);
    int lock = changes ? JardLockDictationPreferences(YES) : -1;
    if (changes && lock < 0) error = @"Close Jard before applying settings offline.";
    if (lock >= 0) {
        NSString *identifier = NSBundle.mainBundle.bundleIdentifier;
        pid_t current = NSProcessInfo.processInfo.processIdentifier;
        for (NSRunningApplication *application in [NSRunningApplication runningApplicationsWithBundleIdentifier:identifier]) {
            if (application.processIdentifier != current && !application.terminated) {
                error = @"Close Jard before applying settings offline.";
                break;
            }
        }
        if (!error && !changes.count) {
            JardWritePreferences(application, &error);
        } else if (!error) {
            id saved = [NSUserDefaults.standardUserDefaults objectForKey:preferencesKey];
            if (saved && ![saved isKindOfClass:NSDictionary.class]) {
                error = @"Jard could not read its saved dictation preferences. The existing value was preserved.";
            } else {
                NSDictionary *base = JardDecodePreferences(saved, NSMutableArray.array);
                NSMutableDictionary *edited = [base mutableCopy];
                [edited addEntriesFromDictionary:changes];
                if (changes[@"vocabulary"]) JardPruneVocabularyAliases(edited);
                if ([changes[@"retention"] integerValue] > 0) edited[@"previousRetention"] = changes[@"retention"];
                if (JardValidateCapturePreferences(base, changes, &layout, &error))
                    JardCommitPreferences(saved, base, edited, application, &error);
            }
        }
        close(lock);
    }
    NSDictionary *reply = error ? @{@"error": error} : requested;
    NSData *json = [NSJSONSerialization dataWithJSONObject:reply options:0 error:nil];
    if (json) fwrite(json.bytes, 1, json.length, stdout);
    fputc('\n', stdout);
    return error ? 1 : 0;
}

- (BOOL)applyPreferences:(NSDictionary *)changes error:(NSString **)error {
    return [self applyPreferences:changes application:nil error:error];
}

- (BOOL)applyPreferences:(NSDictionary *)changes application:(NSDictionary *)application error:(NSString **)error {
    NSMutableDictionary *edited = [self.settings mutableCopy];
    NSString *onboarding = changes[@"onboarding"];
    BOOL enteredReady = [onboarding isEqual:@"ready"] && [edited[@"onboarding"] isEqual:@"practice"];
    if ([onboarding isEqual:@"access"] && !edited[@"onboardingEnabledBefore"]) {
        edited[@"onboardingEnabledBefore"] = edited[@"enabled"];
        edited[@"enabled"] = @YES;
    } else if ([onboarding isEqual:@"later"] || [onboarding isEqual:@"done"]) {
        if ([onboarding isEqual:@"later"] && edited[@"onboardingEnabledBefore"])
            edited[@"enabled"] = edited[@"onboardingEnabledBefore"];
        [edited removeObjectForKey:@"onboardingEnabledBefore"];
    }
    [edited addEntriesFromDictionary:changes];
    if (changes[@"vocabulary"]) JardPruneVocabularyAliases(edited);
    if ([changes[@"retention"] integerValue] > 0) edited[@"previousRetention"] = changes[@"retention"];
    if (![self saveSettings:edited application:application error:error]) return NO;
    [self refresh];
    if (enteredReady) [self playSound:@"success"];
    return YES;
}

static NSArray *JardVocabularyEntries(NSDictionary *settings) {
    NSMutableArray *entries = NSMutableArray.array;
    NSDictionary *spellings = settings[@"vocabularySoundsLike"];
    for (NSString *term in JardVocabulary(settings[@"vocabulary"])) {
        [entries addObject:@{@"term": term, @"soundsLike": spellings[JardVocabularyKey(term)] ?: @[]}];
    }
    return entries;
}

// The editor sends only bounded lists. Normalize the same way as the CLI,
// including when two rows differ only by case.
static NSDictionary *JardIndexVocabulary(id entries, NSMutableArray *order, NSString **error) {
    if (![entries isKindOfClass:NSArray.class] || [entries count] > 32) {
        *error = @"Jaro can use up to 32 vocabulary terms.";
        return nil;
    }
    NSMutableDictionary *indexed = NSMutableDictionary.dictionary;
    for (id row in entries) {
        id term = [row isKindOfClass:NSDictionary.class] ? row[@"term"] : nil;
        if (!JardValidVocabularyText(term) || ![term stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet].length) {
            *error = @"Enter one term of up to 80 characters.";
            return nil;
        }
        term = [term stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        NSString *key = JardVocabularyKey(term);
        id spellings = row[@"soundsLike"];
        if (![spellings isKindOfClass:NSArray.class] || [spellings count] > 8) {
            *error = @"Provide up to eight alternate spellings per term.";
            return nil;
        }
        NSDictionary *previous = indexed[key];
        NSMutableArray *merged = [previous[@"soundsLike"] mutableCopy] ?: NSMutableArray.array;
        NSMutableSet *seen = [NSMutableSet setWithObject:key];
        for (NSString *word in merged) [seen addObject:JardVocabularyKey(word)];
        for (id value in spellings) {
            if (!JardValidVocabularyText(value)) { *error = @"Enter alternate spellings of up to 80 characters."; return nil; }
            NSString *word = [value stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
            if (word.length < 2) { *error = @"An alternate spelling needs at least two characters."; return nil; }
            NSString *wordKey = JardVocabularyKey(word);
            if ([seen containsObject:wordKey]) continue;
            [seen addObject:wordKey];
            [merged addObject:word];
        }
        if (merged.count > 8) { *error = @"Provide up to eight alternate spellings per term."; return nil; }
        if (!previous) [order addObject:key];
        indexed[key] = @{@"term": previous[@"term"] ?: term, @"soundsLike": merged};
    }
    return indexed;
}

static void JardInsertVocabularyTerm(NSMutableArray *order, NSArray *desired, NSUInteger index) {
    for (NSUInteger cursor = index; cursor > 0; --cursor) {
        NSUInteger position = [order indexOfObject:desired[cursor - 1]];
        if (position != NSNotFound) { [order insertObject:desired[index] atIndex:position + 1]; return; }
    }
    for (NSUInteger cursor = index + 1; cursor < desired.count; ++cursor) {
        NSUInteger position = [order indexOfObject:desired[cursor]];
        if (position != NSNotFound) { [order insertObject:desired[index] atIndex:position]; return; }
    }
    [order addObject:desired[index]];
}

- (void)editVocabulary:(NSDictionary *)parameters reply:(JardDictationReply)reply {
    NSString *error = nil;
    NSMutableArray *desiredOrder = NSMutableArray.array, *order = NSMutableArray.array;
    NSDictionary *base = JardIndexVocabulary(parameters[@"base"], nil, &error);
    NSDictionary *desired = base ? JardIndexVocabulary(parameters[@"entries"], desiredOrder, &error) : nil;
    if (!desired) { reply(nil, error); return; }
    NSDictionary *saved = JardIndexVocabulary(JardVocabularyEntries(self.settings), order, &error);
    if (!saved) { reply(nil, error); return; }
    NSMutableDictionary *merged = [saved mutableCopy];
    for (NSString *key in base) {
        NSDictionary *previous = base[key], *candidate = desired[key], *current = saved[key];
        if (!current) continue;
        if (!candidate) {
            [merged removeObjectForKey:key];
            [order removeObject:key];
        } else {
            merged[key] = @{
                @"term": [previous[@"term"] isEqual:candidate[@"term"]] ? current[@"term"] : candidate[@"term"],
                @"soundsLike": [previous[@"soundsLike"] isEqual:candidate[@"soundsLike"]] ? current[@"soundsLike"] : candidate[@"soundsLike"],
            };
        }
    }
    for (NSUInteger index = 0; index < desiredOrder.count; ++index) {
        NSString *key = desiredOrder[index];
        if (base[key]) continue;
        if (!saved[key]) {
            JardInsertVocabularyTerm(order, desiredOrder, index);
            merged[key] = desired[key];
        } else {
            // A CLI addition of the same term during the edit keeps both
            // sets of spellings, with the original saved capitalization.
            NSDictionary *joined = JardIndexVocabulary(@[saved[key], desired[key]], nil, &error);
            if (!joined) { reply(nil, error); return; }
            merged[key] = joined[key];
        }
    }
    if (![merged isEqual:saved]) {
        NSMutableArray *terms = NSMutableArray.array;
        NSMutableDictionary *aliases = NSMutableDictionary.dictionary;
        for (NSString *key in order) {
            [terms addObject:merged[key][@"term"]];
            if ([merged[key][@"soundsLike"] count]) aliases[key] = merged[key][@"soundsLike"];
        }
        NSString *original = self.settings[@"vocabulary"];
        NSString *vocabulary = original;
        if (![terms isEqual:JardVocabulary(original)]) {
            // Preserve legacy lines past the active model limit. They can
            // become active when a more recent term is removed.
            for (NSString *line in [original componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet]) {
                NSString *term = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
                NSString *key = JardVocabularyKey(term);
                if (term.length && !saved[key] && !merged[key]) [terms addObject:line];
            }
            vocabulary = [terms componentsJoinedByString:@"\n"];
        }
        if (order.count > 32 || !JardValidPreference(@"vocabulary", vocabulary)) {
            reply(nil, @"Jaro can use up to 32 vocabulary terms.");
            return;
        }
        if (![self applyPreferences:@{@"vocabulary": vocabulary, @"vocabularySoundsLike": aliases} error:&error]) {
            reply(nil, error);
            return;
        }
    }
    reply([self status], nil);
}

- (void)controlVocabulary:(NSDictionary *)parameters reply:(JardDictationReply)reply {
    NSString *action = parameters[@"action"];
    if ([action isEqual:@"vocabulary-list"]) {
        reply(@{@"entries": JardVocabularyEntries(self.settings)}, nil);
        return;
    }
    BOOL add = [action isEqual:@"vocabulary-add"];
    if (!add && ![action isEqual:@"vocabulary-remove"]) { reply(nil, @"Unknown vocabulary command."); return; }
    id supplied = parameters[@"term"];
    if (!JardValidVocabularyText(supplied)) { reply(nil, @"Enter one term of up to 80 characters."); return; }
    NSString *term = [supplied stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    if (!term.length) { reply(nil, @"Enter one term of up to 80 characters."); return; }
    NSString *key = JardVocabularyKey(term);
    NSString *original = self.settings[@"vocabulary"];
    NSMutableArray *lines = [[original componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet] mutableCopy];
    NSIndexSet *matches = [lines indexesOfObjectsPassingTest:^BOOL(NSString *line, NSUInteger index, BOOL *stop) {
        return [JardVocabularyKey([line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet]) isEqual:key];
    }];
    NSMutableDictionary *aliases = [self.settings[@"vocabularySoundsLike"] mutableCopy];
    NSArray *existing = aliases[key] ?: @[];
    if (add) {
        id requested = parameters[@"soundsLike"] ?: @[];
        if (![requested isKindOfClass:NSArray.class] || [requested count] > 8) {
            reply(nil, @"Provide up to eight alternate spellings."); return;
        }
        NSMutableArray *merged = [existing mutableCopy];
        NSMutableSet *known = [NSMutableSet setWithObject:key];
        for (NSString *alias in existing) [known addObject:JardVocabularyKey(alias)];
        for (id value in requested) {
            if (!JardValidVocabularyText(value)) { reply(nil, @"Enter alternate spellings of up to 80 characters."); return; }
            NSString *alias = [value stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
            if (alias.length < 2) { reply(nil, @"An alternate spelling needs at least two characters."); return; }
            NSString *aliasKey = JardVocabularyKey(alias);
            if ([known containsObject:aliasKey]) continue;
            [known addObject:aliasKey];
            [merged addObject:alias];
        }
        if (merged.count > 8) { reply(nil, @"A term can have up to eight alternate spellings."); return; }
        NSArray *active = JardVocabulary(original);
        if (active.count >= 32 && [active indexOfObjectPassingTest:^BOOL(NSString *word, NSUInteger index, BOOL *stop) {
            return [JardVocabularyKey(word) isEqual:key];
        }] == NSNotFound) { reply(nil, @"Jaro can use up to 32 vocabulary terms."); return; }
        if (!matches.count) {
            if (!lines.lastObject || ![lines.lastObject length]) [lines removeLastObject];
            [lines addObject:term];
        }
        if (merged.count) aliases[key] = merged;
    } else {
        [lines removeObjectsAtIndexes:matches];
        [aliases removeObjectForKey:key];
    }
    NSString *updated = [lines componentsJoinedByString:@"\n"];
    // Only edit the text field when a line was added or removed.
    if (add ? matches.count > 0 : matches.count == 0) updated = original;
    BOOL changed = ![updated isEqual:original] || ![aliases isEqual:self.settings[@"vocabularySoundsLike"]];
    NSString *error = nil;
    if (changed && (!JardValidPreference(@"vocabulary", updated) ||
        ![self applyPreferences:@{@"vocabulary": updated, @"vocabularySoundsLike": aliases} error:&error])) {
        reply(nil, error ?: @"The vocabulary list is too large.");
        return;
    }
    reply(@{@"changed": @(changed), @"entries": JardVocabularyEntries(self.settings)}, nil);
}

- (void)controlSettings:(NSDictionary *)parameters reply:(JardDictationReply)reply {
    NSString *action = parameters[@"action"];
    if ([action hasPrefix:@"vocabulary-"]) { [self controlVocabulary:parameters reply:reply]; return; }
    NSString *prefix = @"dictation.";
    NSDictionary *catalog = JardPreferenceCatalog();
    NSMutableDictionary *result = NSMutableDictionary.dictionary;
    if ([action isEqual:@"settings-list"]) {
        for (NSString *key in catalog) result[[prefix stringByAppendingString:key]] = catalog[key];
        result[captureEnabledKey] = @{@"type": @"boolean", @"readOnly": @(!self.annotationEvents)};
        result[captureHotkeyKey] = @{@"type": @"string", @"readOnly": @(!self.annotationEvents)};
        result[captureOverlayKey] = @{@"type": @"boolean", @"readOnly": @(!self.annotationEvents)};
        NSArray *languages = [[JardInterfaceLanguageStatus()[@"choices"] allKeys] sortedArrayUsingSelector:@selector(compare:)];
        result[JardInterfaceLanguageSettingKey] = @{@"type": @"string", @"choices": [@[@"system"] arrayByAddingObjectsFromArray:languages]};
        result[JardShowDockIconSettingKey] = @{@"type": @"boolean"};
    } else if ([action isEqual:@"settings-get"]) {
        NSString *requested = parameters[@"key"];
        if (![requested isKindOfClass:NSString.class] || !requested.length) {
            reply(nil, @"Provide a setting key or prefix.");
            return;
        }
        for (NSString *key in catalog) {
            NSString *full = [prefix stringByAppendingString:key];
            if ([full isEqual:requested]) { reply(@{full: self.settings[key]}, nil); return; }
            if ([full hasPrefix:requested]) result[full] = self.settings[key];
        }
        if ([captureEnabledKey hasPrefix:requested]) result[captureEnabledKey] = @(self.annotationEnabled);
        if ([captureHotkeyKey hasPrefix:requested]) result[captureHotkeyKey] = JardFormatCaptureHotkey(self.settings);
        if ([captureOverlayKey hasPrefix:requested]) result[captureOverlayKey] = self.settings[@"annotationShowOverlays"];
        if ([JardInterfaceLanguageSettingKey hasPrefix:requested])
            result[JardInterfaceLanguageSettingKey] = JardInterfaceLanguageStatus()[@"selected"];
        if ([JardShowDockIconSettingKey hasPrefix:requested]) result[JardShowDockIconSettingKey] = @(JardShowDockIconEnabled());
        if (!result.count) { reply(nil, @"No settings match that key or prefix."); return; }
    } else if ([action isEqual:@"settings-apply"]) {
        id requested = parameters[@"changes"];
        BOOL annotation = JardChangesAnnotationSettings(requested);
        if (annotation && !self.annotationEvents) { reply(nil, @"Screen annotation is unavailable."); return; }
        BOOL input = JardChangesAnnotationInputSettings(requested);
        if (annotation && (self.closed || self.updatePaused || (input &&
            (self.annotationActive || self.annotationKeyDown || self.hasActivity || self.choosingShortcut)))) {
            reply(nil, @"Finish the current recording or shortcut edit first.");
            return;
        }
        const JardShortcutLayout *layout = _shortcutLayoutPending ? &_pendingShortcutLayout : _hasShortcutLayout ? &_shortcutLayout : NULL;
        NSString *error = nil;
        NSDictionary *application = nil;
        NSDictionary *changes = JardValidatePreferenceBatch(requested, layout, &application, &error);
        if (!changes || (input && !JardValidateCapturePreferences(self.settings, changes, layout, &error))) {
            reply(nil, error);
            return;
        }
        BOOL saved = changes.count ? [self applyPreferences:changes application:application error:&error] :
            JardWritePreferences(application, &error);
        reply(saved ? requested : nil, error);
        return;
    } else {
        reply(nil, @"Unknown settings command.");
        return;
    }
    reply(result, nil);
}

static BOOL JardEmbeddedConfig(NSString *path) {
    return [@[@"jaro.config.js", @"jaro.config.mjs", @"jaro.config.ts", @"jaro.config.mts"]
        containsObject:path.lastPathComponent ?: @""];
}

- (void)saveOutputScriptPath:(NSString *)path reply:(JardDictationReply)reply {
    NSMutableDictionary *edited = [self.settings mutableCopy];
    edited[@"outputScript"] = path;
    if (![path isEqual:self.settings[@"outputScript"]]) edited[@"outputScriptApproval"] = @{};
    NSString *failure = nil;
    if (![self saveSettings:edited error:&failure]) { reply(nil, failure); return; }
    reply([self status], nil);
}

- (void)createOutputConfig:(JardDictationReply)reply {
    if (self.closed || self.updatePaused || [self.settings[@"outputScript"] length]) {
        reply(nil, @"A script is already selected or Jaro is restarting.");
        return;
    }
    NSUInteger revision = self.outputScriptRevision;
    self.send(@"jard.createOutputConfig", @{}, ^(id result, NSString *error) {
        if (error || self.closed || self.updatePaused || revision != self.outputScriptRevision) {
            reply(nil, error ?: @"The script selection changed while preparing its configuration.");
            return;
        }
        NSString *path = [result isKindOfClass:NSDictionary.class] ? result[@"path"] : nil;
        if (![path isKindOfClass:NSString.class] || !path.isAbsolutePath || !JardEmbeddedConfig(path)) {
            reply(nil, @"Jaro could not prepare the configuration.");
            return;
        }
        [self saveOutputScriptPath:path reply:reply];
    });
}

- (void)chooseOutputScript:(JardDictationReply)reply {
    NSOpenPanel *panel = NSOpenPanel.openPanel;
    panel.title = @"Choose a dictation script or catalog";
    panel.message = @"Select an executable, a JSON catalog, or a jaro.config.js or jaro.config.ts file.";
    panel.allowsMultipleSelection = NO;
    panel.canChooseDirectories = NO;
    [panel beginWithCompletionHandler:^(NSModalResponse response) {
        if (response == NSModalResponseOK) {
            NSString *path = panel.URL.URLByResolvingSymlinksInPath.path;
            BOOL config = [path.pathExtension.lowercaseString isEqual:@"json"] || JardEmbeddedConfig(path);
            if (!path || !(config ? [NSFileManager.defaultManager isReadableFileAtPath:path] :
                           [NSFileManager.defaultManager isExecutableFileAtPath:path])) {
                reply(nil, @"Choose an executable script or a readable transformation configuration.");
                return;
            }
            [self saveOutputScriptPath:path reply:reply];
        } else {
            reply([self status], nil);
        }
    }];
}

static NSString *JardOutputPolicy(id result) {
    id policy = [result isKindOfClass:NSDictionary.class] ? result[@"policy"] : nil;
    return [@[@"loopback", @"disabled"] containsObject:policy ?: @""] ? policy : @"normal";
}

- (void)inspectOutputScriptFresh:(BOOL)fresh reply:(JardDictationReply)reply {
    NSString *path = self.settings[@"outputScript"];
    NSUInteger revision = self.outputScriptRevision;
    if (!path.length) {
        self.send(@"jard.findOutputConfig", @{}, ^(id result, NSString *error) {
            if (self.closed || revision != self.outputScriptRevision) {
                reply(nil, @"The script selection changed.");
                return;
            }
            NSDictionary *discovery = [result isKindOfClass:NSDictionary.class] ? result : @{};
            NSString *name = discovery[@"name"];
            NSString *policy = JardOutputPolicy(discovery);
            NSString *message = error ?: discovery[@"message"];
            if (message) reply(@{@"state": @"unavailable", @"message": message, @"policy": policy}, nil);
            else if ([name isKindOfClass:NSString.class] && name.length)
                reply(@{@"state": @"found", @"file": name, @"policy": policy}, nil);
            else reply(@{@"state": @"none", @"policy": policy}, nil);
        });
        return;
    }
    self.send(@"jard.inspectOutputScript", @{@"path": path, @"fresh": @(fresh)}, ^(id result, NSString *error) {
        if (self.closed || revision != self.outputScriptRevision) {
            reply(nil, @"The selected script changed. Check its version again.");
            return;
        }
        NSDictionary *review = [result isKindOfClass:NSDictionary.class] ? result : @{};
        id digest = review[@"digest"];
        NSString *policy = JardOutputPolicy(review);
        if (error || !JardValidScriptDigest(digest)) {
            self.outputScriptReview = nil;
            NSString *message = [review[@"message"] isKindOfClass:NSString.class] ? review[@"message"] : nil;
            NSArray *transforms = [review[@"transforms"] isKindOfClass:NSArray.class] ? review[@"transforms"] : @[];
            reply(@{@"state": @"unavailable", @"message": error ?: message ?: @"Jaro could not inspect the selected script.",
                @"transforms": transforms, @"stale": @(transforms.count > 0), @"policy": policy}, nil);
            return;
        }
        if (![self.outputScriptReview[@"digest"] isEqual:digest]) {
            self.outputScriptReview = @{@"digest": digest, @"token": NSUUID.UUID.UUIDString};
        }
        BOOL approved = [JardApprovedScriptDigest(self.settings) isEqual:digest];
        NSArray *transforms = [result[@"transforms"] isKindOfClass:NSArray.class] ? result[@"transforms"] : @[];
        NSString *kind = [result[@"kind"] isEqual:@"embedded"] ? @"embedded" : @"executable";
        id reported = result[@"permissions"];
        NSMutableDictionary *permissions = [reported isKindOfClass:NSDictionary.class] ? [reported mutableCopy] : NSMutableDictionary.dictionary;
        if ([kind isEqual:@"embedded"]) permissions[@"pageContext"] = @YES;
        NSDictionary *grants = approved ? JardApprovedScriptGrants(self.settings) : @{};
        reply(@{@"state": approved ? @"approved" : @"review", @"version": [digest substringToIndex:12],
            @"token": self.outputScriptReview[@"token"], @"transforms": transforms, @"kind": kind,
            @"permissions": permissions, @"grants": grants, @"policy": policy}, nil);
    });
}

- (void)approveOutputScript:(id)token grants:(id)requested reply:(JardDictationReply)reply {
    if (self.closed || self.updatePaused || ![token isKindOfClass:NSString.class] ||
        ![self.outputScriptReview[@"token"] isEqual:token]) {
        reply(nil, @"Check the selected script's version before approving it.");
        return;
    }
    if (requested && !JardValidScriptGrants(requested)) {
        reply(nil, @"Choose valid transformation permissions.");
        return;
    }
    [self inspectOutputScriptFresh:YES reply:^(id result, NSString *error) {
        if (self.updatePaused || error || ![result[@"token"] isEqual:token]) {
            reply(nil, error ?: @"The selected script changed. Review its current version before approving it.");
            return;
        }
        NSDictionary *permissions = result[@"permissions"];
        NSDictionary *selection = requested ?: result[@"grants"];
        NSMutableDictionary *grants = [NSMutableDictionary dictionary];
        NSString *policy = JardOutputPolicy(result);
        for (NSString *key in @[@"network", @"programs", @"pageContext"]) {
            if ([policy isEqual:@"loopback"] && [key isEqual:@"programs"]) {
                if ([result[@"grants"][key] isEqual:@YES]) grants[key] = @YES;
                else if ([selection[key] isEqual:@YES]) {
                    reply(nil, @"Your organization has disabled transformation programs.");
                    return;
                }
                continue;
            }
            if (![selection[key] isEqual:@YES]) continue;
            if (![permissions[key] isEqual:@YES] || ![result[@"kind"] isEqual:@"embedded"]) {
                reply(nil, @"The configuration does not request that permission.");
                return;
            }
            grants[key] = @YES;
        }
        if (![result[@"state"] isEqual:@"approved"] || ![result[@"grants"] isEqual:grants]) {
            NSMutableDictionary *edited = [self.settings mutableCopy];
            NSMutableDictionary *approval = [@{@"path": self.settings[@"outputScript"],
                @"digest": self.outputScriptReview[@"digest"]} mutableCopy];
            if (grants.count) approval[@"grants"] = [grants copy];
            edited[@"outputScriptApproval"] = approval;
            NSString *failure = nil;
            if (![self saveSettings:edited error:&failure]) { reply(nil, failure); return; }
        }
        reply(@{@"state": @"approved", @"version": result[@"version"], @"token": result[@"token"],
            @"transforms": result[@"transforms"] ?: @[], @"kind": result[@"kind"] ?: @"executable",
            @"permissions": permissions ?: @{}, @"grants": grants, @"policy": policy}, nil);
    }];
}

static NSDictionary *JardProfileApplication(NSURL *url) {
    if (![url.pathExtension.lowercaseString isEqual:@"app"]) return nil;
    NSString *identifier = [NSBundle bundleWithURL:url].bundleIdentifier;
    if (!identifier.length || [identifier isEqual:NSBundle.mainBundle.bundleIdentifier]) return nil;
    NSString *name = [NSFileManager.defaultManager displayNameAtPath:url.path];
    if ([name.lowercaseString hasSuffix:@".app"]) name = name.stringByDeletingPathExtension;
    return @{@"id":identifier, @"name":name};
}

static NSArray *JardProfileApplications(void) {
    NSFileManager *files = NSFileManager.defaultManager;
    NSMutableDictionary *applications = [NSMutableDictionary dictionary];
    NSArray *roots = @[@"/Applications", @"/System/Applications", @"/System/Library/CoreServices/Applications",
                       [NSHomeDirectory() stringByAppendingPathComponent:@"Applications"]];
    for (NSString *root in roots) {
        NSDirectoryEnumerator *entries = [files enumeratorAtURL:[NSURL fileURLWithPath:root]
            includingPropertiesForKeys:nil options:NSDirectoryEnumerationSkipsPackageDescendants |
                NSDirectoryEnumerationSkipsHiddenFiles errorHandler:nil];
        for (NSURL *url in entries) {
            if (![url.pathExtension.lowercaseString isEqual:@"app"]) continue;
            [entries skipDescendants];
            NSDictionary *app = JardProfileApplication(url);
            if (app) applications[app[@"id"]] = app;
        }
    }
    return [applications.allValues sortedArrayUsingDescriptors:@[
        [NSSortDescriptor sortDescriptorWithKey:@"name" ascending:YES selector:@selector(localizedCaseInsensitiveCompare:)],
        [NSSortDescriptor sortDescriptorWithKey:@"id" ascending:YES]]];
}

- (void)request:(NSDictionary *)parameters reply:(JardDictationReply)reply {
    if (![parameters isKindOfClass:NSDictionary.class]) { reply(nil, @"Invalid dictation settings."); return; }
    NSString *action = parameters[@"action"];
    if ([action isEqual:@"status"]) {
        [self refresh];
    } else if ([action isEqual:@"set"]) {
        NSString *key = parameters[@"key"];
        id value = parameters[@"value"];
        if (![key isKindOfClass:NSString.class] || !JardValidPreference(key, value)) { reply(nil, @"Invalid dictation setting."); return; }
        NSString *failure = nil;
        if (![self applyPreferences:@{key: value} error:&failure]) { reply(nil, failure); return; }
    } else if ([action isEqual:@"editVocabulary"]) {
        [self editVocabulary:parameters reply:reply];
        return;
    } else if ([action isEqual:@"previewSound"]) {
        NSString *cue = parameters[@"cue"];
        NSString *style = parameters[@"style"] ?: self.settings[@"soundSet"];
        if (![@[@"sample", @"start", @"stop", @"done", @"error"] containsObject:cue ?: NSNull.null] ||
            ![JardDictationSoundStyles() containsObject:style ?: @""] ||
            ([cue isEqual:@"done"] && !JardDictationSoundHasDone(style))) {
            reply(nil, @"That sound is unavailable for this style.");
            return;
        }
        if (![self previewSound:cue style:style]) {
            reply(nil, @"Sound previews are unavailable while Jaro is busy.");
            return;
        }
    } else if ([action isEqual:@"stopSoundPreview"]) {
        [self stopSoundPreview];
    } else if ([action isEqual:@"chooseProfileApplication"]) {
        NSOpenPanel *panel = NSOpenPanel.openPanel;
        panel.title = @"Choose an application";
        panel.directoryURL = [NSURL fileURLWithPath:@"/Applications" isDirectory:YES];
        panel.allowedContentTypes = @[UTTypeApplicationBundle];
        [panel beginWithCompletionHandler:^(NSModalResponse response) {
            if (response != NSModalResponseOK) { reply(@{@"application":NSNull.null}, nil); return; }
            NSDictionary *app = JardProfileApplication(panel.URL);
            if (app) reply(@{@"application":app}, nil);
            else reply(nil, @"Choose an application with a bundle identifier.");
        }];
        return;
    } else if ([action isEqual:@"profileApplications"]) {
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            NSArray *items = JardProfileApplications();
            dispatch_async(dispatch_get_main_queue(), ^{ reply(@{@"applications":items}, nil); });
        });
        return;
    } else if ([action isEqual:@"chooseOutputScript"]) {
        [self chooseOutputScript:reply];
        return;
    } else if ([action isEqual:@"createOutputConfig"]) {
        [self createOutputConfig:reply];
        return;
    } else if ([action isEqual:@"inspectOutputScript"]) {
        [self inspectOutputScriptFresh:NO reply:reply];
        return;
    } else if ([action isEqual:@"approveOutputScript"]) {
        [self approveOutputScript:parameters[@"token"] grants:parameters[@"grants"] reply:reply];
        return;
    } else if ([action isEqual:@"revealOutputScript"]) {
        NSString *path = self.settings[@"outputScript"];
        if (!path.length) { reply(nil, @"Select a script first."); return; }
        [NSWorkspace.sharedWorkspace activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:path]]];
    } else if ([action isEqual:@"clearOutputScript"]) {
        NSMutableDictionary *edited = [self.settings mutableCopy];
        edited[@"outputScript"] = @"";
        edited[@"outputScriptApproval"] = @{};
        NSString *failure = nil;
        if (![self saveSettings:edited error:&failure]) { reply(nil, failure); return; }
    } else if ([action isEqual:@"copyOutputAgentPrompt"]) {
        if (![self copyText:JardOutputAgentPrompt()]) { reply(nil, @"Could not copy the output instructions."); return; }
    } else if ([action isEqual:@"revealOutputSettings"]) {
        NSString *identifier = NSBundle.mainBundle.bundleIdentifier;
        if (!identifier.length || [identifier containsString:@"/"]) {
            reply(nil, @"The settings file could not be located.");
            return;
        }
        NSString *folder = [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Preferences"];
        NSString *path = [folder stringByAppendingPathComponent:[identifier stringByAppendingPathExtension:@"plist"]];
        if (![NSFileManager.defaultManager fileExistsAtPath:path]) {
            reply(nil, @"The settings file appears after you save a preference.");
            return;
        }
        [NSWorkspace.sharedWorkspace activateFileViewerSelectingURLs:@[[NSURL fileURLWithPath:path]]];
    } else if ([action isEqual:@"previewOutput"] || [action isEqual:@"describeOutputProfiles"] || [action isEqual:@"tryOutput"]) {
        BOOL describe = [action isEqual:@"describeOutputProfiles"];
        BOOL tryBuiltins = [action isEqual:@"tryOutput"];
        NSDictionary *preferences = [self.settings copy];
        NSString *text = describe ? @"" : parameters[@"text"], *profiles = tryBuiltins ? preferences[@"outputProfiles"] : parameters[@"profiles"],
            *app = describe || tryBuiltins ? @"" : parameters[@"app"], *site = describe || tryBuiltins ? @"" : parameters[@"site"];
        if (![text isKindOfClass:NSString.class] || text.length > 8000 ||
            ![profiles isKindOfClass:NSString.class] || profiles.length > 48000 ||
            ![app isKindOfClass:NSString.class] || app.length > 300 ||
            ![site isKindOfClass:NSString.class] || site.length > 300) {
            reply(nil, @"Invalid output preview.");
            return;
        }
        self.send(@"jard.output", @{@"text": text, @"profiles": profiles, @"app": app, @"site": site, @"describe": @(describe),
            @"builtinsOnly": @(tryBuiltins), @"replacements": preferences[@"replacements"],
            @"instruction": preferences[@"rewriteInstructions"], @"vocabulary": JardVocabulary(preferences[@"vocabulary"]),
            @"aliases": JardVocabularyAliases(preferences)},
            ^(id result, NSString *error) {
                if (tryBuiltins && !error && [result isKindOfClass:NSDictionary.class] && ![result[@"profile"] length]) {
                    NSMutableDictionary *formatted = [result mutableCopy];
                    formatted[@"text"] = [self format:text preferences:preferences];
                    reply(formatted, nil);
                } else reply(result, error);
            });
        return;
    } else if ([action isEqual:@"microphone"]) {
        if ([microphoneStatus() isEqual:@"request"]) {
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) {
                dispatch_async(dispatch_get_main_queue(), ^{ [self refresh]; reply([self status], nil); });
            }];
            return;
        }
        if (![microphoneStatus() isEqual:@"granted"]) {
            [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:
                @"x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"]];
        }
    } else if ([action isEqual:@"accessibility"]) {
        [self permissionGuide];
    } else if ([action isEqual:@"repairAccessibility"]) {
        [self repairAccessibility:reply];
        return;
    } else if ([action isEqual:@"requestMediaAccess"]) {
        [self requestMediaAccess:reply];
        return;
    } else if ([action isEqual:@"requestBrowserAccess"]) {
        [self requestBrowserAccess:reply];
        return;
    } else if ([action isEqual:@"openMediaAutomationSettings"] || [action isEqual:@"openBrowserAutomationSettings"]) {
        [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:
            @"x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"]];
    } else if ([action isEqual:@"inputMonitoring"]) {
        if (accessibilityAllowed() && !JardEventTapEnabled(&_sessionTap) && !CGPreflightListenEventAccess()) {
            CGRequestListenEventAccess();
            [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:
                @"x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"]];
        }
    } else if ([action isEqual:@"chooseShortcut"]) {
        NSString *target = parameters[@"target"] ?: @"shortcut";
        BOOL annotate = [target isEqual:@"annotate"];
        if (annotate && (!self.annotationEvents || self.annotationActive || self.annotationKeyDown || self.updatePaused || self.secureInput)) {
            reply(nil, @"The Annotate shortcut can’t be edited right now.");
            return;
        }
        if (annotate && !self.annotationEnabled) {
            self.shortcutTarget = target;
            self.choosingShortcut = YES;
            [self refresh];
        }
        if (![self canOpenShortcutRecorder]) {
            if (annotate) { self.choosingShortcut = NO; [self refresh]; }
            reply(nil, annotate ? @"Allow Accessibility and Input Monitoring to record the Annotate shortcut." :
                @"Enable dictation and allow access before changing its shortcut.");
            return;
        }
        if (![@[@"shortcut", @"raw", @"paste", @"cancel", @"annotate"] containsObject:target]) { reply(nil, @"Unknown shortcut."); return; }
        if ([target isEqual:@"cancel"] && _shortcut.mode != JardToggle) {
            reply(nil, @"Choose Tap mode to change its Cancel shortcut.");
            return;
        }
        if (annotate) [self handleAnnotationEvent:kCGEventTapDisabledByUserInput event:NULL chordFree:NO];
        self.shortcutTarget = target;
        self.choosingShortcut = YES;
        [self resetShortcutCandidate];
        self.message = @"";
        if (self.secureInput) [self updateSecureHotkeys:YES];
    } else if ([action isEqual:@"cancelShortcut"] || [action isEqual:@"cancelAnnotationShortcut"]) {
        if ([action isEqual:@"cancelShortcut"] || [self.shortcutTarget isEqual:@"annotate"]) {
            self.choosingShortcut = NO;
            if (self.secureInput) [self updateSecureHotkeys:YES];
        }
    } else if ([action isEqual:@"resetAnnotationShortcut"]) {
        if (!self.annotationEvents || self.annotationActive || self.annotationKeyDown || self.updatePaused || self.hasActivity || self.choosingShortcut) {
            reply(nil, @"The Annotate shortcut can’t be edited right now.");
            return;
        }
        self.shortcutTarget = @"annotate";
        if (![self saveRecordedCode:kVK_ANSI_S flags:defaultAnnotationFlags label:@"⌥⇧S"]) { reply(nil, self.message); return; }
    } else if ([action isEqual:@"setAnnotationOverlays"]) {
        id show = parameters[@"show"];
        if (!self.annotationEvents || self.closed || self.updatePaused || !JardBoolean(show)) {
            reply(nil, @"The Annotate recording display setting is unavailable."); return;
        }
        if (![self.settings[@"annotationShowOverlays"] isEqual:show]) {
            NSString *failure = nil;
            if (![self applyPreferences:@{@"annotationShowOverlays": show} error:&failure]) { reply(nil, failure); return; }
        }
    } else if ([action isEqual:@"setAnnotationEnabled"]) {
        id enabled = parameters[@"enabled"];
        if (!self.annotationEvents || !JardBoolean(enabled)) { reply(nil, @"Choose whether to enable Annotate."); return; }
        if (self.annotationActive || self.annotationKeyDown || self.updatePaused || self.hasActivity || self.choosingShortcut) {
            reply(nil, @"Finish the current recording or shortcut edit first."); return;
        }
        if (self.annotationEnabled != [enabled boolValue]) {
            if ([enabled boolValue] && ![self annotationChordFreeIncludingInactive:YES]) {
                reply(nil, @"The Annotate shortcut is already in use. Change it before turning Annotate on."); return;
            }
            NSMutableDictionary *edited = [self.settings mutableCopy];
            edited[@"annotationEnabled"] = enabled;
            NSString *failure = nil;
            if (![self saveSettings:edited error:&failure]) { reply(nil, failure); return; }
        }
    } else if ([action isEqual:@"clearRawShortcut"]) {
        if (self.captureID || self.keyDown) { reply(nil, @"Finish the take before changing shortcuts."); return; }
        if (![self setShortcutBinding:(JardShortcutBinding){.code = -1} label:@"" target:@"raw"]) {
            reply(nil, self.message);
            return;
        }
    } else if ([action isEqual:@"setShortcutSide"]) {
        if (self.captureID || self.transcribing || self.keyDown || _shortcut.phase != JardIdle) {
            reply(nil, @"Finish the take before changing shortcuts."); return;
        }
        CGKeyCode code = [self.settings[@"shortcutCode"] unsignedShortValue];
        CGKeyCode left, right;
        NSString *side = parameters[@"value"];
        if (!JardModifierPair(code, &left, &right) || ![@[@"left", @"either", @"right"] containsObject:side]) {
            reply(nil, @"Choose a left, right, or either side for a single modifier shortcut."); return;
        }
        BOOL either = [side isEqual:@"either"];
        if (!either) code = [side isEqual:@"left"] ? left : right;
        self.shortcutTarget = @"shortcut";
        if (![self saveRecordedBinding:(JardShortcutBinding){.code = code, .flags = modifierFlag(code), .either = either}
            label:modifierLabel(code, either)]) { reply(nil, self.message); return; }
    } else if ([@[@"resetShortcut", @"resetCancelShortcut", @"resetPasteShortcut"] containsObject:action]) {
        if (self.captureID || self.keyDown) { reply(nil, @"Finish the take before changing shortcuts."); return; }
        BOOL cancel = [action isEqual:@"resetCancelShortcut"];
        BOOL paste = [action isEqual:@"resetPasteShortcut"];
        if (cancel && _shortcut.mode != JardToggle) { reply(nil, @"Choose Tap mode to change its Cancel shortcut."); return; }
        self.shortcutTarget = paste ? @"paste" : cancel ? @"cancel" : @"shortcut";
        CGKeyCode code = paste ? kVK_ANSI_V : cancel ? kVK_Escape : kVK_RightOption;
        CGEventFlags flags = paste ? kCGEventFlagMaskAlternate | kCGEventFlagMaskShift : cancel ? 0 : kCGEventFlagMaskAlternate;
        if (![self saveRecordedCode:code flags:flags label:paste ? @"⌥⇧V" : cancel ? @"Escape" : @"Right ⌥"]) {
            if (![self canOpenShortcutRecorder]) { reply(nil, self.message); return; }
            [self resetShortcutCandidate];
            self.choosingShortcut = YES;
            if (self.secureInput) [self updateSecureHotkeys:YES];
        }
    } else if ([action isEqual:@"historyStats"]) {
        if (![self.settings[@"showHistoryStats"] boolValue]) { reply(@{@"stats": NSNull.null}, nil); return; }
        [self trimHistory];
        NSArray *snapshot = [self.history copy];
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            NSDictionary *stats = JardHistoryStats(snapshot, NSDate.date, NSCalendar.currentCalendar);
            dispatch_async(dispatch_get_main_queue(), ^{ reply(@{@"stats": stats}, nil); });
        });
        return;
    } else if ([action isEqual:@"history"]) {
        [self trimHistory];
        NSString *query = [parameters[@"query"] isKindOfClass:NSString.class] ? parameters[@"query"] : @"";
        NSInteger offset = [parameters[@"offset"] isKindOfClass:NSNumber.class] ? [parameters[@"offset"] integerValue] : 0;
        NSArray *snapshot = [self.history copy];
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            NSMutableArray *page = NSMutableArray.array;
            NSInteger matches = 0;
            NSUInteger words = 0;
            double seconds = 0;
            for (NSDictionary *entry in snapshot) {
                if (query.length && ![entry[@"text"] localizedCaseInsensitiveContainsString:query] &&
                    ![entry[@"raw"] localizedCaseInsensitiveContainsString:query] &&
                    ![entry[@"app"] localizedCaseInsensitiveContainsString:query]) continue;
                if (matches++ >= MAX(0, offset) && page.count < 15) {
                    if (entry[@"audioExpiresAt"] && ![self availableHistoryAudio:entry]) {
                        NSMutableDictionary *textOnly = [entry mutableCopy];
                        [textOnly removeObjectForKey:@"audioExpiresAt"];
                        [page addObject:textOnly];
                    } else [page addObject:entry];
                }
                words += JardHistoryWords(entry[@"text"]);
                seconds += [entry[@"seconds"] doubleValue];
            }
            NSDictionary *result = @{@"entries": page, @"total": @(matches), @"words": @(words), @"seconds": @(seconds)};
            dispatch_async(dispatch_get_main_queue(), ^{ reply(result, nil); });
        });
        return;
    } else if ([action isEqual:@"playHistoryAudio"] || [action isEqual:@"stopHistoryAudio"]) {
        NSString *identifier = parameters[@"id"];
        if ([action isEqual:@"stopHistoryAudio"]) {
            if ([self.playingHistoryID isEqual:identifier]) [self stopHistoryAudio];
        } else {
            if (self.closed || self.updatePaused || self.captureID || self.sdkRecording || self.annotationActive) {
                reply(nil, @"Finish the current recording or update to play dictation audio.");
                return;
            }
            NSUInteger index = [self.history indexOfObjectPassingTest:^BOOL(NSDictionary *entry, NSUInteger index, BOOL *stop) {
                return [entry[@"id"] isEqual:identifier];
            }];
            NSURL *clip = index == NSNotFound ? nil : [self availableHistoryAudio:self.history[index]];
            if (!clip) {
                self.historyAudioRevision++;
                reply(nil, @"This dictation audio is no longer available.");
                return;
            }
            [self stopHistoryAudio];
            AVAudioPlayer *player = [[AVAudioPlayer alloc] initWithContentsOfURL:clip error:NULL];
            if (![player play]) { reply(nil, @"Could not play this dictation audio."); return; }
            player.delegate = self;
            self.historyAudioPlayer = player;
            self.playingHistoryID = identifier;
        }
    } else if ([action isEqual:@"clearHistory"]) {
        NSString *failure = nil;
        if (![self clearHistoryWithError:&failure]) { reply(nil, failure); return; }
    } else if ([action isEqual:@"bulkHistory"]) {
        NSString *operation = parameters[@"operation"];
        NSArray *ids = parameters[@"ids"];
        if (![ids isKindOfClass:NSArray.class] || ids.count > 250 ||
            ![@[@"copy", @"delete"] containsObject:operation ?: @""]) {
            reply(nil, @"Invalid history selection.");
            return;
        }
        NSSet *selected = [NSSet setWithArray:ids];
        NSIndexSet *indices = [self.history indexesOfObjectsPassingTest:^BOOL(NSDictionary *entry, NSUInteger index, BOOL *stop) {
            return [selected containsObject:entry[@"id"]];
        }];
        NSArray *entries = [self.history objectsAtIndexes:indices];
        if ([operation isEqual:@"copy"]) {
            if (!entries.count) { reply(nil, @"The selected dictations are no longer in History."); return; }
            NSArray *texts = [[[entries reverseObjectEnumerator] allObjects] valueForKey:@"text"];
            if (![self copyText:[texts componentsJoinedByString:@"\n\n"]]) { reply(nil, @"Could not update the clipboard."); return; }
        } else if ([operation isEqual:@"delete"]) {
            if (self.lastHistoryID && [selected containsObject:self.lastHistoryID]) [self forgetLastDictation];
            [self.history removeObjectsAtIndexes:indices];
            [self sweepHistoryAudioAt:NSDate.date.timeIntervalSince1970];
            [self saveHistory];
        }
    } else if ([action isEqual:@"copyLast"]) {
        if (self.lastText.length && ![self copyText:self.lastText]) { reply(nil, @"Could not update the clipboard."); return; }
    } else {
        reply(nil, @"Unsupported system dictation action.");
        return;
    }
    reply([self status], nil);
}

- (BOOL)canStartCommandCapture {
    return self.dictationKeyEnabled && JardEventTapEnabled(&_sessionTap) && !self.closed && !self.updatePaused && !self.secureInput &&
        _shortcut.phase == JardIdle && !self.captureID && !self.transcribing && !self.sdkRecording && !self.annotationPasteReply;
}

- (NSDictionary *)historySnapshot {
    return @{@"bundleId": NSBundle.mainBundle.bundleIdentifier ?: @"local.jardaudio.desktop",
        historyKey: [self.history copy],
        preferencesKey: @{@"retention": self.settings[@"retention"], @"previousRetention": self.settings[@"previousRetention"],
            @"vocabulary": self.settings[@"vocabulary"] ?: @"", @"vocabularySoundsLike": self.settings[@"vocabularySoundsLike"] ?: @{}}};
}

- (void)writeHistorySnapshot:(NSString *)path reply:(JardDictationReply)reply {
    [self trimHistory];
    NSDictionary *snapshot = [self historySnapshot];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSData *data = [NSPropertyListSerialization dataWithPropertyList:snapshot format:NSPropertyListBinaryFormat_v1_0
            options:0 error:nil];
        BOOL written = NO;
        int fd = data ? open(path.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600) : -1;
        if (fd >= 0) {
            NSFileHandle *file = [[NSFileHandle alloc] initWithFileDescriptor:fd closeOnDealloc:YES];
            BOOL complete = [file writeData:data error:nil];
            written = [file closeAndReturnError:nil] && complete;
            if (!written) unlink(path.fileSystemRepresentation);
        }
        dispatch_async(dispatch_get_main_queue(), ^{
            reply(written ? @YES : nil, written ? nil : @"Jaro could not read the live dictation history.");
        });
    });
}

- (void)control:(NSString *)action reply:(JardDictationReply)reply {
    if ([action isEqual:@"history-snapshot"]) {
        NSUInteger bytes = 0;
        for (NSDictionary *entry in self.history) {
            bytes += JardHistoryTextBytes(entry);
            if (bytes > 56000) break;
        }
        if (bytes > 56000 || self.history.count > 250) {
            reply(nil, @"Read the saved history from the preferences file.");
            return;
        }
        NSDictionary *snapshot = [self historySnapshot];
        NSData *encoded = [NSJSONSerialization dataWithJSONObject:snapshot options:0 error:nil];
        if (!encoded || encoded.length > 56000) reply(nil, @"Read the saved history from the preferences file.");
        else reply(snapshot, nil);
        return;
    }
    if ([action isEqual:@"history-clear"]) {
        NSString *error = nil;
        NSDictionary *result = [self clearHistoryWithError:&error];
        reply(result, error);
        return;
    }
    BOOL rawPrintStart = [action isEqual:@"dictation-start-raw-print"];
    BOOL printStart = rawPrintStart || [action isEqual:@"dictation-start-print"];
    BOOL printStop = [action isEqual:@"dictation-stop-print"];
    BOOL rawToggle = [action isEqual:@"dictation-toggle-raw"];
    BOOL raw = rawPrintStart || rawToggle || [action isEqual:@"dictation-start-raw"];
    if (printStop) action = @"stop";
    else if (printStart || raw) action = rawToggle ? @"toggle" : @"start";
    if ([action isEqual:@"toggle"]) action = self.captureID ? @"stop" : @"start";
    if ([action isEqual:@"start"]) {
        if (![self canStartCommandCapture]) {
            reply(nil, @"Complete dictation setup or finish the current take.");
            return;
        }
        self.usingRawBinding = NO;
        self.commandRawTake = raw;
        self.captureFromCommand = YES;
        _shortcut.phase = JardLocked;
        if (printStart) self.printRequest = [JardTranscriptReply waitFor:reply seconds:31 * 60];
        [self begin];
        if (printStart) {
            if (!self.captureID && self.printRequest) [self fail:@"The microphone did not start."];
            return;
        }
    } else if ([action isEqual:@"stop"]) {
        if (!self.captureID) { reply(nil, @"There is no active take."); return; }
        if (printStop) {
            if (self.stopping || self.cancelled || self.practice) {
                reply(nil, @"There is no active dictation available to print.");
                return;
            }
            JardTranscriptReply *stop = [JardTranscriptReply waitFor:reply seconds:60];
            JardTranscriptReply *start = self.printRequest;
            if (start) {
                self.printRequest = JardTranscriptReply.new;
                self.printRequest.reply = ^(id result, NSString *error) {
                    [start finish:result error:error];
                    [stop finish:result error:error];
                };
            } else self.printRequest = stop;
        }
        if (!self.closingReleaseTail) {
            _shortcut.phase = JardFinishing;
            self.shortcutReleasedAt = 0;
            [self stopCapture];
        }
        if (printStop) return;
    } else if ([action isEqual:@"cancel"]) {
        if (self.captureID || self.transcribing) [self cancelCapture];
        else [self cancelPendingOutput];
    } else if ([action isEqual:@"paste-last"]) {
        if (![self canInvokePasteLast]) {
            reply(nil, @"There is no dictation available to paste.");
            return;
        }
        [self pasteLast];
    } else if (![action isEqual:@"status"]) {
        reply(nil, @"Unknown desktop control.");
        return;
    }
    reply(@{@"phase": self.phase, @"enabled": self.settings[@"enabled"], @"listener": @(self.dictationKeyEnabled && JardEventTapEnabled(&_sessionTap)),
        @"pending": @(self.pendingJobs.count), @"message": self.message ?: @"", @"lastFailure": self.lastFailure ?: @""}, nil);
}

- (void)shutdown {
    if (self.closed) return;
    self.closed = YES;
    [self stopSoundPreview];
    [self stopHistoryAudio];
    [self.historyAudioTimer invalidate];
    self.historyAudioTimer = nil;
    [self finishAnnotationPaste:@"Jard is closing."];
    [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self];
    [NSDistributedNotificationCenter.defaultCenter removeObserver:self];
    [self cancelCapture];
    [self.recordingEnvironment shutdown];
    if (self.activeMeetingPaste || self.activeOutput) [self resetCapture];
    [self discardPendingOutput:YES];
    [self updateWarm:NO];
    [self.secureHotkeys update:NULL count:0];
    self.secureHotkeys = nil;
    [self removeTap];
    JardDetachEventTap(&_modifierFilter);
    [self keepShortcutListenerAwake:NO];
    [self.monitor invalidate];
    self.monitor = nil;
    [self hideHUD];
    [self.guide orderOut:nil];
    [self restoreClipboard];
    [self clearDestination];
}

@end

static CGEventRef dictationPhysicalKeyTap(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *context) {
    @autoreleasepool {
        JardSystemDictation *owner = (__bridge JardSystemDictation *)context;
        if (type == kCGEventKeyDown && event) [owner observePhysicalKeyDown:event];
        else if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput)
            [owner eventTapDisabled:kCGHIDEventTap type:type];
    }
    return event;
}

static CGEventRef dictationTap(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *context) {
    @autoreleasepool {
        JardSystemDictation *owner = (__bridge JardSystemDictation *)context;
        if ([owner handleKey:event type:type]) return NULL;
        if (event && CGEventGetIntegerValueField(event, kCGEventSourceUserData) != pasteMarker &&
            ![owner filterModifierClaim:event type:type]) {
            [owner observeUnmatchedInput:type event:event];
        }
        return event;
    }
}

static CGEventRef dictationModifierTap(CGEventTapProxy proxy, CGEventType type, CGEventRef event, void *context) {
    @autoreleasepool {
        JardSystemDictation *owner = (__bridge JardSystemDictation *)context;
        if (type == kCGEventTapDisabledByTimeout || type == kCGEventTapDisabledByUserInput)
            [owner eventTapDisabled:kCGAnnotatedSessionEventTap type:type];
        return [owner filterModifierClaim:event type:type] ? NULL : event;
    }
}
