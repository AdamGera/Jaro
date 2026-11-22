#import "../jard_meeting_panel.h"
#import "../jard_meetings.h"
#import "../meeting_presence.h"
#import "../jard_interface_language.h"
#import "../indicator/JardDictationMeter.h"
#import <assert.h>
#import <objc/runtime.h>

@interface JardMeetings (Fixture)
@property(nonatomic, readonly) JardMeetingPanel *panel;
@property(nonatomic, readonly) NSTextField *caption;
@property(nonatomic, readonly) NSTextField *completionBody;
@property(nonatomic, readonly) JardDictationMeter *recordingMeter;
@property(nonatomic, readonly) NSTimeInterval recordingLevelAt;
@property(nonatomic, readonly) NSTimeInterval completionDeadline;
@property(nonatomic, readonly) NSButton *recordingDisclosure;
@property(nonatomic, readonly) NSTextField *recordingResultTitle;
@property(nonatomic, readonly) NSTimeInterval recordingResultAt;
@property(nonatomic, readonly) NSTimer *meterTimer;
@property(nonatomic, readonly) NSMutableDictionary *policies;
@property(nonatomic) NSTimeInterval offeredAt;
@property(nonatomic, readonly) NSTimeInterval offerDepartureAt;
@property(nonatomic) BOOL showIndicator;
@property(nonatomic, copy) NSString *panelCorner;
@property(nonatomic) BOOL recordingHoverHeld;
- (NSDictionary *)status;
- (void)finish:(NSString *)error session:(NSString *)session;
- (void)updateCompletionAt:(NSTimeInterval)now pointer:(NSPoint)pointer;
- (void)updateRecordingMeterAt:(NSTimeInterval)now;
- (void)layoutRecordingPanelAt:(NSTimeInterval)now pointer:(NSPoint)pointer pressed:(BOOL)pressed;
- (void)layoutOfferDepartureAt:(NSTimeInterval)now;
- (void)layoutRecordingResultAt:(NSTimeInterval)now;
- (void)copyCompletedMeeting:(BOOL)onlyNew to:(NSPasteboard *)board;
- (void)copyLatestTranscriptTo:(NSPasteboard *)board reply:(JardDictationReply)reply;
- (void)copyTranscriptForMeeting:(NSString *)identifier to:(NSPasteboard *)board reply:(JardDictationReply)reply;
- (void)copyMeetingAgentPromptTo:(NSPasteboard *)board;
- (void)announceMeetingStatus:(NSString *)message;
- (void)observeCalls:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now date:(NSDate *)date;
@end

static double presentedRMS;
static NSPoint offerFixturePointer;
static void sampleRMS(id self, SEL selector, NSTimeInterval now, double rms) { presentedRMS = rms; }
static BOOL reduceEveryMotion(id self, SEL selector) { return YES; }
static BOOL allowMotion(id self, SEL selector) { return NO; }
static NSPoint offerPointerLocation(id self, SEL selector) { return offerFixturePointer; }

@interface MotionlessPanel : JardMeetingPanel
@end
@implementation MotionlessPanel
- (BOOL)reduceMotion { return YES; }
@end

@interface SpeakingMeeting : JardMeetings
@property(nonatomic, strong) NSMutableArray<NSString *> *announcements;
@end
@implementation SpeakingMeeting
- (void)announceMeetingStatus:(NSString *)message {
    [self.announcements addObject:message];
}
@end

static NSButton *button(JardMeetingPanel *panel, NSString *label) {
    for (NSView *view in panel.contentView.subviews) {
        if ([view isKindOfClass:NSButton.class] && [((NSButton *)view).title isEqual:label]) return (NSButton *)view;
    }
    return nil;
}

static void tickRecordingPanel(JardMeetings *meeting, NSTimeInterval *time, NSPoint pointer, BOOL pressed) {
    for (NSUInteger frame = 0; frame < 24; frame++) {
        *time += 1.0 / 30.0;
        [meeting layoutRecordingPanelAt:*time pointer:pointer pressed:pressed];
    }
}

