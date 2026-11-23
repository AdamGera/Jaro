#import "../system_dictation.h"
#import "../dictation_shortcut.h"
#import "../jard_dictation_typing.h"
#import "../jard_interface_language.h"
#import "../jard_recording_environment.h"
#import "../jard_secure_hotkeys.h"
#import "../indicator/JardDictationMeter.h"
#import "../indicator/JardHUDPlacement.h"
#import <AVFoundation/AVFoundation.h>
#import <Carbon/Carbon.h>
#import <IOKit/hid/IOHIDUsageTables.h>
#import <IOKit/hidsystem/IOLLEvent.h>
#import <assert.h>
#import <dlfcn.h>
#import <objc/runtime.h>
#import <stdatomic.h>
#import <string.h>

static Boolean fixtureSecureInput;
Boolean IsSecureEventInputEnabled(void) { return fixtureSecureInput; }

// Use real run-loop sources and invalidatable Mach ports without registering
// OS input taps or requiring permissions on the test runner.
static _Atomic bool fixtureEventTaps;
static bool fixturePresence, fixtureDisplayAsleep;
static NSDictionary *fixtureSession;
static const pid_t windowFixturePID = 8251;
static pid_t windowFixtureIdentity;
static AXError windowFixtureFocusStatus;
static NSMutableArray<NSArray *> *nativePasteEvents;

void CGEventPost(CGEventTapLocation location, CGEventRef event) {
    if (nativePasteEvents) {
        [nativePasteEvents addObject:@[@(location), @(CGEventGetType(event)),
            @(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)), @(CGEventGetFlags(event))]];
        return;
    }
    ((typeof(&CGEventPost))dlsym(RTLD_NEXT, "CGEventPost"))(location, event);
}

AXError AXUIElementCopyAttributeValue(AXUIElementRef element, CFStringRef attribute, CFTypeRef *value) {
    pid_t pid = 0;
    AXUIElementGetPid(element, &pid);
    if (windowFixtureIdentity && pid == windowFixturePID) {
        *value = NULL;
        if (CFEqual(attribute, kAXFocusedUIElementAttribute)) {
            if (windowFixtureFocusStatus == kAXErrorSuccess)
                *value = AXUIElementCreateApplication(windowFixturePID + 1);
            return windowFixtureFocusStatus;
        }
        if (CFEqual(attribute, kAXFocusedWindowAttribute)) {
            *value = AXUIElementCreateApplication(windowFixtureIdentity);
            return kAXErrorSuccess;
        }
        return kAXErrorNoValue;
    }
    return ((typeof(&AXUIElementCopyAttributeValue))dlsym(RTLD_NEXT, "AXUIElementCopyAttributeValue"))(element, attribute, value);
}

boolean_t CGDisplayIsAsleep(CGDirectDisplayID display) {
    if (fixturePresence) return fixtureDisplayAsleep;
    return ((typeof(&CGDisplayIsAsleep))dlsym(RTLD_NEXT, "CGDisplayIsAsleep"))(display);
}

CFDictionaryRef CGSessionCopyCurrentDictionary(void) {
    if (fixturePresence) return (CFDictionaryRef)CFBridgingRetain(fixtureSession);
    return ((typeof(&CGSessionCopyCurrentDictionary))dlsym(RTLD_NEXT, "CGSessionCopyCurrentDictionary"))();
}

static struct {
    CFMachPortRef port;
    CGEventTapCallBack callback;
    void *context;
    unsigned creations;
    bool enabled, blockCreation, blockEnable;
} fixtureTaps[3];

bool CGPreflightListenEventAccess(void) {
    if (fixtureEventTaps) return NSThread.isMainThread;
    return ((bool (*)(void))dlsym(RTLD_NEXT, "CGPreflightListenEventAccess"))();
}

bool CGPreflightPostEventAccess(void) {
    if (fixtureEventTaps) return true;
    return ((bool (*)(void))dlsym(RTLD_NEXT, "CGPreflightPostEventAccess"))();
}

static void fixturePortMessage(CFMachPortRef port, void *message, CFIndex size, void *context) {}

CFMachPortRef CGEventTapCreate(CGEventTapLocation location, CGEventTapPlacement placement, CGEventTapOptions options,
                               CGEventMask mask, CGEventTapCallBack callback, void *context) {
    if (!fixtureEventTaps) return ((typeof(&CGEventTapCreate))dlsym(RTLD_NEXT, "CGEventTapCreate"))(
        location, placement, options, mask, callback, context);
    assert(location <= kCGAnnotatedSessionEventTap);
    typeof(fixtureTaps[0]) *tap = &fixtureTaps[location];
    tap->creations++;
    if (tap->blockCreation) return NULL;
    CFMachPortRef port = CFMachPortCreate(NULL, fixturePortMessage, NULL, NULL);
    assert(port);
    if (tap->port) CFRelease(tap->port);
    tap->port = (CFMachPortRef)CFRetain(port);
    tap->callback = callback;
    tap->context = context;
    tap->enabled = false;
    return port;
}

bool CGEventTapIsEnabled(CFMachPortRef port) {
    if (!fixtureEventTaps) return ((typeof(&CGEventTapIsEnabled))dlsym(RTLD_NEXT, "CGEventTapIsEnabled"))(port);
    for (NSUInteger i = 0; i < 3; i++) if (fixtureTaps[i].port == port) return fixtureTaps[i].enabled;
    assert(false);
    return false;
}

void CGEventTapEnable(CFMachPortRef port, bool enabled) {
    if (!fixtureEventTaps) {
        ((typeof(&CGEventTapEnable))dlsym(RTLD_NEXT, "CGEventTapEnable"))(port, enabled);
        return;
    }
    for (NSUInteger i = 0; i < 3; i++) {
        if (fixtureTaps[i].port != port) continue;
        if (!fixtureTaps[i].blockEnable) fixtureTaps[i].enabled = enabled;
        return;
    }
    assert(false);
}

// Test the host’s real queue while leaving the microphone and the clipboard closed.
@interface JardDictationStartCue : NSObject
- (void)playURL:(NSURL *)url volume:(float)volume at:(NSTimeInterval)deadline;
- (void)withdraw;
@end

@interface JardSystemDictation (QueueTest)
@property(nonatomic, strong) NSMutableDictionary *settings;
@property(nonatomic, strong) NSMutableArray *pendingJobs;
@property(nonatomic, strong) NSMutableArray *history;
@property(nonatomic, strong) AVAudioPlayer *historyAudioPlayer;
- (void)sweepHistoryAudioAt:(NSTimeInterval)now;
@property(nonatomic, copy) NSDictionary *capturePreferences;
@property(nonatomic, copy) NSString *captureID, *lastText, *phase;
@property(nonatomic, copy) NSString *message;
@property(nonatomic, strong) NSArray *clipboard;
@property(nonatomic, strong) JardRecordingEnvironment *recordingEnvironment;
@property(nonatomic) NSUInteger takeSequence, recordingGeneration;
@property(nonatomic) NSUInteger takeInputRevision;
@property(nonatomic) NSUInteger inputRevision;
@property(nonatomic) AXUIElementRef focusedElement;
@property(nonatomic, strong) NSValue *selectedRange;
@property(nonatomic, copy) void (^activeMeetingPaste)(BOOL);
@property(nonatomic) BOOL accessibilityStale;
@property(nonatomic) BOOL usingRawBinding;
@property(nonatomic) BOOL captureRawBinding;
@property(nonatomic) BOOL dictationKeyEnabled;
@property(nonatomic, strong) JardSecureHotkeys *secureHotkeys;
@property(nonatomic) BOOL choosingShortcut, annotationActive;
@property(nonatomic) BOOL keyDown, suppressedKey;
@property(nonatomic, readonly) id<NSObject> listenerActivity;
@property(nonatomic) BOOL suppressedPaste, suppressedMeetingPaste;
@property(nonatomic) NSTimeInterval nextPreferencesRead;
@property(nonatomic) JardShortcut shortcut;
@property(nonatomic, copy) NSString *shortcutTarget;
@property(nonatomic, strong) NSPanel *hud;
@property(nonatomic, strong) JardDictationMeter *meter;
- (void)showHUD:(NSString *)label stage:(JardIndicatorStage)stage;
- (void)checkHUDSpaceAt:(NSTimeInterval)time;
- (void)trackHUDAt:(NSTimeInterval)time;
- (void)hideHUD;
- (void)drainOutput;
- (void)cancelPendingOutput;
- (void)begin;
- (void)rememberDestination;
- (NSRunningApplication *)foregroundOutputApplication;
- (NSDictionary *)readOutputContextForPID:(pid_t)pid deadline:(NSTimeInterval)deadline detailed:(BOOL)detailed;
- (void)stopCapture;
- (BOOL)handleKey:(CGEventRef)event type:(CGEventType)type;
- (void)rememberExternalApp:(NSRunningApplication *)app;
- (void)noteAccessibilityRequestAt:(NSTimeInterval)now;
- (BOOL)observeAccessibilityAt:(NSTimeInterval)now trusted:(BOOL)trusted
                  settingsOpen:(BOOL)settingsOpen settingsFront:(BOOL)settingsFront active:(BOOL)active;
- (void)activationChanged:(NSNotification *)notification;
- (void)powerChanged:(NSNotification *)notification;
- (BOOL)canStartCommandCapture;
- (void)deliver:(NSString *)text mode:(NSString *)mode warning:(NSString *)warning;
- (BOOL)postPasteShortcut;
- (void)pasteLast;
- (void)cancelCapture;
- (void)discardShortcutTake;
- (void)maintainEventTapsForDictation:(BOOL)dictation;
- (void)updateSecureHotkeys:(BOOL)available;
- (void)routeSecureChord:(JardSecureChord)chord edge:(JardSecureEdge)edge;
- (void)applyShortcutMode;
- (void)updateWarm:(BOOL)wanted;
- (void)checkSavedPreferences;
- (void)acceptSavedPreferences:(id)saved readable:(BOOL)readable revision:(NSUInteger)revision;
- (void)stageShortcutLayout:(JardShortcutLayout)layout;
- (NSInteger)shortcutCodeForSlot:(NSString *)slot;
- (CGKeyCode)annotationShortcutCode;
- (BOOL)saveRecordedCode:(CGKeyCode)code flags:(CGEventFlags)flags label:(NSString *)label;
- (void)recordModifierChange:(CGKeyCode)code flags:(CGEventFlags)flags raw:(CGEventFlags)raw;
- (BOOL)ordinaryKeyHeldForShortcutAt:(NSTimeInterval)now bindingFlags:(CGEventFlags)bindingFlags;
- (BOOL)filterModifierClaim:(CGEventRef)event type:(CGEventType)type;
- (BOOL)modifierClaimAvailable;
- (void)sendModifierClaimAt:(NSTimeInterval)when;
- (void)queueAction:(JardShortcutAction)action at:(NSTimeInterval)time;
- (void)observePhysicalKeyDown:(CGEventRef)event;
- (void)observeUnmatchedInput:(CGEventType)type event:(CGEventRef)event;
- (void)repairShortcutReleaseAt:(NSTimeInterval)now inputHeld:(BOOL)inputHeld;
- (void)sampleShortcutReleaseAt:(NSTimeInterval)now;
- (uint16_t)physicalModifierKeys;
- (NSString *)layoutLabelForShortcut:(CGKeyCode)code source:(TISInputSourceRef)source;
- (BOOL)shortcutRecorderIsFrontmost;
- (BOOL)canOpenShortcutRecorder;
- (BOOL)canStartCommandCapture;
- (void)playSound:(NSString *)stage;
- (NSURL *)soundURL:(NSString *)cue style:(NSString *)style;
- (NSSound *)soundForCue:(NSString *)cue style:(NSString *)style;
- (JardDictationStartCue *)newShortcutStartCueAt:(NSTimeInterval)deadline preferences:(NSDictionary *)preferences;
- (void)admitCaptureIntentionAt:(NSTimeInterval)now;
- (void)interruptShortcutInput:(CGEventType)type;
@property(nonatomic) NSTimeInterval lastStopCueAt;
@end

@interface ShortcutRegistrationFixture : JardSecureHotkeys
@property(nonatomic, copy) NSArray<NSNumber *> *actions;
@end
@implementation ShortcutRegistrationFixture
- (void)update:(const JardSecureChord *)chords count:(NSUInteger)count {
    NSMutableArray *actions = NSMutableArray.array;
    for (NSUInteger i = 0; i < count; i++) [actions addObject:@(chords[i].action)];
    self.actions = actions;
}
@end

static void testSecureRouting(void) {
    JardSystemDictation *dictation = [[JardSystemDictation alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) { assert(false); }];
    dictation.settings[@"placement"] = @"off";
    dictation.dictationKeyEnabled = YES;
    dictation.lastText = @"A recovery take";
    __block NSUInteger annotationPresses = 0, annotationReleases = 0, reset = 0, meetingPaste = 0;
    [dictation setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL chordFree) {
        if (type == kCGEventTapDisabledByUserInput) { reset++; return NO; }
        if (type == kCGEventFlagsChanged) return NO;
        assert(chordFree && CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == kVK_ANSI_S);
        if (type == kCGEventKeyDown) annotationPresses++;
        if (type == kCGEventKeyUp) annotationReleases++;
        return YES;
    }];
    [dictation setMeetingPasteAction:^{ meetingPaste++; }];
    [dictation setMeetingPasteAvailable:YES];
    dictation.dictationKeyEnabled = YES;
    ShortcutRegistrationFixture *registration = [[ShortcutRegistrationFixture alloc] initWithHandler:nil];
    dictation.secureHotkeys = registration;
    fixtureSecureInput = true;
    [dictation updateSecureHotkeys:YES];
    assert(([registration.actions isEqual:@[@(JardSecureAnnotate), @(JardSecureMeetingPaste), @(JardSecurePasteLast)]]));
    CGEventFlags annotate = kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
    [dictation routeSecureChord:(JardSecureChord){JardSecureAnnotate, kVK_ANSI_S, annotate} edge:JardSecurePressed];
    [dictation routeSecureChord:(JardSecureChord){JardSecureAnnotate, kVK_ANSI_S, annotate} edge:JardSecureReleased];
    assert(annotationPresses == 1 && annotationReleases == 1 && reset == 1);
    [dictation routeSecureChord:(JardSecureChord){JardSecurePasteLast, kVK_ANSI_V, annotate} edge:JardSecurePressed];
    [dictation routeSecureChord:(JardSecureChord){JardSecureMeetingPaste, kVK_ANSI_V, kCGEventFlagMaskControl | kCGEventFlagMaskAlternate}
        edge:JardSecurePressed];
    assert(meetingPaste == 0 && [dictation.message containsString:@"Secure keyboard input"]);
    assert([dictation.lastText isEqual:@"A recovery take"]);

    dictation.settings[@"pasteCode"] = @(kVK_ANSI_S);
    dictation.settings[@"pasteFlags"] = @(annotate);
    [dictation updateSecureHotkeys:YES];
    assert(([registration.actions isEqual:@[@(JardSecureMeetingPaste), @(JardSecurePasteLast)]]));
    dictation.choosingShortcut = YES;
    [dictation updateSecureHotkeys:YES];
    assert(!registration.actions.count);
    dictation.choosingShortcut = NO;
    [dictation updateSecureHotkeys:NO];
    assert(!registration.actions.count);
    fixtureSecureInput = false;
    [dictation routeSecureChord:(JardSecureChord){JardSecureAnnotate, kVK_ANSI_S, annotate} edge:JardSecurePressed];
    assert(annotationPresses == 1 && reset == 2);
}

@interface AnnotationTargetFixture : NSObject
@property(nonatomic) pid_t processIdentifier;
@property(nonatomic) NSApplicationActivationPolicy activationPolicy;
@property(nonatomic, copy) NSString *bundleIdentifier, *localizedName;
@property(nonatomic, strong) NSURL *bundleURL;
@property(nonatomic, getter=isTerminated) BOOL terminated;
@end

@interface DictationDeliveryFixture : JardSystemDictation
@property(nonatomic, strong) NSRunningApplication *destination;
@property(nonatomic, strong) NSPasteboard *board;
@property(nonatomic, strong) NSMutableArray<NSDictionary *> *events;
@property(nonatomic, strong) AnnotationTargetFixture *editor;
@property(nonatomic, strong) NSValue *caret;
@property(nonatomic, copy) NSString *expectedPaste;
@property(nonatomic, copy) NSArray<NSString *> *expectedPastes;
@property(nonatomic, copy) void (^readPasteLater)(NSUInteger index);
@property(nonatomic) NSUInteger checks, pasteCount, disarmAfter, moveAfter, cancelAfter;
@property(nonatomic) BOOL mutateDuringFocusCheck, copyAfterPaste, moved;
@end

@implementation DictationDeliveryFixture
@dynamic destination;
- (NSPasteboard *)pasteboard { return self.board; }
- (BOOL)targetStillFocusedCheckingSelection:(BOOL)selection {
    if (self.mutateDuringFocusCheck && ++self.checks == 2) {
        [self.board clearContents];
        [self.board setString:@"New copy while checking focus" forType:NSPasteboardTypeString];
    }
    return !fixtureSecureInput && !self.moved && (!self.editor ||
        (self.takeInputRevision == self.inputRevision && (!selection || !self.selectedRange || [self.selectedRange isEqual:self.caret])));
}
- (BOOL)dictationPasteAppStillActive { return [self targetStillFocusedCheckingSelection:YES]; }
- (NSRunningApplication *)foregroundOutputApplication {
    return (NSRunningApplication *)(id)self.editor;
}
- (void)rememberDestination {
    if (!self.editor) { [super rememberDestination]; return; }
    self.destination = (NSRunningApplication *)(id)self.editor;
    self.focusedElement = AXUIElementCreateApplication(self.editor.processIdentifier);
    self.selectedRange = self.caret;
}
- (BOOL)postPasteShortcut {
    self.pasteCount++;
    NSString *inserted = [self.board stringForType:NSPasteboardTypeString];
    NSString *expected = self.expectedPastes ? self.expectedPastes[self.pasteCount - 1] : self.expectedPaste ?: self.lastText;
    assert([inserted isEqual:expected]);
    if (self.caret) self.caret = [NSValue valueWithRange:NSMakeRange(self.caret.rangeValue.location + inserted.length, 0)];
    NSArray *types = self.board.pasteboardItems.firstObject.types;
    assert([types containsObject:@"org.nspasteboard.TransientType"]);
    assert([types containsObject:@"org.nspasteboard.AutoGeneratedType"]);
    if (self.copyAfterPaste) {
        [self.board clearContents];
        [self.board setString:@"New copy after paste" forType:NSPasteboardTypeString];
    }
    if (self.readPasteLater) {
        NSUInteger index = self.pasteCount - 1;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 70 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
            if (self.readPasteLater) self.readPasteLater(index);
        });
    }
    return YES;
}
- (void)postDictationKeyEvent:(CGEventRef)event toProcess:(pid_t)pid {
    assert(pid == self.destination.processIdentifier);
    UniChar units[32];
    UniCharCount count = 0;
    CGEventKeyboardGetUnicodeString(event, 32, &count, units);
    NSString *text = [NSString stringWithCharacters:units length:count];
    [self.events addObject:@{@"key": @(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)),
        @"type": @(CGEventGetType(event)), @"flags": @(CGEventGetFlags(event)), @"text": text,
        @"time": @(NSProcessInfo.processInfo.systemUptime)}];
    if (self.disarmAfter && self.events.count == self.disarmAfter) fixtureSecureInput = true;
    if (self.moveAfter && self.events.count == self.moveAfter) self.moved = YES;
    if (self.cancelAfter && self.events.count == self.cancelAfter) [self cancelCapture];
}
@end

static void waitForDelivery(JardSystemDictation *paster) {
    NSDate *until = [NSDate dateWithTimeIntervalSinceNow:4];
    while (paster.hasActivity && until.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
    assert(!paster.hasActivity);
}

static void testDictationContinuation(void) {
    DictationDeliveryFixture *paster = [[DictationDeliveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) { assert(false); }];
    paster.settings[@"placement"] = @"off";
    paster.board = NSPasteboard.pasteboardWithUniqueName;
    paster.editor = AnnotationTargetFixture.new;
    paster.editor.processIdentifier = getpid();
    paster.caret = [NSValue valueWithRange:NSMakeRange(4, 2)];
    NSValue *beforeFirst = paster.caret;
    void (^deliver)(NSString *, NSString *, NSValue *) = ^(NSString *text, NSString *expected, NSValue *queuedSelection) {
        [paster rememberDestination];
        paster.takeInputRevision = paster.inputRevision;
        if (queuedSelection) paster.selectedRange = queuedSelection;
        paster.lastText = text;
        paster.expectedPaste = expected;
        [paster deliver:text mode:@"paste" warning:nil];
        waitForDelivery(paster);
        assert([paster.phase isEqual:@"inserted"] && [paster.lastText isEqual:text]);
    };
    deliver(@"Because if I don't do that,", @"Because if I don't do that,", nil);
    deliver(@"And if you see it.", @" and if you see it.", beforeFirst);
    paster.lastText = @"well, that works.";
    paster.expectedPaste = @" Well, that works.";
    [paster pasteLast];
    waitForDelivery(paster);
    assert(paster.pasteCount == 3 && [paster.lastText isEqual:@"well, that works."]);
    deliver(@"open (", @" Open (", nil);
    deliver(@"GitHub", @"GitHub", nil);
    deliver(@", then Slack.)", @", then Slack.)", nil);
    deliver(@"next", @" Next", nil);
    // Moving the caret without a keyboard event starts again at the new position.
    paster.caret = beforeFirst;
    deliver(@"And new position", @"And new position", nil);
    CGEventRef key = CGEventCreateKeyboardEvent(NULL, kVK_ANSI_A, true);
    [paster observeUnmatchedInput:kCGEventKeyDown event:key];
    CFRelease(key);
    deliver(@"Next after user input", @"Next after user input", nil);
    __block BOOL meetingDelivered = NO;
    paster.activeMeetingPaste = ^(BOOL sent) { meetingDelivered = sent; };
    deliver(@"Meeting transcript", @"Meeting transcript", nil);
    assert(meetingDelivered);
    deliver(@"And after meeting", @"And after meeting", nil);
    [paster deliver:@"Copy for manual use" mode:@"copy" warning:nil];
    deliver(@"And after copying", @"And after copying", nil);

    // Some editable applications expose a focused element but no selection range.
    [paster activationChanged:nil];
    paster.caret = nil;
    deliver(@"No published selection.", @"No published selection.", nil);
    paster.events = NSMutableArray.array;
    [paster rememberDestination];
    paster.takeInputRevision = paster.inputRevision;
    [paster deliver:@"typing in the same field" mode:@"type" warning:nil];
    waitForDelivery(paster);
    NSMutableString *typed = NSMutableString.string;
    for (NSDictionary *event in paster.events)
        if ([event[@"type"] unsignedIntValue] == kCGEventKeyDown) [typed appendString:event[@"text"]];
    assert([typed isEqual:@" Typing in the same field"]);
    [paster.board releaseGlobally];
    puts("Consecutive paste, Paste Last and typed output shared sentence-aware joins and ended on caret moves, user input and standalone output.");
}

static void testQueuedClipboardRestore(void) {
    for (NSUInteger scenario = 0; scenario < 3; ++scenario) {
        DictationDeliveryFixture *paster = [[DictationDeliveryFixture alloc] initWithSend:
            ^(NSString *command, NSDictionary *payload, JardDictationReply reply) { assert(false); }];
        paster.settings[@"placement"] = @"off";
        paster.board = NSPasteboard.pasteboardWithUniqueName;
        paster.editor = AnnotationTargetFixture.new;
        paster.editor.processIdentifier = getpid();
        paster.caret = [NSValue valueWithRange:NSMakeRange(3, 0)];
        paster.expectedPastes = @[@"First.", @" Second."];
        NSData *data = [@"Fixture original format" dataUsingEncoding:NSUTF8StringEncoding];
        NSPasteboardItem *original = NSPasteboardItem.new;
        [original setString:@"Original clipboard" forType:NSPasteboardTypeString];
        [original setData:data forType:@"local.jard.fixture"];
        NSPasteboardItem *attachment = NSPasteboardItem.new;
        [attachment setData:data forType:@"local.jard.attachment"];
        [paster.board writeObjects:@[original, attachment]];
        NSMutableArray *consumed = NSMutableArray.array;
        NSTimeInterval firstPosted = NSProcessInfo.processInfo.systemUptime;
        __block NSTimeInterval secondRead = 0;
        __weak DictationDeliveryFixture *weakPaster = paster;
        paster.readPasteLater = ^(NSUInteger index) {
            DictationDeliveryFixture *paster = weakPaster;
            [consumed addObject:[paster.board stringForType:NSPasteboardTypeString] ?: @""];
            if (index == 0 && scenario == 1) {
                [paster.board clearContents];
                [paster.board setString:@"Newer external copy" forType:NSPasteboardTypeString];
            }
            if (index == 1) {
                secondRead = NSProcessInfo.processInfo.systemUptime;
                if (scenario == 2) {
                    paster.lastText = @"Explicit recovery copy";
                    [paster request:@{@"action": @"copyLast"} reply:^(id response, NSString *error) {
                        assert(response && !error);
                    }];
                }
            }
        };
        paster.lastText = @"First.";
        [paster pasteLast];
        assert(paster.pasteCount == 1);
        paster.lastText = @"second.";
        [paster pasteLast];
        assert(paster.pendingJobs.count == 1 && paster.pasteCount == 1);
        waitForDelivery(paster);
        assert([consumed isEqual:paster.expectedPastes] && paster.pasteCount == 2);
        // The second read occurs after settling, but before the first restore would have fired.
        assert(secondRead - firstPosted >= .15 && secondRead - firstPosted < .5);
        if (scenario == 2) {
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.5]];
            assert([[paster.board stringForType:NSPasteboardTypeString] isEqual:@"Explicit recovery copy"]);
        } else if (scenario == 1) {
            assert([[paster.board stringForType:NSPasteboardTypeString] isEqual:@"Newer external copy"]);
        } else {
            NSArray<NSPasteboardItem *> *restored = paster.board.pasteboardItems;
            assert(restored.count == 2 && [[restored[0] stringForType:NSPasteboardTypeString] isEqual:@"Original clipboard"]);
            assert([[restored[0] dataForType:@"local.jard.fixture"] isEqual:data]);
            assert([[restored[1] dataForType:@"local.jard.attachment"] isEqual:data]);
        }
        paster.readPasteLater = nil;
        [paster.board releaseGlobally];
    }
    puts("Queued pastes waited for delayed readers and restored the original or a newer clipboard without overwriting an explicit copy.");
}

static OSStatus unavailableClipboardData(PasteboardRef board, PasteboardItemID item, CFStringRef type, void *context) {
    *(BOOL *)context = YES;
    return badPasteboardFlavorErr;
}

static void testClipboardFormats(void) {
    DictationDeliveryFixture *paster = [[DictationDeliveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) { assert(false); }];
    paster.settings[@"placement"] = @"off";
    paster.board = NSPasteboard.pasteboardWithUniqueName;
    paster.lastText = @"Paste between two clipboard writes";
    PasteboardRef board = NULL;
    assert(PasteboardCreate((__bridge CFStringRef)paster.board.name, &board) == noErr);
    assert(PasteboardClear(board) == noErr);
    NSData *note = [@"Clipboard data with format flags" dataUsingEncoding:NSUTF8StringEncoding];
    CFStringRef noteType = CFSTR("local.jard.fixture.flagged");
    PasteboardFlavorFlags originalFlags = kPasteboardFlavorNotSaved | kPasteboardFlavorSenderTranslated;
    assert(PasteboardPutItemFlavor(board, (PasteboardItemID)1, noteType,
        (__bridge CFDataRef)note, originalFlags) == noErr);

    NSBitmapImageRep *pixel = [[NSBitmapImageRep alloc] initWithBitmapDataPlanes:NULL pixelsWide:1 pixelsHigh:1
        bitsPerSample:8 samplesPerPixel:4 hasAlpha:YES isPlanar:NO colorSpaceName:NSDeviceRGBColorSpace
        bytesPerRow:4 bitsPerPixel:32];
    memset(pixel.bitmapData, 255, 4);
    NSData *png = [pixel representationUsingType:NSBitmapImageFileTypePNG properties:@{}];
    CFStringRef pngType = (__bridge CFStringRef)NSPasteboardTypePNG;
    CFStringRef tiffType = (__bridge CFStringRef)NSPasteboardTypeTIFF;
    assert(PasteboardPutItemFlavor(board, (PasteboardItemID)2, pngType, (__bridge CFDataRef)png, 0) == noErr);

    PasteboardFlavorFlags flags = 0;
    PasteboardSynchronize(board);
    assert(PasteboardGetItemFlavorFlags(board, (PasteboardItemID)2, tiffType, &flags) == noErr &&
        (flags & kPasteboardFlavorSystemTranslated));
    [paster deliver:paster.lastText mode:@"paste" warning:nil];
    assert(paster.clipboard.count == 2);
    assert([[paster.clipboard.lastObject valueForKey:@"type"] isEqual:@[NSPasteboardTypePNG]]);
    waitForDelivery(paster);
    assert(paster.pasteCount == 1 && [paster.phase isEqual:@"inserted"]);
    assert(paster.board.pasteboardItems.count == 2);
    assert([[paster.board.pasteboardItems[0] dataForType:(__bridge NSString *)noteType] isEqual:note]);
    assert([[paster.board.pasteboardItems[1] dataForType:NSPasteboardTypePNG] isEqual:png]);
    PasteboardSynchronize(board);
    PasteboardItemID restored = NULL;
    assert(PasteboardGetItemIdentifier(board, 1, &restored) == noErr);
    assert(PasteboardGetItemFlavorFlags(board, restored, noteType, &flags) == noErr && flags == originalFlags);
    assert(PasteboardGetItemIdentifier(board, 2, &restored) == noErr);
    assert(PasteboardGetItemFlavorFlags(board, restored, tiffType, &flags) == noErr &&
        (flags & kPasteboardFlavorSystemTranslated));

    assert(PasteboardClear(board) == noErr);
    NSData *oversized = [NSMutableData dataWithLength:16 * 1024 * 1024 + 1];
    assert(PasteboardPutItemFlavor(board, (PasteboardItemID)1, noteType, (__bridge CFDataRef)oversized, 0) == noErr);
    NSInteger revision = paster.board.changeCount;
    [paster deliver:paster.lastText mode:@"paste" warning:nil];
    assert(paster.pasteCount == 1 && [paster.phase isEqual:@"saved"]);
    assert(paster.board.changeCount == revision && [paster.message containsString:@"preserved your clipboard"]);

    assert(PasteboardClear(board) == noErr);
    BOOL triedPromise = NO;
    assert(PasteboardSetPromiseKeeper(board, unavailableClipboardData, &triedPromise) == noErr);
    assert(PasteboardPutItemFlavor(board, (PasteboardItemID)1, noteType, kPasteboardPromisedData, 0) == noErr);
    revision = paster.board.changeCount;
    [paster deliver:paster.lastText mode:@"paste" warning:nil];
    assert(triedPromise && paster.pasteCount == 1 && [paster.phase isEqual:@"saved"]);
    assert(paster.board.changeCount == revision && [paster.lastText isEqual:@"Paste between two clipboard writes"]);

    CFRelease(board);
    [paster.board releaseGlobally];
    // Use a fresh board so the failed promise cannot publish a change during this case.
    paster.board = NSPasteboard.pasteboardWithUniqueName;
    [paster.board clearContents];
    [paster deliver:paster.lastText mode:@"paste" warning:nil];
    waitForDelivery(paster);
    assert(paster.pasteCount == 2 && paster.board.pasteboardItems.count == 0);
    [paster.board releaseGlobally];
    puts("Native clipboard preserved item order and flavor flags, skipped macOS translations, and kept oversized or unreadable original data untouched.");
}

static void testDictationDelivery(void) {
    DictationDeliveryFixture *paster = [[DictationDeliveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) { assert(false); }];
    paster.settings[@"placement"] = @"off";
    paster.board = [NSPasteboard pasteboardWithUniqueName];
    AnnotationTargetFixture *target = AnnotationTargetFixture.new;
    target.processIdentifier = 414;
    NSURL *bundle = [[NSURL fileURLWithPath:NSTemporaryDirectory()] URLByAppendingPathComponent:NSUUID.UUID.UUIDString];
    NSURL *versions = [bundle URLByAppendingPathComponent:@"Contents/Frameworks/Chromium Framework.framework/Versions"];
    NSFileManager *manager = NSFileManager.defaultManager;
    for (NSString *name in @[@"A", @"157.0.1.2", @"155.1", @"156.0.9.21"]) {
        assert([manager createDirectoryAtURL:[versions URLByAppendingPathComponent:name] withIntermediateDirectories:YES
            attributes:nil error:nil]);
    }
    assert(JardInsertionForApp(@"paste", @"example-browser", bundle) == JardInsertBurst);
    assert([manager removeItemAtURL:[versions URLByAppendingPathComponent:@"156.0.9.21"] error:nil]);
    assert(JardInsertionForApp(@"paste", @"example-browser", bundle) == JardInsertPaste);
    assert(JardInsertionForApp(@"paste", @"COM.MICROSOFT.WINDOWSAPP", nil) == JardInsertPaced);
    assert(JardInsertionForApp(@"type", @"com.microsoft.rdc.macos", nil) == JardInsertPaced);

    NSUInteger cursor = 0;
    NSArray *stroke = JardTypingEvents(@"A", &cursor, JardInsertPaced,
        @{@"A": @{@"code": @(kVK_ANSI_A), @"shift": @YES}}, 42);
    assert(cursor == 1 && stroke.count == 4);
    assert(CGEventGetType((__bridge CGEventRef)stroke.firstObject) == kCGEventFlagsChanged);
    assert(CGEventGetType((__bridge CGEventRef)stroke.lastObject) == kCGEventFlagsChanged);
    assert(CGEventGetFlags((__bridge CGEventRef)stroke.lastObject) == 0);
    assert(CGEventGetIntegerValueField((__bridge CGEventRef)stroke[1], kCGKeyboardEventKeycode) == kVK_ANSI_A);

    NSURL *affected = [versions URLByAppendingPathComponent:@"155.0.3.19"];
    assert([manager createDirectoryAtURL:affected withIntermediateDirectories:NO attributes:nil error:nil]);
    for (NSUInteger scenario = 0; scenario < 9; scenario++) {
        fixtureSecureInput = false;
        BOOL remote = scenario == 1 || scenario == 2 || scenario >= 6;
        target.bundleIdentifier = remote ? @"com.microsoft.windowsapp" : @"example-editor";
        target.bundleURL = scenario == 0 ? bundle : nil;
        paster.destination = (NSRunningApplication *)(id)target;
        paster.events = NSMutableArray.array;
        paster.disarmAfter = scenario == 2 ? 2 : scenario == 6 ? 1 : 0;
        paster.moveAfter = scenario == 7 ? 2 : 0;
        paster.cancelAfter = scenario == 8 ? 2 : 0;
        paster.moved = NO;
        paster.mutateDuringFocusCheck = scenario == 3;
        paster.copyAfterPaste = scenario == 5;
        paster.checks = paster.pasteCount = 0;
        [paster.board clearContents];
        NSPasteboardItem *original = NSPasteboardItem.new;
        NSData *custom = [@"other flavor" dataUsingEncoding:NSUTF8StringEncoding];
        [original setString:@"Original clipboard" forType:NSPasteboardTypeString];
        [original setData:custom forType:@"local.jard.fixture"];
        [paster.board writeObjects:@[original]];
        NSInteger revision = paster.board.changeCount;
        NSString *text = scenario == 0 ? @"1234567890123456789😀\r\nNext" :
            scenario == 6 ? @"\nNext" : remote ? @"a😀\r\nnext" : @"Regular paste";
        paster.lastText = text;
        [paster deliver:text mode:@"paste" warning:nil];
        waitForDelivery(paster);
        assert([paster.lastText isEqual:text]);
        if (scenario < 2) {
            assert([paster.phase isEqual:@"inserted"] && paster.pasteCount == 0 && paster.board.changeCount == revision);
            NSPredicate *downs = [NSPredicate predicateWithBlock:^BOOL(NSDictionary *event, NSDictionary *bindings) {
                return [event[@"type"] unsignedIntValue] == kCGEventKeyDown;
            }];
            NSArray *keys = [paster.events filteredArrayUsingPredicate:downs];
            assert([keys[0][@"text"] isEqual:scenario == 0 ? @"1234567890123456789" : @"a"]);
            assert([keys[1][@"text"] isEqual:@"😀"]);
            assert([keys[2][@"key"] unsignedIntValue] == kVK_Return &&
                   [keys[2][@"flags"] unsignedLongLongValue] == kCGEventFlagMaskShift);
            if (scenario == 1) assert([paster.events[1][@"time"] doubleValue] - [paster.events[0][@"time"] doubleValue] >= .003);
        } else if (scenario == 2 || scenario >= 7) {
            assert([paster.phase isEqual:@"saved"] && [paster.message containsString:@"part of the text"]);
            assert(paster.events.count == 2 && [paster.events.firstObject[@"text"] isEqual:@"a"] &&
                   paster.board.changeCount == revision);
        } else if (scenario == 3) {
            assert(!paster.pasteCount && [paster.message containsString:@"clipboard changed"]);
            assert([[paster.board stringForType:NSPasteboardTypeString] isEqual:@"New copy while checking focus"]);
        } else if (scenario == 4) {
            assert(paster.pasteCount == 1 && [[paster.board stringForType:NSPasteboardTypeString] isEqual:@"Original clipboard"]);
            assert([[paster.board dataForType:@"local.jard.fixture"] isEqual:custom]);
        } else if (scenario == 5) {
            assert(paster.pasteCount == 1 && [[paster.board stringForType:NSPasteboardTypeString] isEqual:@"New copy after paste"]);
        } else {
            assert([paster.phase isEqual:@"saved"] && paster.events.count == 2);
            assert([paster.events[0][@"type"] unsignedIntValue] == kCGEventFlagsChanged &&
                   [paster.events[1][@"type"] unsignedIntValue] == kCGEventFlagsChanged &&
                   [paster.events[1][@"flags"] unsignedLongLongValue] == 0);
        }
    }
    [paster.board releaseGlobally];
    fixtureSecureInput = false;
    [manager removeItemAtURL:bundle error:nil];
    puts("Native typing validated app fallbacks, scancodes and Shift edges, multiline Unicode, focus changes, cancellation, and clipboard ownership.");
}
@implementation AnnotationTargetFixture
@end

@interface AnnotationPasteFixture : JardSystemDictation
@property(nonatomic, strong) NSPasteboard *fixtureBoard;
@property(nonatomic, strong) NSMutableArray *posted;
@property(nonatomic, strong) AnnotationTargetFixture *activeApp;
@property(nonatomic) pid_t front;
@property(nonatomic) NSUInteger activations;
@property(nonatomic) BOOL accessible, editable, focused, modifiersHeld, changeFocusAfterSheet, replaceClipboardAfterSheet;
@end

@implementation AnnotationPasteFixture
- (NSPasteboard *)pasteboard { return self.fixtureBoard; }
- (BOOL)annotationAccessibilityAvailable { return self.accessible; }
- (pid_t)annotationForegroundPID { return self.front; }
- (NSRunningApplication *)foregroundOutputApplication { return (id)self.activeApp; }
- (BOOL)annotationModifiersReleased { return !self.modifiersHeld; }
- (BOOL)activateAnnotationApp:(NSRunningApplication *)app { self.activations++; self.front = app.processIdentifier; return YES; }
- (BOOL)prepareAnnotationFocus:(pid_t)pid { return self.editable && pid == self.front; }
- (BOOL)annotationPasteFocusMatches:(NSUInteger)revision {
    return self.focused && revision == [self annotationPreparationRevision];
}
- (BOOL)postAnnotationPaste {
    id entry = [self.fixtureBoard dataForType:NSPasteboardTypePNG] ?: (id)[self.fixtureBoard stringForType:NSPasteboardTypeString];
    assert(entry);
    [self.posted addObject:entry];
    if (self.posted.count == 1 && self.changeFocusAfterSheet) self.focused = NO;
    if (self.posted.count == 1 && self.replaceClipboardAfterSheet) {
        [self.fixtureBoard clearContents];
        [self.fixtureBoard setString:@"Newer user copy" forType:NSPasteboardTypeString];
    }
    return YES;
}
@end

static void testAnnotationPaste(void) {
    AnnotationPasteFixture *paster = [[AnnotationPasteFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) { assert(false); }];
    paster.fixtureBoard = [NSPasteboard pasteboardWithUniqueName];
    paster.accessible = paster.editable = YES;
    AnnotationTargetFixture *target = AnnotationTargetFixture.new;
    target.processIdentifier = 414;
    target.localizedName = @"Fixture editor";
    target.activationPolicy = NSApplicationActivationPolicyRegular;
    [paster rememberExternalApp:(id)target];
    AnnotationTargetFixture *own = AnnotationTargetFixture.new;
    own.processIdentifier = NSProcessInfo.processInfo.processIdentifier;
    own.activationPolicy = NSApplicationActivationPolicyRegular;
    [paster rememberExternalApp:(id)own];

    NSData *sheet = [@"fixture PNG" dataUsingEncoding:NSUTF8StringEncoding];
    NSData *custom = [@"original extra type" dataUsingEncoding:NSUTF8StringEncoding];
    NSString *context = @"# Annotation\nReview the chart.";
    NSString *path = @"/private/tmp/fixture-contact-sheet.png";
    for (NSUInteger scenario = 0; scenario < 10; scenario++) {
        BOOL fromPanel = scenario >= 7;
        paster.front = fromPanel ? target.processIdentifier : own.processIdentifier;
        paster.activeApp = fromPanel ? target : own;
        paster.activations = 0;
        assert([paster annotationPasteDestinationFromPanel:fromPanel].processIdentifier == target.processIdentifier);
        assert(![paster annotationPasteDestinationFromPanel:!fromPanel]);
        target.bundleIdentifier = scenario == 1 ? @"com.apple.Terminal" : @"test.editor";
        paster.posted = NSMutableArray.array;
        paster.focused = YES;
        paster.modifiersHeld = scenario == 9;
        paster.changeFocusAfterSheet = scenario == 2;
        paster.replaceClipboardAfterSheet = scenario == 3;
        paster.editable = scenario != 4;
        [paster.fixtureBoard clearContents];
        NSPasteboardItem *original = NSPasteboardItem.new;
        [original setString:@"Original clipboard" forType:NSPasteboardTypeString];
        [original setData:custom forType:@"local.jard.test-extra-type"];
        [paster.fixtureBoard writeObjects:@[original]];
        NSUInteger revision = [paster annotationPreparationRevision];
        if (scenario == 5) [paster activationChanged:nil];
        if (scenario == 8) paster.front = 999;
        __block BOOL finished = NO;
        __block NSDictionary *result;
        __block NSString *error;
        [paster pasteAnnotation:context image:scenario == 6 ? nil : sheet path:path destination:(id)target fromPanel:fromPanel preparationRevision:revision
            clipboardRevision:paster.fixtureBoard.changeCount reply:^(id value, NSString *problem) {
                result = value; error = problem; finished = YES;
            }];
        if (scenario == 9) {
            [paster activationChanged:nil];
            paster.modifiersHeld = NO;
        }
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:3];
        while (!finished && deadline.timeIntervalSinceNow > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        assert(finished && !paster.hasActivity);
        NSString *clipboard = [paster.fixtureBoard stringForType:NSPasteboardTypeString];
        if (scenario < 2 || scenario == 7) {
            assert(!error && [result[@"destination"] isEqual:target.localizedName] && [result[@"includedSheet"] boolValue]);
            assert(([paster.posted isEqualToArray:@[(scenario == 1 ? (id)path : sheet), context]]));
            assert(paster.activations == (fromPanel ? 0 : 1));
        } else if (scenario < 4) {
            assert(!result && [error containsString:@"Part of the annotation"] && [paster.posted isEqualToArray:@[sheet]]);
        } else if (scenario == 6) {
            assert(!error && ![result[@"includedSheet"] boolValue] && [paster.posted isEqualToArray:@[context]]);
        } else assert(!result && error.length && !paster.posted.count);
        if (scenario == 3) assert([clipboard isEqual:@"Newer user copy"]);
        else {
            assert([clipboard isEqual:@"Original clipboard"]);
            assert([[paster.fixtureBoard dataForType:@"local.jard.test-extra-type"] isEqual:custom]);
        }
    }
    [paster.fixtureBoard releaseGlobally];
    puts("Annotation paste delivered ordered editor and terminal payloads, preserved clipboard ownership, and kept the panel's original foreground app.");
}

@interface TestDictation : JardSystemDictation
@property(nonatomic, copy) NSString *copiedText;
@property(nonatomic, strong) AnnotationTargetFixture *outputDestination;
@property(nonatomic, strong) AnnotationTargetFixture *outputForeground;
@end

@implementation TestDictation
- (void)rememberDestination {
    if (self.outputDestination) [self setValue:self.outputDestination forKey:@"destination"];
}
- (NSRunningApplication *)foregroundOutputApplication {
    AnnotationTargetFixture *application = self.outputForeground ?: self.outputDestination;
    return (NSRunningApplication *)application;
}
- (BOOL)copyText:(NSString *)text { self.copiedText = text; return YES; }
- (BOOL)canStartCommandCapture { return !self.captureID; }
@end

@interface OutputContextFixture : TestDictation
@property(nonatomic, strong) dispatch_semaphore_t siteRequested, siteUnblocked, siteFinished;
@property(nonatomic) pid_t expectedSitePID;
@property(nonatomic, copy) NSString *siteResponse;
@property(nonatomic) BOOL expectedDetail;
@end

@implementation OutputContextFixture
- (NSDictionary *)readOutputContextForPID:(pid_t)pid deadline:(NSTimeInterval)deadline detailed:(BOOL)detailed {
    assert(!NSThread.isMainThread && pid == self.expectedSitePID);
    assert(detailed == self.expectedDetail);
    NSString *site = self.siteResponse ?: @"release.example";
    dispatch_semaphore_signal(self.siteRequested);
    assert(dispatch_semaphore_wait(self.siteUnblocked, dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC)) == 0);
    dispatch_semaphore_signal(self.siteFinished);
    if (!detailed) return @{@"site": site};
    return @{@"site": site, @"browserUrl": @"https://initial.example/notes?q=review#draft",
        @"windowTitle": @"Fixture notes window"};
}
@end

static AnnotationTargetFixture *outputTarget(NSString *name, pid_t pid) {
    AnnotationTargetFixture *application = AnnotationTargetFixture.new;
    application.localizedName = name;
    application.processIdentifier = pid;
    return application;
}

@interface MicrophoneRecoveryFixture : TestDictation
@property(nonatomic, strong) NSPasteboard *board;
@end

@implementation MicrophoneRecoveryFixture
- (NSPasteboard *)pasteboard { return self.board; }
@end

@interface ColdReopenFixture : TestDictation
@property(nonatomic) NSUInteger destinations, startCues;
@end

@implementation ColdReopenFixture
- (void)rememberDestination { self.destinations++; }
- (void)playSound:(NSString *)stage { if ([stage isEqual:@"start"]) self.startCues++; }
@end

@interface FeedbackPlayerFixture : NSObject
@property(nonatomic) float volume;
@property(nonatomic, getter=isPlaying) BOOL playing;
@property(nonatomic) NSTimeInterval playedAt, fadedAt, fadeDuration, stoppedAt;
@property(nonatomic, strong) dispatch_semaphore_t prepared, unblock, played, stopped;
@end

@implementation FeedbackPlayerFixture
- (instancetype)init {
    self = [super init];
    if (self) {
        _prepared = dispatch_semaphore_create(0);
        _played = dispatch_semaphore_create(0);
        _stopped = dispatch_semaphore_create(0);
    }
    return self;
}
- (BOOL)prepareToPlay {
    assert(!NSThread.isMainThread);
    dispatch_semaphore_signal(self.prepared);
    if (self.unblock) assert(dispatch_semaphore_wait(self.unblock, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
    return YES;
}
- (BOOL)play {
    self.playing = YES;
    self.playedAt = NSProcessInfo.processInfo.systemUptime;
    dispatch_semaphore_signal(self.played);
    return YES;
}
- (NSTimeInterval)duration { return .5; }
- (void)setVolume:(float)volume fadeDuration:(NSTimeInterval)duration {
    assert(volume == 0);
    self.fadedAt = NSProcessInfo.processInfo.systemUptime;
    self.fadeDuration = duration;
}
- (void)stop {
    self.playing = NO;
    self.stoppedAt = NSProcessInfo.processInfo.systemUptime;
    dispatch_semaphore_signal(self.stopped);
}
@end

@interface StartCueWorkerFixture : JardDictationStartCue
@property(nonatomic, strong) FeedbackPlayerFixture *player;
@end

@implementation StartCueWorkerFixture
- (AVAudioPlayer *)openSound:(NSURL *)url { return (AVAudioPlayer *)(id)self.player; }
@end

@interface StartCueTokenFixture : JardDictationStartCue
@property(nonatomic) NSUInteger withdrawals;
@property(nonatomic) NSTimeInterval deadline;
@end

@implementation StartCueTokenFixture
- (void)withdraw { self.withdrawals++; }
@end

@interface FeedbackControllerFixture : ColdReopenFixture
@property(nonatomic, strong) NSMutableArray<StartCueTokenFixture *> *provisional;
@end

@implementation FeedbackControllerFixture
- (void)playSound:(NSString *)stage {
    if ([self.settings[@"sound"] boolValue] && [self.settings[@"soundVolume"] floatValue] > 0)
        [super playSound:stage];
}
- (JardDictationStartCue *)newShortcutStartCueAt:(NSTimeInterval)deadline preferences:(NSDictionary *)preferences {
    StartCueTokenFixture *cue = StartCueTokenFixture.new;
    cue.deadline = deadline;
    [self.provisional addObject:cue];
    return cue;
}
@end

@interface CaptureEnvironmentFixture : JardRecordingEnvironment
@property(nonatomic, copy) NSString *mode;
@property(nonatomic) NSUInteger ended;
@property(nonatomic) BOOL active;
@end

@implementation CaptureEnvironmentFixture
- (void)beginWithMode:(NSString *)mode percent:(double)percent {
    assert(!self.active && percent == 20);
    self.mode = mode;
    self.active = YES;
}
- (void)finish:(dispatch_block_t)completion {
    if (!self.active) return;
    self.active = NO;
    self.ended++;
    if (completion) completion();
}
@end

@interface RestoringAudioFixture : CaptureEnvironmentFixture
@property(nonatomic, copy) dispatch_block_t afterRestore;
@end

@implementation RestoringAudioFixture
- (void)finish:(dispatch_block_t)completion {
    if (completion) self.afterRestore = completion;
    [super finish:nil];
}
@end

@interface AnnotationCueEnvironmentFixture : JardRecordingEnvironment
@property(nonatomic) BOOL recording;
@property(nonatomic, copy) dispatch_block_t annotationCompletion;
@end

@implementation AnnotationCueEnvironmentFixture
- (void)setAnnotationRecording:(BOOL)recording mode:(NSString *)mode percent:(double)percent
    completion:(dispatch_block_t)completion {
    self.recording = recording;
    if (completion) self.annotationCompletion = completion;
}
@end

@interface AuditionClipFixture : NSObject
@property(nonatomic, copy) NSString *cue;
@property(nonatomic, strong) NSMutableArray<NSString *> *events;
@property(nonatomic, strong) NSMutableArray<NSNumber *> *volumes;
@property(nonatomic) float volume;
@property(nonatomic, getter=isPlaying) BOOL playing;
@end

@implementation AuditionClipFixture
- (BOOL)play {
    self.playing = YES;
    [self.volumes addObject:@(self.volume)];
    [self.events addObject:self.cue];
    return YES;
}
- (void)setVolume:(float)volume fadeDuration:(NSTimeInterval)duration {
    assert(self.playing && volume == 0 && duration == .020);
    [self.events addObject:[@"fading " stringByAppendingString:self.cue]];
}
- (void)stop {
    self.playing = NO;
    [self.events addObject:[@"stopped " stringByAppendingString:self.cue]];
}
@end

@interface SoundSettingsFixture : TestDictation
@property(nonatomic, strong) NSMutableArray<NSString *> *played, *auditioned;
@property(nonatomic, strong) NSMutableArray<NSNumber *> *auditionVolumes;
@end

@implementation SoundSettingsFixture
- (void)playSound:(NSString *)cue { [self.played addObject:cue]; }
- (AVAudioPlayer *)previewPlayerForCue:(NSString *)cue style:(NSString *)style {
    if (![style isEqual:@"signature"]) return nil;
    AuditionClipFixture *clip = AuditionClipFixture.new;
    clip.cue = cue;
    clip.events = self.auditioned;
    clip.volumes = self.auditionVolumes;
    return (AVAudioPlayer *)(id)clip;
}
@end

static void testSoundSettings(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictation"];
    SoundSettingsFixture *fixture = [[SoundSettingsFixture alloc] initWithSend:nil];
    fixture.played = NSMutableArray.array;
    fixture.auditioned = NSMutableArray.array;
    fixture.auditionVolumes = NSMutableArray.array;
    assert([fixture.settings[@"soundSet"] isEqual:@"classic"]);
    assert([[fixture soundURL:@"error" style:@"classic"].lastPathComponent isEqual:@"Basso.aiff"]);
    assert([[fixture soundURL:@"start" style:@"soft"].lastPathComponent isEqual:@"Purr.aiff"]);
    assert([[fixture soundURL:@"stop" style:@"glass"].lastPathComponent isEqual:@"Ping.aiff"]);
    if ([fixture soundURL:@"start" style:@"classic"]) {
        assert([[fixture soundURL:@"success" style:@"classic"] isEqual:[fixture soundURL:@"stop" style:@"classic"]]);
        assert([[fixture soundURL:@"start" style:@"soft-tone"].path hasSuffix:@"/dictation-sounds/soft/start.wav"]);
        assert([fixture soundURL:@"success" style:@"thock"] && ![fixture soundURL:@"done" style:@"thock"]);
    }
    [fixture request:@{@"action": @"previewSound", @"cue": @"done"}
        reply:^(NSDictionary *status, NSString *error) { assert(!status && [error containsString:@"unavailable"]); }];
    for (NSString *style in @[@"signature", @"click", @"soft-tone", @"wood", @"analog", @"thock", @"soft", @"glass", @"signature"]) {
        [fixture request:@{@"action": @"set", @"key": @"soundSet", @"value": style}
            reply:^(NSDictionary *status, NSString *error) { assert(!error && [status[@"soundSet"] isEqual:style]); }];
        assert([[[[JardSystemDictation alloc] initWithSend:nil] settings][@"soundSet"] isEqual:style]);
    }
    [fixture request:@{@"action": @"set", @"key": @"soundVolume", @"value": @37}
        reply:^(NSDictionary *status, NSString *error) { assert(!error && [status[@"soundVolume"] isEqual:@37]); }];
    [fixture request:@{@"action": @"set", @"key": @"soundSet", @"value": @"classic"} reply:^(id status, NSString *error) { assert(status && !error); }];
    __block NSUInteger previewVersion = 0;
    __block BOOL announcedStop = NO;
    fixture.preferencesChanged = ^(NSDictionary *status) {
        if (![status[@"soundPreview"] boolValue] && [status[@"soundPreviewRevision"] unsignedIntegerValue] > previewVersion)
            announcedStop = YES;
    };
    void (^preview)(NSString *) = ^(NSString *cue) {
        [fixture request:@{@"action": @"previewSound", @"cue": cue, @"style": @"signature"}
            reply:^(NSDictionary *status, NSString *error) {
                assert(!error && [status[@"soundPreview"] boolValue] && [status[@"soundSet"] isEqual:@"classic"]);
                NSDictionary *animation = status[@"soundPreviewAnimation"];
                assert([animation[@"cue"] isEqual:cue] && [animation[@"style"] isEqual:@"signature"]);
                assert([animation[@"id"] isEqual:status[@"soundPreviewRevision"]]);
                assert(fabs([animation[@"startedAt"] doubleValue] / 1000 - NSDate.date.timeIntervalSince1970) < .5);
                assert([animation[@"id"] unsignedIntegerValue] > previewVersion);
                previewVersion = [animation[@"id"] unsignedIntegerValue];
            }];
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    };
    preview(@"sample");
    assert(([fixture.auditioned isEqual:@[@"start"]]));
    preview(@"error");
    assert([fixture.auditioned[1] isEqual:@"fading start"] && [fixture.auditioned containsObject:@"error"]);
    assert(([fixture.auditionVolumes isEqual:@[@.37f, @.37f]]));
    [fixture request:@{@"action": @"stopSoundPreview"}
        reply:^(NSDictionary *status, NSString *error) {
            assert(!error && ![status[@"soundPreview"] boolValue] && !status[@"soundPreviewAnimation"] && announcedStop);
        }];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:2.15]];
    assert(fixture.auditioned.count == 6 && [fixture.auditioned containsObject:@"stopped start"]);
    assert([fixture.auditioned containsObject:@"fading error"] && [fixture.auditioned.lastObject isEqual:@"stopped error"]);
    [fixture.auditioned removeAllObjects];
    preview(@"sample");
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:2.15]];
    assert(([fixture.auditioned isEqual:@[@"start", @"stop", @"done"]]));
    assert(fixture.lastStopCueAt == 0 && !fixture.played.count);
    [fixture setSDKCaptureActive:YES];
    assert(![fixture.status[@"soundPreview"] boolValue] && [fixture.auditioned.lastObject isEqual:@"fading done"]);
    [fixture request:@{@"action": @"previewSound", @"cue": @"start", @"style": @"signature"}
        reply:^(NSDictionary *status, NSString *error) { assert(!status && [error containsString:@"busy"]); }];
    [fixture setSDKCaptureActive:NO];
    [fixture request:@{@"action": @"set", @"key": @"sound", @"value": @NO} reply:^(id status, NSString *error) { assert(status && !error); }];
    preview(@"start");
    assert(fixture.auditionVolumes.lastObject.floatValue == .5f && ![fixture.status[@"sound"] boolValue]);
    [fixture request:@{@"action": @"set", @"key": @"sound", @"value": @YES} reply:^(id status, NSString *error) { assert(status && !error); }];
    [fixture request:@{@"action": @"set", @"key": @"soundVolume", @"value": @0} reply:^(id status, NSString *error) { assert(status && !error); }];
    preview(@"start");
    assert(fixture.auditionVolumes.lastObject.floatValue == .5f && [fixture.status[@"soundVolume"] isEqual:@0]);
    for (NSString *step in @[@"practice", @"ready", @"ready", @"done"]) {
        [fixture request:@{@"action": @"set", @"key": @"onboarding", @"value": step}
            reply:^(NSDictionary *status, NSString *error) { assert(!error && [status[@"onboarding"] isEqual:step]); }];
    }
    assert(([fixture.played isEqual:@[@"success"]]));
    [fixture shutdown];
    [defaults removeObjectForKey:@"JardSystemDictation"];
    puts("Sound choices persisted, legacy assets resolved, auditions owned their clips and timing, and the setup-ready cue played once.");
}

static void testAnnotationCues(void) {
    SoundSettingsFixture *fixture = [[SoundSettingsFixture alloc] initWithSend:nil];
    fixture.played = NSMutableArray.array;
    AnnotationCueEnvironmentFixture *environment = AnnotationCueEnvironmentFixture.new;
    fixture.recordingEnvironment = environment;
    assert([[fixture soundURL:@"annotationSent" style:@"signature"] isEqual:[fixture soundURL:@"done" style:@"signature"]]);
    assert([[fixture soundURL:@"annotationSent" style:@"classic"].lastPathComponent isEqual:@"annotationSent.mp3"]);
    assert([[fixture soundURL:@"annotationUndo" style:@"signature"].lastPathComponent isEqual:@"annotationUndo.mp3"]);

    [fixture setAnnotationPhase:@"starting"];
    [fixture setAnnotationPhase:@"picking"];
    assert(!environment.recording && !fixture.played.count);
    [fixture setAnnotationPhase:@"recording"];
    [fixture setAnnotationPhase:@"recording"];
    assert(environment.recording && [fixture.played isEqual:@[@"start"]]);
    [fixture setAnnotationPhase:@"saving"];
    assert(!environment.recording && [fixture.played isEqual:@[@"start"]]);
    [fixture setAnnotationPhase:@"finished"];
    environment.annotationCompletion();
    assert(([fixture.played isEqual:@[@"start", @"stop"]]));

    [fixture setAnnotationPhase:@"starting"];
    [fixture setAnnotationPhase:@"recording"];
    [fixture setAnnotationPhase:@"saving"];
    dispatch_block_t staleStop = environment.annotationCompletion;
    [fixture setAnnotationPhase:@"error"];
    staleStop();
    environment.annotationCompletion();
    assert(([fixture.played isEqual:@[@"start", @"stop", @"start", @"error"]]));

    [fixture setAnnotationPhase:@"starting"];
    [fixture setAnnotationPhase:@"recording"];
    [fixture setAnnotationPhase:@"cancelling"];
    assert(!environment.recording && [fixture.played.lastObject isEqual:@"start"]);
    [fixture setAnnotationPhase:@"cancelled"];
    environment.annotationCompletion();
    assert([fixture.played.lastObject isEqual:@"cancel"]);
    [fixture shutdown];
    puts("Annotation cues tracked confirmed recording and discard, waited for playback restoration, and discarded an outdated stop.");
}