static void testPlacement(NSScreen *screen) {
    NSRect visible = screen.visibleFrame;
    NSSize size = NSMakeSize(360, 94);
    for (NSString *corner in @[@"topLeft", @"topRight", @"bottomLeft", @"bottomRight"]) {
        NSRect frame = JardMeetingCornerFrame(visible, size, corner);
        assert(NSContainsRect(visible, frame));
        assert([JardNearestMeetingCorner(visible, NSMakePoint(NSMidX(frame), NSMidY(frame))) isEqual:corner]);
    }
    MotionlessPanel *panel = [[MotionlessPanel alloc] initWithContentRect:JardMeetingCornerFrame(visible, size, @"topRight")
        styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
    __block NSString *saved = nil;
    panel.placementChanged = ^(NSString *corner, NSScreen *display) {
        assert(display == screen); saved = corner;
    };
    NSRect original = panel.frame;
    NSPoint start = NSMakePoint(NSMidX(original), NSMidY(original));
    NSRect destination = JardMeetingCornerFrame(visible, size, @"bottomLeft");
    NSPoint end = NSMakePoint(NSMidX(destination), NSMidY(destination));

    [panel beginDragAt:start time:1];
    [panel dragTo:end time:1.2];
    assert(NSEqualRects(panel.frame, destination));
    [panel cancelDrag];
    assert(NSEqualRects(panel.frame, original) && saved == nil);

    [panel beginDragAt:start time:2];
    [panel dragTo:end time:2.2];
    [panel endDragAt:end time:2.21];
    assert([saved isEqual:@"bottomLeft"] && NSEqualRects(panel.frame, destination));
    assert(!panel.canBecomeKeyWindow && !panel.canBecomeMainWindow);
    [panel orderOut:nil];
}

static void testOfferChoices(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *policyKey = @"JardMeetingProviderPolicies";
    id savedPolicies = [defaults objectForKey:policyKey];
    __block NSUInteger starts = 0;
    JardMeetings *meeting = [[JardMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(prompt && [options[@"meetingLabel"] isEqual:@"Google Meet"]);
        starts++;
        reply(@{}, nil);
    } send:nil changed:nil];
    [meeting.policies removeAllObjects];
    NSString *chrome = @"com.google.chrome";
    JardCall *first = JardMeetingBrowserTitleCall(@"Meet - abc-defg-hij", chrome);
    JardCall *second = JardMeetingBrowserTitleCall(@"Meet - xyz-wxyz-xyz", chrome);
    NSDate *date = NSDate.date;
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    [meeting observeCalls:@[first] now:now - 2 date:date];
    [meeting observeCalls:@[first] now:now date:date];
    JardMeetingPanel *panel = meeting.panel;
    NSWindow *activeWindow = NSApp.keyWindow;
    assert(panel.visible && !panel.canBecomeKeyWindow);
    NSString *corner = JardNearestMeetingCorner(panel.screen.visibleFrame, NSMakePoint(NSMidX(panel.frame), NSMidY(panel.frame)));
    NSRect compact = panel.frame;
    NSButton *more = button(panel, @"…");
    assert([more.accessibilityLabel isEqual:JardLocalized(@"Meeting recording options")]);
    assert(button(panel, JardLocalized(@"Record")) && button(panel, JardLocalized(@"Not Now")));
    assert(!button(panel, JardLocalized(@"Always record")));

    Method reduce = class_getInstanceMethod(JardMeetingPanel.class, @selector(reduceMotion));
    IMP original = method_setImplementation(reduce, (IMP)reduceEveryMotion);
    [more performClick:nil];
    assert(meeting.panel == panel && NSApp.keyWindow == activeWindow);
    assert(NSWidth(panel.frame) > NSWidth(compact) && NSHeight(panel.frame) > NSHeight(compact));
    assert(NSEqualRects(panel.frame, JardMeetingCornerFrame(panel.screen.visibleFrame, panel.frame.size, corner)));
    assert([meeting.caption.stringValue isEqual:@"Google Meet"]);
    assert(!button(panel, JardLocalized(@"Record")) && !button(panel, JardLocalized(@"Not Now")));
    NSButton *staleAlways = button(panel, JardLocalized(@"Always record"));
    assert(staleAlways && button(panel, JardLocalized(@"Never offer")));
    [button(panel, JardLocalized(@"Cancel")) performClick:nil];
    assert(meeting.offeredAt == now && NSEqualRects(panel.frame, compact));
    [NSApp sendAction:staleAlways.action to:meeting from:staleAlways];
    assert(!starts && !meeting.policies.count);

    [button(panel, @"…") performClick:nil];
    meeting.offeredAt = now - 21;
    [meeting observeCalls:@[first] now:now + 1 date:date];
    assert(meeting.panel == panel && button(panel, JardLocalized(@"Never offer")));
    [button(panel, JardLocalized(@"Cancel")) performClick:nil];
    NSTimeInterval renewed = meeting.offeredAt;
    assert(renewed >= now && NSEqualRects(panel.frame, compact) && !starts && !meeting.policies.count);
    method_setImplementation(reduce, original);
    [meeting observeCalls:@[first] now:renewed + 19 date:date];
    assert(button(panel, JardLocalized(@"Record")));
    [meeting observeCalls:@[first] now:renewed + 20 date:date];
    assert(!meeting.panel);

    [meeting observeCalls:@[] now:renewed + 21 date:date];
    [meeting observeCalls:@[first] now:renewed + 22 date:date];
    [meeting observeCalls:@[first] now:renewed + 24 date:date];
    [button(meeting.panel, @"…") performClick:nil];
    NSButton *staleNever = button(meeting.panel, JardLocalized(@"Never offer"));
    [meeting observeCalls:@[second] now:renewed + 25 date:date];
    [meeting observeCalls:@[second] now:renewed + 27 date:date];
    assert(button(meeting.panel, JardLocalized(@"Record")));
    [NSApp sendAction:staleNever.action to:meeting from:staleNever];
    assert(!starts && !meeting.policies.count);
    [button(meeting.panel, @"…") performClick:nil];
    [button(meeting.panel, JardLocalized(@"Always record")) performClick:nil];
    assert(starts == 1 && [meeting.policies[second.provider][@"policy"] isEqual:@"auto"]);
    assert([[[defaults dictionaryForKey:policyKey] objectForKey:second.provider][@"policy"] isEqual:@"auto"]);
    [meeting shutdown];

    meeting = [[JardMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(NO);
    } send:nil changed:nil];
    JardCall *unnamed = JardMeetingUnnamedBrowser(chrome, @"Chrome");
    for (NSUInteger i = 0; i <= 20; ++i) [meeting observeCalls:@[unnamed] now:now + i date:date];
    [button(meeting.panel, @"…") performClick:nil];
    assert(button(meeting.panel, JardLocalized(@"Cancel")) && !button(meeting.panel, JardLocalized(@"Always record")));
    [button(meeting.panel, JardLocalized(@"Never offer")) performClick:nil];
    assert(!meeting.panel && [meeting.policies[unnamed.provider][@"policy"] isEqual:@"never"]);
    [meeting shutdown];
    if (savedPolicies) [defaults setObject:savedPolicies forKey:policyKey];
    else [defaults removeObjectForKey:policyKey];
    puts("Meeting offers kept their native corner, held an open choice past the deadline, and rejected stale buttons.");
}

typedef NS_ENUM(NSUInteger, OfferStartFixture) { QuickOfferStart, SlowOfferStart, RefusedOfferStart, HiddenOfferStart };

static void testOfferToRecording(OfferStartFixture scenario) {
    BOOL reduced = scenario == SlowOfferStart;
    Method motion = class_getInstanceMethod(JardMeetingPanel.class, @selector(reduceMotion));
    IMP previousMotion = method_setImplementation(motion, reduced ? (IMP)reduceEveryMotion : (IMP)allowMotion);
    __block NSString *take = nil;
    __block JardDictationReply startReply = nil;
    __block NSUInteger starts = 0;
    JardMeetings *meeting = [[JardMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(prompt && [options[@"meetingLabel"] isEqual:@"Google Meet"]);
        take = options[@"meeting"];
        startReply = [reply copy];
        starts++;
    } send:nil changed:nil];
    [meeting.policies removeAllObjects];
    meeting.showIndicator = scenario != HiddenOfferStart;
    meeting.panelCorner = @[@"topRight", @"bottomLeft", @"topLeft", @"bottomRight"][scenario];
    JardCall *call = JardMeetingBrowserTitleCall(@"Meet - abc-defg-hij", @"com.google.chrome");
    NSDate *date = NSDate.date;
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    [meeting observeCalls:@[call] now:now - 2 date:date];
    [meeting observeCalls:@[call] now:now date:date];
    JardMeetingPanel *panel = meeting.panel;
    assert(panel.visible && !panel.canBecomeKeyWindow);
    NSWindow *activeWindow = NSApp.keyWindow;
    NSRect initial = panel.frame;
    NSRect visible = panel.screen.visibleFrame;
    NSString *corner = JardNearestMeetingCorner(visible, NSMakePoint(NSMidX(initial), NSMidY(initial)));
    offerFixturePointer = NSMakePoint([corner hasSuffix:@"Left"] ? NSMinX(initial) + 20 : NSMaxX(initial) - 20,
        [corner hasPrefix:@"top"] ? NSMaxY(initial) - 18 : NSMinY(initial) + 18);
    Method pointer = class_getClassMethod(NSEvent.class, @selector(mouseLocation));
    IMP previousPointer = method_setImplementation(pointer, (IMP)offerPointerLocation);
    NSButton *record = button(panel, JardLocalized(@"Record"));
    [record performClick:nil];
    assert(starts == 1 && take && startReply && meeting.panel == panel && NSApp.keyWindow == activeWindow);
    assert([[meeting status][@"phase"] isEqual:@"starting"] && !record.enabled && meeting.meterTimer);
    [NSApp sendAction:record.action to:meeting from:record];
    assert(starts == 1);
    NSTimeInterval departed = meeting.offerDepartureAt;
    assert(departed >= now && ![meeting.caption.stringValue isEqual:JardLocalized(@"Starting…")]);

    if (scenario != QuickOfferStart) {
        [meeting layoutOfferDepartureAt:departed + .08];
        assert(NSWidth(panel.frame) < NSWidth(initial) && NSHeight(panel.frame) < NSHeight(initial));
        assert(![meeting.caption.stringValue isEqual:JardLocalized(@"Starting…")]);
        assert(NSEqualRects(panel.frame, JardMeetingCornerFrame(visible, panel.frame.size, corner)));
        if (reduced) assert(NSHeight(panel.frame) == 40);
        [meeting handleCapture:@{@"meeting": @"old-take", @"event": @"started"}];
        assert(!meeting.recording);
        [meeting layoutOfferDepartureAt:departed + .19];
        assert([meeting.caption.stringValue isEqual:JardLocalized(@"Starting…")] && !button(panel, JardLocalized(@"Record")));
    }
    if (scenario == RefusedOfferStart) {
        startReply(@{@"permission": @"mic"}, @"Microphone permission is required.");
        [meeting layoutOfferDepartureAt:departed + 1];
        assert([[[meeting status] objectForKey:@"permission"] isEqual:@"mic"] && !meeting.meterTimer);
        assert(button(meeting.panel, @"Try Again") && [meeting.caption.stringValue containsString:@"Microphone"]);
    } else {
        startReply(@{}, nil);
        [meeting handleCapture:@{@"meeting": take, @"event": @"started"}];
        assert(meeting.recording);
        [meeting layoutOfferDepartureAt:departed + 1];
        if (scenario == HiddenOfferStart) {
            assert(!meeting.panel && !meeting.meterTimer);
        } else {
            assert(meeting.panel == panel && NSApp.keyWindow == activeWindow && [meeting.caption.stringValue isEqual:@"0:00"]);
            NSButton *stop = button(panel, @"Stop & Save");
            assert(stop.hidden && !stop.enabled);
            NSTimeInterval clock = departed + .22;
            tickRecordingPanel(meeting, &clock, offerFixturePointer, NO);
            assert(stop.hidden && !stop.enabled && NSHeight(panel.frame) == 40);
            assert(NSEqualRects(panel.frame, JardMeetingCornerFrame(visible, panel.frame.size, corner)));
            tickRecordingPanel(meeting, &clock, NSMakePoint(-50000, -50000), NO);
            tickRecordingPanel(meeting, &clock, offerFixturePointer, NO);
            assert(!stop.hidden && stop.enabled && starts == 1);
        }
    }
    [meeting shutdown];
    method_setImplementation(pointer, previousPointer);
    method_setImplementation(motion, previousMotion);
}

static void testMeetingWindow(void) {
    NSString *draft = NSUUID.UUID.UUIDString.lowercaseString;
    __block NSString *take = nil;
    __block NSInteger attempts = 0, stops = 0, recoveries = 0;
    __block JardDictationReply readCompletion = nil;
    __block JardDictationReply readMarkdown = nil;
    JardMeetings *meeting = [[JardMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(prompt);
        take = options[@"meeting"];
        attempts++;
        if (attempts < 3) reply(@{@"permission": attempts == 1 ? @"mic" : @"system"}, @"Fixture permission denial.");
        else reply(@{}, nil);
    } send:^(NSString *command, NSDictionary *options, JardDictationReply reply) {
        if ([command isEqual:@"jard.stopMeeting"]) {
            assert([options[@"meeting"] isEqual:take]);
            stops++;
            reply(nil, nil);
        } else if ([command isEqual:@"jard.meetingCompletion"]) {
            assert([options[@"session"] isEqual:draft] && !readCompletion);
            readCompletion = [reply copy];
        } else if ([command isEqual:@"jard.meetingExcerpt"]) {
            assert([options[@"session"] isEqual:draft]);
            reply(@{@"settled": @YES, @"turns": @[@{@"speaker": @1, @"start": @2, @"end": @4, @"text": @"Approve the plan."}]}, nil);
        } else if ([command isEqual:@"jard.meetingMarkdown"]) {
            assert([options[@"session"] isEqual:draft] && !readMarkdown);
            readMarkdown = [reply copy];
        } else {
            assert([command isEqual:@"jard.recoverMeeting"] && [options[@"id"] isEqual:draft]);
            recoveries++;
            reply(@{@"id": draft}, nil);
        }
    } changed:nil];
    __block NSUInteger audioSettingsOpened = 0;
    meeting.showSystemAudioSettings = ^{ audioSettingsOpened++; };
    void (^accept)(id, NSString *) = ^(id result, NSString *error) { assert(!error); };
    [meeting request:@{@"action": @"start"} reply:^(id result, NSString *error) { assert(error); }];
    assert([[meeting status][@"permission"] isEqual:@"mic"] && meeting.panel.visible);
    assert([meeting.caption.stringValue containsString:@"Microphone"]);
    assert([button(meeting.panel, @"Try Again").accessibilityLabel isEqual:@"Try Again"]);
    [button(meeting.panel, @"Try Again") performClick:nil];
    assert([[meeting status][@"permission"] isEqual:@"system"] && [meeting.caption.stringValue containsString:@"Mac audio"]);
    [button(meeting.panel, @"Open System Settings") performClick:nil];
    [meeting request:@{@"action": @"recordingPrivacy"} reply:accept];
    assert(audioSettingsOpened == 2);
    [button(meeting.panel, @"Try Again") performClick:nil];
    [meeting handleCapture:@{@"meeting": take, @"event": @"started", @"microphone": @"Built-in", @"session": draft}];
    assert([[meeting status][@"microphone"] isEqual:@"Built-in"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"sourceChanged", @"microphone": @"Headset"}];
    assert([[meeting status][@"microphone"] isEqual:@"Headset"] && meeting.recording);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingSystemAudio", @"available": @NO}];
    assert([[meeting status][@"systemAudioUnavailable"] boolValue] && meeting.recording);
    assert([meeting.caption.accessibilityLabel containsString:@"Mac audio reconnecting"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingSystemAudio", @"available": @YES}];
    assert(![[meeting status][@"systemAudioUnavailable"] boolValue] && meeting.recording);
    [meeting handleCapture:@{@"meeting": take, @"event": @"levels", @"seconds": @8, @"meetingRms": @0.45}];
    assert([meeting.caption.accessibilityLabel containsString:@"0:08"]);
    assert(meeting.recordingMeter.embedded && [meeting.panel.appearance.name isEqual:NSAppearanceNameDarkAqua]);
    Method advance = class_getInstanceMethod(JardDictationMeter.class, @selector(advanceAt:rms:));
    IMP original = method_setImplementation(advance, (IMP)sampleRMS);
    [meeting updateRecordingMeterAt:meeting.recordingLevelAt + .1];
    assert(presentedRMS == .45);
    [meeting updateRecordingMeterAt:meeting.recordingLevelAt + .31];
    assert(presentedRMS == 0);
    method_setImplementation(advance, original);
    [meeting handleCapture:@{@"meeting": NSUUID.UUID.UUIDString, @"event": @"meetingTitle", @"title": @"Other take"}];
    assert([[meeting status][@"title"] isEqual:@""]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingTitle", @"title": @"Review the launch"}];
    assert([[meeting status][@"title"] isEqual:@"Review the launch"]);
    assert([meeting.caption.accessibilityLabel containsString:@"Review the launch · 0:08"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingMicWarning", @"active": @YES}];
    assert([[meeting status][@"microphoneSilent"] boolValue] && meeting.recording);
    assert([meeting.caption.accessibilityLabel containsString:@"Microphone silent"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"sourceChanged", @"microphone": @"Headset", @"microphoneAvailable": @NO}];
    assert([[meeting status][@"microphoneUnavailable"] boolValue] && ![[meeting status][@"microphoneSilent"] boolValue]);
    assert([[meeting status][@"microphone"] isEqual:@""] && meeting.recording);
    assert([meeting.caption.accessibilityLabel containsString:@"Microphone reconnecting"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingSystemAudio", @"available": @NO}];
    assert([meeting.caption.accessibilityLabel containsString:@"Audio inputs reconnecting"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"sourceChanged", @"microphone": @"Headset", @"microphoneAvailable": @YES}];
    assert(![[meeting status][@"microphoneUnavailable"] boolValue] && [[meeting status][@"microphone"] isEqual:@"Headset"]);
    assert([meeting.caption.accessibilityLabel containsString:@"Mac audio reconnecting"]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingSystemAudio", @"available": @YES}];
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingMicWarning", @"active": @NO}];
    assert(![[meeting status][@"microphoneSilent"] boolValue]);
    assert([meeting.caption.accessibilityLabel containsString:@"Review the launch"]);
    NSString *longTitle = @"Extraordinary international photographic calibration workshop preparations";
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingTitle", @"title": longTitle}];
    assert([meeting.caption.stringValue isEqual:@"0:08"]);
    assert([meeting.caption.accessibilityLabel containsString:longTitle]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingTitle", @"title": @"Review the launch"}];
    NSPoint outside = NSMakePoint(-100000, -100000);
    NSTimeInterval clock = NSProcessInfo.processInfo.systemUptime;
    tickRecordingPanel(meeting, &clock, outside, NO);
    NSRect rest = meeting.panel.frame;
    NSPoint hover = NSMakePoint(NSMidX(rest), NSMidY(rest));
    NSButton *stop = button(meeting.panel, @"Stop & Save");
    assert(stop.hidden && !stop.enabled && NSHeight(rest) == 40);
    assert([meeting.recordingDisclosure.accessibilityLabel isEqual:@"Show meeting controls"]);
    meeting.recordingHoverHeld = YES;
    tickRecordingPanel(meeting, &clock, hover, NO);
    assert(NSEqualRects(meeting.panel.frame, rest) && stop.hidden);
    tickRecordingPanel(meeting, &clock, outside, NO);
    tickRecordingPanel(meeting, &clock, hover, NO);
    NSRect expanded = meeting.panel.frame;
    assert(NSWidth(expanded) > NSWidth(rest) && NSHeight(expanded) > NSHeight(rest));
    assert(NSMaxX(expanded) == NSMaxX(rest) && NSMaxY(expanded) == NSMaxY(rest));
    assert(!stop.hidden && stop.enabled && [stop.accessibilityLabel isEqual:@"Stop & Save"]);
    NSButton *copyNew = button(meeting.panel, @"Copy New");
    assert([copyNew.accessibilityLabel isEqual:@"Copy new meeting transcript"]);
    assert(NSMinY(stop.frame) > NSMinY(copyNew.frame));
    assert(button(meeting.panel, @"Copy Agent Prompt").enabled);
    tickRecordingPanel(meeting, &clock, outside, YES);
    assert(NSEqualRects(meeting.panel.frame, expanded));
    tickRecordingPanel(meeting, &clock, outside, NO);
    assert(NSEqualRects(meeting.panel.frame, rest) && stop.hidden && !stop.enabled);
    [meeting.recordingDisclosure performClick:nil];
    tickRecordingPanel(meeting, &clock, outside, NO);
    assert(NSEqualRects(meeting.panel.frame, expanded));
    [meeting.recordingDisclosure performClick:nil];
    tickRecordingPanel(meeting, &clock, outside, NO);
    assert(NSEqualRects(meeting.panel.frame, rest));
    Method reduceMotion = class_getInstanceMethod(JardMeetingPanel.class, @selector(reduceMotion));
    IMP reduce = method_setImplementation(reduceMotion, (IMP)reduceEveryMotion);
    [meeting layoutRecordingPanelAt:clock pointer:hover pressed:NO];
    assert(NSEqualRects(meeting.panel.frame, expanded) && stop.enabled);
    [meeting layoutRecordingPanelAt:clock pointer:outside pressed:NO];
    assert(NSEqualRects(meeting.panel.frame, rest) && stop.hidden);
    method_setImplementation(reduceMotion, reduce);
    [meeting request:@{@"action": @"indicatorCorner", @"corner": @"bottomLeft"} reply:accept];
    tickRecordingPanel(meeting, &clock, outside, NO);
    rest = meeting.panel.frame;
    hover = NSMakePoint(NSMidX(rest), NSMidY(rest));
    tickRecordingPanel(meeting, &clock, hover, NO);
    expanded = meeting.panel.frame;
    assert(NSMinX(expanded) == NSMinX(rest) && NSMinY(expanded) == NSMinY(rest));
    assert(NSMinY(stop.frame) < NSMinY(copyNew.frame));

    [meeting request:@{@"action": @"indicator", @"enabled": @NO} reply:accept];
    assert(meeting.recording && !meeting.panel && !meeting.recordingMeter);
    [meeting handleCapture:@{@"meeting": take, @"event": @"levels", @"seconds": @9}];
    [meeting request:@{@"action": @"indicatorCorner", @"corner": @"topLeft"} reply:accept];
    [meeting request:@{@"action": @"indicator", @"enabled": @YES} reply:accept];
    assert(meeting.panel.visible && meeting.recordingMeter.embedded && [meeting.caption.accessibilityLabel containsString:@"0:09"]);
    assert(NSMinX(meeting.panel.frame) == NSMinX(meeting.panel.screen.visibleFrame) + 18);
    [meeting request:@{@"action": @"indicator", @"enabled": @NO} reply:accept];
    [meeting request:@{@"action": @"stop"} reply:accept];
    assert(stops == 1);
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingTitle", @"title": @"Late title"}];
    assert([[meeting status][@"title"] isEqual:@"Review the launch"]);

    [meeting handleCapture:@{@"meeting": take, @"event": @"error", @"draft": draft}];
    assert([[meeting status][@"phase"] isEqual:@"interrupted"] && meeting.panel.visible);
    [button(meeting.panel, @"Recover Meeting") performClick:nil];
    assert(recoveries == 1 && [[meeting status][@"session"] isEqual:draft] && !meeting.busy);
    assert(meeting.panel.visible && meeting.keepsRunning && readCompletion && !meeting.recordingMeter);
    assert([meeting.completionBody.stringValue isEqual:@"Transcribing the saved meeting…"]);
    assert(!button(meeting.panel, @"Copy New").enabled && !button(meeting.panel, @"Copy Agent Prompt").enabled);
    NSDictionary *ready = @{@"status": @"complete", @"duration": @125, @"title": @"Approved plan",
                            @"summaryStatus": @"queued", @"hasSpeech": @YES};
    JardDictationReply reply = readCompletion;
    readCompletion = nil;
    reply(ready, nil);
    assert([meeting.caption.stringValue isEqual:@"2:05 · Approved plan"]);
    assert([meeting.completionBody.stringValue containsString:@"local Qwen"]);
    assert([meeting.panel.appearance.name isEqual:NSAppearanceNameDarkAqua]);
    assert(button(meeting.panel, @"Copy New").enabled && button(meeting.panel, @"Copy Agent Prompt").enabled);
    NSPasteboard *board = NSPasteboard.pasteboardWithUniqueName;
    [meeting copyCompletedMeeting:YES to:board];
    assert([[board stringForType:NSPasteboardTypeString] containsString:@"Approve the plan."]);
    [meeting copyCompletedMeeting:NO to:board];
    assert(readMarkdown);
    reply = readMarkdown;
    readMarkdown = nil;
    reply(@{@"text": @"# Approved plan\n\nApprove the plan."}, nil);
    assert([[board stringForType:NSPasteboardTypeString] hasPrefix:@"# Approved plan"]);
    [meeting copyCompletedMeeting:NO to:board];
    reply = readMarkdown;
    readMarkdown = nil;
    [board clearContents];
    [board setString:@"A newer copy" forType:NSPasteboardTypeString];
    reply(@{@"text": @"# A late Markdown result"}, nil);
    assert([[board stringForType:NSPasteboardTypeString] isEqual:@"A newer copy"]);
    [meeting copyCompletedMeeting:NO to:board];
    reply = readMarkdown;
    readMarkdown = nil;
    reply(nil, @"Markdown unavailable");
    assert([[board stringForType:NSPasteboardTypeString] containsString:@"Approve the plan."]);
    [meeting copyMeetingAgentPromptTo:board];
    NSString *command = [NSString stringWithFormat:@"jardctl meetings show %@ --md", draft];
    assert([[board stringForType:NSPasteboardTypeString] containsString:command]);
    [board releaseGlobally];

    NSTimeInterval deadline = meeting.completionDeadline;
    assert(deadline > NSProcessInfo.processInfo.systemUptime + 13);
    NSPoint inside = NSMakePoint(NSMidX(meeting.panel.frame), NSMidY(meeting.panel.frame));
    [meeting updateCompletionAt:deadline + 1 pointer:inside];
    assert(meeting.panel.visible && readCompletion);
    reply = readCompletion;
    readCompletion = nil;
    NSMutableDictionary *lateSummary = ready.mutableCopy;
    lateSummary[@"summary"] = @"The group approved the plan.";
    lateSummary[@"summaryStatus"] = @"ready";
    [button(meeting.panel, @"×") performClick:nil];
    reply(lateSummary, nil);
    assert(!meeting.panel);

    [meeting finish:nil session:draft];
    reply = readCompletion;
    readCompletion = nil;
    reply(lateSummary, nil);
    assert([meeting.completionBody.stringValue isEqual:@"The group approved the plan."]);
    [meeting updateCompletionAt:meeting.completionDeadline + 1 pointer:NSMakePoint(-100000, -100000)];
    assert(!meeting.panel);
    [meeting shutdown];
}

static void testMeetingAnnouncements(void) {
    __block NSString *take;
    NSString *saved = NSUUID.UUID.UUIDString.lowercaseString;
    SpeakingMeeting *meeting = [[SpeakingMeeting alloc] initWithManaged:NO
        start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
            take = options[@"meeting"];
            reply(@{}, nil);
        } send:^(NSString *command, NSDictionary *options, JardDictationReply reply) {
            if ([command isEqual:@"jard.stopMeeting"]) {
                assert([options[@"meeting"] isEqual:take]);
                reply(@{}, nil);
            } else {
                assert([command isEqual:@"jard.meetingCompletion"] && [options[@"session"] isEqual:saved]);
                reply(@{@"status": @"complete", @"hasSpeech": @YES}, nil);
            }
        } changed:nil];
    meeting.announcements = NSMutableArray.array;
    NSWindow *keyWindow = NSApp.keyWindow;
    void (^accept)(id, NSString *) = ^(id result, NSString *error) { assert(!error); };
    [meeting request:@{@"action": @"indicator", @"enabled": @YES} reply:accept];
    [meeting request:@{@"action": @"start"} reply:accept];
    NSDictionary *started = @{@"meeting": take, @"event": @"started", @"microphone": @"Built-in",
                              @"microphoneAvailable": @NO};
    [meeting handleCapture:started];
    [meeting handleCapture:started];
    NSString *ready = [NSString stringWithFormat:@"%@ %@", JardLocalized(@"Meeting recording started."),
        JardLocalized(@"Meeting microphone reconnecting.")];
    assert([meeting.announcements isEqual:@[ready]] && meeting.caption && !meeting.panel.canBecomeKeyWindow);
    if ([NSProcessInfo.processInfo.environment[@"JARD_TEST_FRENCH"] boolValue]) {
        assert([JardInterfaceLanguageStatus()[@"resolved"] isEqual:@"fr"]);
        assert([meeting.announcements.firstObject hasPrefix:@"L’enregistrement de la réunion a commencé."]);
        puts("The native meeting announcement used the bundled French interface language.");
    }

    [meeting handleCapture:@{@"meeting": take, @"event": @"levels", @"seconds": @17}];
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingTitle", @"title": @"Fixture title"}];
    [meeting handleCapture:@{@"meeting": @"old take", @"event": @"meetingSystemAudio", @"available": @NO}];
    assert(meeting.announcements.count == 1 && [meeting.caption.accessibilityLabel containsString:@"0:17"]);
    NSDictionary *microphone = @{@"meeting": take, @"event": @"sourceChanged", @"microphone": @"Built-in",
                                 @"microphoneAvailable": @YES};
    NSDictionary *macAudioLost = @{@"meeting": take, @"event": @"meetingSystemAudio", @"available": @NO};
    NSDictionary *silentMic = @{@"meeting": take, @"event": @"meetingMicWarning", @"active": @YES};
    for (NSDictionary *event in @[microphone, microphone, macAudioLost, macAudioLost, silentMic, silentMic])
        [meeting handleCapture:event];
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingSystemAudio", @"available": @YES}];
    assert([meeting.announcements isEqual:(@[
        ready, JardLocalized(@"Meeting microphone reconnected."), JardLocalized(@"Mac audio reconnecting."),
        JardLocalized(@"Meeting microphone is silent."), JardLocalized(@"Mac audio reconnected.")
    ])]);

    [meeting request:@{@"action": @"indicator", @"enabled": @NO} reply:accept];
    assert(!meeting.panel);
    [meeting request:@{@"action": @"stop"} reply:accept];
    [meeting handleCapture:@{@"meeting": take, @"event": @"finished", @"seconds": @18}];
    [meeting handleCapture:@{@"meeting": take, @"event": @"accepted", @"id": saved}];
    assert([meeting.announcements isEqual:(@[
        ready, JardLocalized(@"Meeting microphone reconnected."), JardLocalized(@"Mac audio reconnecting."),
        JardLocalized(@"Meeting microphone is silent."), JardLocalized(@"Mac audio reconnected."),
        [NSString stringWithFormat:@"%@. %@", JardLocalized(@"Meetings"), JardLocalized(@"Saving…")],
        JardLocalized(@"Meeting saved to Sessions."), JardLocalized(@"Meeting transcript is ready.")
    ])]);
    assert(NSApp.keyWindow == keyWindow);
    [meeting shutdown];
    puts("Meeting status announced start, audio transitions, save, and transcript readiness without repeating meter and old events.");
}

static void testTitledMeeting(void) {
    NSString *title = @"A named meeting";
    NSString *session = NSUUID.UUID.UUIDString.lowercaseString;
    __block NSUInteger attempts = 0, stops = 0;
    __block NSString *take;
    __block JardDictationReply completion;
    JardMeetings *meeting = [[JardMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert([options[@"meetingTitle"] isEqual:title]);
        take = options[@"meeting"];
        assert(prompt == (attempts > 0));
        if (!attempts++) reply(@{@"permission": @"mic"}, @"Fixture permission denial.");
        else reply(@{}, nil);
    } send:^(NSString *command, NSDictionary *options, JardDictationReply reply) {
        if ([command isEqual:@"jard.stopMeeting"]) {
            assert([options[@"meeting"] isEqual:take]);
            stops++;
            reply(@{}, nil);
            return;
        }
        assert([command isEqual:@"jard.meetingCompletion"] && [options[@"session"] isEqual:session]);
        completion = [reply copy];
    } changed:nil];
    [meeting request:@{@"action": @"indicator", @"enabled": @YES} reply:^(id result, NSString *error) { assert(!error); }];
    [meeting startWithEstablishedPermissionsTitle:title reply:^(id result, NSString *error) { assert(error && !result); }];
    assert([[meeting status][@"permission"] isEqual:@"mic"] && [[meeting status][@"title"] isEqual:title]);
    [button(meeting.panel, @"Try Again") performClick:nil];
    assert(attempts == 2);
    [meeting handleCapture:@{@"meeting": take, @"event": @"started", @"session": session}];
    [meeting startWithEstablishedPermissionsTitle:@"Another name" reply:^(id result, NSString *error) { assert(error && !result); }];
    [meeting handleCapture:@{@"meeting": take, @"event": @"meetingTitle", @"title": @"Late generated title"}];
    assert(attempts == 2 && [[meeting status][@"title"] isEqual:title]);
    assert([meeting.caption.accessibilityLabel containsString:title]);
    [meeting handleCapture:@{@"meeting": take, @"event": @"preview", @"deltaSegments":
        @[@{@"speaker": @1, @"start": @1, @"end": @4.9, @"text": @"Review the plan."}]}];
    NSPasteboard *board = NSPasteboard.pasteboardWithUniqueName;
    [meeting copyMeetingAgentPromptTo:board];
    NSString *prompt = [board stringForType:NSPasteboardTypeString];
    assert([prompt containsString:@"still recording"] && [prompt containsString:@"--from 0:04 --md"]);
    NSString *read = [NSString stringWithFormat:@"meetings show %@ --md", session];
    NSString *scope = [NSString stringWithFormat:@"--meeting %@", session];
    assert([prompt containsString:read] && [prompt containsString:scope] && ![prompt containsString:take]);
    [board releaseGlobally];

    NSPoint outside = NSMakePoint(-100000, -100000);
    NSTimeInterval clock = NSProcessInfo.processInfo.systemUptime;
    tickRecordingPanel(meeting, &clock, outside, NO);
    [meeting.recordingDisclosure performClick:nil];
    tickRecordingPanel(meeting, &clock, outside, NO);
    JardMeetingPanel *panel = meeting.panel;
    NSView *surface = panel.contentView;
    [meeting handleCapture:@{@"meeting": take, @"event": @"levels", @"seconds": @6.3, @"meetingRms": @.45}];
    for (NSUInteger i = 0; i < 30; i++)
        [meeting.recordingMeter advanceAt:clock + i / 30.0 rms:.45];
    NSRect timerPosition = [panel convertRectToScreen:meeting.caption.frame];
    NSButton *copy = button(panel, @"Copy All");
    NSRect copyPosition = [panel convertRectToScreen:copy.frame];
    NSButton *stop = button(meeting.panel, @"Stop & Save");
    NSRect row = stop.frame;
    [stop performClick:nil];
    assert(stops == 1 && [stop.title isEqual:@"Stopping…"] && !stop.enabled && NSEqualRects(stop.frame, row));
    assert(meeting.recordingMeter && [[meeting status][@"phase"] isEqual:@"saving"]);
    [meeting request:@{@"action": @"stop"} reply:^(id result, NSString *error) { assert(error); }];
    assert(stops == 1);
    [meeting handleCapture:@{@"meeting": take, @"event": @"finished", @"seconds": @6.5}];
    NSTimeInterval resultAt = meeting.recordingResultAt;
    assert(meeting.panel == panel && panel.contentView == surface && meeting.recordingMeter);
    assert([meeting.caption.stringValue isEqual:@"0:06"] && !stop.enabled && !copy.enabled && !meeting.recordingDisclosure.enabled);
    if (!panel.reduceMotion) {
        [meeting layoutRecordingResultAt:resultAt + .05];
        assert(!stop.hidden && meeting.recordingResultTitle.hidden);
    }
    [meeting layoutRecordingResultAt:resultAt + .28];
    assert(stop.hidden && !meeting.recordingResultTitle.hidden);
    assert(NSEqualRects(timerPosition, [panel convertRectToScreen:meeting.caption.frame]));
    [meeting handleCapture:@{@"meeting": take, @"event": @"levels", @"seconds": @90, @"meetingRms": @.9}];
    assert([[[meeting status] objectForKey:@"seconds"] doubleValue] == 6.5);
    Method advance = class_getInstanceMethod(JardDictationMeter.class, @selector(advanceAt:rms:));
    IMP implementation = method_setImplementation(advance, (IMP)sampleRMS);
    presentedRMS = -1;
    [meeting updateRecordingMeterAt:resultAt + .30];
    assert(presentedRMS == 0);
    method_setImplementation(advance, implementation);

    [meeting handleCapture:@{@"meeting": take, @"event": @"accepted", @"id": session}];
    assert(completion && meeting.panel == panel && panel.contentView == surface);
    completion(@{@"status": @"processing", @"title": title, @"duration": @6.5}, nil);
    assert(!copy.enabled && meeting.recordingDisclosure.hidden);
    [meeting updateCompletionAt:resultAt + .4 pointer:outside];
    completion(@{@"status": @"complete", @"duration": @6.5, @"title": title, @"hasSpeech": @YES, @"summaryStatus": @"queued"}, nil);
    assert([[meeting status][@"title"] isEqual:title] && [meeting.recordingResultTitle.stringValue isEqual:title]);
    assert([meeting.caption.stringValue isEqual:@"0:06"] && copy.enabled && !copy.hidden && meeting.recordingDisclosure.enabled);
    assert(NSEqualRects(copyPosition, [panel convertRectToScreen:copy.frame]));
    [meeting updateCompletionAt:resultAt + .5 pointer:outside];
    NSString *summary = @"The team reviewed the launch timeline and agreed on a short follow-up meeting next week. The owner will share dates tomorrow.";
    completion(@{@"status": @"complete", @"duration": @6.5, @"title": title, @"hasSpeech": @YES,
        @"summaryStatus": @"ready", @"summary": summary}, nil);
    assert([meeting.completionBody.stringValue isEqual:summary] && panel.contentView == surface);
    assert(NSHeight(meeting.completionBody.frame) >= 42);
    assert(NSEqualRects(copyPosition, [panel convertRectToScreen:copy.frame]));
    for (NSUInteger i = 0; i < 90; i++) [meeting updateRecordingMeterAt:clock + 1 + i / 30.0];
    [meeting layoutRecordingResultAt:resultAt + 1];
    assert(!meeting.recordingMeter && !meeting.meterTimer);
    [meeting.recordingDisclosure performClick:nil];
    assert(!meeting.panel);
    [meeting shutdown];
}

static void testCopyLatestMeeting(void) {
    __block JardDictationReply receive = nil;
    __block NSString *requestedMeeting = nil;
    JardMeetings *meeting = [[JardMeetings alloc] initWithManaged:NO start:nil
        send:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
            if (requestedMeeting) {
                assert([command isEqual:@"jard.savedMeetingTranscript"] && [payload[@"id"] isEqual:requestedMeeting]);
            } else {
                assert([command isEqual:@"jard.latestMeetingTranscript"] && payload.count == 0);
            }
            receive = reply;
        } changed:nil];
    NSPasteboard *board = NSPasteboard.pasteboardWithUniqueName;
    NSDictionary *transcript = @{@"text": @"**00:04 · Them**  \nReady for review."};
    [meeting copyLatestTranscriptTo:board reply:^(id result, NSString *error) {
        assert(!error && [result[@"copied"] boolValue]);
    }];
    receive(transcript, nil);
    assert([[board stringForType:NSPasteboardTypeString] isEqual:transcript[@"text"]]);
    [meeting copyLatestTranscriptTo:board reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"clipboard changed"]);
    }];
    [board clearContents];
    [board setString:@"Newer copy" forType:NSPasteboardTypeString];
    receive(transcript, nil);
    assert([[board stringForType:NSPasteboardTypeString] isEqual:@"Newer copy"]);
    [meeting copyLatestTranscriptTo:board reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"No meeting transcript"]);
    }];
    receive(@{}, nil);
    assert([[board stringForType:NSPasteboardTypeString] isEqual:@"Newer copy"]);
    requestedMeeting = NSUUID.UUID.UUIDString;
    [meeting copyTranscriptForMeeting:requestedMeeting to:board reply:^(id result, NSString *error) {
        assert(!result && [error containsString:@"clipboard changed"]);
    }];
    [board clearContents];
    [board setString:@"Newer than selected meeting" forType:NSPasteboardTypeString];
    receive(transcript, nil);
    assert([[board stringForType:NSPasteboardTypeString] isEqual:@"Newer than selected meeting"]);
    [meeting copyTranscriptForMeeting:requestedMeeting to:board reply:^(id result, NSString *error) {
        assert(!error && [result[@"copied"] boolValue]);
    }];
    receive(transcript, nil);
    assert([[board stringForType:NSPasteboardTypeString] isEqual:transcript[@"text"]]);
    receive = nil;
    [board releaseGlobally];
    [meeting shutdown];
}

static void testManagedMeetings(void) {
    NSUserDefaults *preferences = NSUserDefaults.standardUserDefaults;
    [preferences setBool:YES forKey:@"JardDetectMeetings"];
    NSString *session = NSUUID.UUID.UUIDString.lowercaseString;
    __block NSUInteger reads = 0;
    JardMeetings *controller = [[JardMeetings alloc] initWithManaged:YES start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(false && "A managed meeting tried to capture audio");
    } send:^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
        assert([command isEqual:@"jard.latestMeeting"]);
        reads++;
        reply(@{@"session": session}, nil);
    } changed:nil];
    [controller workspaceReady];
    assert(reads == 1 && controller.canPaste && !controller.keepsRunning);
    assert(![controller valueForKey:@"timer"] && ![controller valueForKey:@"calendar"]);
    NSString *message = @"Meetings are managed by your organization.";
    [controller request:@{@"action": @"status"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"managed"] boolValue] && ![result[@"detection"] boolValue]);
        assert([result[@"message"] isEqual:message] && [result[@"hasText"] boolValue]);
    }];
    for (NSString *action in @[@"start", @"calendarConnect", @"detection", @"refreshRecovery", @"recoverMeeting"]) {
        [controller request:@{@"action": action, @"enabled": @NO} reply:^(id result, NSString *error) {
            assert(!result && [error isEqual:message]);
        }];
    }
    [controller startWithEstablishedPermissionsTitle:@"Managed test" reply:^(id result, NSString *error) {
        assert(!result && [error isEqual:message]);
    }];
    assert(reads == 1 && [preferences boolForKey:@"JardDetectMeetings"]);
    [controller shutdown];
    JardMeetings *reopened = [[JardMeetings alloc] initWithManaged:NO start:nil send:nil changed:nil];
    [reopened request:@{@"action": @"status"} reply:^(id result, NSString *error) {
        assert(!error && ![result[@"managed"] boolValue] && [result[@"detection"] boolValue]);
    }];
    [reopened shutdown];
}