static void testCompletionSounds(void) {
    for (NSString *scenario in @[@"fast", @"slow", @"restoring", @"classic", @"thock", @"empty", @"failed", @"cancelled", @"command"]) {
        __block JardDictationReply transcript;
        RestoringAudioFixture *environment = RestoringAudioFixture.new;
        SoundSettingsFixture *fixture = [[SoundSettingsFixture alloc] initWithSend:
            ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
                if ([payload[@"action"] isEqual:@"transcribe"]) transcript = [reply copy];
                else if (reply) reply(@{}, nil);
            }];
        fixture.played = NSMutableArray.array;
        fixture.recordingEnvironment = environment;
        fixture.settings[@"placement"] = @"off";
        fixture.settings[@"retention"] = @0;
        fixture.settings[@"output"] = @"copy";
        fixture.settings[@"sound"] = @YES;
        fixture.settings[@"soundVolume"] = @70;
        fixture.settings[@"soundSet"] = [@[@"classic", @"thock"] containsObject:scenario] ? scenario : @"signature";
        if ([scenario isEqual:@"command"]) {
            [fixture control:@"start" reply:^(id status, NSString *error) { assert(status && !error); }];
        } else {
            fixture.shortcut = (JardShortcut){.phase = JardHeld, .pressedAt = NSProcessInfo.processInfo.systemUptime - 1};
            [fixture begin];
        }
        assert(fixture.captureID && environment.active);
        assert(([fixture handleCapture:@{@"event": @"dictationReady", @"dictation": fixture.captureID,
            @"id": @"sample-stage", @"seconds": @1}]));
        assert(transcript && environment.afterRestore && !fixture.played.count);
        BOOL restoring = [scenario isEqual:@"restoring"];
        if (!restoring) {
            environment.afterRestore();
            environment.afterRestore = nil;
            assert(([fixture.played isEqual:@[@"stop"]]));
            if (![scenario isEqual:@"fast"]) fixture.lastStopCueAt -= 2.01;
        }
        if ([scenario isEqual:@"cancelled"]) {
            [fixture control:@"cancel" reply:^(id status, NSString *error) { assert(status && !error); }];
        }
        transcript(@{@"text": [scenario isEqual:@"empty"] ? @"" : @"A sample take"},
            [scenario isEqual:@"failed"] ? @"Synthetic model failure." : nil);
        if (restoring) {
            assert(!fixture.played.count && [fixture.copiedText isEqual:@"A sample take"]);
            environment.afterRestore();
            environment.afterRestore = nil;
        }
        assert([fixture.played containsObject:@"done"] == [scenario isEqual:@"slow"]);
        if ([scenario isEqual:@"slow"]) {
            assert(([fixture.played isEqual:@[@"stop", @"done"]]));
            [fixture deliver:@"Previously saved" mode:@"copy" warning:nil];
            assert(([fixture.played isEqual:@[@"stop", @"done"]]));
        }
        [fixture shutdown];
    }
    puts("Completion sound followed slow keyboard output only, respecting delayed audio restoration, failed takes, style, and command origin.");
}

static void testReleaseOutputContext(void) {
    for (NSString *scenario in @[@"site", @"changed", @"deadline", @"cancel", @"raw", @"secure", @"app-only"]) {
        NSMutableArray<NSDictionary *> *submitted = NSMutableArray.array, *discarded = NSMutableArray.array;
        OutputContextFixture *dictation = [[OutputContextFixture alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([payload[@"action"] isEqual:@"transcribe"]) [submitted addObject:payload];
            else if ([payload[@"action"] isEqual:@"discard"]) [discarded addObject:payload];
            else if (reply) reply(@{}, nil);
        }];
        dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
        dictation.settings[@"placement"] = @"off";
        dictation.settings[@"sound"] = @NO;
        dictation.settings[@"retention"] = @0;
        dictation.settings[@"outputProfiles"] = @"site:release.example | lowercase\napp:Release | trim";
        BOOL appOnly = [scenario isEqual:@"app-only"];
        if (appOnly) dictation.settings[@"outputProfiles"] = @"[{\"target\":\"app:Release\",\"instruction\":\"Keep site: labels\"}]";
        if ([scenario isEqual:@"site"]) dictation.settings[@"outputProfiles"] = @"[{\"target\":\"site\\u003arelease.example\",\"steps\":[\"lowercase\"]}]";
        dictation.outputDestination = outputTarget(@"Opening", 31001);
        dictation.outputForeground = outputTarget(@"Release", 31002);
        dictation.expectedSitePID = 31002;
        dictation.siteRequested = dispatch_semaphore_create(0);
        dictation.siteUnblocked = dispatch_semaphore_create(0);
        dictation.siteFinished = dispatch_semaphore_create(0);
        JardDictationReply acknowledge = ^(id result, NSString *error) { assert(result && !error); };
        BOOL raw = [scenario isEqual:@"raw"], secure = [scenario isEqual:@"secure"];
        [dictation control:raw ? @"dictation-start-raw" : @"start" reply:acknowledge];
        assert(dispatch_semaphore_wait(dictation.siteRequested, DISPATCH_TIME_NOW) != 0);
        fixtureSecureInput = secure;
        [dictation control:@"stop" reply:acknowledge];
        if (!raw && !secure && !appOnly)
            assert(dispatch_semaphore_wait(dictation.siteRequested, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
        assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": dictation.captureID,
            @"id": @"first-stage", @"seconds": @1}]));
        assert([dictation.pendingJobs.firstObject valueForKey:@"destination"] == dictation.outputDestination);
        if (raw || secure || appOnly) {
            assert(submitted.count == 1);
            NSDictionary *profile = submitted[0][@"outputProfile"];
            if (raw) assert(!profile.count);
            else assert([profile[@"app"] isEqual:@"Release"] && ![profile[@"site"] length]);
            assert(dispatch_semaphore_wait(dictation.siteRequested, DISPATCH_TIME_NOW) != 0);
            fixtureSecureInput = false;
            [dictation shutdown];
            continue;
        }
        assert(!submitted.count && !dictation.captureID);
        if ([scenario isEqual:@"changed"]) {
            [dictation activationChanged:nil];
            dictation.outputForeground = outputTarget(@"Next", 31003);
            dictation.settings[@"outputProfiles"] = @"app:Next | sentence";
            [dictation control:@"start" reply:acknowledge];
            [dictation control:@"stop" reply:acknowledge];
            assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": dictation.captureID,
                @"id": @"second-stage", @"seconds": @1}]));
            assert(!submitted.count);
        } else if ([scenario isEqual:@"cancel"]) {
            [dictation control:@"cancel" reply:acknowledge];
            assert(discarded.count == 1 && [discarded[0][@"id"] isEqual:@"first-stage"]);
        }
        BOOL slow = [scenario isEqual:@"deadline"];
        if (!slow) dispatch_semaphore_signal(dictation.siteUnblocked);
        NSDate *budget = [NSDate dateWithTimeIntervalSinceNow:1.7];
        while (!submitted.count && !discarded.count && [budget timeIntervalSinceNow] > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        if (slow) dispatch_semaphore_signal(dictation.siteUnblocked);
        assert(dispatch_semaphore_wait(dictation.siteFinished, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.04]];
        if ([scenario isEqual:@"cancel"]) assert(!submitted.count);
        else {
            assert(submitted.count == ([scenario isEqual:@"changed"] ? 2 : 1));
            assert([submitted[0][@"id"] isEqual:@"first-stage"]);
            assert([submitted[0][@"outputProfile"][@"app"] isEqual:@"Release"]);
            assert([submitted[0][@"outputProfile"][@"site"] isEqual:[scenario isEqual:@"site"] ? @"release.example" : @""]);
            if (submitted.count == 2) {
                assert([submitted[1][@"id"] isEqual:@"second-stage"]);
                assert([submitted[1][@"outputProfile"][@"app"] isEqual:@"Next"]);
            }
        }
        [dictation shutdown];
    }
    puts("Release selected the output app and page, retained the insertion target, and fenced changed, late, cancelled and raw reads.");
}

static void testIntendedOutputContext(void) {
    for (NSString *scenario in @[@"rewrite", @"switched", @"tab", @"script", @"unapproved", @"discard",
                                 @"detailed", @"revoked-detail", @"secure-detail"]) {
        BOOL detailed = [scenario isEqual:@"detailed"] || [scenario hasSuffix:@"-detail"];
        __block NSDictionary *submitted;
        OutputContextFixture *dictation = [[OutputContextFixture alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([payload[@"action"] isEqual:@"transcribe"]) submitted = payload[@"outputProfile"];
            else if (reply) reply(@{}, nil);
        }];
        dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
        dictation.settings[@"placement"] = @"off";
        dictation.settings[@"sound"] = @NO;
        dictation.settings[@"retention"] = @0;
        dictation.settings[@"outputProfiles"] = @"[{\"target\":\"default\",\"steps\":[\"rewrite\"],\"instruction\":\"Keep the meaning.\"}]";
        if (detailed || [scenario isEqual:@"script"] || [scenario isEqual:@"unapproved"]) {
            dictation.settings[@"outputProfiles"] = @"default | local:caption";
            dictation.settings[@"outputScript"] = @"/tmp/fixture-config.ts";
            if (![scenario isEqual:@"unapproved"]) dictation.settings[@"outputScriptApproval"] = @{
                @"path": dictation.settings[@"outputScript"], @"digest": [@"a" stringByPaddingToLength:64 withString:@"a" startingAtIndex:0],
                @"grants": detailed ? @{@"pageContext": @YES} : @{}};
        }
        dictation.outputDestination = outputTarget(@"Opening", 31001);
        dictation.outputForeground = outputTarget(@"Browser", 31002);
        dictation.expectedSitePID = 31002;
        dictation.expectedDetail = detailed;
        dictation.siteResponse = @"initial.example";
        dictation.siteRequested = dispatch_semaphore_create(0);
        dictation.siteUnblocked = dispatch_semaphore_create(0);
        dictation.siteFinished = dispatch_semaphore_create(0);
        if ([scenario isEqual:@"discard"]) {
            dictation.shortcut = (JardShortcut){.mode = JardHold, .phase = JardHeld,
                .pressedAt = NSProcessInfo.processInfo.systemUptime};
            [dictation begin];
            [dictation discardShortcutTake];
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.3]];
            assert(dispatch_semaphore_wait(dictation.siteRequested, DISPATCH_TIME_NOW) != 0 && !submitted);
            [dictation shutdown];
            continue;
        }
        JardDictationReply acknowledge = ^(id result, NSString *error) { assert(result && !error); };
        [dictation control:@"start" reply:acknowledge];
        BOOL unapproved = [scenario isEqual:@"unapproved"];
        BOOL switched = [scenario isEqual:@"switched"];
        BOOL changed = switched || [scenario isEqual:@"tab"];
        if (detailed || unapproved) {
            assert(dispatch_semaphore_wait(dictation.siteRequested, DISPATCH_TIME_NOW) != 0);
        } else {
            assert(dispatch_semaphore_wait(dictation.siteRequested, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
            dispatch_semaphore_signal(dictation.siteUnblocked);
            assert(dispatch_semaphore_wait(dictation.siteFinished, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        }
        if (changed) {
            [dictation activationChanged:nil];
            if (switched) {
                dictation.outputForeground = outputTarget(@"Next", 31003);
                dictation.expectedSitePID = 31003;
            }
            dictation.siteResponse = @"fresh.example";
        }
        BOOL secureDetail = [scenario isEqual:@"secure-detail"];
        fixtureSecureInput = secureDetail;
        [dictation control:@"stop" reply:acknowledge];
        if (changed || (detailed && !secureDetail)) {
            assert(dispatch_semaphore_wait(dictation.siteRequested, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
            if ([scenario isEqual:@"revoked-detail"]) dictation.settings[@"outputScriptApproval"] = @{};
            dispatch_semaphore_signal(dictation.siteUnblocked);
        }
        assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": dictation.captureID,
            @"id": @"intent-stage", @"seconds": @1}]));
        assert([dictation.pendingJobs.firstObject valueForKey:@"destination"] == dictation.outputDestination);
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:1.2];
        while (!submitted && [deadline timeIntervalSinceNow] > 0)
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        assert([submitted[@"app"] isEqual:switched ? @"Next" : @"Browser"]);
        assert([submitted[@"site"] isEqual:changed ? @"fresh.example" : (unapproved || secureDetail) ? @"" : @"initial.example"]);
        if ([scenario isEqual:@"detailed"]) {
            assert([submitted[@"browserUrl"] isEqual:@"https://initial.example/notes?q=review#draft"]);
            assert([submitted[@"windowTitle"] isEqual:@"Fixture notes window"]);
        } else {
            assert(!submitted[@"browserUrl"] && !submitted[@"windowTitle"]);
        }
        fixtureSecureInput = NO;
        assert(dispatch_semaphore_wait(dictation.siteRequested, DISPATCH_TIME_NOW) != 0);
        [dictation shutdown];
    }
    puts("Native output context respected intent, release, app and tab changes, secure input, and per-version page context grants.");
}

static void testCommandDictation(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictationHistory"];
    NSMutableArray<JardDictationReply> *transcriptions = NSMutableArray.array;
    NSMutableArray<NSDictionary *> *options = NSMutableArray.array;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([payload[@"action"] isEqual:@"transcribe"]) {
            [options addObject:payload];
            [transcriptions addObject:[reply copy]];
        } else {
            assert(([command isEqual:@"jard.record"] || [@[@"stop", @"cancel"] containsObject:payload[@"action"]]));
            assert(![payload[@"releaseTail"] boolValue]);
            reply(@{}, nil);
        }
    }];
    dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"sound"] = @NO;
    dictation.settings[@"retention"] = @7;
    dictation.settings[@"output"] = @"copy";
    dictation.settings[@"vocabulary"] = @"Fixture phrase";
    dictation.settings[@"outputProfiles"] = @"app:Fixture Editor.app | lowercase";
    AnnotationTargetFixture *target = AnnotationTargetFixture.new;
    target.localizedName = @"Fixture process";
    target.bundleIdentifier = @"local.jard.fixture.editor";
    target.bundleURL = [[NSURL fileURLWithPath:NSTemporaryDirectory() isDirectory:YES]
        URLByAppendingPathComponent:@"Fixture Editor.app" isDirectory:YES];
    dictation.outputDestination = target;
    JardDictationReply acknowledge = ^(id result, NSString *error) { assert(result && !error); };
    void (^ready)(void) = ^{
        assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": dictation.captureID,
            @"id": NSUUID.UUID.UUIDString, @"seconds": @1}]));
    };

    [dictation control:@"dictation-start-raw" reply:acknowledge];
    [dictation control:@"stop" reply:acknowledge];
    ready();
    assert([options[0][@"outputProfile"] count] == 0 && [options[0][@"vocabulary"] count] == 0);
    transcriptions[0](@{@"text": @"Plain raw words", @"processed": @"Processed words"}, nil);
    assert([dictation.copiedText isEqual:@"Plain raw words"]);
    assert([dictation.history.firstObject[@"kind"] isEqual:@"raw"]);

    __block NSUInteger returned = 0;
    [dictation control:@"start" reply:acknowledge];
    [dictation control:@"dictation-stop-print" reply:^(id result, NSString *error) {
        assert(!error && [result[@"text"] isEqual:@"Processed words"]);
        returned++;
    }];
    assert(!returned);
    ready();
    assert([options[1][@"outputProfile"] count] > 0 && [options[1][@"vocabulary"] count] == 1);
    NSDictionary *destination = options[1][@"outputProfile"];
    assert([destination[@"app"] isEqual:@"Fixture process"]);
    assert([destination[@"bundle"] isEqual:@"local.jard.fixture.editor"]);
    assert([destination[@"bundleName"] hasPrefix:@"Fixture Editor"]);
    [dictation control:@"dictation-toggle-raw" reply:acknowledge];
    NSString *active = dictation.captureID;
    assert(active && !returned);
    transcriptions[1](@{@"text": @"Plain raw words", @"processed": @"Processed words"}, nil);
    assert(returned == 1 && [dictation.captureID isEqual:active]);
    assert([dictation.copiedText isEqual:@"Plain raw words"] && [dictation.lastText isEqual:@"Plain raw words"]);
    assert([dictation.history.firstObject[@"kind"] isEqual:@"printed"]);
    assert([[[TestDictation alloc] initWithSend:nil].lastText isEqual:@"Plain raw words"]);

    [dictation control:@"dictation-stop-print" reply:^(id result, NSString *error) {
        assert(!error && [result[@"status"] isEqual:@"no-speech"]);
        returned++;
    }];
    ready();
    transcriptions[2](@{@"text": @""}, nil);
    assert(returned == 2 && dictation.history.count == 2);
    [dictation control:@"start" reply:acknowledge];
    [dictation control:@"dictation-stop-print" reply:^(id result, NSString *error) {
        assert(!error && [result[@"status"] isEqual:@"no-speech"]);
        returned++;
    }];
    assert(([dictation handleCapture:@{@"event": @"error", @"dictation": dictation.captureID, @"noSpeech": @YES,
        @"openingAbandoned": @YES}]));
    assert(returned == 3 && !dictation.captureID && [dictation.phase isEqual:@"idle"]);
    [dictation control:@"start" reply:acknowledge];
    [dictation control:@"dictation-stop-print" reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"cancelled"]);
        returned++;
    }];
    ready();
    [dictation control:@"cancel" reply:acknowledge];
    transcriptions[3](@{@"text": @"Never delivered"}, nil);
    assert(returned == 4 && dictation.history.count == 2 && [dictation.lastText isEqual:@"Plain raw words"]);
    [defaults removeObjectForKey:@"JardSystemDictationHistory"];
    puts("Command dictation printed only to its caller, skipped raw processing, and preserved Paste Last through reopen, silence, and cancellation.");
}

static void testOutputOverview(void) {
    __block NSDictionary *request = nil;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            assert([command isEqual:@"jard.output"]);
            request = payload;
            reply(@{@"profile": @"", @"steps": @[], @"text": @"Raw fixture text", @"warnings": @[],
                @"vocabulary": @[], @"skipped": @[]}, nil);
        }];
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"outputProfiles"] = @"app:Notes | rewrite";
    dictation.settings[@"replacements"] = @"jaro audio => Jaro";
    [dictation request:@{@"action": @"tryOutput", @"text": @"jaro audio", @"profiles": @"default | script"}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"text"] isEqual:@"Jaro"]);
        }];
    assert([request[@"builtinsOnly"] boolValue] && [request[@"profiles"] isEqual:@"app:Notes | rewrite"]);
    assert([request[@"app"] isEqual:@""] && [request[@"site"] isEqual:@""]);
    [dictation request:@{@"action": @"copyOutputAgentPrompt"} reply:^(id result, NSString *error) { assert(result && !error); }];
    assert([dictation.copiedText containsString:@"jardctl settings get dictation.outputProfiles"]);
    assert([dictation.copiedText containsString:@"jardctl settings apply -"]);
    assert([dictation.copiedText containsString:@"Rumble2 Flash"] && [dictation.copiedText containsString:@"local Qwen"]);
    [dictation shutdown];
    puts("Output sample used saved rules and the standard fallback, and the agent prompt named the supported local CLI.");
}

static void testOutputScriptReview(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictation"];
    __block NSString *policy = @"normal";
    __block NSString *digest = [@"a" stringByPaddingToLength:64 withString:@"a" startingAtIndex:0];
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        assert([command isEqual:@"jard.inspectOutputScript"]);
        assert([payload[@"path"] isEqual:@"/private/tmp/fixture-script"]);
        reply(@{@"digest": digest, @"kind": @"embedded", @"policy": policy, @"permissions": @{@"network": @YES, @"programs": @YES}, @"transforms": @[@{@"id": @"add-label", @"name": @"Add label", @"description": @""}]}, nil);
    }];
    dictation.settings[@"outputScript"] = @"/private/tmp/fixture-script";
    __block NSString *token = nil;
    JardDictationReply remember = ^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"review"]);
        assert([result[@"transforms"][0][@"id"] isEqual:@"add-label"]);
        assert([result[@"permissions"][@"network"] isEqual:@YES]);
        assert([result[@"permissions"][@"pageContext"] isEqual:@YES]);
        assert(![result[@"grants"] count]);
        token = result[@"token"];
        assert(token.length);
    };
    [dictation request:@{@"action": @"inspectOutputScript"} reply:remember];
    digest = [@"b" stringByPaddingToLength:64 withString:@"b" startingAtIndex:0];
    [dictation request:@{@"action": @"approveOutputScript", @"token": token} reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"changed"]);
    }];
    assert(![dictation.settings[@"outputScriptApproval"] count]);
    [dictation request:@{@"action": @"inspectOutputScript"} reply:remember];
    [dictation request:@{@"action": @"approveOutputScript", @"token": token} reply:^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"approved"] && [result[@"version"] isEqual:@"bbbbbbbbbbbb"]);
        assert([result[@"transforms"][0][@"name"] isEqual:@"Add label"]);
        assert([result[@"kind"] isEqual:@"embedded"]);
    }];
    assert([dictation.settings[@"outputScriptApproval"][@"digest"] isEqual:digest]);
    assert(!dictation.status[@"outputScriptApproval"]);
    [dictation request:@{@"action": @"approveOutputScript", @"token": token, @"grants": @{@"pageContext": @YES}}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"grants"] isEqual:@{@"pageContext": @YES}]);
        }];
    assert([dictation.settings[@"outputScriptApproval"][@"grants"][@"pageContext"] isEqual:@YES]);
    [dictation request:@{@"action": @"approveOutputScript", @"token": token, @"grants": @{@"programs": @YES, @"network": @NO}}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"grants"] isEqual:@{@"programs": @YES}]);
        }];
    assert([dictation.settings[@"outputScriptApproval"][@"grants"] isEqual:@{@"programs": @YES}]);
    policy = @"loopback";
    [dictation request:@{@"action": @"approveOutputScript", @"token": token, @"grants": @{@"network": @YES}}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"policy"] isEqual:@"loopback"]);
            assert([result[@"grants"][@"programs"] isEqual:@YES] && [result[@"grants"][@"network"] isEqual:@YES]);
        }];
    policy = @"normal";
    [dictation request:@{@"action": @"approveOutputScript", @"token": token, @"grants": @{@"programs": @NO}}
        reply:^(id result, NSString *error) {
            assert(!error && ![result[@"grants"] count]);
        }];
    assert(!dictation.settings[@"outputScriptApproval"][@"grants"]);
    policy = @"loopback";
    [dictation request:@{@"action": @"approveOutputScript", @"token": token, @"grants": @{@"programs": @YES}}
        reply:^(id result, NSString *error) {
            assert(!result && [error containsString:@"disabled transformation programs"]);
        }];
    policy = @"normal";
    [dictation request:@{@"action": @"approveOutputScript", @"token": token, @"grants": @{@"unexpected": @YES}}
        reply:^(id result, NSString *error) {
            assert(!result && [error containsString:@"valid transformation permissions"]);
        }];
    [dictation request:@{@"action": @"clearOutputScript"} reply:^(id result, NSString *error) {
        assert(!error && ![result[@"outputScriptName"] length]);
    }];
    assert(![dictation.settings[@"outputScriptApproval"] count]);
    [dictation request:@{@"action": @"approveOutputScript", @"token": token} reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"Check the selected"]);
    }];
    [dictation shutdown];
    [defaults removeObjectForKey:@"JardSystemDictation"];
    puts("Output approval rejected an altered version and retired a removed selection's review token.");
}

static void testOutputConfigStarter(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictation"];
    __block BOOL found = NO;
    __block BOOL broken = NO;
    __block BOOL disabled = NO;
    __block NSUInteger creations = 0;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.findOutputConfig"]) {
            if (disabled) reply(@{@"policy": @"disabled", @"message": @"Managed by your organization"}, nil);
            else reply(@{@"name": found ? @"jaro.config.ts" : @""}, nil);
        } else if ([command isEqual:@"jard.createOutputConfig"]) {
            creations++;
            reply(@{@"path": @"/private/tmp/jaro.config.ts"}, nil);
        } else if ([command isEqual:@"jard.inspectOutputScript"]) {
            assert([payload[@"path"] isEqual:@"/private/tmp/jaro.config.ts"]);
            NSArray *steps = @[@{@"id": @"trim-lines", @"name": @"Trim lines", @"description": @""}];
            if (broken) reply(@{@"message": @"Broken import", @"transforms": steps}, nil);
            else reply(@{@"digest": [@"a" stringByPaddingToLength:64 withString:@"a" startingAtIndex:0],
                @"kind": @"embedded", @"transforms": steps}, nil);
        } else {
            assert(NO);
        }
    }];
    [dictation request:@{@"action": @"inspectOutputScript"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"none"]);
    }];
    disabled = YES;
    [dictation request:@{@"action": @"inspectOutputScript"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"unavailable"] && [result[@"policy"] isEqual:@"disabled"]);
        assert([result[@"message"] isEqual:@"Managed by your organization"]);
    }];
    disabled = NO;
    found = YES;
    [dictation request:@{@"action": @"inspectOutputScript"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"found"] && [result[@"file"] isEqual:@"jaro.config.ts"]);
    }];
    assert(![dictation.settings[@"outputScript"] length]);
    [dictation request:@{@"action": @"createOutputConfig"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"outputScriptName"] isEqual:@"jaro.config.ts"]);
    }];
    assert(creations == 1 && ![dictation.settings[@"outputScriptApproval"] count]);
    [dictation request:@{@"action": @"createOutputConfig"} reply:^(id result, NSString *error) {
        assert(!result && error);
    }];
    [dictation request:@{@"action": @"inspectOutputScript"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"review"]);
    }];
    broken = YES;
    [dictation request:@{@"action": @"inspectOutputScript"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"state"] isEqual:@"unavailable"] && [result[@"stale"] isEqual:@YES]);
        assert([result[@"message"] isEqual:@"Broken import"] && [result[@"transforms"][0][@"id"] isEqual:@"trim-lines"]);
        assert(!result[@"token"]);
    }];
    [dictation shutdown];
    [defaults removeObjectForKey:@"JardSystemDictation"];
    puts("Output discovered a default configuration, selected it explicitly, and showed stale definitions after a load failure.");
}

static void testSharedVocabulary(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictation"];
    __block NSDictionary *output = nil;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        assert([command isEqual:@"jard.output"]);
        output = payload;
        reply(@{@"profile": @""}, nil);
    }];
    [dictation request:@{@"action": @"set", @"key": @"replacements", @"value": @"Mei Lin => Director Mei Lin"}
        reply:^(id result, NSString *error) { assert(result && !error); }];
    [dictation controlSettings:@{@"action": @"vocabulary-add", @"term": @"Mei Lin", @"soundsLike": @[@"May Lean"]}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"changed"] boolValue] && [result[@"entries"][0][@"soundsLike"] isEqual:@[@"May Lean"]]);
        }];
    [dictation controlSettings:@{@"action": @"vocabulary-add", @"term": @"mei lin", @"soundsLike": @[@"may lean", @"May Linn"]}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"changed"] boolValue] && [result[@"entries"] count] == 1);
            assert(([result[@"entries"][0][@"soundsLike"] isEqual:@[@"May Lean", @"May Linn"]]));
        }];
    [dictation controlSettings:@{@"action": @"vocabulary-add", @"term": @"MEI LIN", @"soundsLike": @[@"MAY LEAN"]}
        reply:^(id result, NSString *error) { assert(!error && ![result[@"changed"] boolValue]); }];
    [dictation request:@{@"action": @"tryOutput", @"text": @"MAY LEAN and may linn; MAY LEANS."}
        reply:^(id result, NSString *error) {
            assert(!error && [result[@"text"] isEqual:@"Director Mei Lin and Director Mei Lin; MAY LEANS."]);
        }];
    assert(([output[@"vocabulary"] isEqual:@[@"Mei Lin"]] &&
        [output[@"aliases"] isEqual:@[@[@"May Lean", @"Mei Lin"], @[@"May Linn", @"Mei Lin"]]]));
    TestDictation *reopened = [[TestDictation alloc] initWithSend:nil];
    [reopened controlSettings:@{@"action": @"vocabulary-list"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"entries"][0][@"term"] isEqual:@"Mei Lin"] && [result[@"entries"][0][@"soundsLike"] count] == 2);
    }];
    [reopened controlSettings:@{@"action": @"vocabulary-remove", @"term": @"MEI LIN"}
        reply:^(id result, NSString *error) { assert(!error && [result[@"changed"] boolValue] && ![result[@"entries"] count]); }];
    assert([reopened.settings[@"replacements"] isEqual:@"Mei Lin => Director Mei Lin"]);
    assert(![reopened.settings[@"vocabularySoundsLike"] count]);
    [reopened shutdown];
    [dictation shutdown];
    [defaults removeObjectForKey:@"JardSystemDictation"];
    puts("Shared vocabulary commands preserved settings, normalized alternate spellings, and applied whole-phrase fallback corrections.");
}

static void testVocabularyEditor(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictation"];
    TestDictation *dictation = [[TestDictation alloc] initWithSend:nil];
    JardDictationReply acknowledge = ^(id result, NSString *error) { assert(result && !error); };
    [dictation controlSettings:@{@"action": @"vocabulary-add", @"term": @"Mei Lin", @"soundsLike": @[@"May Lean"]} reply:acknowledge];
    NSArray *base = dictation.status[@"vocabularyEntries"];
    [dictation controlSettings:@{@"action": @"vocabulary-add", @"term": @"Mei Lin", @"soundsLike": @[@"May Linn"]} reply:acknowledge];
    [dictation controlSettings:@{@"action": @"vocabulary-add", @"term": @"Atlas"} reply:acknowledge];
    [dictation request:@{@"action": @"editVocabulary", @"base": base, @"entries":
        @[@{@"term": @"Mei LIN", @"soundsLike": @[@"May Lean"]}]} reply:^(id result, NSString *error) {
        assert(!error && [result[@"vocabularyEntries"] count] == 2);
        assert([result[@"vocabularyEntries"][0][@"term"] isEqual:@"Mei LIN"]);
        assert(([result[@"vocabularyEntries"][0][@"soundsLike"] isEqual:@[@"May Lean", @"May Linn"]]));
    }];
    NSArray *latest = dictation.status[@"vocabularyEntries"];
    [dictation request:@{@"action": @"editVocabulary", @"base": latest, @"entries":
        @[@{@"term": @"Mei Lee", @"soundsLike": @[@"May Lean", @"May Linn"]}, latest[1]]} reply:^(id result, NSString *error) {
        assert(!error && [result[@"vocabularyEntries"][0][@"term"] isEqual:@"Mei Lee"]);
    }];
    latest = dictation.status[@"vocabularyEntries"];
    [dictation controlSettings:@{@"action": @"vocabulary-remove", @"term": @"Mei Lee"} reply:acknowledge];
    [dictation request:@{@"action": @"editVocabulary", @"base": latest, @"entries":
        @[@{@"term": @"Mei Lee", @"soundsLike": @[@"Late edit"]}, @{@"term": @"Atlas", @"soundsLike": @[]}]} reply:^(id result, NSString *error) {
        assert(!error && [result[@"vocabularyEntries"] count] == 1 && [result[@"vocabularyEntries"][0][@"term"] isEqual:@"Atlas"]);
    }];
    [dictation request:@{@"action": @"editVocabulary", @"base": @[], @"entries": @[
        @{@"term": @"Atlas", @"soundsLike": @[@"At Less"]}, @{@"term": @"AtLAS", @"soundsLike": @[@"Add Last"]},
    ]} reply:^(id result, NSString *error) {
        assert(!error && [result[@"vocabularyEntries"] count] == 1);
        assert(([result[@"vocabularyEntries"][0][@"soundsLike"] isEqual:@[@"At Less", @"Add Last"]]));
    }];
    [dictation request:@{@"action": @"editVocabulary", @"base": @[], @"entries": @[@{@"term": @"Invalid", @"soundsLike": @[@"x"]}]}
        reply:^(id result, NSString *error) { assert(!result && [error containsString:@"two characters"]); }];
    assert([dictation.status[@"vocabularyEntries"] count] == 1);
    [dictation shutdown];
    [defaults removeObjectForKey:@"JardSystemDictation"];
    puts("The vocabulary editor merged rows and concurrent CLI changes, fenced deleted rows, and rejected invalid aliases atomically.");
}

static void testMicrophoneRecovery(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults removeObjectForKey:@"JardSystemDictationHistory"];
    NSMutableArray<JardDictationReply> *replies = NSMutableArray.array;
    NSMutableArray<NSString *> *discarded = NSMutableArray.array;
    MicrophoneRecoveryFixture *fixture = [[MicrophoneRecoveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([command isEqual:@"jard.record"]) reply(@{}, nil);
            else if ([payload[@"action"] isEqual:@"transcribe"]) [replies addObject:[reply copy]];
            else if ([payload[@"action"] isEqual:@"discard"]) [discarded addObject:payload[@"id"]];
            else assert(false);
        }];
    fixture.board = [NSPasteboard pasteboardWithUniqueName];
    fixture.recordingEnvironment = CaptureEnvironmentFixture.new;
    fixture.settings[@"placement"] = @"off";
    fixture.settings[@"sound"] = @NO;
    fixture.settings[@"retention"] = @7;
    fixture.settings[@"output"] = @"type";
    fixture.lastText = @"Earlier dictation";
    NSTimeInterval press = NSProcessInfo.processInfo.systemUptime - 1;
    NSDictionary *(^lost)(NSString *, NSString *, BOOL, double) = ^(NSString *stage, NSString *kind, BOOL live, double elapsed) {
        return @{@"event": @"dictationReady", @"dictation": fixture.captureID, @"id": stage, @"global": @YES,
            @"interruption": kind, @"hasLiveAudio": @(live), @"capturedThrough": @(press + elapsed),
            @"seconds": @0.6, @"reason": @"Fixture microphone interrupted."};
    };
    void (^begin)(BOOL) = ^(BOOL raw) {
        fixture.shortcut = (JardShortcut){.phase = JardHeld, .pressedAt = press};
        fixture.usingRawBinding = raw;
        [fixture begin];
    };

    begin(NO);
    assert([fixture handleCapture:lost(@"early", @"microphone", YES, .20)]);
    begin(NO);
    assert([fixture handleCapture:lost(@"standby", @"microphone", NO, .42)]);
    begin(NO);
    assert([fixture handleCapture:lost(@"gap", @"discontinuity", YES, .42)]);
    assert(([discarded isEqual:@[@"early", @"standby", @"gap"]] && !replies.count));
    assert([fixture.lastText isEqual:@"Earlier dictation"]);

    begin(NO);
    NSDictionary *accepted = lost(@"accepted", @"microphone", YES, .45);
    assert([fixture handleCapture:accepted]);
    assert([fixture handleCapture:accepted] && replies.count == 1 && discarded.count == 3);
    replies[0](@{@"text": @"Raw speech", @"processed": @"Recovered words"}, nil);
    assert([fixture.copiedText isEqual:@"Recovered words"] && [fixture.lastText isEqual:fixture.copiedText]);
    assert([fixture.history.firstObject[@"kind"] isEqual:@"dictation"] && [fixture.status[@"lastFailure"] length] == 0);

    begin(YES);
    NSString *next = fixture.captureID;
    NSMutableDictionary *stale = [accepted mutableCopy];
    stale[@"id"] = @"stranded";
    assert([fixture handleCapture:stale] && [fixture.captureID isEqual:next]);
    assert([discarded.lastObject isEqual:@"stranded"]);
    assert([fixture handleCapture:lost(@"raw", @"microphone", YES, .42)]);
    replies[1](@{@"text": @"Unprocessed words", @"processed": @"Unused transformation"}, nil);
    assert([fixture.copiedText isEqual:@"Unprocessed words"] && [fixture.history.firstObject[@"kind"] isEqual:@"raw"]);

    begin(NO);
    assert([fixture handleCapture:lost(@"clipboard-changed", @"microphone", YES, .42)]);
    [fixture.board clearContents];
    [fixture.board setString:@"New clipboard value" forType:NSPasteboardTypeString];
    replies[2](@{@"text": @"Retained despite clipboard change"}, nil);
    assert([fixture.copiedText isEqual:@"Unprocessed words"]);
    assert([fixture.lastText isEqual:@"Retained despite clipboard change"]);
    assert([[fixture.board stringForType:NSPasteboardTypeString] isEqual:@"New clipboard value"]);

    begin(NO);
    assert([fixture handleCapture:lost(@"empty", @"microphone", YES, .42)]);
    replies[3](@{@"text": @""}, nil);
    assert([fixture.status[@"lastFailure"] isEqual:@"Fixture microphone interrupted."] && fixture.history.count == 3);
    [fixture.board releaseGlobally];
    [defaults removeObjectForKey:@"JardSystemDictationHistory"];
    puts("Microphone loss copied only an intentional take, preserved a newer clipboard, and fenced stale loss notifications.");
}

static void testColdReopenDuringHold(void) {
    for (NSString *ending in @[@"resume", @"before-ready", @"short-hold", @"cancel", @"failed", @"permission", @"warm"]) {
        NSMutableArray<NSDictionary *> *starts = NSMutableArray.array;
        NSMutableArray<JardDictationReply> *startReplies = NSMutableArray.array;
        NSMutableArray<NSString *> *discarded = NSMutableArray.array;
        NSMutableArray<NSDictionary *> *stops = NSMutableArray.array;
        __block NSUInteger transcribed = 0;
        ColdReopenFixture *fixture = [[ColdReopenFixture alloc] initWithSend:
            ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
                if ([command isEqual:@"jard.record"]) {
                    [starts addObject:payload];
                    [startReplies addObject:[reply copy]];
                } else if ([payload[@"action"] isEqual:@"discard"]) {
                    [discarded addObject:payload[@"id"]];
                } else if ([payload[@"action"] isEqual:@"transcribe"]) {
                    transcribed++;
                    reply(@{@"text": @"Speech after reopening"}, nil);
                } else {
                    [stops addObject:payload];
                    reply(@{}, nil);
                }
            }];
        CaptureEnvironmentFixture *environment = CaptureEnvironmentFixture.new;
        fixture.recordingEnvironment = environment;
        fixture.settings[@"placement"] = @"off";
        fixture.settings[@"output"] = @"paste";
        fixture.settings[@"retention"] = @0;
        fixture.settings[@"inputDevice"] = @"Original input";
        fixture.settings[@"keepMicrophoneReady"] = @([ending isEqual:@"warm"]);
        fixture.dictationKeyEnabled = fixture.keyDown = YES;
        NSTimeInterval press = NSProcessInfo.processInfo.systemUptime - 1;
        fixture.shortcut = (JardShortcut){.phase = JardHeld, .pressedAt = press};
        [fixture begin];
        NSUInteger sequence = fixture.takeSequence, revision = fixture.takeInputRevision;
        NSString *first = fixture.captureID;

        [fixture activationChanged:nil];
        fixture.settings[@"inputDevice"] = @"Edited input";
        BOOL streamWasReady = [ending isEqual:@"resume"];
        NSMutableDictionary *failure = [@{@"event": streamWasReady ? @"dictationReady" : @"error", @"global": @YES,
            @"dictation": first, @"interruption": @"microphone", @"failedAt": @(press + .22),
            @"message": @"Fixture microphone interrupted."} mutableCopy];
        if (streamWasReady) {
            assert(([fixture handleCapture:@{@"event": @"started", @"dictation": first, @"openedAt": @(press + .18),
                @"microphone": @"Mac microphone", @"usingDefaultMicrophone": @YES}]));
            assert([fixture.message containsString:@"Mac microphone"]);
            [failure addEntriesFromDictionary:@{@"id": @"first-stage", @"capturedThrough": @(press + .20), @"hasLiveAudio": @YES}];
        }
        if ([ending isEqual:@"permission"]) failure[@"permission"] = @"mic";
        assert([fixture handleCapture:failure]);
        if ([ending isEqual:@"permission"] || [ending isEqual:@"warm"]) {
            assert(starts.count == 1 && !fixture.captureID);
            continue;
        }

        assert(starts.count == 2 && ![fixture.captureID isEqual:first]);
        assert(fixture.takeSequence == sequence && fixture.takeInputRevision == revision && fixture.destinations == 1);
        assert([starts.lastObject[@"inputDevice"] isEqual:@"Original input"] && [fixture.phase isEqual:@"starting"]);
        assert(environment.active && environment.ended == 0);
        if (streamWasReady) assert([discarded.lastObject isEqual:@"first-stage"]);
        NSString *second = fixture.captureID;
        startReplies[0](nil, @"Late failure from the first open.");
        assert([fixture.captureID isEqual:second]);
        assert(([fixture handleCapture:@{@"event": @"started", @"global": @YES, @"dictation": first, @"openedAt": @(press + .24)}]));
        assert([fixture.phase isEqual:@"starting"]);

        if ([ending isEqual:@"failed"]) {
            failure[@"dictation"] = second;
            assert([fixture handleCapture:failure] && starts.count == 2 && !fixture.captureID);
            assert(!environment.active && !transcribed);
            continue;
        }
        if ([ending isEqual:@"cancel"] || [ending isEqual:@"short-hold"]) {
            if ([ending isEqual:@"short-hold"]) {
                JardShortcut gesture = fixture.shortcut;
                NSTimeInterval release = press + .25;
                assert(jardShortcutUp(&gesture, release) == JardDiscard);
                fixture.shortcut = gesture;
                fixture.keyDown = NO;
                [fixture queueAction:JardDiscard at:release];
                [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
            } else [fixture cancelCapture];
            assert(!stops.count);
            startReplies[1](@{}, nil);
            assert([stops.lastObject[@"action"] isEqual:@"cancel"] && !fixture.captureID);
            assert(([fixture handleCapture:@{@"event": @"dictationReady", @"dictation": second, @"global": @YES, @"id": @"cancelled-stage"}]));
            assert([discarded.lastObject isEqual:@"cancelled-stage"] && !transcribed);
            continue;
        }

        NSTimeInterval release = press + .49;
        JardShortcut gesture = fixture.shortcut;
        assert(jardShortcutUp(&gesture, release) == JardFinish);
        fixture.shortcut = gesture;
        fixture.keyDown = NO;
        [fixture queueAction:JardFinish at:release];
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        assert(!stops.count);
        startReplies[1](@{}, nil);
        assert(stops.count == 1 && [stops[0][@"releaseAt"] doubleValue] == release && [stops[0][@"releaseTail"] boolValue]);
        assert(([fixture handleCapture:@{@"event": @"started", @"dictation": second, @"openedAt": @(release + (streamWasReady ? -.09 : .01))}]));
        assert(([fixture handleCapture:@{@"event": @"dictationReady", @"dictation": second, @"id": @"reopened-stage"}]));
        assert(!fixture.captureID && !environment.active);
        if (streamWasReady) {
            assert(fixture.startCues == 1 && transcribed == 1);
            assert([fixture.lastText isEqual:@"Speech after reopening"] && !fixture.copiedText);
        } else {
            assert(!transcribed && [discarded.lastObject isEqual:@"reopened-stage"] && [fixture.phase isEqual:@"idle"]);
        }
    }
    puts("Cold reopen preserved the held gesture and its target, balanced delayed releases and cancellation, and ignored retired attempts.");
}

static void testCaptureEnvironment(void) {
    for (NSString *mode in @[@"none", @"lower", @"mute", @"pause"]) {
        CaptureEnvironmentFixture *environment = CaptureEnvironmentFixture.new;
        TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([command isEqual:@"jard.record"]) {
                assert(environment.active && [environment.mode isEqual:mode]);
                assert([payload[@"preRoll"] boolValue] == [mode isEqual:@"none"]);
                if ([mode isEqual:@"mute"]) reply(nil, @"Fixture microphone could not start.");
                else reply(@{}, nil);
            } else {
                assert([command isEqual:@"jard.dictation"] && !environment.active);
                if ([mode isEqual:@"none"]) assert([payload[@"action"] isEqual:@"transcribe"]);
                if ([mode isEqual:@"lower"] || [mode isEqual:@"pause"]) assert([payload[@"action"] isEqual:@"cancel"]);
                reply(@{@"text": @"Fixture take"}, nil);
            }
        }];
        dictation.recordingEnvironment = environment;
        dictation.settings[@"sound"] = @NO;
        dictation.settings[@"placement"] = @"off";
        dictation.settings[@"retention"] = @0;
        dictation.settings[@"lowerAudioPercent"] = @20;
        dictation.settings[@"audioWhileDictating"] = mode;
        [dictation begin];
        if ([mode isEqual:@"none"]) {
            NSDictionary *ready = @{@"event": @"dictationReady", @"dictation": dictation.captureID, @"id": @"fixture"};
            assert([dictation handleCapture:ready]);
        } else if ([mode isEqual:@"lower"] || [mode isEqual:@"pause"]) {
            [dictation cancelCapture];
        }
        assert(!dictation.captureID && !environment.active && environment.ended == 1);
    }
    puts("Dictation released the recording environment on capture completion, cancellation, and microphone startup failure.");
}

static void finishTake(JardSystemDictation *dictation, NSString *identifier, BOOL interrupted) {
    dictation.captureID = identifier;
    dictation.takeSequence = ++dictation.recordingGeneration;
    dictation.capturePreferences = [dictation.settings copy];
    NSDictionary *detail = @{@"event": @"dictationReady", @"id": identifier, @"dictation": identifier,
        @"interrupted": @(interrupted), @"seconds": @1};
    assert([dictation handleCapture:detail]);
    assert(dictation.captureID == nil);
}

static void testDictationTiming(void) {
    NSMutableArray<JardDictationReply> *replies = NSMutableArray.array;
    NSMutableArray<NSDictionary *> *timings = NSMutableArray.array;
    DictationDeliveryFixture *dictation = [[DictationDeliveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([command isEqual:@"jard.diagnosticTiming"]) {
                assert(!reply);
                [timings addObject:[payload copy]];
            } else {
                assert([command isEqual:@"jard.dictation"] && [payload[@"action"] isEqual:@"transcribe"]);
                [replies addObject:[reply copy]];
            }
        }];
    dictation.settings[@"retention"] = @0;
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"output"] = @"paste";
    dictation.board = NSPasteboard.pasteboardWithUniqueName;
    dictation.editor = AnnotationTargetFixture.new;
    dictation.editor.processIdentifier = getpid();
    dictation.caret = [NSValue valueWithRange:NSMakeRange(0, 0)];
    dictation.expectedPastes = @[@"First.", @"Second."];
    [dictation.board setString:@"Original clipboard" forType:NSPasteboardTypeString];
    [dictation setValue:@(NSProcessInfo.processInfo.systemUptime - .25) forKey:@"captureStopRequestedAt"];
    finishTake(dictation, @"timing-first", NO);
    finishTake(dictation, @"timing-second", NO);
    replies[1](@{@"text": @"Second.", @"inferenceMs": @24, @"speechRequestMs": @80}, nil);
    assert(timings.count == 0 && dictation.pasteCount == 0);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
    replies[0](@{@"text": @"First.", @"inferenceMs": @36, @"speechRequestMs": @90}, nil);
    waitForDelivery(dictation);
    assert(dictation.pasteCount == 2 && timings.count == 2);
    NSDictionary *first = timings[0], *second = timings[1];
    assert([first[@"captureFinalizeMs"] intValue] >= 250 && second[@"captureFinalizeMs"] == nil);
    assert([first[@"speechRequestMs"] isEqual:@90] && [first[@"inferenceMs"] isEqual:@36]);
    assert([second[@"speechRequestMs"] isEqual:@80] && [second[@"inferenceMs"] isEqual:@24]);
    assert([second[@"outputWaitMs"] intValue] >= 125);
    assert(first[@"clipboardSnapshotMs"] && second[@"clipboardSnapshotMs"]);
    assert([first[@"totalMs"] intValue] >= [first[@"captureFinalizeMs"] intValue]);
    assert([[dictation.board stringForType:NSPasteboardTypeString] isEqual:@"Original clipboard"]);
    [dictation.board releaseGlobally];
    puts("Dictation timing separated the model, queued delivery, and clipboard even with History off.");
}

@interface WindowOnlyDictationFixture : JardSystemDictation
@property(nonatomic, strong) AnnotationTargetFixture *editor;
@property(nonatomic, strong) NSPasteboard *board;
@property(nonatomic) NSUInteger pastes;
@end

@implementation WindowOnlyDictationFixture
- (NSRunningApplication *)foregroundOutputApplication { return (NSRunningApplication *)(id)self.editor; }
- (NSPasteboard *)pasteboard { return self.board; }
- (BOOL)postPasteShortcut {
    assert([[self.board stringForType:NSPasteboardTypeString] isEqual:@"A candidate sentence."]);
    BOOL sent = [super postPasteShortcut];
    if (sent) self.pastes++;
    return sent;
}
@end

static void testWindowOnlyDictationTarget(void) {
    WindowOnlyDictationFixture *dictation = [[WindowOnlyDictationFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            assert([payload[@"action"] isEqual:@"transcribe"]);
            reply(@{@"text": @"A candidate sentence."}, nil);
        }];
    dictation.board = NSPasteboard.pasteboardWithUniqueName;
    [dictation.board clearContents];
    [dictation.board setString:@"Original clipboard" forType:NSPasteboardTypeString];
    dictation.editor = outputTarget(@"Window-only editor", windowFixturePID);
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"sound"] = @NO;
    dictation.settings[@"output"] = @"paste";
    dictation.settings[@"appRules"] = @"";
    dictation.settings[@"retention"] = @0;
    fixtureEventTaps = true;
    NSUInteger total = 0;
    for (NSUInteger scenario = 0; scenario < 9; scenario++) {
        dictation.editor = outputTarget(@"Window-only editor", windowFixturePID);
        windowFixtureIdentity = windowFixturePID + 2;
        windowFixtureFocusStatus = scenario == 6 ? kAXErrorCannotComplete : kAXErrorNoValue;
        [dictation rememberDestination];
        dictation.takeInputRevision = dictation.inputRevision;
        switch (scenario) {
            case 1: windowFixtureIdentity++; break;
            case 2: [dictation activationChanged:nil]; break;
            case 3: fixtureSecureInput = true; break;
            case 4: windowFixtureFocusStatus = kAXErrorCannotComplete; break;
            case 5: windowFixtureFocusStatus = kAXErrorSuccess; break;
            case 7: dictation.editor = outputTarget(@"Another active editor", windowFixturePID + 10); break;
            case 8: dictation.editor = nil; break;
        }
        nativePasteEvents = NSMutableArray.array;
        finishTake(dictation, @"window-only", NO);
        BOOL allowed = scenario != 3 && scenario != 8;
        total += allowed;
        assert([dictation.phase isEqual:allowed ? @"inserted" : @"saved"] && dictation.pastes == total);
        assert(nativePasteEvents.count == (allowed ? 4 : 0));
        if (allowed) {
            assert(([nativePasteEvents.firstObject isEqual:@[@(kCGHIDEventTap), @(kCGEventFlagsChanged), @(kVK_Command), @(kCGEventFlagMaskCommand)]]));
            assert(([nativePasteEvents.lastObject isEqual:@[@(kCGHIDEventTap), @(kCGEventFlagsChanged), @(kVK_Command), @0]]));
            assert([nativePasteEvents[1][1] unsignedIntegerValue] == kCGEventKeyDown);
            assert([nativePasteEvents[2][1] unsignedIntegerValue] == kCGEventKeyUp);
            assert([nativePasteEvents[1][2] isEqual:nativePasteEvents[2][2]]);
        }
        nativePasteEvents = nil;
        waitForDelivery(dictation);
        assert([[dictation.board stringForType:NSPasteboardTypeString] isEqual:@"Original clipboard"]);
        fixtureSecureInput = false;
    }
    fixtureEventTaps = false;
    windowFixtureIdentity = 0;
    [dictation.board releaseGlobally];
    puts("Fresh dictation followed the active caret through AX errors and changes, posted the full HID chord, and blocked secure input or a missing app.");
}

static void testHistoryTiming(void) {
    DictationDeliveryFixture *dictation = [[DictationDeliveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            assert([payload[@"action"] isEqual:@"transcribe"]);
            reply(@{@"text": @"A timed take", @"inferenceMs": @0}, nil);
        }];
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"output"] = @"paste";
    dictation.settings[@"retention"] = @7;
    dictation.board = NSPasteboard.pasteboardWithUniqueName;
    finishTake(dictation, @"timed-paste", NO);
    NSDictionary *pending = dictation.history.firstObject;
    assert([pending[@"text"] isEqual:@"A timed take"] && [pending[@"inferenceMs"] isEqual:@0]);
    assert(!pending[@"totalMs"]);
    waitForDelivery(dictation);
    NSDictionary *finished = dictation.history.firstObject;
    assert([finished[@"id"] isEqual:pending[@"id"]] && [finished[@"totalMs"] integerValue] >= 60);
    assert(dictation.pasteCount == 1);

    finishTake(dictation, @"timed-interruption", YES);
    assert([dictation.history.firstObject[@"totalMs"] isKindOfClass:NSNumber.class]);
    assert(dictation.pasteCount == 1);

    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults setObject:@"preserve" forKey:@"QueueHistorySentinel"];
    assert([defaults synchronize]);
    NSString *identifier = NSBundle.mainBundle.bundleIdentifier;
    BOOL disposableDomain = [identifier hasPrefix:@"local.jardaudio.preferences-fixture."];
    NSDictionary *(^readSavedHistory)(void) = ^NSDictionary *{
        NSTask *reader = NSTask.new;
        reader.executableURL = [NSURL fileURLWithPath:@"/usr/bin/defaults"];
        reader.arguments = @[@"export", identifier, @"-"];
        NSPipe *output = NSPipe.pipe;
        reader.standardOutput = output;
        assert([reader launchAndReturnError:nil]);
        NSData *data = [output.fileHandleForReading readDataToEndOfFile];
        [reader waitUntilExit];
        assert(reader.terminationStatus == 0);
        return [NSPropertyListSerialization propertyListWithData:data
            options:NSPropertyListImmutable format:nil error:nil];
    };
    if (disposableDomain) {
        NSDictionary *saved = readSavedHistory();
        assert([saved[@"QueueHistorySentinel"] isEqual:@"preserve"] && [saved[@"JardSystemDictationHistory"] count] > 0);
    }

    finishTake(dictation, @"timed-delete", NO);
    NSUInteger count = dictation.history.count;
    assert(count >= 3 && dictation.lastText.length);
    NSInteger clipboardChange = dictation.board.changeCount;
    assert(dictation.pasteCount == 2);
    [dictation control:@"history-clear" reply:^(id result, NSString *error) {
        assert(!error && [result[@"changed"] boolValue] && [result[@"removed"] unsignedIntegerValue] == count);
    }];
    assert(![dictation.status[@"hasLast"] boolValue] && ![dictation.status[@"historyRevision"] length]);
    dictation.settings[@"vocabulary"] = @"Zinash";
    dictation.settings[@"vocabularySoundsLike"] = @{@"zinash": @[@"Zin Ash"]};
    [dictation control:@"history-snapshot" reply:^(id result, NSString *error) {
        assert(!error && [result[@"JardSystemDictationHistory"] isEqual:@[]]);
        NSDictionary *searchSettings = result[@"JardSystemDictation"];
        assert([searchSettings[@"retention"] isEqual:@7] && [searchSettings[@"vocabulary"] isEqual:@"Zinash"]);
        assert([searchSettings[@"vocabularySoundsLike"][@"zinash"] isEqual:@[@"Zin Ash"]]);
        if (identifier) assert([result[@"bundleId"] isEqual:identifier]);
    }];
    if (disposableDomain) {
        NSDictionary *saved = readSavedHistory();
        assert([saved[@"JardSystemDictationHistory"] isEqual:@[]] && [saved[@"QueueHistorySentinel"] isEqual:@"preserve"]);
    }
    [defaults removeObjectForKey:@"QueueHistorySentinel"];
    assert(dictation.board.changeCount == clipboardChange);
    [dictation control:@"history-clear" reply:^(id result, NSString *error) {
        assert(!error && ![result[@"changed"] boolValue] && [result[@"removed"] unsignedIntegerValue] == 0);
    }];
    waitForDelivery(dictation);
    assert(!dictation.history.count && dictation.pasteCount == 2);

    dictation.settings[@"retention"] = @0;
    finishTake(dictation, @"after-clear", YES);
    assert(!dictation.history.count && dictation.lastText.length);
    [dictation control:@"history-clear" reply:^(id result, NSString *error) {
        assert(!error && [result[@"changed"] boolValue] && [result[@"removed"] unsignedIntegerValue] == 0);
    }];
    dictation.settings[@"retention"] = @7;
    finishTake(dictation, @"after-clear-retained", YES);
    assert(dictation.history.count == 1 && dictation.lastText.length);
    [dictation request:@{@"action": @"clearHistory"} reply:^(id status, NSString *error) {
        assert(!error && ![status[@"hasLast"] boolValue] && ![status[@"historyRevision"] length]);
    }];
    assert(!dictation.history.count);
    [dictation.board releaseGlobally];
    puts("History timing and clearing persisted, kept the clipboard and new dictations, and reset Paste Last.");
}

static void testLastFailure(void) {
    NSMutableArray<JardDictationReply> *replies = NSMutableArray.array;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.record"]) reply(nil, @"The selected microphone is unavailable.");
        else {
            assert([payload[@"action"] isEqual:@"transcribe"]);
            [replies addObject:[reply copy]];
        }
    }];
    dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"retention"] = @0;
    dictation.settings[@"sound"] = @NO;
    dictation.settings[@"output"] = @"copy";
    dictation.settings[@"lowerAudioPercent"] = @20;
    finishTake(dictation, @"earlier-take", NO);
    [dictation begin];
    NSString *failed = dictation.status[@"lastFailure"];
    assert([failed containsString:@"Reconnect it or choose another microphone"]);
    replies[0](@{@"text": @"Earlier successful take"}, nil);
    assert([dictation.copiedText isEqual:@"Earlier successful take"]);
    assert([dictation.status[@"lastFailure"] isEqual:failed]);
    [dictation request:@{@"action": @"copyLast"} reply:^(id status, NSString *error) {
        assert(!error && [status[@"lastFailure"] isEqual:failed]);
    }];

    finishTake(dictation, @"cancelled-take", NO);
    [dictation cancelPendingOutput];
    replies[1](@{@"text": @"Cancelled take"}, nil);
    assert([dictation.status[@"lastFailure"] isEqual:failed]);

    finishTake(dictation, @"later-take", NO);
    replies[2](@{@"text": @"Later successful take"}, nil);
    assert([dictation.status[@"lastFailure"] isEqual:@""]);

    dictation.settings[@"output"] = @"paste";
    finishTake(dictation, @"blocked-paste", NO);
    replies[3](@{@"text": @"Keep this text"}, nil);
    assert([dictation.status[@"lastFailure"] containsString:@"active app is unavailable"]);
    assert([dictation.lastText isEqual:@"Keep this text"]);
    finishTake(dictation, @"failed-transcription", NO);
    replies[4](nil, @"Could not load the local speech model.");
    assert([dictation.status[@"lastFailure"] isEqual:@"Could not load the local speech model."]);
    dictation.settings[@"retention"] = @7;
    NSUInteger previousHistoryCount = dictation.history.count;
    finishTake(dictation, @"silent-take", NO);
    replies[5](@{@"text": @"", @"inferenceMs": @0}, nil);
    assert([dictation.status[@"lastFailure"] containsString:@"No speech was detected."]);
    assert([dictation.lastText isEqual:@"Keep this text"]);
    assert(dictation.history.count == previousHistoryCount);
    [dictation control:@"status" reply:^(id status, NSString *error) {
        assert(!error && [status[@"lastFailure"] isEqual:dictation.status[@"lastFailure"]]);
    }];
    TestDictation *newRun = [[TestDictation alloc] initWithSend:nil];
    assert([newRun.status[@"lastFailure"] isEqual:@""]);
    puts("Dictation failure feedback kept the newest reason, cleared on a later success, and stayed local to the running app.");
}

static void testWarmInputStatus(void) {
    NSMutableArray<NSDictionary *> *requests = NSMutableArray.array;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.warm"]) [requests addObject:payload];
    }];
    dictation.settings[@"keepMicrophoneReady"] = @YES;
    dictation.settings[@"inputDevice"] = @"";
    [dictation updateWarm:YES];
    NSString *first = requests.lastObject[@"request"];
    assert(first.length);
    __block NSUInteger updates = 0;
    dictation.preferencesChanged = ^(NSDictionary *status) { updates++; };
    NSDictionary *problem = @{@"request": first, @"phase": @"unavailable", @"message": @"Fixture microphone failed to open."};
    [dictation updateWarmInputStatus:problem];
    [dictation updateWarmInputStatus:problem];
    assert(updates == 1 && [dictation.status[@"warmInput"][@"message"] isEqual:problem[@"message"]]);
    assert([dictation.status[@"lastFailure"] isEqual:@""]);

    dictation.settings[@"inputDevice"] = @"fixture-selected";
    [dictation updateWarm:YES];
    NSString *second = requests.lastObject[@"request"];
    assert(second.length && ![second isEqual:first] && !dictation.status[@"warmInput"]);
    [dictation updateWarmInputStatus:problem];
    assert(updates == 2 && !dictation.status[@"warmInput"]);
    [dictation updateWarmInputStatus:@{@"request": second, @"phase": @"fallback", @"microphone": @"Fixture USB"}];
    assert(updates == 3 && [dictation.status[@"warmInput"][@"microphone"] isEqual:@"Fixture USB"]);
    [dictation updateWarmInputStatus:@{@"request": second, @"phase": @"ready"}];
    assert(updates == 4 && !dictation.status[@"warmInput"]);

    [dictation updateWarm:NO];
    [dictation updateWarmInputStatus:@{@"request": second, @"phase": @"unavailable", @"message": @"Late warning."}];
    assert(updates == 4 && !dictation.status[@"warmInput"]);
    puts("Warm input feedback followed the active request, rejected stale events, and left the last take intact.");
}