int main(void) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        NSScreen *screen = NSScreen.mainScreen;
        assert(screen);
        NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
        NSArray *keys = @[@"JardShowMeetingIndicator", @"JardMeetingIndicatorCorner", @"JardMeetingIndicatorDisplay",
                         @"JardLastMeetingSession", @"JardDetectMeetings"];
        NSMutableDictionary *previous = NSMutableDictionary.dictionary;
        for (NSString *key in keys) {
            if ([defaults objectForKey:key]) previous[key] = [defaults objectForKey:key];
            [defaults removeObjectForKey:key];
        }
        testPlacement(screen);
        testOfferChoices();
        for (OfferStartFixture mode = QuickOfferStart; mode <= HiddenOfferStart; mode++) testOfferToRecording(mode);
        puts("Record requested capture immediately, preserved the offer panel, and delayed Starting until a slow open.");
        testMeetingWindow();
        testMeetingAnnouncements();
        testTitledMeeting();
        testManagedMeetings();
        testCopyLatestMeeting();
        puts("The capsule kept an explicit title through permission retry, duplicate Start, and completion.");
        puts("The recording capsule expanded at both edges, held a press, exposed its controls and live agent prompt, and kept Stop in place through capture teardown.");
        puts("The same surface froze its timer, cooled the waveform, replaced Stop in place, and kept copy actions anchored as the summary appeared.");
        for (NSString *key in keys) [defaults setObject:previous[key] forKey:key];
        puts("Native meeting panel passed four corners, Reduced Motion drag and cancel, permission retry, accessibility labels, hidden stop, and recovery.");
        puts("Managed Meetings skipped permissions, Calendar, detection and recovery. Relaunch restored the saved detection choice.");
    }
}