static AVAuthorizationStatus fixtureMicrophoneGranted(id owner, SEL selector, AVMediaType mediaType) {
    assert([mediaType isEqual:AVMediaTypeAudio]);
    return AVAuthorizationStatusAuthorized;
}

static void testWarmMicrophoneWhenAway(void) {
    Method method = class_getClassMethod(AVCaptureDevice.class, @selector(authorizationStatusForMediaType:));
    IMP original = method_setImplementation(method, (IMP)fixtureMicrophoneGranted);
    fixturePresence = fixtureEventTaps = true;
    void (^notify)(JardSystemDictation *, NSString *) = ^(JardSystemDictation *dictation, NSString *name) {
        [dictation powerChanged:[NSNotification notificationWithName:name object:nil]];
    };
    for (NSNumber *lockedAtLaunch in @[@YES, @NO]) {
        fixtureDisplayAsleep = !lockedAtLaunch.boolValue;
        fixtureSession = @{@"CGSSessionScreenIsLocked": lockedAtLaunch};
        NSMutableArray<NSDictionary *> *warm = NSMutableArray.array;
        JardSystemDictation *dictation = [[JardSystemDictation alloc] initWithSend:
            ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
                if ([command isEqual:@"jard.warm"]) [warm addObject:payload];
            }];
        dictation.settings[@"enabled"] = @YES;
        dictation.settings[@"onboarding"] = @"done";
        dictation.settings[@"keepMicrophoneReady"] = @YES;
        [dictation workspaceReady];
        assert(!warm.count && !dictation.dictationKeyEnabled && ![dictation canStartCommandCapture]);
        assert(![dictation annotationRoutingDisplayAvailable]);

        notify(dictation, lockedAtLaunch.boolValue ? NSWorkspaceScreensDidWakeNotification : @"com.apple.screenIsUnlocked");
        assert(!warm.count && !dictation.dictationKeyEnabled);
        notify(dictation, lockedAtLaunch.boolValue ? @"com.apple.screenIsUnlocked" : NSWorkspaceScreensDidWakeNotification);
        assert(warm.count == 1 && [warm.lastObject[@"keep"] boolValue] && dictation.dictationKeyEnabled);
        NSString *first = warm.lastObject[@"request"];

        [dictation setAnnotationActive:YES microphoneShared:YES];
        assert(dictation.dictationKeyEnabled && warm.count == 1);
        [dictation setAnnotationActive:NO microphoneShared:NO];
        assert(dictation.dictationKeyEnabled && warm.count == 1);

        notify(dictation, @"com.apple.screenIsLocked");
        assert(warm.count == 2 && ![warm.lastObject[@"keep"] boolValue] && !dictation.dictationKeyEnabled);
        notify(dictation, NSWorkspaceScreensDidSleepNotification);
        notify(dictation, NSWorkspaceWillSleepNotification);
        notify(dictation, NSWorkspaceSessionDidResignActiveNotification);
        notify(dictation, @"com.apple.screenIsUnlocked");
        notify(dictation, NSWorkspaceDidWakeNotification);
        notify(dictation, NSWorkspaceSessionDidBecomeActiveNotification);
        assert(warm.count == 2 && !dictation.dictationKeyEnabled);
        notify(dictation, NSWorkspaceScreensDidWakeNotification);
        assert(warm.count == 3 && [warm.lastObject[@"keep"] boolValue] && dictation.dictationKeyEnabled);
        assert(first.length && ![warm.lastObject[@"request"] isEqual:first]);
        assert([dictation annotationRoutingDisplayAvailable]);
        [dictation shutdown];
    }
    fixtureSession = nil;
    fixturePresence = fixtureEventTaps = false;
    for (NSUInteger i = 0; i < 3; i++) {
        if (fixtureTaps[i].port) CFRelease(fixtureTaps[i].port);
        memset(&fixtureTaps[i], 0, sizeof fixtureTaps[i]);
    }
    method_setImplementation(method, original);
    puts("Warm dictation stayed closed at locked or display-asleep launch and reopened after the last idle hold ended.");
}

static void testAccessibilityRecovery(void) {
    JardSystemDictation *setup = [[JardSystemDictation alloc] initWithSend:nil];
    setup.settings[@"onboarding"] = @"access";
    [setup noteAccessibilityRequestAt:100];
    [setup observeAccessibilityAt:101 trusted:NO settingsOpen:YES settingsFront:YES active:NO];
    [setup observeAccessibilityAt:108 trusted:NO settingsOpen:YES settingsFront:NO active:NO];
    assert(!setup.accessibilityStale); // An authentication sheet can take focus while Settings stays open.
    [setup observeAccessibilityAt:109 trusted:NO settingsOpen:YES settingsFront:NO active:YES];
    [setup observeAccessibilityAt:110.9 trusted:NO settingsOpen:YES settingsFront:NO active:YES];
    assert(!setup.accessibilityStale);
    [setup observeAccessibilityAt:111 trusted:NO settingsOpen:YES settingsFront:NO active:YES];
    assert(setup.accessibilityStale);
    [setup observeAccessibilityAt:112 trusted:YES settingsOpen:YES settingsFront:NO active:YES];
    assert(!setup.accessibilityStale);

    [setup noteAccessibilityRequestAt:200];
    [setup observeAccessibilityAt:205.9 trusted:NO settingsOpen:YES settingsFront:NO active:YES];
    assert(!setup.accessibilityStale);
    [setup observeAccessibilityAt:206 trusted:NO settingsOpen:YES settingsFront:NO active:YES];
    assert(setup.accessibilityStale);
    setup.settings[@"onboarding"] = @"later";
    [setup observeAccessibilityAt:207 trusted:NO settingsOpen:YES settingsFront:NO active:YES];
    assert(!setup.accessibilityStale);
    [setup request:@{@"action": @"repairAccessibility"} reply:^(id result, NSString *failure) {
        assert(!result && [failure containsString:@"after returning from System Settings"]);
    }];

    for (NSString *step in @[@"practice", @"ready"]) {
        setup.settings[@"onboarding"] = step;
        [setup noteAccessibilityRequestAt:300];
        [setup observeAccessibilityAt:301 trusted:NO settingsOpen:YES settingsFront:YES active:NO];
        [setup observeAccessibilityAt:302 trusted:NO settingsOpen:NO settingsFront:NO active:YES];
        assert(!setup.accessibilityStale);
        [setup observeAccessibilityAt:304 trusted:NO settingsOpen:NO settingsFront:NO active:YES];
        assert(setup.accessibilityStale && [setup.settings[@"onboarding"] isEqual:step]);
        [setup observeAccessibilityAt:305 trusted:YES settingsOpen:NO settingsFront:NO active:YES];
        assert(!setup.accessibilityStale && [setup.settings[@"onboarding"] isEqual:step]);
    }
    puts("Accessibility repair waited for Settings and kept the saved practice and ready steps through recovery.");
}

static void testReturnFromAccessibilitySettings(void) {
    JardSystemDictation *setup = [[JardSystemDictation alloc] initWithSend:nil];
    setup.settings[@"onboarding"] = @"practice";
    assert(![setup observeAccessibilityAt:100 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    [setup noteAccessibilityRequestAt:101];
    assert(![setup observeAccessibilityAt:102 trusted:NO settingsOpen:YES settingsFront:YES active:NO]);
    assert([setup observeAccessibilityAt:103 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    assert(![setup observeAccessibilityAt:104 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);

    // An authentication sheet can be frontmost when the grant first becomes usable.
    [setup noteAccessibilityRequestAt:200];
    assert(![setup observeAccessibilityAt:202 trusted:YES settingsOpen:YES settingsFront:NO active:NO]);
    assert([setup observeAccessibilityAt:204 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    [setup noteAccessibilityRequestAt:300];
    assert(![setup observeAccessibilityAt:302 trusted:YES settingsOpen:YES settingsFront:NO active:NO]);
    assert(![setup observeAccessibilityAt:306 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    [setup noteAccessibilityRequestAt:400];
    assert(![setup observeAccessibilityAt:402 trusted:YES settingsOpen:YES settingsFront:NO active:YES]);
    assert(![setup observeAccessibilityAt:403 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    [setup noteAccessibilityRequestAt:500];
    assert(![setup observeAccessibilityAt:502 trusted:YES settingsOpen:NO settingsFront:NO active:NO]);
    assert(![setup observeAccessibilityAt:503 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);

    [setup noteAccessibilityRequestAt:600];
    assert(![setup observeAccessibilityAt:602 trusted:YES settingsOpen:YES settingsFront:NO active:NO]);
    [setup setValue:@YES forKey:@"screenLocked"];
    assert(![setup observeAccessibilityAt:603 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    [setup setValue:@NO forKey:@"screenLocked"];
    assert(![setup observeAccessibilityAt:604 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    [setup noteAccessibilityRequestAt:700];
    assert(![setup observeAccessibilityAt:702 trusted:YES settingsOpen:YES settingsFront:NO active:NO]);
    setup.settings[@"onboarding"] = @"later";
    assert(![setup observeAccessibilityAt:703 trusted:YES settingsOpen:YES settingsFront:YES active:NO]);
    puts("An Accessibility grant returned from System Settings once and respected focus, timeout, lock, and deferral.");
}

static BOOL press(JardSystemDictation *dictation, CGKeyCode code, CGEventType type, CGEventFlags flags, BOOL repeat) {
    CGEventRef event = CGEventCreateKeyboardEvent(NULL, code, type == kCGEventKeyDown);
    assert(event);
    CGEventSetFlags(event, flags);
    CGEventSetIntegerValueField(event, kCGKeyboardEventAutorepeat, repeat);
    BOOL handled = [dictation handleKey:event type:type];
    CFRelease(event);
    return handled;
}

@interface ShortcutEventFixture : JardSystemDictation
@property(nonatomic) NSUInteger starts, cancels, finishes;
@property(nonatomic) BOOL ordinaryKeyHeld, mouseHeld;
@property(nonatomic, copy) NSIndexSet *sessionKeys, *hidKeys;
@property(nonatomic) CGEventFlags liveModifiers;
@property(nonatomic) uint16_t physicalModifiersHeld;
@end

@implementation ShortcutEventFixture
- (BOOL)ordinaryKeyHeldForShortcutAt:(NSTimeInterval)now bindingFlags:(CGEventFlags)bindingFlags {
    return self.ordinaryKeyHeld || [super ordinaryKeyHeldForShortcutAt:now bindingFlags:bindingFlags];
}
- (BOOL)shortcutKeyDown:(CGKeyCode)code source:(CGEventSourceStateID)source {
    NSIndexSet *pressed = source == kCGEventSourceStateCombinedSessionState ? self.sessionKeys : self.hidKeys;
    return [pressed containsIndex:code];
}
- (BOOL)shortcutMouseButtonDown:(CGMouseButton)button { return button == kCGMouseButtonLeft && self.mouseHeld; }
- (CGEventFlags)shortcutLiveModifiers { return self.liveModifiers; }
- (BOOL)shortcutRecorderIsFrontmost { return YES; }
- (BOOL)canOpenShortcutRecorder { return self.shortcut.phase == JardIdle; }
- (uint16_t)physicalModifierKeys { return self.physicalModifiersHeld; }
- (void)queueAction:(JardShortcutAction)action at:(NSTimeInterval)time {
    if (action == JardStart) self.starts++;
    if (action == JardCancel || action == JardDiscard) self.cancels++;
    if (action == JardFinish) self.finishes++;
}
@end

static BOOL shortcutEvent(JardSystemDictation *fixture, CGKeyCode code, CGEventType type,
                          CGEventFlags flags, NSTimeInterval time, BOOL physical) {
    CGEventRef event = CGEventCreateKeyboardEvent(NULL, code, type == kCGEventKeyDown);
    assert(event);
    CGEventSetFlags(event, flags);
    CGEventSetTimestamp(event, (CGEventTimestamp)(time * NSEC_PER_SEC));
    BOOL handled = NO;
    if (physical) [fixture observePhysicalKeyDown:event];
    else handled = [fixture handleKey:event type:type];
    CFRelease(event);
    return handled;
}

static void testOptionalBindings(void) {
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    NSTimeInterval t = NSProcessInfo.processInfo.systemUptime;
    [fixture request:@{@"action": @"chooseShortcut", @"target": @"paste"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"shortcutTarget"] isEqual:@"paste"]);
    }];
    assert(shortcutEvent(fixture, kVK_ForwardDelete, kCGEventKeyDown, kCGEventFlagMaskSecondaryFn, t, NO));
    assert(shortcutEvent(fixture, kVK_ForwardDelete, kCGEventKeyUp, kCGEventFlagMaskSecondaryFn, t, NO));
    assert(!fixture.choosingShortcut && ![fixture.settings[@"pasteLabel"] length] && [fixture.settings[@"pasteCode"] integerValue] == -1);
    ShortcutEventFixture *reopened = [[ShortcutEventFixture alloc] initWithSend:nil];
    assert(![reopened.settings[@"pasteLabel"] length]);
    fixture.shortcutTarget = @"raw";
    CGEventFlags paste = kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
    assert([fixture saveRecordedCode:kVK_ANSI_V flags:paste label:@"⌥⇧V"]);
    [fixture request:@{@"action": @"resetPasteShortcut"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"message"] containsString:@"already in use"]);
        assert([result[@"shortcutTarget"] isEqual:@"paste"] && ![result[@"pasteLabel"] length]);
        assert([result[@"rawLabel"] isEqual:@"⌥⇧V"]);
    }];
    assert(shortcutEvent(fixture, kVK_ForwardDelete, kCGEventKeyDown, kCGEventFlagMaskControl | kCGEventFlagMaskSecondaryFn, t, NO));
    assert(shortcutEvent(fixture, kVK_ForwardDelete, kCGEventKeyUp, 0, t, NO));
    assert([fixture.settings[@"pasteLabel"] isEqual:@"⌃⌦"]);
    assert([fixture.settings[@"pasteFlags"] unsignedLongLongValue] == kCGEventFlagMaskControl);
    [fixture request:@{@"action": @"chooseShortcut", @"target": @"raw"} reply:^(id result, NSString *error) { assert(!error); }];
    assert(shortcutEvent(fixture, kVK_Delete, kCGEventKeyDown, 0, t, NO));
    assert(shortcutEvent(fixture, kVK_Delete, kCGEventKeyUp, 0, t, NO));
    assert(![fixture.settings[@"rawLabel"] length]);
    [fixture request:@{@"action": @"resetPasteShortcut"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"pasteLabel"] isEqual:@"⌥⇧V"]);
    }];
    [fixture request:@{@"action": @"chooseShortcut"} reply:^(id result, NSString *error) { assert(!error); }];
    assert(shortcutEvent(fixture, kVK_Delete, kCGEventKeyDown, 0, t, NO));
    assert(shortcutEvent(fixture, kVK_Delete, kCGEventKeyUp, 0, t, NO));
    assert(fixture.choosingShortcut && [fixture.message containsString:@"modifier"]);
    assert([fixture.settings[@"shortcutLabel"] isEqual:@"Right ⌥"]);
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    puts("Delete disabled optional bindings, modified Delete recorded, and reset conflicts preserved the bindings.");
}

static void testRemappedModifiersAndGlobe(void) {
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    fixture.settings[@"shortcutCode"] = @(kVK_RightControl);
    fixture.settings[@"shortcutFlags"] = @(kCGEventFlagMaskControl);
    fixture.settings[@"rawCode"] = @(-1);
    fixture.settings[@"rawFlags"] = @0;
    fixture.settings[@"shortcutEitherSide"] = @NO;
    fixture.shortcut = (JardShortcut){.mode = JardHold};
    CGEventFlags control = kCGEventFlagMaskControl | NX_DEVICERCTLKEYMASK;
    const uint16_t caps = 1, rightControl = 2 << (kHIDUsage_KeyboardRightControl - kHIDUsage_KeyboardLeftControl);
    NSTimeInterval t = NSProcessInfo.processInfo.systemUptime - 3;
    fixture.physicalModifiersHeld = caps;
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, control, t, NO);
    assert(!fixture.keyDown && fixture.shortcut.phase == JardIdle);

    // The real modifier can start and finish with remapped Caps Lock held underneath.
    fixture.physicalModifiersHeld = caps | rightControl;
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, control, t+.11, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed);
    JardShortcut gesture = fixture.shortcut;
    assert(jardShortcutArm(&gesture) == JardStart);
    fixture.shortcut = gesture;
    fixture.physicalModifiersHeld = caps;
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, control, t+.45, NO);
    assert(!fixture.keyDown && fixture.finishes == 1);
    fixture.physicalModifiersHeld = 0;
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, 0, t+.50, NO);

    fixture.shortcut = (JardShortcut){.mode = JardHold};
    fixture.shortcutTarget = @"shortcut";
    fixture.physicalModifiersHeld = caps;
    [fixture recordModifierChange:kVK_RightControl flags:kCGEventFlagMaskControl raw:control];
    [fixture recordModifierChange:kVK_RightControl flags:0 raw:0];
    assert([fixture.status[@"shortcutSide"] isEqual:@"either"]);
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, control, t+.60, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed);
    fixture.physicalModifiersHeld = 0;
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, 0, t+.65, NO);

    // Without a raw snapshot the existing Quartz side bits still work.
    fixture.settings[@"shortcutEitherSide"] = @NO;
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, control, t+.73, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed);
    shortcutEvent(fixture, kVK_RightControl, kCGEventFlagsChanged, 0, t+.81, NO);

    fixture.settings[@"shortcutCode"] = @(kVK_Function);
    fixture.settings[@"shortcutFlags"] = @(kCGEventFlagMaskSecondaryFn);
    const CGKeyCode globe = 179;
    CGEventFlags fn = kCGEventFlagMaskSecondaryFn;
    // The HID tap may see the Globe event before the session tap sees Fn.
    shortcutEvent(fixture, globe, kCGEventKeyDown, fn, t+1.02, YES);
    shortcutEvent(fixture, kVK_Function, kCGEventFlagsChanged, fn, t+1, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed);
    assert(!shortcutEvent(fixture, globe, kCGEventKeyDown, fn, t+1.03, NO));
    assert(!shortcutEvent(fixture, globe, kCGEventKeyUp, fn, t+1.04, NO));
    assert(fixture.shortcut.phase == JardArmed);
    NSUInteger revision = [fixture annotationPreparationRevision];
    CGEventRef key = CGEventCreateKeyboardEvent(NULL, globe, YES);
    [fixture observeUnmatchedInput:kCGEventKeyDown event:key];
    assert([fixture annotationPreparationRevision] == revision);
    CGEventSetIntegerValueField(key, kCGKeyboardEventKeycode, kVK_ANSI_B);
    [fixture observeUnmatchedInput:kCGEventKeyDown event:key];
    assert([fixture annotationPreparationRevision] == revision + 1);
    CFRelease(key);
    shortcutEvent(fixture, kVK_ANSI_B, kCGEventKeyDown, fn, t+1.10, NO);
    assert(fixture.shortcut.phase == JardIdle);
    shortcutEvent(fixture, kVK_Function, kCGEventFlagsChanged, 0, t+1.15, NO);

    CFArrayRef keyboards = TISCreateInputSourceList((__bridge CFDictionaryRef)@{
        (__bridge NSString *)kTISPropertyInputSourceID: @"com.apple.keylayout.German"
    }, true);
    assert(keyboards && CFArrayGetCount(keyboards) > 0);
    TISInputSourceRef german = (TISInputSourceRef)CFArrayGetValueAtIndex(keyboards, 0);
    assert([[fixture layoutLabelForShortcut:kVK_ISO_Section source:german] isEqual:@"^"]);
    CFRelease(keyboards);
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    puts("Shortcuts distinguished remapped Caps Lock and genuine Control, ignored Globe companion events, and named a German dead key.");
}

@interface ModifierClaimFixture : ShortcutEventFixture
@property(nonatomic) BOOL filterAvailable;
@property(nonatomic) CGEventFlags hardwareFlags;
@property(nonatomic, strong) NSMutableArray<NSDictionary *> *posted;
@end

@implementation ModifierClaimFixture
- (BOOL)modifierClaimAvailable { return self.filterAvailable; }
- (CGEventFlags)physicalModifierClaimFlags { return self.hardwareFlags; }
- (void)postModifierClaimEvent:(CGEventRef)event {
    CGEventType type = CGEventGetType(event);
    assert([self filterModifierClaim:event type:type]);
    assert(![self handleKey:event type:type]);
    if (type == kCGEventKeyDown) [self observePhysicalKeyDown:event];
    [self.posted addObject:@{@"type": @(type), @"flags": @(CGEventGetFlags(event)),
        @"key": @(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)),
        @"marker": @(CGEventGetIntegerValueField(event, kCGEventSourceUserData))}];
}
@end

static void testModifierClaim(void) {
    ModifierClaimFixture *fixture = [[ModifierClaimFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = fixture.filterAvailable = YES;
    fixture.posted = NSMutableArray.array;
    fixture.settings[@"shortcutCode"] = @(kVK_RightOption);
    fixture.settings[@"shortcutFlags"] = @(kCGEventFlagMaskAlternate);
    fixture.settings[@"shortcutEitherSide"] = @NO;
    CGEventFlags flags = kCGEventFlagMaskAlternate | NX_DEVICERALTKEYMASK;
    fixture.hardwareFlags = flags;
    fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
    NSTimeInterval t = NSProcessInfo.processInfo.systemUptime - 4;
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+.10, NO);
    [fixture sendModifierClaimAt:t+.40];
    assert(!fixture.posted.count);

    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t+.5, NO);
    JardShortcut gesture = fixture.shortcut;
    assert(jardShortcutArm(&gesture) == JardStart);
    fixture.shortcut = gesture;
    fixture.captureID = @"held-fixture";
    [fixture sendModifierClaimAt:t+.799];
    assert(!fixture.posted.count);
    [fixture sendModifierClaimAt:t+.801];
    [fixture sendModifierClaimAt:t+.9];
    assert(fixture.posted.count == 2 && fixture.shortcut.phase == JardHeld);
    assert([fixture.posted[0][@"type"] isEqual:@(kCGEventKeyDown)] && [fixture.posted[1][@"type"] isEqual:@(kCGEventKeyUp)]);
    assert([fixture.posted[0][@"flags"] isEqual:@(flags)] && [fixture.posted[1][@"flags"] isEqual:@(flags)]);
    assert([fixture.posted[0][@"key"] isEqual:@(0x44)] && [fixture.posted[0][@"marker"] isEqual:fixture.posted[1][@"marker"]]);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+.95, NO);

    CGEventRef event = CGEventCreateKeyboardEvent(NULL, 0x44, true);
    assert(![fixture filterModifierClaim:event type:kCGEventKeyDown]);
    CGEventSetIntegerValueField(event, kCGEventSourceUserData, [fixture.posted[0][@"marker"] longLongValue]);
    assert([fixture filterModifierClaim:event type:kCGEventKeyDown]);
    fixture.filterAvailable = NO;
    assert([fixture handleKey:event type:kCGEventKeyDown]);
    CGEventSetIntegerValueField(event, kCGKeyboardEventKeycode, kVK_ANSI_A);
    assert(![fixture filterModifierClaim:event type:kCGEventKeyDown]);
    CFRelease(event);
    fixture.filterAvailable = YES;

    fixture.shortcut = (JardShortcut){.mode = JardToggle};
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t+1, NO);
    [fixture sendModifierClaimAt:t+1.039];
    assert(fixture.posted.count == 2);
    [fixture sendModifierClaimAt:t+1.041];
    assert(fixture.posted.count == 4);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+1.1, NO);
    assert(fixture.shortcut.phase == JardLocked);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t+1.2, NO);
    [fixture sendModifierClaimAt:t+1.2];
    assert(fixture.posted.count == 6 && fixture.shortcut.phase == JardFinishing);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+1.21, NO);

    fixture.shortcut = (JardShortcut){.mode = JardToggle};
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t+1.4, NO);
    shortcutEvent(fixture, kVK_Shift, kCGEventFlagsChanged, flags | kCGEventFlagMaskShift, t+1.42, NO);
    [fixture sendModifierClaimAt:t+1.45];
    assert(fixture.posted.count == 6);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+1.46, NO);

    fixture.shortcut = (JardShortcut){.mode = JardToggle};
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t+1.6, NO);
    fixture.hardwareFlags = 0;
    [fixture sendModifierClaimAt:t+1.65];
    assert(fixture.posted.count == 6);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+1.66, NO);

    fixture.shortcut = (JardShortcut){.mode = JardToggle};
    fixture.settings[@"shortcutCode"] = @(kVK_Function);
    fixture.settings[@"shortcutFlags"] = @(kCGEventFlagMaskSecondaryFn);
    fixture.hardwareFlags = kCGEventFlagMaskSecondaryFn;
    shortcutEvent(fixture, kVK_Function, kCGEventFlagsChanged, fixture.hardwareFlags, t+2, NO);
    [fixture sendModifierClaimAt:t+2.3];
    assert(fixture.posted.count == 6);
    shortcutEvent(fixture, kVK_Function, kCGEventFlagsChanged, 0, t+2.4, NO);

    fixture.shortcut = (JardShortcut){.mode = JardToggle};
    fixture.settings[@"shortcutCode"] = @(kVK_F9);
    fixture.settings[@"shortcutFlags"] = @0;
    assert(shortcutEvent(fixture, kVK_F9, kCGEventKeyDown, 0, t+2.5, NO));
    [fixture sendModifierClaimAt:t+2.8];
    assert(fixture.posted.count == 6);
    assert(shortcutEvent(fixture, kVK_F9, kCGEventKeyUp, 0, t+2.81, NO));

    fixture.shortcut = (JardShortcut){.mode = JardDoubleTap};
    fixture.settings[@"shortcutCode"] = @(kVK_RightOption);
    fixture.settings[@"shortcutFlags"] = @(kCGEventFlagMaskAlternate);
    fixture.hardwareFlags = flags;
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, t+3, NO);
    [fixture sendModifierClaimAt:t+3.4];
    assert(fixture.posted.count == 6);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+3.41, NO);

    fixture.shortcut = (JardShortcut){.mode = JardHold};
    NSTimeInterval live = NSProcessInfo.processInfo.systemUptime;
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, flags, live, NO);
    gesture = fixture.shortcut;
    assert(jardShortcutArm(&gesture) == JardStart);
    fixture.shortcut = gesture;
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.36]];
    assert(fixture.posted.count == 8);
    puts("Modifier holds and Toggle claimed one marked pair, taps and Fn passed through, and both marked edges were filtered.");
}

static void testShortcutSide(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults setObject:@{@"shortcutCode": @(kVK_RightOption), @"shortcutFlags": @(kCGEventFlagMaskAlternate),
        @"shortcutLabel": @"Right ⌥"} forKey:@"JardSystemDictation"];
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    assert([fixture.status[@"shortcutSide"] isEqual:@"right"]);
    CGEventFlags left = kCGEventFlagMaskAlternate | NX_DEVICELALTKEYMASK;
    CGEventFlags right = kCGEventFlagMaskAlternate | NX_DEVICERALTKEYMASK;
    NSTimeInterval t = NSProcessInfo.processInfo.systemUptime - 4;
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, left | right, t+.02, NO);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, right, t+.04, NO);
    assert(!fixture.keyDown && fixture.shortcut.phase == JardIdle);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+.06, NO);

    [fixture request:@{@"action": @"setShortcutSide", @"value": @"left"} reply:^(id status, NSString *error) {
        assert(!error && [status[@"shortcutSide"] isEqual:@"left"] && [status[@"shortcutLabel"] isEqual:@"Left ⌥"]);
    }];
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t+.1, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed);
    [fixture request:@{@"action": @"setShortcutSide", @"value": @"either"} reply:^(id status, NSString *error) {
        assert(!status && [error containsString:@"Finish the take"]);
    }];
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, right | left, t+.15, NO);
    assert(fixture.shortcut.phase == JardIdle);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, right, t+.17, NO);
    assert(!fixture.keyDown);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+.18, NO);

    [fixture request:@{@"action": @"setShortcutSide", @"value": @"either"} reply:^(id status, NSString *error) {
        assert(!error && [status[@"shortcutSide"] isEqual:@"either"] && [status[@"shortcutLabel"] isEqual:@"⌥"]);
    }];
    for (NSUInteger order = 0; order < 2; order++) {
        t += .6;
        fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
        shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, right, t, NO);
        JardShortcut gesture = fixture.shortcut;
        assert(jardShortcutArm(&gesture) == JardStart);
        fixture.shortcut = gesture;
        shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, right | left, t+.22, NO);
        shortcutEvent(fixture, order ? kVK_RightOption : kVK_Option, kCGEventFlagsChanged, order ? left : right, t+.34, NO);
        assert(fixture.keyDown && fixture.shortcut.phase == JardHeld && fixture.finishes == order);
        shortcutEvent(fixture, order ? kVK_Option : kVK_RightOption, kCGEventFlagsChanged, 0, t+.35, NO);
        assert(!fixture.keyDown && fixture.shortcut.phase == JardFinishing && fixture.finishes == order + 1);
    }
    t += .6;
    fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t, NO);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, 0, t+.06, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, right, t+.1, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+.16, NO);
    assert(fixture.shortcut.phase == JardLocked && fixture.starts == 1);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t+.5, NO);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, 0, t+.55, NO);
    assert(fixture.shortcut.phase == JardFinishing && fixture.finishes == 3);

    JardSystemDictation *reloaded = [[JardSystemDictation alloc] initWithSend:nil];
    assert([reloaded.status[@"shortcutSide"] isEqual:@"either"]);
    assert([reloaded saveRecordedCode:kVK_F9 flags:0 label:@"F9"]);
    assert([reloaded.status[@"shortcutSide"] isEqual:@""]);
    [reloaded request:@{@"action": @"setShortcutSide", @"value": @"right"} reply:^(id status, NSString *error) {
        assert(!status && error);
    }];
    [defaults removeObjectForKey:@"JardSystemDictation"];
    puts("Shortcut side retained old bindings, balanced both release orders, and locked across hands.");
}

static void testModifierChords(void) {
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    CGEventFlags option = kCGEventFlagMaskAlternate, command = kCGEventFlagMaskCommand;
    CGEventFlags left = option | NX_DEVICELALTKEYMASK, right = option | NX_DEVICERALTKEYMASK;
    fixture.shortcutTarget = @"shortcut";
    fixture.choosingShortcut = YES;
    [fixture recordModifierChange:kVK_Option flags:option raw:left];
    [fixture recordModifierChange:kVK_RightCommand flags:option | command raw:left | command | NX_DEVICERCMDKEYMASK];
    [fixture recordModifierChange:kVK_Option flags:command raw:command | NX_DEVICERCMDKEYMASK];
    assert(fixture.choosingShortcut);
    [fixture recordModifierChange:kVK_RightCommand flags:0 raw:0];
    assert(!fixture.choosingShortcut && [fixture.status[@"shortcutLabel"] isEqual:@"⌥⌘"]);
    assert([fixture.status[@"shortcutSide"] isEqual:@""]);

    NSTimeInterval t = NSProcessInfo.processInfo.systemUptime - 4;
    CGEventFlags leftCommand = command | NX_DEVICELCMDKEYMASK;
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, right, t, NO);
    assert(fixture.shortcut.phase == JardIdle);
    shortcutEvent(fixture, kVK_Command, kCGEventFlagsChanged, right | leftCommand, t+.4, NO);
    assert(fixture.shortcut.phase == JardArmed && fixture.keyDown);
    JardShortcut gesture = fixture.shortcut;
    assert(jardShortcutArm(&gesture) == JardStart);
    fixture.shortcut = gesture;
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, leftCommand, t+.8, NO);
    assert(fixture.finishes == 1 && !fixture.keyDown);
    shortcutEvent(fixture, kVK_Command, kCGEventFlagsChanged, 0, t+.82, NO);

    fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
    fixture.shortcutTarget = @"raw";
    fixture.choosingShortcut = YES;
    [fixture recordModifierChange:kVK_RightOption flags:option raw:right];
    [fixture recordModifierChange:kVK_Option flags:option raw:left | right];
    [fixture recordModifierChange:kVK_RightOption flags:option raw:left];
    [fixture recordModifierChange:kVK_Option flags:0 raw:0];
    assert(!fixture.choosingShortcut && [fixture.status[@"rawLabel"] isEqual:@"⌥⌥"]);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t+1, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, left | right, t+1.081, NO);
    assert(!fixture.keyDown && fixture.shortcut.phase == JardIdle);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, right, t+1.11, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+1.12, NO);

    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, right, t+1.3, NO);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left | right, t+1.379, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed && fixture.usingRawBinding);
    gesture = fixture.shortcut;
    assert(jardShortcutArm(&gesture) == JardStart);
    fixture.shortcut = gesture;
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, right, t+1.8, NO);
    assert(fixture.finishes == 2 && !fixture.keyDown);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+1.81, NO);

    fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
    fixture.shortcutTarget = @"shortcut";
    assert([fixture saveRecordedCode:kVK_Option flags:option label:@"Left ⌥"]);
    [fixture request:@{@"action": @"setShortcutSide", @"value": @"either"} reply:^(id status, NSString *error) {
        assert(!status && [error containsString:@"already in use"]);
    }];
    assert([fixture.status[@"shortcutLabel"] isEqual:@"Left ⌥"] && [fixture.status[@"rawLabel"] isEqual:@"⌥⌥"]);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t+2.0, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed && !fixture.usingRawBinding);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, left | right, t+2.05, NO);
    assert(fixture.keyDown && fixture.shortcut.phase == JardArmed && fixture.usingRawBinding);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, right, t+2.15, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, t+2.16, NO);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t+2.21, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, left | right, t+2.24, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, left, t+2.30, NO);
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, 0, t+2.31, NO);
    assert(fixture.shortcut.phase == JardLocked && fixture.starts == 1 && fixture.usingRawBinding);

    fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
    fixture.ordinaryKeyHeld = YES;
    shortcutEvent(fixture, kVK_Option, kCGEventFlagsChanged, left, t+2.5, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, left | right, t+2.53, NO);
    assert(fixture.shortcut.phase == JardIdle && !fixture.keyDown);

    JardSystemDictation *reloaded = [[JardSystemDictation alloc] initWithSend:nil];
    assert([reloaded.status[@"rawLabel"] isEqual:@"⌥⌥"]);
    [reloaded request:@{@"action": @"clearRawShortcut"} reply:^(id status, NSString *error) {
        assert(!error && [status[@"rawLabel"] isEqual:@""] && ![status[@"rawBothSides"] unsignedLongLongValue]);
    }];
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    puts("Modifier chords retained the peak set, matched either hand, enforced the 80 ms pair, and switched to raw dictation on exact completion.");
}

static void testShortcutChordEvidence(void) {
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    fixture.settings[@"shortcutCode"] = @(kVK_RightOption);
    fixture.settings[@"shortcutFlags"] = @(kCGEventFlagMaskAlternate);
    fixture.settings[@"rawCode"] = @(-1);
    fixture.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
    CGEventFlags option = kCGEventFlagMaskAlternate;
    NSTimeInterval start = NSProcessInfo.processInfo.systemUptime - 2.0;
    for (NSUInteger i = 0; i < 2; i++) {
        NSTimeInterval now = start + .15 * i;
        assert(!shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, option, now, NO));
        assert(fixture.shortcut.phase == JardArmed);
        shortcutEvent(fixture, kVK_ANSI_1, kCGEventKeyDown, option, now + .04, YES);
        assert(fixture.shortcut.phase == JardIdle);
        shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, now + .09, NO);
        assert(fixture.shortcut.lastTap == 0 && !fixture.keyDown && !fixture.starts);
    }
    // This chord reached the HID tap before its earlier modifier edge reached the session tap.
    shortcutEvent(fixture, kVK_ANSI_2, kCGEventKeyDown, option, start + .55, YES);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, option, start + .5, NO);
    assert(fixture.shortcut.phase == JardIdle && fixture.keyDown);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, start + .6, NO);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, option, start + .8, NO);
    assert(fixture.shortcut.phase == JardArmed);

    JardShortcut held = fixture.shortcut;
    held.phase = JardHeld;
    fixture.shortcut = held;
    assert(!shortcutEvent(fixture, kVK_Return, kCGEventKeyDown, option, start + 1.15, NO));
    shortcutEvent(fixture, kVK_ANSI_KeypadEnter, kCGEventKeyDown, option, start + 1.2, YES);
    assert(fixture.shortcut.phase == JardHeld && !fixture.cancels);
    shortcutEvent(fixture, kVK_ANSI_A, kCGEventKeyDown, option, start + 1.3, YES);
    assert(fixture.shortcut.phase == JardIdle && fixture.cancels == 1);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, start + 1.35, NO);

    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, option, start + 1.36, NO);
    assert(fixture.shortcut.phase == JardArmed);
    CGEventRef click = CGEventCreateMouseEvent(NULL, kCGEventLeftMouseDown, CGPointZero, kCGMouseButtonLeft);
    assert(click);
    CGEventSetFlags(click, option);
    CGEventSetTimestamp(click, (CGEventTimestamp)((start + 1.38) * NSEC_PER_SEC));
    assert(![fixture handleKey:click type:kCGEventLeftMouseDown] && fixture.shortcut.phase == JardIdle);
    CFRelease(click);
    shortcutEvent(fixture, kVK_RightOption, kCGEventFlagsChanged, 0, start + 1.39, NO);

    fixture.shortcut = (JardShortcut){.mode = JardToggle, .phase = JardLocked};
    fixture.settings[@"shortcutCode"] = @(kVK_F9);
    fixture.settings[@"shortcutFlags"] = @0;
    fixture.keyDown = fixture.suppressedKey = YES;
    [fixture repairShortcutReleaseAt:start+1.4 inputHeld:NO];
    [fixture repairShortcutReleaseAt:start+1.42 inputHeld:YES];
    [fixture repairShortcutReleaseAt:start+1.5 inputHeld:NO];
    [fixture repairShortcutReleaseAt:start+1.55 inputHeld:NO];
    assert(fixture.keyDown && fixture.shortcut.phase == JardLocked && !fixture.finishes);
    [fixture repairShortcutReleaseAt:start+1.65 inputHeld:NO];
    assert(!fixture.keyDown && !fixture.suppressedKey && fixture.shortcut.phase == JardLocked && !fixture.finishes);
    assert(shortcutEvent(fixture, kVK_F9, kCGEventKeyDown, 0, start + 1.7, NO));
    assert(fixture.finishes == 1);
    fixture.shortcut = (JardShortcut){.mode = JardToggle, .phase = JardLocked};
    assert(shortcutEvent(fixture, kVK_F9, kCGEventKeyDown, 0, start + 1.8, NO));
    assert(fixture.finishes == 2);
    assert(!shortcutEvent(fixture, kVK_F9, kCGEventKeyUp, 0, start + 1.75, NO));
    assert(fixture.keyDown && fixture.suppressedKey && fixture.finishes == 2);
    puts("Chord evidence handled intercepted and out-of-order keys, kept long holds through Return, and repaired lost releases without ending a lock.");
}

static void testStaleShortcutInput(void) {
    ShortcutEventFixture *(^fixture)(void) = ^{
        ShortcutEventFixture *input = [[ShortcutEventFixture alloc] initWithSend:nil];
        input.dictationKeyEnabled = YES;
        input.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap};
        input.settings[@"shortcutCode"] = @(kVK_RightOption);
        input.settings[@"shortcutFlags"] = @(kCGEventFlagMaskAlternate);
        input.settings[@"rawCode"] = @(-1);
        input.settings[@"rawFlags"] = @0;
        return input;
    };
    CGEventFlags command = kCGEventFlagMaskCommand, option = kCGEventFlagMaskAlternate;
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime - .05;
    for (NSUInteger scenario = 0; scenario < 5; scenario++) {
        ShortcutEventFixture *input = fixture();
        input.sessionKeys = [NSIndexSet indexSetWithIndex:kVK_Tab];
        if (scenario) input.hidKeys = input.sessionKeys;
        NSTimeInterval tab = scenario >= 2 ? now - 6 : now - .15;
        shortcutEvent(input, kVK_Command, kCGEventFlagsChanged, command, tab - .02, NO);
        shortcutEvent(input, kVK_Tab, kCGEventKeyDown, command, tab, NO);
        if (scenario != 4) shortcutEvent(input, kVK_Command, kCGEventFlagsChanged, 0, tab + .02, NO);
        if (scenario == 3) {
            CGEventRef repeat = CGEventCreateKeyboardEvent(NULL, kVK_Tab, true);
            CGEventSetTimestamp(repeat, (CGEventTimestamp)((now - .09) * NSEC_PER_SEC));
            CGEventSetIntegerValueField(repeat, kCGKeyboardEventAutorepeat, 1);
            [input handleKey:repeat type:kCGEventKeyDown];
            CFRelease(repeat);
        }
        shortcutEvent(input, kVK_RightOption, kCGEventFlagsChanged, option, now, NO);
        BOOL accepts = scenario == 0 || scenario == 2;
        assert(input.keyDown == accepts && input.shortcut.phase == (accepts ? JardArmed : JardIdle));
    }

    ShortcutEventFixture *locked = fixture();
    locked.shortcut = (JardShortcut){.mode = JardToggle, .phase = JardLocked};
    locked.keyDown = YES;
    NSTimeInterval poll = now - .70;
    locked.liveModifiers = kCGEventFlagMaskSecondaryFn;
    [locked sampleShortcutReleaseAt:poll];
    [locked sampleShortcutReleaseAt:poll + .15];
    assert(locked.keyDown); // Unknown Fn cannot be dismissed as navigation metadata.
    shortcutEvent(locked, kVK_Function, kCGEventFlagsChanged, 0, poll + .16, NO);
    // The clean Fn edge released the tracked trigger. Recreate its missed release for the poll.
    locked.keyDown = YES;
    locked.mouseHeld = YES;
    [locked sampleShortcutReleaseAt:poll + .20];
    [locked sampleShortcutReleaseAt:poll + .33];
    assert(locked.keyDown && !locked.finishes);
    locked.mouseHeld = NO;
    [locked sampleShortcutReleaseAt:poll + .36];
    shortcutEvent(locked, kVK_ANSI_A, kCGEventKeyUp, 0, poll + .40, NO);
    [locked sampleShortcutReleaseAt:poll + .47];
    [locked sampleShortcutReleaseAt:poll + .52];
    assert(locked.keyDown);
    [locked sampleShortcutReleaseAt:poll + .59];
    assert(!locked.keyDown && locked.shortcut.phase == JardLocked && !locked.finishes);
    shortcutEvent(locked, kVK_RightOption, kCGEventFlagsChanged, option, now - .07, NO);
    assert(locked.finishes == 1 && locked.keyDown);
    shortcutEvent(locked, kVK_RightOption, kCGEventFlagsChanged, 0, poll + .50, NO);
    assert(locked.keyDown && locked.shortcut.phase == JardFinishing);

    ShortcutEventFixture *held = fixture();
    held.shortcut = (JardShortcut){.mode = JardHold, .phase = JardHeld, .pressedAt = now - 1};
    held.keyDown = YES;
    [held sampleShortcutReleaseAt:now - .2];
    [held sampleShortcutReleaseAt:now];
    assert(held.keyDown && held.shortcut.phase == JardHeld && !held.finishes);

    ShortcutEventFixture *paste = fixture();
    paste.suppressedPaste = paste.suppressedMeetingPaste = YES;
    [paste sampleShortcutReleaseAt:now - .2];
    assert(paste.suppressedPaste && paste.suppressedMeetingPaste);
    [paste sampleShortcutReleaseAt:now];
    assert(!paste.suppressedPaste && !paste.suppressedMeetingPaste && paste.shortcut.phase == JardIdle);

    ShortcutEventFixture *chord = fixture();
    shortcutEvent(chord, kVK_ANSI_A, kCGEventKeyDown, option, now, YES);
    shortcutEvent(chord, kVK_RightOption, kCGEventFlagsChanged, option, now, NO);
    assert(chord.shortcut.phase == JardIdle && !chord.starts);
    puts("Shortcut recovery distinguished stale session state and quiet latched keys from held input, and kept late releases out of fresh captures.");
}

static void testCancelShortcuts(void) {
    __block NSUInteger cancelled = 0;
    __block JardDictationReply transcription;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.record"]) reply(@{}, nil);
        else if ([payload[@"action"] isEqual:@"transcribe"]) transcription = [reply copy];
        else if ([payload[@"action"] isEqual:@"cancel"]) { cancelled++; reply(@{}, nil); }
        else assert(false);
    }];
    dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
    dictation.settings[@"sound"] = @NO;
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"output"] = @"copy";
    dictation.settings[@"retention"] = @0;
    dictation.settings[@"mode"] = @"tap";
    dictation.settings[@"shortcutCode"] = @(kVK_F9);
    dictation.settings[@"shortcutFlags"] = @0;
    [dictation applyShortcutMode];
    __block BOOL annotationHandlesEscape = NO;
    [dictation setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL chordFree) {
        return annotationHandlesEscape && type == kCGEventKeyDown &&
            CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) == kVK_Escape;
    }];
    dictation.dictationKeyEnabled = YES;
    assert([dictation.status[@"cancelLabel"] isEqual:@"Escape"]);
    dictation.shortcutTarget = @"cancel";
    assert(![dictation saveRecordedCode:kVK_F9 flags:0 label:@"F9"]);
    assert(![dictation saveRecordedCode:kVK_ANSI_S flags:kCGEventFlagMaskAlternate | kCGEventFlagMaskShift label:@"⌥⇧S"]);
    assert([dictation saveRecordedCode:kVK_ANSI_K flags:kCGEventFlagMaskControl label:@"⌃K"]);
    assert([[[[TestDictation alloc] initWithSend:nil] status][@"cancelLabel"] isEqual:@"⌃K"]);

    finishTake(dictation, @"accepted-output", NO);
    assert(!press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(!press(dictation, kVK_ANSI_K, kCGEventKeyDown, kCGEventFlagMaskControl, NO));
    assert(!press(dictation, kVK_ANSI_K, kCGEventKeyUp, kCGEventFlagMaskControl, NO));
    assert(dictation.pendingJobs.count == 1);
    assert(press(dictation, kVK_F9, kCGEventKeyDown, 0, NO));
    assert(press(dictation, kVK_F9, kCGEventKeyUp, 0, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(dictation.captureID);
    assert(!press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(!press(dictation, kVK_ANSI_K, kCGEventKeyDown, 0, NO));
    assert(press(dictation, kVK_ANSI_K, kCGEventKeyDown, kCGEventFlagMaskControl, NO));
    assert(press(dictation, kVK_ANSI_K, kCGEventKeyDown, 0, YES));
    assert(press(dictation, kVK_ANSI_K, kCGEventKeyUp, 0, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(cancelled == 1 && !dictation.captureID && dictation.pendingJobs.count == 1);
    transcription(@{@"text": @"Keep the accepted output"}, nil);
    assert([dictation.copiedText isEqual:@"Keep the accepted output"]);

    [dictation request:@{@"action": @"resetCancelShortcut"} reply:^(id status, NSString *error) {
        assert(!error && [status[@"cancelLabel"] isEqual:@"Escape"]);
    }];
    assert(press(dictation, kVK_F9, kCGEventKeyDown, 0, NO));
    assert(press(dictation, kVK_F9, kCGEventKeyUp, 0, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    annotationHandlesEscape = YES;
    assert(press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(dictation.captureID && dictation.shortcut.phase == JardLocked);
    annotationHandlesEscape = NO;
    assert(press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(press(dictation, kVK_Escape, kCGEventKeyUp, 0, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(cancelled == 2);

    dictation.settings[@"mode"] = @"hold";
    [dictation applyShortcutMode];
    assert(press(dictation, kVK_F9, kCGEventKeyDown, 0, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(dictation.captureID && dictation.shortcut.phase == JardHeld);
    JardShortcut held = dictation.shortcut;
    held.pressedAt = NSProcessInfo.processInfo.systemUptime;
    dictation.shortcut = held;
    assert(!press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(!press(dictation, kVK_Escape, kCGEventKeyUp, 0, NO));
    held.pressedAt -= 1;
    dictation.shortcut = held;
    assert(press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(press(dictation, kVK_Escape, kCGEventKeyUp, 0, NO));
    assert(press(dictation, kVK_F9, kCGEventKeyUp, 0, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(cancelled == 3 && !dictation.captureID);

    dictation.shortcut = (JardShortcut){.mode = JardHoldOrDoubleTap, .phase = JardLocked};
    assert(!press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(dictation.shortcut.phase == JardLocked);
    dictation.shortcut = (JardShortcut){.mode = JardToggle, .phase = JardFinishing};
    assert(!press(dictation, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(dictation.shortcut.phase == JardFinishing);
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    puts("Tap cancellation suppressed the configured chord, preserved accepted output, and respected annotation and hands-free capture.");
}

static void testShortcutTimingCapture(void) {
    __block NSUInteger starts = 0, stops = 0, submissions = 0;
    __block NSTimeInterval expectedRelease = 0;
    __block JardDictationReply discardReply;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.record"]) {
            starts++;
            assert([payload[@"preRoll"] boolValue]);
            reply(@{}, nil);
        } else if ([payload[@"action"] isEqual:@"cancel"]) {
            assert(![payload[@"releaseTail"] boolValue] && !discardReply);
            discardReply = [reply copy];
        } else if ([payload[@"action"] isEqual:@"stop"]) {
            stops++;
            assert([payload[@"releaseTail"] boolValue]);
            assert(fabs([payload[@"releaseAt"] doubleValue] - expectedRelease) < 1e-6);
            reply(@{}, nil);
        } else if ([payload[@"action"] isEqual:@"transcribe"]) {
            submissions++;
            reply(@{@"text": @"Save the locked take"}, nil);
        } else assert(false);
    }];
    dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"sound"] = @NO;
    dictation.settings[@"retention"] = @0;
    dictation.settings[@"output"] = @"copy";
    dictation.settings[@"audioWhileDictating"] = @"none";
    dictation.settings[@"mode"] = @"holdDouble";
    dictation.settings[@"shortcutCode"] = @(kVK_F9);
    dictation.settings[@"shortcutFlags"] = @(kCGEventFlagMaskAlternate);
    [dictation applyShortcutMode];
    dictation.dictationKeyEnabled = YES;

    NSTimeInterval at = NSProcessInfo.processInfo.systemUptime - 2;
    CGEventFlags option = kCGEventFlagMaskAlternate;
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, option, at, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    NSString *first = dictation.captureID;
    assert(first && starts == 1 && dictation.shortcut.phase == JardHeld);

    // Releasing the required modifier ends the gesture while its key-up stays balanced.
    assert(!shortcutEvent(dictation, kVK_Option, kCGEventFlagsChanged, 0, at+.20, NO));
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, 0, at+.21, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(discardReply && !stops && !submissions && [dictation.phase isEqual:@"idle"]);
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, option, at+.30, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(dictation.shortcut.phase == JardHeld && starts == 1);
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, option, at+.40, NO));
    assert(dictation.shortcut.phase == JardLocked);
    discardReply(@{}, nil);
    NSString *second = dictation.captureID;
    assert(second && ![second isEqual:first] && starts == 2);
    assert(!stops && !submissions && !dictation.copiedText);

    expectedRelease = at + 1;
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, option, expectedRelease, NO));
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, option, at+1.1, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(stops == 1 && !submissions);
    assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": second, @"id": @"second-stage", @"seconds": @1}]));
    assert(submissions == 1 && [dictation.copiedText isEqual:@"Save the locked take"]);
    puts("Short holds discarded their capture, the second release locked, and the bridge retained normal pre-roll and release-tail requests.");
}

static void testStartCueWorker(void) {
    NSURL *url = [NSURL fileURLWithPath:@"/fixture.wav"];
    StartCueWorkerFixture *withdrawn = StartCueWorkerFixture.new;
    withdrawn.player = FeedbackPlayerFixture.new;
    withdrawn.player.unblock = dispatch_semaphore_create(0);
    NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + .04;
    [withdrawn playURL:url volume:.7 at:deadline];
    assert(dispatch_semaphore_wait(withdrawn.player.prepared, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
    [withdrawn withdraw];
    dispatch_semaphore_signal(withdrawn.player.unblock);
    assert(dispatch_semaphore_wait(withdrawn.player.played, dispatch_time(DISPATCH_TIME_NOW, 80 * NSEC_PER_MSEC)) != 0);

    StartCueWorkerFixture *sounding = StartCueWorkerFixture.new;
    sounding.player = FeedbackPlayerFixture.new;
    deadline = NSProcessInfo.processInfo.systemUptime + .04;
    [sounding playURL:url volume:.7 at:deadline];
    assert(dispatch_semaphore_wait(sounding.player.played, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
    assert(sounding.player.volume == .7f && sounding.player.playedAt >= deadline - .002);
    [sounding withdraw];
    assert(dispatch_semaphore_wait(sounding.player.stopped, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC)) == 0);
    assert(sounding.player.fadeDuration == .02 && sounding.player.stoppedAt >= sounding.player.fadedAt + .018);
    puts("Start cue preparation ran off the input thread, cancellation beat a stalled audio open, and a sounding cue faded for 20 ms.");
}

static void testStartCueGestures(void) {
    for (NSString *scenario in @[@"chord", @"tap", @"warm", @"cold-early", @"cold-late", @"silent", @"gap", @"timer"]) {
        __block NSUInteger starts = 0;
        CaptureEnvironmentFixture *environment = CaptureEnvironmentFixture.new;
        FeedbackControllerFixture *dictation = [[FeedbackControllerFixture alloc] initWithSend:
            ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
                if ([command isEqual:@"jard.record"]) {
                    starts++;
                    assert(!environment.active);
                } else assert([payload[@"action"] isEqual:@"cancel"]);
                if (reply) reply(@{}, nil);
            }];
        BOOL keep = ![scenario hasPrefix:@"cold"];
        BOOL silent = [scenario isEqual:@"silent"];
        dictation.provisional = NSMutableArray.array;
        dictation.recordingEnvironment = environment;
        dictation.settings[@"keepMicrophoneReady"] = @(keep);
        dictation.settings[@"sound"] = @YES;
        dictation.settings[@"soundVolume"] = silent ? @0 : @70;
        dictation.settings[@"mode"] = @"hold";
        dictation.settings[@"shortcutCode"] = @(kVK_F9);
        dictation.settings[@"shortcutFlags"] = @0;
        dictation.settings[@"placement"] = @"off";
        dictation.settings[@"audioWhileDictating"] = @"mute";
        dictation.dictationKeyEnabled = YES;
        [dictation applyShortcutMode];

        NSTimeInterval press = NSProcessInfo.processInfo.systemUptime;
        assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, press, NO));
        StartCueTokenFixture *cue = dictation.provisional.firstObject;
        assert(dictation.provisional.count == (keep && !silent ? 1 : 0));
        if (cue) assert(fabs(cue.deadline - press - .04) < 1e-6);
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.015]];
        assert(starts == 1 && !environment.active && !dictation.startCues);
        NSDictionary *opened = @{@"event": @"started", @"dictation": dictation.captureID, @"openedAt": @(press + .02)};
        if (![scenario isEqual:@"cold-late"]) assert([dictation handleCapture:opened]);
        assert(!environment.active && !dictation.startCues);
        if ([scenario isEqual:@"chord"] || [scenario isEqual:@"tap"] || [scenario isEqual:@"gap"]) {
            if ([scenario isEqual:@"gap"]) [dictation interruptShortcutInput:kCGEventTapDisabledByUserInput];
            else if ([scenario isEqual:@"chord"])
                assert(!shortcutEvent(dictation, kVK_ANSI_A, kCGEventKeyDown, 0, press + .02, NO));
            else assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, 0, press + .06, NO));
            [dictation admitCaptureIntentionAt:press + .3];
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.015]];
            assert(cue.withdrawals && !environment.active && !environment.ended && !dictation.startCues && !dictation.captureID);
        } else {
            if ([scenario isEqual:@"timer"]) {
                NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:1];
                while (!environment.active && deadline.timeIntervalSinceNow > 0)
                    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
                assert(NSProcessInfo.processInfo.systemUptime >= press + .299);
            } else {
                [dictation admitCaptureIntentionAt:press + .295];
                assert(!environment.active);
                [dictation admitCaptureIntentionAt:press + .3];
            }
            assert(environment.active && [environment.mode isEqual:@"mute"]);
            if ([scenario isEqual:@"cold-late"]) {
                assert(!dictation.startCues);
                assert([dictation handleCapture:opened]);
            }
            assert(dictation.startCues == (keep ? 0 : 1));
            [dictation cancelCapture];
            assert(!environment.active && environment.ended == 1);
        }
        [dictation shutdown];
    }
    puts("Shortcut cues were scoped to the gesture, and cold feedback and recording effects waited for the correct intention and readiness states.");
}

static void testPressDuringReleaseTail(void) {
    for (NSString *mode in @[@"warm", @"cold", @"cancel", @"profile"]) {
        BOOL profile = [mode isEqual:@"profile"];
        CGKeyCode secondKey = profile ? kVK_F9 : kVK_F8;
        NSMutableArray<NSDictionary *> *starts = NSMutableArray.array, *stops = NSMutableArray.array;
        NSMutableArray<NSDictionary *> *jobs = NSMutableArray.array;
        NSMutableArray<JardDictationReply> *replies = NSMutableArray.array;
        FeedbackControllerFixture *dictation = [[FeedbackControllerFixture alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([command isEqual:@"jard.record"]) { [starts addObject:payload]; reply(@{}, nil); }
            else if ([payload[@"action"] isEqual:@"stop"]) { [stops addObject:payload]; reply(@{}, nil); }
            else if ([payload[@"action"] isEqual:@"transcribe"]) { [jobs addObject:payload]; [replies addObject:[reply copy]]; }
            else assert(false);
        }];
        dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
        dictation.provisional = NSMutableArray.array;
        dictation.settings[@"sound"] = @YES;
        dictation.settings[@"soundVolume"] = @70;
        dictation.settings[@"placement"] = @"off";
        dictation.settings[@"retention"] = @0;
        dictation.settings[@"output"] = @"copy";
        dictation.settings[@"audioWhileDictating"] = @"none";
        dictation.settings[@"keepMicrophoneReady"] = @(![mode isEqual:@"cold"]);
        dictation.settings[@"mode"] = @"hold";
        dictation.settings[@"shortcutCode"] = @(kVK_F9);
        dictation.settings[@"shortcutFlags"] = @0;
        dictation.settings[@"rawCode"] = @(kVK_F8);
        dictation.settings[@"rawFlags"] = @0;
        dictation.settings[@"inputDevice"] = @"First input";
        dictation.settings[@"vocabulary"] = @"First phrase";
        if (profile) dictation.settings[@"outputProfiles"] = @"app:First release | lowercase\napp:Second release | trim";
        [dictation applyShortcutMode];
        dictation.dictationKeyEnabled = YES;
        NSTimeInterval t = NSProcessInfo.processInfo.systemUptime - 2;
        assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, t, NO));
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        NSString *first = dictation.captureID;
        assert(first && starts.count == 1);
        if (profile) dictation.outputForeground = outputTarget(@"First release", 31002);
        assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, 0, t+.40, NO));
        [dictation activationChanged:nil];
        dictation.settings[@"inputDevice"] = @"Second input";
        NSUInteger nextRevision = [dictation annotationPreparationRevision];
        assert(shortcutEvent(dictation, secondKey, kCGEventKeyDown, 0, t+.45, NO));
        assert([dictation.captureID isEqual:first] && starts.count == 1);
        assert(dictation.provisional.count == ([mode isEqual:@"cold"] ? 0 : 2));
        [dictation activationChanged:nil];
        dictation.settings[@"inputDevice"] = @"Third input";
        if ([mode isEqual:@"cancel"])
            assert(shortcutEvent(dictation, kVK_Escape, kCGEventKeyDown, 0, t+.80, NO));
        if (profile) dictation.outputForeground = outputTarget(@"Second release", 31003);
        if (![mode isEqual:@"cold"])
            assert(shortcutEvent(dictation, secondKey, kCGEventKeyUp, 0, t+.84, NO));
        if (profile) dictation.outputForeground = outputTarget(@"After both releases", 31004);
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        assert(stops.count == 1 && [stops[0][@"id"] isEqual:first] && fabs([stops[0][@"releaseAt"] doubleValue] - (t+.40)) < 1e-6);
        assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": first, @"id": @"first", @"seconds": @1}]));
        assert(jobs.count == 1 && [jobs[0][@"vocabulary"] count] == 1);
        if ([mode isEqual:@"cancel"]) {
            assert(!dictation.captureID && starts.count == 1);
            assert(dictation.provisional[1].withdrawals && !dictation.provisional[0].withdrawals);
            replies[0](@{@"text": @"Keep the first take"}, nil);
            assert([dictation.lastText isEqual:@"Keep the first take"]);
            continue;
        }
        NSString *second = dictation.captureID;
        assert(second && ![second isEqual:first] && starts.count == 2 && dictation.takeInputRevision == nextRevision);
        for (StartCueTokenFixture *cue in dictation.provisional) assert(!cue.withdrawals);
        assert([starts[1][@"inputDevice"] isEqual:@"Second input"] && fabs([starts[1][@"queuedAt"] doubleValue] - (t+.45)) < 1e-6);
        if ([mode isEqual:@"cold"]) {
            assert(shortcutEvent(dictation, kVK_F8, kCGEventKeyUp, 0, t+.84, NO));
            [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
        }
        assert(stops.count == 2 && [stops[1][@"id"] isEqual:second] && fabs([stops[1][@"releaseAt"] doubleValue] - (t+.84)) < 1e-6);
        assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": second, @"id": @"second", @"seconds": @1}]));
        assert(jobs.count == 2 && [jobs[1][@"vocabulary"] count] == (profile ? 1 : 0));
        if (profile) {
            assert([jobs[0][@"outputProfile"][@"app"] isEqual:@"First release"]);
            assert([jobs[1][@"outputProfile"][@"app"] isEqual:@"Second release"]);
        }
        replies[1](@{@"text": @"Second words"}, nil);
        assert(![dictation.lastText isEqual:@"Second words"]);
        replies[0](@{@"text": @"First words"}, nil);
        [dictation drainOutput];
        assert([dictation.lastText isEqual:@"Second words"]);
    }
    puts("A new press during the release tail retained its timestamp, options and raw mode, with independent cancellation and ordered output.");
}

@interface MenuPasteFixture : TestDictation
@property(nonatomic) NSUInteger requestedPastes;
@end
@implementation MenuPasteFixture
- (void)pasteLast { self.requestedPastes++; }
@end

static void testPasteLastMenu(void) {
    MenuPasteFixture *fixture = [[MenuPasteFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    fixture.outputForeground = outputTarget(@"Disposable editor", 30010);
    fixture.settings[@"pasteCode"] = @(kVK_ANSI_V);
    fixture.settings[@"pasteFlags"] = @(kCGEventFlagMaskAlternate | kCGEventFlagMaskShift);
    fixture.settings[@"pasteLabel"] = @"⌥⇧V";
    fixtureEventTaps = true;
    [fixture maintainEventTapsForDictation:YES];
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:@"Paste Last Dictation" action:NULL keyEquivalent:@""];
    fixture.lastText = nil;
    [fixture configurePasteLastMenuItem:item];
    assert(!item.enabled && [item.keyEquivalent isEqual:@"v"]);
    fixture.lastText = @"Saved words";
    [fixture configurePasteLastMenuItem:item];
    assert(item.enabled && item.keyEquivalentModifierMask == (NSEventModifierFlagOption | NSEventModifierFlagShift));
    fixture.settings[@"pasteCode"] = @(kVK_F12);
    [fixture configurePasteLastMenuItem:item];
    assert([item.keyEquivalent characterAtIndex:0] == NSF12FunctionKey);
    fixture.settings[@"pasteCode"] = @(kVK_Space);
    [fixture configurePasteLastMenuItem:item];
    assert([item.keyEquivalent isEqual:@" "]);
    for (NSNumber *code in @[@(-1), @(kVK_RightOption), @258]) {
        fixture.settings[@"pasteCode"] = code;
        [fixture configurePasteLastMenuItem:item];
        assert(item.enabled && !item.keyEquivalent.length && !item.keyEquivalentModifierMask);
    }
    [fixture pasteLastFromMenu];
    assert(!fixture.requestedPastes);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(fixture.requestedPastes == 1);
    [fixture pasteLastFromMenu];
    fixture.outputForeground = outputTarget(@"Another editor", 30011);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert(fixture.requestedPastes == 1);
    fixture.captureID = @"busy";
    [fixture configurePasteLastMenuItem:item];
    assert(!item.enabled);
    fixture.captureID = nil;
    [fixture shutdown];
    for (NSUInteger i = 0; i < 3; i++) {
        CFRelease(fixtureTaps[i].port);
        memset(&fixtureTaps[i], 0, sizeof fixtureTaps[i]);
    }
    fixtureEventTaps = false;
    puts("Paste Last showed the saved keyed shortcut, kept unbound menu access, and deferred to the original frontmost app.");
}

static void testEventTapRecovery(void) {
    NSMutableArray<NSString *> *commands = NSMutableArray.array;
    TestDictation *dictation = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        if ([command isEqual:@"jard.record"]) [commands addObject:@"record"];
        else [commands addObject:payload[@"action"]];
        if (reply) reply([payload[@"action"] isEqual:@"transcribe"] ? @{@"text": @"Recovered dictation"} : @{}, nil);
    }];
    dictation.recordingEnvironment = CaptureEnvironmentFixture.new;
    dictation.settings[@"sound"] = @NO;
    dictation.settings[@"placement"] = @"off";
    dictation.settings[@"retention"] = @0;
    dictation.settings[@"output"] = @"copy";
    dictation.settings[@"mode"] = @"hold";
    dictation.settings[@"shortcutCode"] = @(kVK_F9);
    dictation.settings[@"shortcutFlags"] = @0;
    [dictation applyShortcutMode];
    __block unsigned inputGaps = 0;
    [dictation setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL free) {
        if (type == kCGEventTapDisabledByUserInput || type == kCGEventTapDisabledByTimeout) inputGaps++;
        return NO;
    }];
    dictation.dictationKeyEnabled = YES;
    fixtureEventTaps = true;
    typeof(fixtureTaps[0]) *session = &fixtureTaps[kCGSessionEventTap];
    typeof(fixtureTaps[0]) *filter = &fixtureTaps[kCGAnnotatedSessionEventTap];
    typeof(fixtureTaps[0]) *physical = &fixtureTaps[kCGHIDEventTap];
    [dictation maintainEventTapsForDictation:YES];
    assert(session->enabled && filter->enabled && physical->enabled && dictation.listenerActivity);
    void (^settle)(void) = ^{ [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]]; };
    NSTimeInterval pressed = NSProcessInfo.processInfo.systemUptime;
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, pressed, NO));
    session->enabled = false;
    [dictation maintainEventTapsForDictation:YES];
    settle();
    assert(inputGaps == 1 && session->enabled && session->creations == 1 && !commands.count && !dictation.captureID);
    assert(!shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, 0, pressed, NO));

    pressed = NSProcessInfo.processInfo.systemUptime;
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, pressed, NO));
    settle();
    NSString *releasedTake = dictation.captureID;
    assert(releasedTake && [commands.lastObject isEqual:@"record"]);
    // Losing the chord observer or the downstream marker filter doesn't lose
    // session edges, so it must not discard the held take.
    filter->enabled = false;
    CFMachPortInvalidate(physical->port);
    [dictation maintainEventTapsForDictation:YES];
    assert(inputGaps == 1 && filter->enabled && physical->creations == 2 &&
        [dictation.captureID isEqual:releasedTake] && dictation.keyDown);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.45]];
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, 0, pressed+.40, NO));
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, NSProcessInfo.processInfo.systemUptime, NO));
    CFMachPortInvalidate(session->port);
    session->blockCreation = true;
    [dictation maintainEventTapsForDictation:YES];
    session->blockCreation = false;
    [dictation maintainEventTapsForDictation:YES];
    settle();
    assert(inputGaps == 2 && session->creations == 2 && ![dictation.status[@"listener"] boolValue] &&
        [dictation.status[@"listenerNeedsRestart"] boolValue]);
    assert(([commands isEqual:@[@"record", @"stop"]]));
    assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": releasedTake, @"id": @"released", @"seconds": @1}]));
    assert([commands.lastObject isEqual:@"transcribe"] && [dictation.lastText isEqual:@"Recovered dictation"]);

    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:5]];
    [dictation maintainEventTapsForDictation:YES];
    assert(inputGaps == 2 && session->creations == 3 && [dictation.status[@"listener"] boolValue]);
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, NSProcessInfo.processInfo.systemUptime, NO));
    settle();
    assert(dictation.captureID);
    session->enabled = filter->enabled = physical->enabled = false;
    session->blockEnable = true;
    CGEventRef notice = CGEventCreate(NULL);
    assert(notice);
    session->callback((CGEventTapProxy)session->port, kCGEventTapDisabledByTimeout, notice, session->context);
    CFRelease(notice);
    settle();
    assert(inputGaps == 3 && [commands.lastObject isEqual:@"cancel"] && filter->enabled && physical->enabled);
    session->blockEnable = false;
    [dictation maintainEventTapsForDictation:YES];
    assert(inputGaps == 3 && [dictation.status[@"listener"] boolValue]);
    dictation.lastText = nil;
    pressed = NSProcessInfo.processInfo.systemUptime;
    assert(shortcutEvent(dictation, kVK_F9, kCGEventKeyDown, 0, pressed, NO));
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.34]];
    NSString *freshTake = dictation.captureID;
    assert(freshTake && shortcutEvent(dictation, kVK_F9, kCGEventKeyUp, 0, NSProcessInfo.processInfo.systemUptime, NO));
    settle();
    assert([commands.lastObject isEqual:@"stop"]);
    assert(([dictation handleCapture:@{@"event": @"dictationReady", @"dictation": freshTake, @"id": @"fresh", @"seconds": @1}]));
    assert([commands.lastObject isEqual:@"transcribe"] && [dictation.lastText isEqual:@"Recovered dictation"]);
    [dictation shutdown];
    assert(!dictation.listenerActivity);
    for (NSUInteger i = 0; i < 3; i++) {
        assert(!CFMachPortIsValid(fixtureTaps[i].port));
        CFRelease(fixtureTaps[i].port);
        memset(&fixtureTaps[i], 0, sizeof fixtureTaps[i]);
    }
    fixtureEventTaps = false;
    puts("Event taps recovered from disable notifications, silent disables and failed recreation without phantom takes or lost released audio.");
}

static TISInputSourceRef inputSource(CFArrayRef layouts, CFStringRef identifier) {
    for (CFIndex index = 0; index < CFArrayGetCount(layouts); index++) {
        TISInputSourceRef source = (TISInputSourceRef)CFArrayGetValueAtIndex(layouts, index);
        CFStringRef actual = TISGetInputSourceProperty(source, kTISPropertyInputSourceID);
        if (actual && CFEqual(actual, identifier)) return source;
    }
    return NULL;
}

static void testPasteKeyboardLayout(void) {
    CFArrayRef layouts = TISCreateInputSourceList((__bridge CFDictionaryRef)@{
        (__bridge NSString *)kTISPropertyInputSourceType: (__bridge NSString *)kTISTypeKeyboardLayout
    }, true);
    assert(layouts);
    TISInputSourceRef dvorak = inputSource(layouts, CFSTR("com.apple.keylayout.Dvorak"));
    TISInputSourceRef commandQwerty = inputSource(layouts, CFSTR("com.apple.keylayout.DVORAK-QWERTYCMD"));
    TISInputSourceRef hebrew = inputSource(layouts, CFSTR("com.apple.keylayout.Hebrew"));
    TISInputSourceRef turkmen = inputSource(layouts, CFSTR("com.apple.keylayout.Turkmen"));
    TISInputSourceRef us = inputSource(layouts, CFSTR("com.apple.keylayout.US"));
    assert(dvorak && commandQwerty && hebrew && turkmen && us);
    assert(JardPasteKeyCodeForSources(dvorak, us) == kVK_ANSI_Period);
    assert(JardPasteKeyCodeForSources(commandQwerty, dvorak) == kVK_ANSI_V);
    assert(JardPasteKeyCodeForSources(us, dvorak) == kVK_ANSI_V);
    assert(JardPasteKeyCodeForSources(hebrew, dvorak) == kVK_ANSI_V);
    assert(JardPasteKeyCodeForSources(turkmen, commandQwerty) == kVK_ANSI_Period);
    assert(JardPasteKeyCodeForSources(NULL, dvorak) == kVK_ANSI_Period);
    assert(JardPasteKeyCodeForSources(turkmen, NULL) == kVK_ANSI_V);
    CFRelease(layouts);
    puts("Native paste resolved the current Command and ASCII fallback layers from macOS layout tables.");
}

@interface LayoutShortcutFixture : ShortcutEventFixture
@property(nonatomic) NSUInteger pastes;
@end

@implementation LayoutShortcutFixture
- (void)pasteLast { self.pastes++; }
@end

static void testShortcutLayoutChange(void) {
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    CFArrayRef sources = TISCreateInputSourceList((__bridge CFDictionaryRef)@{
        (__bridge NSString *)kTISPropertyInputSourceType: (__bridge NSString *)kTISTypeKeyboardLayout
    }, true);
    assert(sources);
    TISInputSourceRef us = inputSource(sources, CFSTR("com.apple.keylayout.US"));
    TISInputSourceRef dvorak = inputSource(sources, CFSTR("com.apple.keylayout.DVORAK-QWERTYCMD"));
    assert(us && dvorak);
    JardShortcutLayout qwerty = JardShortcutLayoutForSources(us, us);
    JardShortcutLayout moved = JardShortcutLayoutForSources(dvorak, us);
    CFRelease(sources);

    LayoutShortcutFixture *fixture = [[LayoutShortcutFixture alloc] initWithSend:nil];
    fixture.dictationKeyEnabled = YES;
    fixture.settings[@"mode"] = @"hold";
    [fixture applyShortcutMode];
    fixture.shortcutTarget = @"shortcut";
    CGEventFlags option = kCGEventFlagMaskAlternate;
    assert([fixture saveRecordedCode:kVK_ANSI_V flags:option label:@"⌥V"]);
    fixture.shortcutTarget = @"raw";
    assert([fixture saveRecordedCode:kVK_ANSI_V flags:kCGEventFlagMaskCommand label:@"⌘V"]);
    fixture.shortcutTarget = @"cancel";
    assert([fixture saveRecordedCode:kVK_ISO_Section flags:option label:@"⌥^"]);
    [fixture stageShortcutLayout:qwerty];
    NSTimeInterval t = NSProcessInfo.processInfo.systemUptime - 2;
    assert(shortcutEvent(fixture, kVK_ANSI_V, kCGEventKeyDown, option, t, NO));
    assert(fixture.starts == 1 && fixture.shortcut.phase == JardHeld);
    [fixture stageShortcutLayout:moved];
    assert([fixture shortcutCodeForSlot:@"shortcut"] == kVK_ANSI_V);
    assert(shortcutEvent(fixture, kVK_ANSI_V, kCGEventKeyUp, option, t + .4, NO));
    assert(fixture.finishes == 1 && fixture.shortcut.phase == JardFinishing);
    JardShortcut shortcut = fixture.shortcut;
    shortcut.phase = JardIdle;
    fixture.shortcut = shortcut;
    assert(shortcutEvent(fixture, kVK_ANSI_Period, kCGEventKeyDown, option, t + .5, NO));
    assert(fixture.starts == 2 && fixture.shortcut.phase == JardHeld);
    assert(shortcutEvent(fixture, kVK_ANSI_Period, kCGEventKeyUp, option, t + .9, NO));
    assert(fixture.finishes == 2);
    fixture.shortcut = shortcut;
    assert([fixture shortcutCodeForSlot:@"raw"] == kVK_ANSI_V);
    assert([fixture shortcutCodeForSlot:@"cancel"] == kVK_ISO_Section);
    assert([fixture.settings[@"shortcutCode"] integerValue] == kVK_ANSI_V);
    fixture.shortcutTarget = @"cancel";
    assert(![fixture saveRecordedCode:kVK_ANSI_Period flags:option label:@"⌥V"]);
    assert([fixture.settings[@"cancelLabel"] isEqual:@"⌥^"]);

    CGEventFlags paste = option | kCGEventFlagMaskShift;
    fixture.lastText = @"Saved for recovery";
    assert(press(fixture, kVK_ANSI_Period, kCGEventKeyDown, paste, NO));
    [fixture stageShortcutLayout:qwerty];
    assert(press(fixture, kVK_ANSI_Period, kCGEventKeyUp, paste, NO));
    assert(press(fixture, kVK_ANSI_V, kCGEventKeyDown, paste, NO));
    assert(press(fixture, kVK_ANSI_V, kCGEventKeyUp, paste, NO));
    [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
    assert(fixture.pastes == 2);

    __block NSUInteger annotationEdges = 0;
    [fixture setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL chordFree) {
        if (type != kCGEventKeyDown && type != kCGEventKeyUp) return NO;
        if (!chordFree || CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) != shortcut.code) return NO;
        annotationEdges++;
        return YES;
    }];
    [fixture stageShortcutLayout:moved];
    CGKeyCode code = [fixture annotationShortcutCode];
    assert(code != kVK_ANSI_S);
    assert(press(fixture, code, kCGEventKeyDown, paste, NO));
    [fixture stageShortcutLayout:qwerty];
    assert(press(fixture, code, kCGEventKeyUp, paste, NO));
    assert(press(fixture, kVK_ANSI_S, kCGEventKeyDown, paste, NO));
    assert(press(fixture, kVK_ANSI_S, kCGEventKeyUp, paste, NO));
    assert(annotationEdges == 4);

    __block NSUInteger meetings = 0;
    [fixture setMeetingPasteAction:^{ meetings++; }];
    [fixture setMeetingPasteAvailable:YES];
    fixture.dictationKeyEnabled = YES;
    [fixture stageShortcutLayout:moved];
    CGEventFlags meeting = option | kCGEventFlagMaskControl;
    assert(press(fixture, kVK_ANSI_Period, kCGEventKeyDown, meeting, NO));
    assert(press(fixture, kVK_ANSI_Period, kCGEventKeyUp, meeting, NO));
    [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
    assert(meetings == 1);

    fixtureSecureInput = true;
    code = [fixture annotationShortcutCode];
    JardSecureChord secure = {JardSecureAnnotate, code, paste};
    [fixture routeSecureChord:secure edge:JardSecurePressed];
    [fixture stageShortcutLayout:qwerty];
    [fixture routeSecureChord:secure edge:JardSecureReleased];
    assert(annotationEdges == 6);
    fixtureSecureInput = false;
    fixture.shortcutTarget = @"annotate";
    assert([fixture saveRecordedCode:kVK_ANSI_S flags:kCGEventFlagMaskControl label:@"⌃S"]);
    [fixture stageShortcutLayout:moved];
    code = [fixture annotationShortcutCode];
    assert(code != kVK_ANSI_S);
    assert(press(fixture, code, kCGEventKeyDown, kCGEventFlagMaskControl, NO));
    [fixture stageShortcutLayout:qwerty];
    assert(press(fixture, code, kCGEventKeyUp, 0, NO));
    assert(press(fixture, kVK_ANSI_S, kCGEventKeyDown, kCGEventFlagMaskControl, NO));
    assert(press(fixture, kVK_ANSI_S, kCGEventKeyUp, 0, NO) && annotationEdges == 10);
    [fixture shutdown];
    [NSUserDefaults.standardUserDefaults removeObjectForKey:@"JardSystemDictation"];
    puts("Native shortcuts followed letter mappings while held captures, paste, annotation and secure hotkeys balanced their original releases.");
}

static void testAnnotationShortcut(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *key = @"JardSystemDictation";
    [defaults removeObjectForKey:key];
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    CGEventFlags control = kCGEventFlagMaskControl;
    CGEventFlags option = kCGEventFlagMaskAlternate;
    __block NSUInteger edges = 0;
    [fixture setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL free) {
        if ((type != kCGEventKeyDown && type != kCGEventKeyUp) || !event || !free ||
            CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) != shortcut.code) return NO;
        if (type == kCGEventKeyDown && shortcut.pressed != shortcut.required) return NO;
        edges++;
        return YES;
    }];
    assert([fixture.status[@"annotateLabel"] isEqual:@"⌥⇧S"]);
    fixture.shortcutTarget = @"annotate";
    assert(![fixture saveRecordedCode:kVK_ANSI_V flags:control | option label:@"⌃⌥V"]);
    assert(![fixture saveRecordedCode:kVK_ANSI_V flags:option | kCGEventFlagMaskShift label:@"⌥⇧V"]);
    assert(![fixture saveRecordedCode:kVK_ANSI_J flags:0 label:@"J"]);
    assert(![fixture saveRecordedCode:kVK_ANSI_J flags:kCGEventFlagMaskSecondaryFn label:@"Fn J"]);
    fixture.choosingShortcut = YES;
    assert(press(fixture, kVK_Escape, kCGEventKeyDown, 0, NO));
    assert(press(fixture, kVK_Escape, kCGEventKeyUp, 0, NO));
    assert(!fixture.choosingShortcut && [fixture.status[@"annotateLabel"] isEqual:@"⌥⇧S"]);
    fixture.choosingShortcut = YES;
    assert(press(fixture, kVK_ANSI_J, kCGEventKeyDown, control, NO));
    assert(press(fixture, kVK_ANSI_J, kCGEventKeyUp, 0, NO));
    assert(!fixture.choosingShortcut && [fixture.status[@"annotateLabel"] isEqual:@"⌃J"]);
    assert(!press(fixture, kVK_ANSI_J, kCGEventKeyDown, option, NO));
    assert(press(fixture, kVK_ANSI_J, kCGEventKeyDown, control, NO));
    assert(press(fixture, kVK_ANSI_J, kCGEventKeyUp, 0, NO));
    fixtureSecureInput = true;
    [fixture routeSecureChord:(JardSecureChord){JardSecureAnnotate, kVK_ANSI_S, control} edge:JardSecurePressed];
    JardSecureChord chord = {JardSecureAnnotate, kVK_ANSI_J, control};
    [fixture routeSecureChord:chord edge:JardSecurePressed];
    [fixture routeSecureChord:chord edge:JardSecureReleased];
    fixtureSecureInput = false;
    assert(edges == 4);
    fixture.shortcutTarget = @"raw";
    assert(![fixture saveRecordedCode:kVK_ANSI_J flags:control label:@"⌃J"]);
    JardSystemDictation *reopened = [[JardSystemDictation alloc] initWithSend:nil];
    assert([reopened.status[@"annotateLabel"] isEqual:@"⌃J"]);
    [reopened shutdown];
    [fixture request:@{@"action": @"resetAnnotationShortcut"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"annotateLabel"] isEqual:@"⌥⇧S"]);
    }];
    [fixture request:@{@"action": @"setAnnotationEnabled", @"enabled": @NO} reply:^(id result, NSString *error) {
        assert(!error && ![result[@"annotationEnabled"] boolValue]);
    }];
    assert(!press(fixture, kVK_ANSI_S, kCGEventKeyDown, option | kCGEventFlagMaskShift, NO) && edges == 4);
    fixture.shortcutTarget = @"raw";
    assert([fixture saveRecordedCode:kVK_ANSI_S flags:option | kCGEventFlagMaskShift label:@"⌥⇧S"]);
    [fixture request:@{@"action": @"setAnnotationEnabled", @"enabled": @YES} reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"already in use"]);
    }];
    JardSystemDictation *disabled = [[JardSystemDictation alloc] initWithSend:nil];
    assert(![disabled.settings[@"annotationEnabled"] boolValue] && [disabled.status[@"annotateLabel"] isEqual:@"⌥⇧S"]);
    [disabled shutdown];
    fixture.shortcutTarget = @"annotate";
    assert([fixture saveRecordedCode:kVK_F8 flags:0 label:@"F8"]);
    [fixture request:@{@"action": @"setAnnotationEnabled", @"enabled": @YES} reply:^(id result, NSString *error) {
        assert(!error && [result[@"annotationEnabled"] boolValue] && [result[@"annotateLabel"] isEqual:@"F8"]);
    }];
    NSDictionary *cli = @{@"action": @"settings-apply", @"changes": @{@"capture.hotkey": @"f10"}};
    [fixture setAnnotationActive:YES];
    [fixture controlSettings:cli reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"Finish the current recording"]);
    }];
    [fixture setAnnotationActive:NO];
    assert([fixture.status[@"annotateLabel"] isEqual:@"F8"]);
    [fixture controlSettings:cli reply:^(id result, NSString *error) {
        assert(!error && [result[@"capture.hotkey"] isEqual:@"f10"]);
    }];
    assert([fixture.status[@"annotateLabel"] isEqual:@"F10"]);
    assert(!press(fixture, kVK_F8, kCGEventKeyDown, 0, NO));
    assert(press(fixture, kVK_F10, kCGEventKeyDown, 0, NO));
    assert(press(fixture, kVK_F10, kCGEventKeyUp, 0, NO));
    [fixture shutdown];
    [defaults setObject:@{@"annotateCode": @(kVK_F5), @"annotateFlags": @0, @"soundVolume": @36} forKey:key];
    JardSystemDictation *repaired = [[JardSystemDictation alloc] initWithSend:nil];
    assert([repaired.status[@"preferencesWarning"] length] && [repaired.status[@"annotateLabel"] isEqual:@"⌥⇧S"]);
    assert([repaired.status[@"soundVolume"] isEqual:@36]);
    [repaired shutdown];
    [defaults removeObjectForKey:key];
    puts("Annotation shortcut editing, conflicts, secure routing, preferences, reset, and disable/enable passed.");
}

static void testDamagedPreferences(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *key = @"JardSystemDictation";
    NSString *historyKey = @"JardSystemDictationHistory";
    NSString *recoveryPrefix = @"JardSystemDictationRecovery-";
    NSSet *existing = [NSSet setWithArray:defaults.dictionaryRepresentation.allKeys];
    NSDictionary *original = @{@"languages": @[@42], @"retention": @90, @"soundVolume": @40,
        @"mode": @"hold", @"rawCode": @(kVK_ANSI_K), @"rawFlags": @"broken", @"rawLabel": @"⌥K",
        @"futurePreference": @{@"format": @3}, @"onboarding": @"done"};
    NSArray *history = @[@{@"id": @"older", @"text": @"An older retained dictation",
        @"time": @(NSDate.date.timeIntervalSince1970 - 10 * 86400)}];
    [defaults setObject:original forKey:key];
    [defaults setObject:history forKey:historyKey];
    JardSystemDictation *dictation = [[JardSystemDictation alloc] initWithSend:nil];
    NSDictionary *status = dictation.status;
    assert([status[@"preferencesWarning"] length] && [status[@"mode"] isEqual:@"hold"]);
    assert([status[@"retention"] isEqual:@7] && [status[@"languages"] isEqual:@[]]);
    assert([status[@"rawCode"] isEqual:@(-1)] && ![status[@"rawLabel"] length]);
    assert(!status[@"futurePreference"]);
    assert([[defaults objectForKey:key] isEqual:original] && [[defaults objectForKey:historyKey] isEqual:history]);

    NSMutableDictionary *newer = [original mutableCopy];
    newer[@"soundVolume"] = @35;
    [defaults setObject:newer forKey:key];
    [dictation controlSettings:@{@"action": @"settings-apply", @"changes": @{@"dictation.inputDevice": @"fixture"}}
        reply:^(id result, NSString *error) { assert(result && !error); }];
    NSDictionary *repaired = [defaults objectForKey:key];
    assert([repaired[@"languages"] isEqual:@[]] && [repaired[@"retention"] isEqual:@7]);
    assert([repaired[@"inputDevice"] isEqual:@"fixture"] && [repaired[@"soundVolume"] isEqual:@35]);
    assert([repaired[@"futurePreference"] isEqual:original[@"futurePreference"]] && ![dictation.status[@"preferencesWarning"] length]);
    NSUInteger recovered = 0;
    for (NSString *backup in defaults.dictionaryRepresentation) {
        if (![backup hasPrefix:recoveryPrefix] || [existing containsObject:backup]) continue;
        NSDictionary *restored = [NSPropertyListSerialization propertyListWithData:[defaults dataForKey:backup]
            options:NSPropertyListImmutable format:nil error:nil];
        assert([restored isEqual:newer]);
        [defaults removeObjectForKey:backup];
        recovered++;
    }
    assert(recovered == 1);
    assert([[defaults objectForKey:historyKey] isEqual:history]);

    [defaults setObject:@[@"unreadable root"] forKey:key];
    JardSystemDictation *blocked = [[JardSystemDictation alloc] initWithSend:nil];
    assert([blocked.status[@"preferencesWarning"] length] && [blocked.status[@"onboarding"] isEqual:@"later"]);
    [blocked request:@{@"action": @"set", @"key": @"sound", @"value": @NO}
        reply:^(id result, NSString *error) { assert(!result && [error containsString:@"preserved"]); }];
    assert(([[defaults objectForKey:key] isEqual:@[@"unreadable root"]]));
    [defaults setObject:@{@"sound": @YES} forKey:key];
    [blocked request:@{@"action": @"set", @"key": @"sound", @"value": @NO}
        reply:^(id result, NSString *error) { assert(!result && [error containsString:@"preserved"]); }];
    assert([[[defaults objectForKey:key] objectForKey:@"sound"] boolValue]);
    [defaults removeObjectForKey:key];
    [defaults removeObjectForKey:historyKey];
    puts("Malformed preferences used defaults, kept history, merged unrelated edits, preserved the original, and refused an unreadable root.");
}

// Run this case in a disposable bundle so defaults writes cannot target the installed app.
static void writeFixturePreferences(NSArray<NSString *> *arguments) {
    NSString *identifier = NSBundle.mainBundle.bundleIdentifier;
    assert([identifier hasPrefix:@"local.jardaudio.preferences-fixture."]);
    NSTask *writer = NSTask.new;
    writer.executableURL = [NSURL fileURLWithPath:@"/usr/bin/defaults"];
    writer.arguments = [@[arguments.firstObject, identifier, @"JardSystemDictation"]
        arrayByAddingObjectsFromArray:[arguments subarrayWithRange:NSMakeRange(1, arguments.count - 1)]];
    assert([writer launchAndReturnError:nil]);
    [writer waitUntilExit];
    assert(writer.terminationStatus == 0);
}

static void waitForPreferenceRevision(JardSystemDictation *fixture, NSUInteger revision) {
    fixture.nextPreferencesRead = 0;
    [fixture checkSavedPreferences];
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:5];
    while ([fixture.status[@"preferencesRevision"] unsignedIntegerValue] < revision && [deadline timeIntervalSinceNow] > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    assert([fixture.status[@"preferencesRevision"] unsignedIntegerValue] == revision);
}

static void testExternalPreferencesReload(void) {
    if (![NSBundle.mainBundle.bundleIdentifier hasPrefix:@"local.jardaudio.preferences-fixture."]) return;
    writeFixturePreferences(@[@"write", @"-dict", @"mode", @"hold", @"soundVolume", @"-int", @"24", @"onboarding", @"done"]);
    ShortcutEventFixture *fixture = [[ShortcutEventFixture alloc] initWithSend:nil];
    __block NSDictionary *published;
    fixture.preferencesChanged = ^(NSDictionary *status) { published = status; };
    [fixture workspaceReady];
    assert([fixture.status[@"soundVolume"] isEqual:@24]);
    fixture.shortcut = (JardShortcut){.mode = JardHold, .phase = JardHeld};
    fixture.keyDown = YES;
    writeFixturePreferences(@[@"write", @"-dict", @"mode", @"tap", @"soundVolume", @"-int", @"41", @"onboarding", @"done"]);
    fixture.nextPreferencesRead = 0;
    [fixture checkSavedPreferences];
    [fixture acceptSavedPreferences:@{@"mode": @"tap", @"soundVolume": @41} readable:YES revision:0];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.3]];
    assert(!published && [fixture.status[@"soundVolume"] isEqual:@24] && fixture.shortcut.phase == JardHeld);
    fixture.keyDown = NO;
    fixture.shortcut = (JardShortcut){.mode = JardHold};
    waitForPreferenceRevision(fixture, 1);
    assert([published[@"soundVolume"] isEqual:@41] && fixture.shortcut.mode == JardToggle);
    assert(fixture.starts == 0 && fixture.cancels == 0 && fixture.finishes == 0);

    writeFixturePreferences(@[@"write", @"-array", @"invalid root"]);
    waitForPreferenceRevision(fixture, 2);
    assert([published[@"preferencesWarning"] length] && [published[@"soundVolume"] isEqual:@41]);
    writeFixturePreferences(@[@"write", @"-dict", @"mode", @"hold", @"soundVolume", @"-int", @"37", @"onboarding", @"done"]);
    [fixture request:@{@"action": @"set", @"key": @"soundVolume", @"value": @90}
        reply:^(id result, NSString *error) { assert(!result && [error containsString:@"preserved"]); }];
    waitForPreferenceRevision(fixture, 3);
    assert(![published[@"preferencesWarning"] length] && [published[@"soundVolume"] isEqual:@37]);
    assert(fixture.shortcut.mode == JardHold);
    [fixture request:@{@"action": @"set", @"key": @"soundVolume", @"value": @51}
        reply:^(id result, NSString *error) { assert(!error && [result[@"preferencesRevision"] isEqual:@4]); }];
    [fixture acceptSavedPreferences:@{@"soundVolume": @37} readable:YES revision:3];
    assert([fixture.status[@"soundVolume"] isEqual:@51] && [published[@"preferencesRevision"] isEqual:@3]);
    writeFixturePreferences(@[@"delete"]);
    waitForPreferenceRevision(fixture, 5);
    assert([published[@"soundVolume"] isEqual:@70]);
    [fixture shutdown];
    puts("Cross-process preferences reloaded after a held take, fenced stale reads, and recovered from an unreadable value.");
}

@interface HUDPanelFixture : NSPanel
@property(nonatomic) BOOL shown, activeSpace;
@property(nonatomic) NSUInteger orderedIn, orderedOut, clearedBehavior;
@end

@implementation HUDPanelFixture
- (BOOL)isVisible { return self.shown; }
- (BOOL)isOnActiveSpace { return self.activeSpace; }
- (void)orderOut:(id)sender { self.orderedOut++; self.shown = NO; }
- (void)orderFrontRegardless { self.orderedIn++; self.shown = YES; }
- (void)setCollectionBehavior:(NSWindowCollectionBehavior)behavior {
    if (behavior == NSWindowCollectionBehaviorDefault) self.clearedBehavior++;
    [super setCollectionBehavior:behavior];
}
@end

@interface HUDTrackingFixture : JardSystemDictation
@property(nonatomic) NSPoint edgeOrigin;
@end

@implementation HUDTrackingFixture
- (NSPoint)hudEdgeOrigin:(NSString *)placement { return self.edgeOrigin; }
@end

static void testHUDSpaces(void) {
    [NSApplication sharedApplication];
    HUDTrackingFixture *fixture = [[HUDTrackingFixture alloc] initWithSend:nil];
    fixture.settings[@"placement"] = @"top";
    fixture.edgeOrigin = NSMakePoint(100, 800);
    HUDPanelFixture *panel = [[HUDPanelFixture alloc] initWithContentRect:NSMakeRect(0, 0, 142, 50)
        styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        backing:NSBackingStoreBuffered defer:NO];
    JardDictationMeter *meter = [[JardDictationMeter alloc] initWithFrame:panel.contentView.bounds];
    fixture.hud = panel;
    fixture.meter = meter;
    NSUInteger clears = panel.clearedBehavior;
    fixture.phase = @"starting";
    [fixture showHUD:@"Starting…" stage:JardIndicatorRecording];
    assert(panel.orderedIn == 1 && panel.clearedBehavior == clears + 1);
    assert(!meter.isAccessibilityElement);
    fixture.phase = @"listening";
    [fixture showHUD:@"Listening · locked" stage:JardIndicatorRecording];
    assert(panel.orderedIn == 1 && [meter.accessibilityLabel isEqual:JardLocalized(@"Listening…")]);
    assert([meter.accessibilityHelp isEqual:JardLocalized(@"Dictation")]);
    assert(panel.collectionBehavior & NSWindowCollectionBehaviorCanJoinAllApplications);
    assert(panel.collectionBehavior & NSWindowCollectionBehaviorFullScreenAuxiliary);
    NSRect initialFrame = panel.frame;

    const NSTimeInterval waits[] = {0.15, 0.3, 0.6, 1.2, 2.4, 3, 3, 3};
    NSTimeInterval now = 100;
    for (NSUInteger attempt = 0; attempt < sizeof(waits) / sizeof(waits[0]); attempt++) {
        [fixture checkHUDSpaceAt:now];
        assert(panel.orderedIn == 2 + attempt && panel.orderedOut == 1 + attempt);
        assert(panel.clearedBehavior == clears + panel.orderedIn);
        [fixture checkHUDSpaceAt:now + waits[attempt] - 0.01];
        assert(panel.orderedIn == 2 + attempt);
        now += waits[attempt] + 0.01;
    }
    [fixture checkHUDSpaceAt:now];
    [fixture checkHUDSpaceAt:now + 60];
    assert(panel.orderedIn == 9 && NSEqualRects(initialFrame, panel.frame));
    assert([meter accessibilityLabel].length);
    [fixture showHUD:@"Processing" stage:JardIndicatorProcessing];
    assert(panel.orderedIn == 9 && fixture.meter == meter);
    panel.activeSpace = YES;
    [fixture checkHUDSpaceAt:now];
    panel.activeSpace = NO;
    [fixture checkHUDSpaceAt:now + 1];
    assert(panel.orderedIn == 10);

    panel.activeSpace = YES;
    fixture.edgeOrigin = NSMakePoint(-800, 1000);
    [fixture trackHUDAt:now + 2];
    assert(NSEqualPoints(panel.frame.origin, fixture.edgeOrigin));
    fixture.settings[@"placement"] = @"cursor";
    fixture.edgeOrigin = NSMakePoint(100, 800);
    [fixture trackHUDAt:now + 3];
    assert(!NSEqualPoints(panel.frame.origin, fixture.edgeOrigin));
    fixture.settings[@"placement"] = @"top";
    [fixture applicationActivationPolicyChanged];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
    assert(panel.orderedIn == 11 && panel.visible);
    [fixture applicationActivationPolicyChanged];
    [fixture hideHUD];
    clears = panel.clearedBehavior;
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
    assert(!panel.visible && panel.clearedBehavior == clears + 1 && panel.orderedIn == 11);

    [fixture showHUD:@"Listening" stage:JardIndicatorRecording];
    panel.activeSpace = NO;
    [fixture checkHUDSpaceAt:0];
    assert(panel.orderedIn == 13);
    [fixture shutdown];

    NSRect screen = NSMakeRect(-1512, 100, 1512, 982);
    NSRect visible = NSMakeRect(-1512, 168, 1512, 876);
    NSSize panelSize = NSMakeSize(142, 50);
    NSPoint top = JardDictationHUDOrigin(screen, visible, 32, panelSize, NO);
    assert(top.x + 71 == NSMidX(screen) && top.y + 25 + 13 == NSMaxY(visible) - 6);
    top = JardDictationHUDOrigin(screen, screen, 32, panelSize, NO);
    assert(top.y + 25 + 13 == NSMaxY(screen) - 32 - 6);
    top = JardDictationHUDOrigin(screen, screen, 0, panelSize, NO);
    assert(top.y + 25 + 13 == NSMaxY(screen) - 6);
    NSPoint bottom = JardDictationHUDOrigin(screen, visible, 32, panelSize, YES);
    assert(bottom.y + 25 - 13 == NSMinY(visible) + 24);
    puts("Dictation HUD repaired Space changes with a bounded retry, stayed hidden after a Dock policy change, and cleared the menu bar, notch, and Dock.");
}

static NSMutableArray *historyRows(NSUInteger count, NSString *text, NSString *raw) {
    NSMutableArray *rows = [NSMutableArray arrayWithCapacity:count];
    NSTimeInterval now = NSDate.date.timeIntervalSince1970;
    for (NSUInteger i = 0; i < count; i++) {
        [rows addObject:@{@"id": @(i).stringValue, @"time": @(now - i), @"text": text, @"raw": raw, @"seconds": @2}];
    }
    return rows;
}

static void testHistoryAudio(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSFileManager *files = NSFileManager.defaultManager;
    NSString *key = @"JardSystemDictationHistory";
    NSArray *before = [defaults arrayForKey:key];
    const char *existing = getenv("RUMBLE_DATA_DIR");
    NSString *priorRoot = existing ? @(existing) : nil;
    NSString *root = [NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString];
    setenv("RUMBLE_DATA_DIR", root.fileSystemRepresentation, 1);
    [defaults removeObjectForKey:key];
    NSString *staging = [root stringByAppendingPathComponent:@"staging"];
    assert([files createDirectoryAtPath:staging withIntermediateDirectories:YES attributes:nil error:nil]);
    NSString *clipRoot = [root stringByAppendingPathComponent:@"dictation-history-audio"];
    AVAudioFormat *format = [[AVAudioFormat alloc] initWithCommonFormat:AVAudioPCMFormatInt16
        sampleRate:48000 channels:1 interleaved:YES];
    AVAudioPCMBuffer *buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:format frameCapacity:48000];
    buffer.frameLength = 48000;
    memset(buffer.int16ChannelData[0], 0, buffer.frameLength * sizeof(int16_t));
    buffer.int16ChannelData[0][1000] = 17;
    NSString *sample = [root stringByAppendingPathComponent:@"sample.wav"];
    AVAudioFile *writer = [[AVAudioFile alloc] initForWriting:[NSURL fileURLWithPath:sample] settings:format.settings
        commonFormat:format.commonFormat interleaved:YES error:nil];
    assert([writer writeFromBuffer:buffer error:nil]);
    writer = nil;
    NSData *original = [NSData dataWithContentsOfFile:sample];
    assert(original.length > 44);

    __block NSString *text = @"Retained dictation";
    DictationDeliveryFixture *(^open)(void) = ^{
        DictationDeliveryFixture *owner = [[DictationDeliveryFixture alloc] initWithSend:^(NSString *command,
            NSDictionary *payload, JardDictationReply reply) {
            if (![payload[@"action"] isEqual:@"transcribe"]) return;
            [files removeItemAtPath:[staging stringByAppendingPathComponent:payload[@"id"]] error:nil];
            reply(@{@"text": text}, nil);
        }];
        owner.settings[@"sound"] = @NO;
        owner.settings[@"placement"] = @"off";
        owner.settings[@"output"] = @"copy";
        owner.board = NSPasteboard.pasteboardWithUniqueName;
        return owner;
    };
    NSString *(^take)(DictationDeliveryFixture *, BOOL) = ^(DictationDeliveryFixture *owner, BOOL record) {
        NSString *stage = NSUUID.UUID.UUIDString.lowercaseString;
        if (record) assert([files createFileAtPath:[staging stringByAppendingPathComponent:stage]
            contents:original attributes:@{NSFilePosixPermissions: @0600}]);
        finishTake(owner, stage, NO);
        return owner.history.firstObject[@"id"];
    };
    NSString *(^clip)(NSString *) = ^(NSString *identifier) {
        return [clipRoot stringByAppendingPathComponent:[identifier stringByAppendingPathExtension:@"wav"]];
    };
    DictationDeliveryFixture *owner = open();
    assert([owner.settings[@"retention"] isEqual:@7]);
    NSString *first = take(owner, YES);
    assert([[NSData dataWithContentsOfFile:clip(first)] isEqual:original]);
    assert([[files attributesOfItemAtPath:clipRoot error:nil] filePosixPermissions] == 0700);
    assert([[files attributesOfItemAtPath:clip(first) error:nil] filePosixPermissions] == 0600);
    NSTimeInterval expires = [owner.history.firstObject[@"audioExpiresAt"] doubleValue];
    assert(fabs(expires - NSDate.date.timeIntervalSince1970 - 86400) < 2);
    [owner request:@{@"action": @"playHistoryAudio", @"id": first} reply:^(id result, NSString *error) {
        assert(!error && [result[@"playingHistoryID"] isEqual:first] && owner.historyAudioPlayer.playing);
    }];
    [owner request:@{@"action": @"bulkHistory", @"operation": @"delete", @"ids": @[first]} reply:^(id result, NSString *error) {
        assert(!error && !result[@"playingHistoryID"]);
    }];
    assert(!owner.history.count && ![files fileExistsAtPath:clip(first)]);

    NSString *timed = take(owner, YES);
    NSTimeInterval now = NSDate.date.timeIntervalSince1970;
    NSMutableDictionary *row = [owner.history.firstObject mutableCopy];
    row[@"audioExpiresAt"] = @(now + 0.06);
    owner.history[0] = row;
    [owner sweepHistoryAudioAt:now];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.18]];
    assert(![files fileExistsAtPath:clip(timed)] && [owner.history.firstObject[@"text"] isEqual:text]);
    [owner request:@{@"action": @"playHistoryAudio", @"id": timed} reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"no longer available"]);
    }];

    NSString *launchExpired = take(owner, YES);
    row = [owner.history.firstObject mutableCopy];
    row[@"audioExpiresAt"] = @(NSDate.date.timeIntervalSince1970 - 1);
    owner.history[0] = row;
    [defaults setObject:[owner.history copy] forKey:key];
    NSString *orphan = [clipRoot stringByAppendingPathComponent:[NSUUID.UUID.UUIDString stringByAppendingPathExtension:@"pending"]];
    assert([files createFileAtPath:orphan contents:original attributes:@{NSFilePosixPermissions: @0600}]);
    [owner shutdown];
    owner = open();
    assert(![files fileExistsAtPath:orphan] && ![files fileExistsAtPath:clip(launchExpired)] && owner.history.count == 2);

    owner.settings[@"retention"] = @0;
    take(owner, YES);
    assert(owner.history.count == 2 && [[files contentsOfDirectoryAtPath:clipRoot error:nil] count] == 0);
    assert([[owner.board stringForType:NSPasteboardTypeString] isEqual:text]);
    owner.settings[@"retention"] = @7;
    NSString *missing = take(owner, NO);
    assert(![files fileExistsAtPath:clip(missing)] && !owner.history.firstObject[@"audioExpiresAt"]);
    assert([[owner.board stringForType:NSPasteboardTypeString] isEqual:text]);

    NSString *invalid = take(owner, YES);
    assert([files createFileAtPath:clip(invalid) contents:[NSMutableData dataWithLength:90] attributes:@{NSFilePosixPermissions: @0600}]);
    [owner request:@{@"action": @"playHistoryAudio", @"id": invalid} reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"Could not play"]);
    }];
    NSString *remaining = take(owner, YES);
    text = @"";
    take(owner, YES);
    assert([owner.history.firstObject[@"id"] isEqual:remaining]);
    assert([[files contentsOfDirectoryAtPath:clipRoot error:nil] count] == 2);
    [owner request:@{@"action": @"clearHistory"} reply:^(id result, NSString *error) { assert(!error); }];
    assert(!owner.history.count && [[files contentsOfDirectoryAtPath:clipRoot error:nil] count] == 0);
    [owner shutdown];
    [files removeItemAtPath:root error:nil];
    if (priorRoot) setenv("RUMBLE_DATA_DIR", priorRoot.fileSystemRepresentation, 1);
    else unsetenv("RUMBLE_DATA_DIR");
    if (before) [defaults setObject:before forKey:key];
    else [defaults removeObjectForKey:key];
    puts("History played the original WAV, expired clips at launch and while open, and removed audio on delete and clear.");
}

static void testLargeHistory(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *key = @"JardSystemDictationHistory";
    NSArray *before = [defaults arrayForKey:key];
    [defaults setObject:historyRows(10001, @"Retained words", @"Retained words") forKey:key];
    JardSystemDictation *dictation = [[JardSystemDictation alloc] initWithSend:nil];
    assert(dictation.history.count == 10000 && [dictation.history.lastObject[@"id"] isEqual:@"9999"]);
    [dictation control:@"history-snapshot" reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"preferences file"]);
    }];

    __block NSDictionary *page = nil;
    [dictation request:@{@"action": @"history", @"query": @"Retained", @"offset": @9998} reply:^(id result, NSString *error) {
        assert(NSThread.isMainThread && !error);
        page = result;
    }];
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:5];
    while (!page && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert([page[@"total"] isEqual:@10000] && [page[@"words"] isEqual:@20000] && [page[@"seconds"] isEqual:@20000]);
    assert(([[page[@"entries"] valueForKey:@"id"] isEqual:@[@"9998", @"9999"]]));
    [dictation shutdown];

    NSString *body = [@"" stringByPaddingToLength:4095 * 2 withString:@"😀" startingAtIndex:0];
    NSMutableArray *large = historyRows(513, @"", @"");
    for (NSUInteger i = 0; i < large.count; i++) {
        NSMutableDictionary *row = [large[i] mutableCopy];
        row[@"text"] = [NSString stringWithFormat:@"%04lX%@", (unsigned long)i, body];
        row[@"raw"] = [NSString stringWithFormat:@"%04lX%@", (unsigned long)(i + 4096), body];
        large[i] = row;
    }
    [defaults setObject:large forKey:key];
    NSString *prefix = [@"" stringByPaddingToLength:16 * 1024 - 1 withString:@"a" startingAtIndex:0];
    NSString *fullTake = [prefix stringByAppendingString:@"😀keep this ending"];
    DictationDeliveryFixture *capture = [[DictationDeliveryFixture alloc] initWithSend:
        ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            assert([payload[@"action"] isEqual:@"transcribe"]);
            reply(@{@"text": fullTake, @"raw": fullTake}, nil);
        }];
    assert(capture.history.count == 512 && [capture.history.lastObject[@"id"] isEqual:@"511"]);
    capture.settings[@"placement"] = @"off";
    capture.settings[@"retention"] = @7;
    capture.settings[@"output"] = @"copy";
    capture.board = NSPasteboard.pasteboardWithUniqueName;
    finishTake(capture, @"bounded-history", NO);
    assert(capture.history.count == 512 && [capture.history.lastObject[@"id"] isEqual:@"510"]);
    NSDictionary *entry = capture.history.firstObject;
    assert([entry[@"text"] isEqual:prefix] && [entry[@"raw"] isEqual:prefix]);
    assert([capture.lastText isEqual:fullTake] && [[capture.board stringForType:NSPasteboardTypeString] isEqual:fullTake]);

    NSString *latest = entry[@"id"];
    [capture request:@{@"action": @"bulkHistory", @"operation": @"delete", @"ids": @[latest]} reply:^(id result, NSString *error) {
        assert(!error && ![result[@"hasLast"] boolValue]);
    }];
    NSString *path = [NSTemporaryDirectory() stringByAppendingPathComponent:NSUUID.UUID.UUIDString];
    __block BOOL checkpoint = NO;
    [capture writeHistorySnapshot:path reply:^(id result, NSString *error) {
        assert(NSThread.isMainThread && !error && [result boolValue]);
        checkpoint = YES;
    }];
    deadline = [NSDate dateWithTimeIntervalSinceNow:5];
    while (!checkpoint && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(checkpoint && [[defaults arrayForKey:key] count] == 511);
    NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:path error:nil];
    assert([attributes filePosixPermissions] == 0600);
    NSData *disk = [NSData dataWithContentsOfFile:path];
    assert(disk.length > 8 * 1024 * 1024);
    NSDictionary *saved = [NSPropertyListSerialization propertyListWithData:disk options:NSPropertyListImmutable format:nil error:nil];
    assert([saved[key] count] == 511 && ![[saved[key] valueForKey:@"id"] containsObject:latest]);
    assert([NSFileManager.defaultManager removeItemAtPath:path error:nil]);
    [capture control:@"history-clear" reply:^(id result, NSString *error) {
        assert(!error && [result[@"removed"] isEqual:@511]);
    }];
    assert([[defaults arrayForKey:key] count] == 0);
    [capture shutdown];
    if (before) [defaults setObject:before forKey:key];
    else [defaults removeObjectForKey:key];
    puts("History retained 10,000 takes under a combined UTF-8 budget, with bounded legacy replies, live checkpoint, and full current delivery.");
}

int main(void) {
    @autoreleasepool {
        testHUDSpaces();
        testDamagedPreferences();
        testExternalPreferencesReload();
        testCommandDictation();
        testOutputOverview();
        testOutputScriptReview();
        testOutputConfigStarter();
        testSharedVocabulary();
        testVocabularyEditor();
        testReleaseOutputContext();
        testIntendedOutputContext();
        testMicrophoneRecovery();
        testColdReopenDuringHold();
        testAccessibilityRecovery();
        testReturnFromAccessibilitySettings();
        testCaptureEnvironment();
        testLastFailure();
        testWarmInputStatus();
        testWarmMicrophoneWhenAway();
        testWindowOnlyDictationTarget();
        testHistoryTiming();
        testHistoryAudio();
        testLargeHistory();
        testCancelShortcuts();
        testAnnotationShortcut();
        testOptionalBindings();
        testRemappedModifiersAndGlobe();
        testShortcutSide();
        testModifierChords();
        testModifierClaim();
        testShortcutChordEvidence();
        testStaleShortcutInput();
        testShortcutTimingCapture();
        testStartCueWorker();
        testStartCueGestures();
        testSoundSettings();
        testAnnotationCues();
        testCompletionSounds();
        testPressDuringReleaseTail();
        testPasteLastMenu();
        testEventTapRecovery();
        testAnnotationPaste();
        testPasteKeyboardLayout();
        testShortcutLayoutChange();
        testDictationDelivery();
        testClipboardFormats();
        testDictationContinuation();
        testQueuedClipboardRestore();
        testDictationTiming();
        testSecureRouting();
        NSMutableArray<JardDictationReply> *replies = NSMutableArray.array;
        TestDictation *dictation = [[TestDictation alloc] initWithSend:
            ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
                assert([command isEqual:@"jard.dictation"]);
                assert([payload[@"action"] isEqual:@"transcribe"]);
                [replies addObject:[reply copy]];
            }];
        dictation.settings[@"retention"] = @0;
        dictation.settings[@"placement"] = @"off";
        dictation.settings[@"output"] = @"paste";
        dictation.lastText = nil;
        finishTake(dictation, @"one", NO);
        finishTake(dictation, @"two", NO);
        assert(dictation.pendingJobs.count == 2);
        replies[1](@{@"text": @"second take"}, nil);
        assert(dictation.lastText == nil);
        replies[0](@{@"text": @"first take"}, nil);
        assert([dictation.lastText isEqual:@"first take"]);
        [dictation drainOutput];
        assert(dictation.pendingJobs.count == 0);
        assert([dictation.lastText isEqual:@"second take"]);

        finishTake(dictation, @"cancelled", NO);
        [dictation cancelPendingOutput];
        replies[2](@{@"text": @"do not deliver"}, nil);
        assert([dictation.lastText isEqual:@"second take"]);

        finishTake(dictation, @"interrupted", YES);
        replies[3](@{@"text": @"partial recovered"}, nil);
        assert([dictation.lastText isEqual:@"partial recovered"]);
        assert([dictation.phase isEqual:@"saved"]);

        dictation.settings[@"output"] = @"copy";
        finishTake(dictation, @"recovered-output", NO);
        replies[4](@{@"text": @"raw take", @"processed": @"corrected take", @"outputWarning": @"Rewrite timed out."}, nil);
        assert([dictation.lastText isEqual:@"corrected take"] && [dictation.copiedText isEqual:@"corrected take"]);
        assert([dictation.message containsString:@"Copied"] && [dictation.message containsString:@"Rewrite timed out."]);

        dictation.copiedText = nil;
        dictation.settings[@"output"] = @"paste";
        finishTake(dictation, @"focus-changed", NO);
        replies[5](@{@"text": @"next take", @"processed": @"recovered next take", @"outputWarning": @"Rewrite timed out."}, nil);
        assert(!dictation.copiedText && [dictation.lastText isEqual:@"recovered next take"] && [dictation.phase isEqual:@"saved"]);
        assert([dictation.message containsString:@"active app is unavailable"]);

        dictation.settings[@"retention"] = @7;
        finishTake(dictation, @"profile-metadata", NO);
        replies[6](@{@"text": @"a profiled take", @"processed": @"edited", @"profile": @"app:Notes",
            @"processing": @{@"latencyMs": @125, @"model": @"Qwen3.5-2B",
                @"fallbacks": @[@"transformations", @"transformations", @"unexpected"], @"detail": @"Private context"}}, nil);
        assert([dictation.history.firstObject[@"mode"] isEqual:@"app:Notes"]);
        assert([dictation.history.firstObject[@"kind"] isEqual:@"dictation"]);
        NSDictionary *metadata = @{@"latencyMs": @125, @"model": @"Qwen3.5-2B", @"fallbacks": @[@"transformations"]};
        assert([dictation.history.firstObject[@"processing"] isEqual:metadata]);
        JardSystemDictation *reloaded = [[JardSystemDictation alloc] initWithSend:nil];
        assert([reloaded.history.firstObject[@"processing"] isEqual:metadata]);
        dictation.captureRawBinding = YES;
        finishTake(dictation, @"raw-metadata", NO);
        replies[7](@{@"text": @"verbatim text"}, nil);
        assert(!dictation.history.firstObject[@"mode"] && [dictation.history.firstObject[@"kind"] isEqual:@"raw"]);
        assert(!dictation.history.firstObject[@"processing"]);
        dictation.captureRawBinding = NO;
        [dictation request:@{@"action": @"clearHistory"} reply:^(id status, NSString *error) { assert(!error); }];
        __block NSString *captureID;
        __block BOOL stopped = NO;
        TestDictation *capture = [[TestDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if ([command isEqual:@"jard.record"]) {
                captureID = payload[@"dictation"];
                assert([[NSUUID alloc] initWithUUIDString:captureID] && [captureID isEqual:captureID.lowercaseString]);
            } else if ([payload[@"action"] isEqual:@"stop"]) {
                assert([command isEqual:@"jard.dictation"] && [payload[@"action"] isEqual:@"stop"]);
                assert([payload[@"id"] isEqual:captureID]);
                assert(![payload[@"releaseTail"] boolValue]);
                stopped = YES;
            } else {
                assert([command isEqual:@"jard.dictation"] && [payload[@"action"] isEqual:@"transcribe"]);
                reply(@{@"text": @"Recovered after secure input"}, nil);
                return;
            }
            reply(@{}, nil);
        }];
        capture.settings[@"placement"] = @"off";
        capture.settings[@"output"] = @"copy";
        fixtureSecureInput = true;
        [capture begin];
        assert(!captureID && [capture.message containsString:@"secure fields"]);
        fixtureSecureInput = false;
        [capture begin];
        NSDictionary *started = @{@"dictation": captureID, @"event": @"started"};
        assert([capture handleCapture:started]);
        fixtureSecureInput = true;
        [capture updateSecureHotkeys:YES];
        assert(stopped);
        fixtureSecureInput = false;
        [capture updateSecureHotkeys:YES];
        NSDictionary *ready = @{@"event": @"dictationReady", @"id": captureID, @"dictation": captureID};
        assert([capture handleCapture:ready]);
        assert([capture.lastText isEqual:@"Recovered after secure input"] && !capture.copiedText);
        assert([capture.phase isEqual:@"saved"] && [capture.message containsString:@"Secure keyboard input interrupted"]);
        __block NSUInteger pasted = 0;
        [dictation setMeetingPasteAction:^{ pasted++; }];
        CGEventFlags chord = kCGEventFlagMaskAlternate | kCGEventFlagMaskControl;
        assert(!press(dictation, kVK_ANSI_V, kCGEventKeyDown, chord, NO));
        [dictation setMeetingPasteAvailable:YES];
        assert(!press(dictation, kVK_ANSI_V, kCGEventKeyDown, kCGEventFlagMaskCommand, NO));
        assert(press(dictation, kVK_ANSI_V, kCGEventKeyDown, chord, NO));
        assert(press(dictation, kVK_ANSI_V, kCGEventKeyDown, chord, YES));
        assert(press(dictation, kVK_ANSI_V, kCGEventKeyUp, 0, NO));
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        assert(pasted == 1);

        CGEventFlags annotationChord = kCGEventFlagMaskAlternate | kCGEventFlagMaskShift;
        __block NSUInteger reserved = 0, annotationEvents = 0, lostTap = 0;
        [dictation setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL chordFree) {
            if (type == kCGEventTapDisabledByTimeout) { lostTap++; return NO; }
            if (type != kCGEventKeyDown || CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode) != shortcut.code) return NO;
            if (chordFree) annotationEvents++;
            else reserved++;
            return chordFree;
        }];
        dictation.dictationKeyEnabled = YES;
        dictation.lastText = nil;
        dictation.settings[@"pasteCode"] = @(kVK_ANSI_S);
        dictation.settings[@"pasteFlags"] = @(annotationChord);
        assert(!press(dictation, kVK_ANSI_S, kCGEventKeyDown, annotationChord, NO) && reserved == 1);
        [dictation setAnnotationActive:YES];
        assert(!dictation.dictationKeyEnabled);
        assert(press(dictation, kVK_ANSI_S, kCGEventKeyDown, annotationChord, NO) && annotationEvents == 1);
        assert(!press(dictation, kVK_ANSI_V, kCGEventKeyDown, chord, NO));
        assert(![dictation handleKey:NULL type:kCGEventTapDisabledByTimeout] && lostTap == 1);
        puts("Native dictation queue, cancellation, secure-input recovery, meeting paste, and annotation tap routing passed.");
    }
}
