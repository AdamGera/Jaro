#import "jard_meetings.h"
#import "meeting_presence.h"
#import "jard_calendar.h"
#import "jard_meeting_panel.h"
#import "jard_overlay_dock.h"
#import "jard_meeting_delta.h"
#import "jard_interface_language.h"
#import "indicator/JardDictationMeter.h"
#import <math.h>

static NSString *const managedMessage = @"Meetings are managed by your organization.";
static NSString *const detectionKey = @"JardDetectMeetings";
static NSString *const policyKey = @"JardMeetingProviderPolicies";
static NSString *const defaultPolicyKey = @"JardMeetingDefaultPolicy";
static NSString *const recentSourcesKey = @"JardMeetingRecentCallSources";
static NSString *const lastSessionKey = @"JardLastMeetingSession";
static NSString *const autoStopKey = @"JardMeetingAutoStop";
static NSString *const noiseCleanupKey = @"JardMeetingNoiseCleanup";
static NSString *const boostMicrophoneKey = @"JardMeetingBoostMicrophone";
static NSString *const indicatorKey = @"JardShowMeetingIndicator";
static NSString *const cornerKey = @"JardMeetingIndicatorCorner";
static NSString *const displayKey = @"JardMeetingIndicatorDisplay";
static const CGFloat recordingHeaderHeight = 40;
static const CGFloat recordingRowHeight = 32;
static const CGFloat recordingInitialWidth = 162;
static const CGFloat recordingOpenWidth = 258;
static const NSTimeInterval callSwitchHold = 4;
static const NSTimeInterval callSwitchObservationGap = 10;
static const NSTimeInterval callStopGrace = 30;
static const NSTimeInterval meetingOfferLifetime = 20;
static const NSTimeInterval offerStartingDelay = .18;
static const NSUInteger recentSourceLimit = 64;

static BOOL validCallPolicy(id policy) {
    return [@[@"ask", @"auto", @"never"] containsObject:policy ?: NSNull.null];
}

static NSString *meetingClock(NSTimeInterval seconds) {
    long elapsed = (long)MAX(0, seconds);
    return [NSString stringWithFormat:@"%ld:%02ld", elapsed / 60, elapsed % 60];
}

static CGFloat recordingRowY(CGFloat height, BOOL top, NSUInteger index) {
    CGFloat offset = index * recordingRowHeight;
    return top ? height - recordingHeaderHeight - recordingRowHeight - offset : recordingHeaderHeight + offset;
}

static CGFloat meetingClockWidth(NSTextField *caption) {
    return MAX(38, ceil([caption.stringValue sizeWithAttributes:@{NSFontAttributeName: caption.font}].width) + 12);
}

@interface JardMeetings ()
@property(nonatomic, copy) JardMeetingStart beginCapture;
@property(nonatomic, copy) JardDictationSend send;
@property(nonatomic, copy) void (^changed)(NSDictionary *);
@property(nonatomic, strong) JardMeetingPresence *presence;
@property(nonatomic, strong) JardCalendar *calendar;
@property(nonatomic, strong) JardCalendarMeeting *calendarOffer;
@property(nonatomic, strong) JardCalendarMeeting *activeCalendar, *permissionCalendar;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSDate *> *reminded, *calendarSnooze;
@property(nonatomic, strong) NSDate *reminderExpires;
@property(nonatomic, strong) NSTimer *timer;
@property(nonatomic) NSTimeInterval timerInterval;
@property(nonatomic, strong) JardCall *offered, *activeCall;
@property(nonatomic) BOOL offerChoicesOpen;
@property(nonatomic) BOOL offerAllowsAutomaticRecording;
@property(nonatomic) NSTimeInterval offerDepartureAt;
@property(nonatomic) NSSize offerDepartureSize;
@property(nonatomic) BOOL offerStartingVisible;
@property(nonatomic, strong) JardCall *permissionCall;
@property(nonatomic, copy) NSArray<NSString *> *activeLanguages;
@property(nonatomic, strong) JardMeetingPanel *panel;
@property(nonatomic, strong) NSTextField *caption;
@property(nonatomic, strong) JardDictationMeter *recordingMeter;
@property(nonatomic, strong) NSTimer *meterTimer;
@property(nonatomic, copy) NSArray<NSButton *> *recordingActions;
@property(nonatomic, weak) NSButton *recordingDisclosure;
@property(nonatomic, weak) NSTextField *recordingNotice;
@property(nonatomic, weak) NSTextField *recordingResultTitle;
@property(nonatomic) CGFloat recordingExpansion;
@property(nonatomic) NSTimeInterval recordingLayoutAt;
@property(nonatomic) NSTimeInterval recordingResultAt;
@property(nonatomic) NSSize recordingResultOrigin;
@property(nonatomic) BOOL recordingHoverHeld, recordingPinned, waitingForCaptureEnd;
@property(nonatomic) BOOL recordingControlsRevealed;
@property(nonatomic) double recordingRMS;
@property(nonatomic) NSTimeInterval recordingLevelAt;
@property(nonatomic, weak) NSTextField *completionBody;
@property(nonatomic, weak) NSButton *incrementalTextButton, *fullTextButton;
@property(nonatomic, weak) NSButton *agentPromptButton;
@property(nonatomic, copy) NSString *phase, *message, *recordingID, *sessionID, *lastSavedSessionID, *providerLabel;
@property(nonatomic, copy) NSString *provisionalTitle, *meetingTitle;
@property(nonatomic) BOOL microphoneSilent;
@property(nonatomic) BOOL microphoneUnavailable;
@property(nonatomic) BOOL systemAudioUnavailable;
@property(nonatomic) BOOL systemAudioPermissionDenied;
@property(nonatomic) BOOL captureUsesSharedMicrophone;
@property(nonatomic, copy) NSString *microphoneName;
@property(nonatomic, copy) NSString *completionSession;
@property(nonatomic, copy) NSDictionary *completion;
@property(nonatomic, copy) NSString *recoveryDraft, *permissionSource, *panelCorner;
@property(nonatomic, strong) NSNumber *panelDisplay;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSDictionary *> *policies;
@property(nonatomic, copy) NSString *defaultPolicy;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSDictionary *> *recentSources;
@property(nonatomic, strong) NSMutableArray<NSDictionary *> *liveTurns;
@property(nonatomic, strong) JardMeetingDelta *delta;
@property(nonatomic, copy) NSString *textMeetingID, *exhaustedMeetingID, *emptyMeetingID;
@property(nonatomic, copy) NSString *excerptFeedback;
@property(nonatomic, strong) JardCall *differentCall;
@property(nonatomic, copy) NSString *keptSwitch, *endedReason;
@property(nonatomic) BOOL copying;
@property(nonatomic) BOOL detection, workspace, suspended, sampling, closed, endPromptAcknowledged;
@property(nonatomic) BOOL autoStop, automaticRecording;
@property(nonatomic) BOOL showIndicator, recovering, scanningRecovery;
@property(nonatomic) BOOL checkingLatest, waitingForRecoveredMeeting;
@property(nonatomic) BOOL checkingCompletion;
@property(nonatomic) NSTimeInterval offeredAt, lastPresent, seconds, automaticUntil, autoStopAt, differentSince, nextLatestCheck;
@property(nonatomic) NSTimeInterval differentLastSeen;
@property(nonatomic) NSTimeInterval completionDeadline;
@property(nonatomic) NSUInteger liveCharacters, excerptGeneration, generation, excerptFeedbackGeneration;
@property(nonatomic) NSUInteger completionGeneration;
@end

@implementation JardMeetings

- (instancetype)initWithManaged:(BOOL)managed start:(JardMeetingStart)start send:(JardDictationSend)send
                      changed:(void (^)(NSDictionary *))changed {
    if (!(self = [super init])) return nil;
    _managed = managed;
    _beginCapture = [start copy];
    _send = [send copy];
    _changed = [changed copy];
    _presence = JardMeetingPresence.new;
    _liveTurns = NSMutableArray.array;
    _delta = JardMeetingDelta.new;
    _phase = @"idle";
    _detection = !managed && [NSUserDefaults.standardUserDefaults boolForKey:detectionKey];
    _autoStop = [NSUserDefaults.standardUserDefaults boolForKey:autoStopKey];
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    _showIndicator = [defaults objectForKey:indicatorKey] ? [defaults boolForKey:indicatorKey] : YES;
    NSString *corner = [defaults stringForKey:cornerKey];
    _panelCorner = JardValidMeetingCorner(corner) ? corner : @"topRight";
    _panelDisplay = [defaults objectForKey:displayKey];
    NSString *savedDefault = [defaults stringForKey:defaultPolicyKey];
    _defaultPolicy = validCallPolicy(savedDefault) ? savedDefault : @"ask";
    _recentSources = NSMutableDictionary.dictionary;
    if (!managed) {
        for (id entry in [defaults arrayForKey:recentSourcesKey]) {
            if (_recentSources.count >= recentSourceLimit) break;
            if (![entry isKindOfClass:NSDictionary.class] || ![entry[@"provider"] isKindOfClass:NSString.class] ||
                ![entry[@"label"] isKindOfClass:NSString.class] || ![entry[@"lastSeen"] isKindOfClass:NSNumber.class]) continue;
            _recentSources[entry[@"provider"]] = entry;
        }
    }
    _policies = NSMutableDictionary.dictionary;
    NSDictionary *savedPolicies = [NSUserDefaults.standardUserDefaults dictionaryForKey:policyKey];
    for (NSString *key in savedPolicies) {
        id entry = savedPolicies[key];
        if (![entry isKindOfClass:NSDictionary.class] || ![entry[@"label"] isKindOfClass:NSString.class] ||
            !validCallPolicy(entry[@"policy"])) continue;
        if ([key hasPrefix:@"browser:"] && [entry[@"policy"] isEqual:@"auto"]) continue;
        _policies[key] = @{@"provider": key, @"label": entry[@"label"], @"policy": entry[@"policy"]};
    }
    _lastSavedSessionID = [NSUserDefaults.standardUserDefaults stringForKey:lastSessionKey];
    _reminded = NSMutableDictionary.dictionary;
    _calendarSnooze = NSMutableDictionary.dictionary;
    __weak typeof(self) weak = self;
    if (!managed) _calendar = [[JardCalendar alloc] initWithChanged:^{ [weak calendarChanged]; }];
    return self;
}

- (BOOL)busy { return self.recordingID != nil || self.recovering; }
- (BOOL)keepsRunning { return self.busy || self.completionSession || self.detection || (self.calendar.connected && self.calendar.reminders); }

- (BOOL)recording { return [@[@"recording", @"callEnded"] containsObject:self.phase]; }
- (BOOL)sharesMicrophone { return self.recording && self.captureUsesSharedMicrophone; }
- (BOOL)hasNewExcerpt {
    if (self.recordingID) return [self.delta unseenTurns:self.liveTurns meeting:self.textMeetingID].count > 0;
    return self.lastSavedSessionID && ![self.exhaustedMeetingID isEqual:self.lastSavedSessionID];
}

- (BOOL)hasExcerpt {
    if (self.recordingID) return self.liveTurns.count > 0;
    return self.lastSavedSessionID && ![self.emptyMeetingID isEqual:self.lastSavedSessionID];
}

- (BOOL)canPaste { return !self.closed && (self.recording || self.hasNewExcerpt); }

- (NSDictionary *)status {
    return @{@"phase": self.phase, @"detection": @(self.detection), @"busy": @(self.busy), @"seconds": @(self.seconds),
        @"provider": self.providerLabel ?: self.offered.label ?: @"", @"managed": @(self.managed),
        @"message": self.managed ? managedMessage : self.message ?: @"",
        @"title": self.meetingTitle ?: self.activeCalendar.title ?: self.provisionalTitle ?: @"",
        @"session": self.sessionID ?: @"", @"hasText": @(self.hasExcerpt),
        @"hasSavedMeeting": @(self.lastSavedSessionID != nil),
        @"hasNewText": @(self.hasNewExcerpt), @"copying": @(self.copying), @"policies": self.policies.allValues,
        @"providerPolicies": [self providerPolicies], @"defaultPolicy": self.defaultPolicy,
        @"microphoneSilent": @(self.recording && self.microphoneSilent && !self.microphoneUnavailable),
        @"microphoneUnavailable": @(self.recording && self.microphoneUnavailable),
        @"systemAudioUnavailable": @(self.recording && self.systemAudioUnavailable),
        @"systemAudioPermissionDenied": @(self.recording && self.systemAudioPermissionDenied),
        @"microphone": self.recording && !self.microphoneUnavailable ? self.microphoneName ?: @"" : @"",
        @"autoStop": @(self.autoStop), @"autoStopSeconds": @(self.autoStopAt > 0 ? ceil(MAX(0, self.autoStopAt - NSProcessInfo.processInfo.systemUptime)) : 0),
        @"noiseCleanup": @([NSUserDefaults.standardUserDefaults boolForKey:noiseCleanupKey]),
        @"boostMicrophone": @([NSUserDefaults.standardUserDefaults boolForKey:boostMicrophoneKey]),
        @"calendar": [self.calendar statusAt:NSDate.date] ?: @{}, @"showIndicator": @(self.showIndicator), @"indicatorCorner": self.panelCorner,
        @"permission": self.permissionSource ?: @"", @"recoveryAvailable": @(self.recoveryDraft != nil)};
}

- (void)announceMeetingStatus:(NSString *)message {
    id target = (id)self.caption ?: NSApp;
    if (message.length && target) NSAccessibilityPostNotificationWithUserInfo(target, NSAccessibilityAnnouncementRequestedNotification,
        @{NSAccessibilityAnnouncementKey: message, NSAccessibilityPriorityKey: @(NSAccessibilityPriorityMedium)});
}

- (void)state:(NSString *)phase message:(NSString *)message {
    NSString *announcement = nil;
    if ([phase isEqual:@"recording"] && [self.phase isEqual:@"starting"]) {
        announcement = JardLocalized(@"Meeting recording started.");
        if (self.microphoneUnavailable)
            announcement = [announcement stringByAppendingFormat:@" %@", JardLocalized(@"Meeting microphone reconnecting.")];
    } else if (![self.phase isEqual:phase] &&
               [@[@"saving", @"permission", @"interrupted", @"callEnded", @"finished", @"error"] containsObject:phase]) {
        announcement = [phase isEqual:@"saving"] && self.waitingForCaptureEnd
            ? [NSString stringWithFormat:@"%@. %@", JardLocalized(@"Meetings"), JardLocalized(@"Saving…")]
            : JardLocalized(message);
    }
    if (![phase isEqual:@"starting"]) {
        if (!([phase isEqual:@"recording"] && [self.phase isEqual:@"starting"])) self.offerDepartureAt = 0;
        self.offerStartingVisible = NO;
    }
    self.phase = phase;
    if (![phase isEqual:@"offered"]) self.offerChoicesOpen = NO;
    if (![phase isEqual:@"saving"]) self.waitingForCaptureEnd = NO;
    self.message = message;
    if (![phase isEqual:@"finished"]) [self clearCompletion];
    [self renderPanel];
    if (announcement.length) [self announceMeetingStatus:announcement];
    if (self.changed) self.changed([self status]);
}

- (BOOL)needsFastPoll {
    return self.busy || self.offered != nil || self.completionSession != nil || self.waitingForRecoveredMeeting;
}

- (void)scheduleTimerIfNeeded {
    if (self.managed || !self.workspace || self.closed) return;
    BOOL fast = [self needsFastPoll];
    NSTimeInterval target = fast ? 1.0 : 1.5;
    if (self.timer && fabs(self.timerInterval - target) < 0.01) return;
    [self.timer invalidate];
    self.timerInterval = target;
    __weak typeof(self) weak = self;
    self.timer = [NSTimer timerWithTimeInterval:target repeats:YES block:^(NSTimer *timer) { [weak poll]; }];
    self.timer.tolerance = fast ? 0.1 : 0.25;
    [NSRunLoop.mainRunLoop addTimer:self.timer forMode:NSRunLoopCommonModes];
}

- (void)workspaceReady {
    if (self.workspace) return;
    self.workspace = YES;
    if (!self.managed) {
        [self scheduleTimerIfNeeded];
        [self.calendar refreshAt:NSDate.date];
        [self scanRecovery:NO];
    }
    [self refreshLatestMeeting];
    if (self.changed) self.changed([self status]);
}

- (void)refreshLatestMeeting {
    if (self.closed || self.busy || self.checkingLatest) return;
    self.checkingLatest = YES;
    NSString *previous = self.lastSavedSessionID ?: @"";
    self.send(@"jard.latestMeeting", @{}, ^(id result, NSString *error) {
        self.checkingLatest = NO;
        if (self.closed || error || ![result isKindOfClass:NSDictionary.class]) return;
        self.waitingForRecoveredMeeting = !self.managed && [result[@"recovering"] boolValue];
        self.nextLatestCheck = NSProcessInfo.processInfo.systemUptime + 3;
        if (self.busy || ![previous isEqual:self.lastSavedSessionID ?: @""]) return;
        NSString *session = result[@"session"];
        self.lastSavedSessionID = [session isKindOfClass:NSString.class] && [[NSUUID alloc] initWithUUIDString:session] ? session : nil;
        if (self.lastSavedSessionID) [NSUserDefaults.standardUserDefaults setObject:self.lastSavedSessionID forKey:lastSessionKey];
        else [NSUserDefaults.standardUserDefaults removeObjectForKey:lastSessionKey];
        if (self.changed) self.changed([self status]);
    });
}

- (void)suspendOffers:(BOOL)suspended {
    if (self.suspended == suspended) return;
    self.suspended = suspended;
    self.generation++;
    [self.presence resetObservations];
    if (suspended) [self.presence clearRememberedSwitch];
    if (suspended && self.calendarOffer) [self closeCalendarOffer];
    if (suspended && self.offered && !self.busy) {
        self.offered = nil;
        [self state:@"idle" message:@""];
    }
}

- (void)poll {
    if (self.managed || !self.workspace || self.closed) return;
    [self scheduleTimerIfNeeded];
    [self updateCompletionAt:NSProcessInfo.processInfo.systemUptime pointer:NSEvent.mouseLocation];
    if (self.waitingForRecoveredMeeting && NSProcessInfo.processInfo.systemUptime >= self.nextLatestCheck)
        [self refreshLatestMeeting];
    NSDate *date = NSDate.date;
    [self.calendar refreshAt:date];
    [self pollCalendarAt:date];
    if (!self.detection || self.suspended || self.sampling) return;
    self.sampling = YES;
    NSUInteger generation = self.generation;
    NSString *outputApplication = self.automaticRecording && self.autoStop ? self.activeCall.application : nil;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSArray *calls = JardActiveCalls(outputApplication);
        dispatch_async(dispatch_get_main_queue(), ^{
            self.sampling = NO;
            if (calls && self.generation == generation && !self.closed && !self.suspended) [self observed:calls];
        });
    });
}

- (void)clearCompletion {
    self.completionGeneration++;
    self.completionSession = nil;
    self.completion = nil;
    self.completionDeadline = 0;
    self.checkingCompletion = NO;
}

- (void)dismissCompletion:(id)sender {
    [self clearCompletion];
    [self renderPanel];
    if (self.changed) self.changed([self status]);
}

- (void)updateCompletionAt:(NSTimeInterval)now pointer:(NSPoint)pointer {
    if (self.closed || !self.completionSession) return;
    if (self.completionDeadline && now >= self.completionDeadline &&
        (!self.panel.visible || !NSPointInRect(pointer, self.panel.frame))) {
        [self dismissCompletion:nil];
        return;
    }
    if (self.checkingCompletion || (self.completionDeadline && ![self.completion[@"summaryStatus"] isEqual:@"queued"] &&
                                   ![self.completion[@"exportStatus"] isEqual:@"pending"])) return;
    self.checkingCompletion = YES;
    NSUInteger generation = self.completionGeneration;
    NSString *session = self.completionSession;
    __weak typeof(self) weak = self;
    self.send(@"jard.meetingCompletion", @{@"session": session}, ^(id result, NSString *error) {
        JardMeetings *owner = weak;
        if (!owner || owner.closed || owner.completionGeneration != generation ||
            ![owner.completionSession isEqual:session]) return;
        owner.checkingCompletion = NO;
        if (error || ![result isKindOfClass:NSDictionary.class]) return;
        NSString *status = result[@"status"];
        if ([status isEqual:@"unavailable"]) {
            [owner dismissCompletion:nil];
            return;
        }
        if (![@[@"processing", @"complete", @"interrupted"] containsObject:status ?: @""]) return;
        owner.completion = result;
        if (!owner.completionDeadline && ![status isEqual:@"processing"]) {
            owner.completionDeadline = NSProcessInfo.processInfo.systemUptime + 15;
            NSString *announcement = [status isEqual:@"interrupted"] ? @"Meeting transcription was interrupted." :
                [result[@"hasSpeech"] boolValue] ? @"Meeting transcript is ready." : @"Meeting saved. No speech was detected.";
            [owner announceMeetingStatus:JardLocalized(announcement)];
        }
        [owner updateCompletionContent];
    });
}

static BOOL verifiedCall(JardCall *target, NSArray<JardCall *> *observations) {
    for (JardCall *call in observations) {
        if (call.inputActive && !call.unnamedBrowser && !call.unverifiedPage && [target matchesObservation:call]) return YES;
    }
    return NO;
}

- (void)observeCalls:(NSArray<JardCall *> *)observations now:(NSTimeInterval)now date:(NSDate *)date {
    if (self.managed) return;
    NSArray<JardCall *> *calls = [self.presence callsResolvingSwitch:observations now:now];
    JardCall *candidate = [self.presence offerFromCalls:calls now:now];
    if (candidate) [self noteCallSource:candidate at:date];
    if (self.busy) {
        if ([self.phase isEqual:@"saving"]) {
            for (JardCall *call in observations) {
                if (call.inputActive && [self.activeCall.application isEqual:call.application]) {
                    self.lastPresent = now;
                    break;
                }
            }
        }
        [self observeActiveCalls:calls now:now];
        return;
    }
    if (self.permissionSource || self.recoveryDraft) return;

    if (self.offered) {
        BOOL present = NO;
        for (JardCall *call in calls) {
            if (!call.inputActive || ![self.offered matchesOfferObservation:call]) continue;
            present = YES;
            self.lastPresent = now;
            if (!self.offered.googleMeetCode && call.googleMeetCode && [self.offered.provider isEqual:call.provider])
                self.offered = call;
            break;
        }
        BOOL verified = verifiedCall(self.offered, observations);
        if (self.offerAllowsAutomaticRecording != verified) {
            self.offerAllowsAutomaticRecording = verified;
            if (self.offerChoicesOpen) [self renderPanel];
        }
        BOOL timedOut = !self.offerChoicesOpen && now - self.offeredAt >= meetingOfferLifetime;
        BOOL replaced = candidate && !present && ![self.offered matchesOfferObservation:candidate];
        if (!replaced && !timedOut && now - self.lastPresent < 3) return;
        if (!timedOut || replaced) [self.presence snooze:self.offered absence:60 now:self.lastPresent];
        else if (present) [self.presence snooze:self.offered absence:0 now:now];
        self.offered = nil;
        [self state:@"idle" message:@""];
        if (!replaced) return;
    }
    if (!candidate) return;
    if ([self snoozedCalendarCall:candidate date:date]) {
        [self.presence snooze:candidate absence:3 now:now];
        return;
    }
    NSString *policy = [self policyForCall:candidate];
    if ([policy isEqual:@"never"]) {
        [self.presence deferOfferForCall:candidate now:now];
        return;
    }
    BOOL verified = verifiedCall(candidate, observations);
    BOOL automatic = verified && [policy isEqual:@"auto"];
    if (self.calendarOffer) {
        if (!automatic || !JardCalendarMatchesCall(self.calendarOffer, candidate)) return;
        [self closeCalendarOffer];
    }
    self.offered = candidate;
    self.offerAllowsAutomaticRecording = verified;
    self.offeredAt = self.lastPresent = now;
    self.providerLabel = nil;
    self.meetingTitle = nil;
    self.provisionalTitle = nil;
    NSString *title = candidate.unnamedBrowser ? [NSString stringWithFormat:@"Call in %@", candidate.label] :
        [NSString stringWithFormat:@"%@ call detected", candidate.label];
    if (automatic) [self begin:NO automatic:YES languages:@[] reply:nil];
    else [self state:@"offered" message:title];
}

- (void)observed:(NSArray<JardCall *> *)calls {
    [self observeCalls:calls now:NSProcessInfo.processInfo.systemUptime date:NSDate.date];
}

- (void)observeActiveCalls:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now {
    if (!self.activeCall || !self.recording) return;
    BOOL automatic = self.automaticRecording && self.autoStop;
    BOOL present = NO, confirmedCurrent = NO;
    JardCall *other = nil;
    for (JardCall *call in calls) {
        if (!call.inputActive && !automatic) continue;
        if ([self.activeCall matchesObservation:call]) {
            present = YES;
            if (!call.unverifiedPage && ([self.activeCall.identity isEqual:call.identity] || call.googleMeetCode)) confirmedCurrent = YES;
            if (!call.unverifiedPage && !self.activeCall.googleMeetCode && call.googleMeetCode) self.activeCall = call;
        } else if ([call.application isEqual:self.activeCall.application]) {
            if (call.unnamedBrowser || call.unverifiedPage) present = YES;
            else if ([self.keptSwitch isEqual:call.identity]) present = confirmedCurrent = YES;
            else if (automatic && self.activeCall.googleMeetCode && call.googleMeetCode) other = call;
        }
    }
    if (confirmedCurrent) other = nil;
    BOOL absent = !present && !other && now - self.lastPresent >= 3;
    if (other) {
        if (![self.differentCall.identity isEqual:other.identity] ||
            (now - self.differentLastSeen > callSwitchObservationGap && ![self.endedReason isEqual:@"switch"])) self.differentSince = now;
        self.differentCall = other;
        self.differentLastSeen = now;
    } else if (confirmedCurrent || absent) self.differentCall = nil;
    if (present || other) {
        self.lastPresent = now;
        self.endPromptAcknowledged = NO;
    }
    BOOL switched = automatic && self.differentCall &&
        (other ? now - self.differentSince >= callSwitchHold : [self.endedReason isEqual:@"switch"]);
    NSString *reason = switched ? @"switch" : absent && !self.endPromptAcknowledged ? @"absence" : nil;
    if (!reason) {
        self.autoStopAt = 0;
        self.endedReason = nil;
        if ([self.phase isEqual:@"callEnded"]) [self state:@"recording" message:@""];
        return;
    }
    if (![self.endedReason isEqual:reason]) {
        self.endedReason = reason;
        self.autoStopAt = automatic ? now + callStopGrace : 0;
        NSString *message = automatic ? (switched ? @"A different call opened. Stopping in 30 seconds." :
            @"The call ended. Stopping in 30 seconds.") : @"The call may have ended.";
        [self state:@"callEnded" message:message];
    } else if (self.autoStopAt && now >= self.autoStopAt) {
        [self stop:nil];
    } else if (self.autoStopAt) {
        NSInteger left = (NSInteger)ceil(self.autoStopAt - now);
        self.message = [NSString stringWithFormat:@"%@ Stopping in %ld second%@.", switched ? @"A different call opened." :
            @"The call ended.", (long)left, left == 1 ? @"" : @"s"];
        [self updateCaption];
        if (self.changed) self.changed([self status]);
    }
}

- (NSString *)policyForCall:(JardCall *)call {
    return [self policyForCall:call defaultPolicy:self.defaultPolicy];
}

- (NSString *)policyForCall:(JardCall *)call defaultPolicy:(NSString *)defaultPolicy {
    NSString *policy = self.policies[call.provider][@"policy"] ?:
        self.policies[[@"browser:" stringByAppendingString:call.application]][@"policy"] ?:
        (call.unnamedBrowser ? nil : [self policyForLabel:call.label]) ?: defaultPolicy;
    return (call.unnamedBrowser || call.unverifiedPage) && [policy isEqual:@"auto"] ? @"ask" : policy;
}

- (NSString *)policyForCalendar:(JardCalendarMeeting *)event {
    JardCall *linked = event.link ? JardMeetingBrowserCall(event.link, @"calendar") : nil;
    NSString *fallback = self.detection && event.provider ? self.defaultPolicy : @"ask";
    return linked ? [self policyForCall:linked defaultPolicy:fallback] : [self policyForLabel:event.provider] ?: fallback;
}

- (NSString *)policyForLabel:(NSString *)label {
    if (!label) return nil;
    for (NSDictionary *provider in JardMeetingProviderCatalog()) {
        if (![provider[@"label"] isEqual:label]) continue;
        NSString *policy = self.policies[provider[@"provider"]][@"policy"];
        if (policy) return policy;
        break;
    }
    for (NSDictionary *entry in self.policies.allValues) {
        if ([entry[@"label"] isEqual:label] && [entry[@"policy"] isEqual:@"never"]) return @"never";
    }
    for (NSDictionary *entry in self.policies.allValues) {
        if ([entry[@"label"] isEqual:label] && [entry[@"policy"] isEqual:@"auto"]) return @"auto";
    }
    return nil;
}

- (NSArray<NSDictionary *> *)orderedCallSources {
    return [self.recentSources.allValues sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *left, NSDictionary *right) {
        NSComparisonResult order = [right[@"lastSeen"] compare:left[@"lastSeen"]];
        return order == NSOrderedSame ? [left[@"provider"] compare:right[@"provider"]] : order;
    }];
}

- (void)noteCallSource:(JardCall *)call at:(NSDate *)date {
    NSNumber *time = @(date.timeIntervalSince1970);
    NSDictionary *previous = self.recentSources[call.provider];
    if ([previous[@"label"] isEqual:call.label] && time.doubleValue - [previous[@"lastSeen"] doubleValue] < 3600) return;
    self.recentSources[call.provider] = @{@"provider": call.provider, @"label": call.label, @"lastSeen": time};
    NSArray<NSDictionary *> *ordered = [self orderedCallSources];
    if (ordered.count > recentSourceLimit) {
        [self.recentSources removeObjectForKey:ordered.lastObject[@"provider"]];
        ordered = [ordered subarrayWithRange:NSMakeRange(0, recentSourceLimit)];
    }
    [NSUserDefaults.standardUserDefaults setObject:ordered forKey:recentSourcesKey];
    if (self.changed) self.changed([self status]);
}

- (NSArray<NSDictionary *> *)providerPolicies {
    NSMutableArray<NSDictionary *> *rows = NSMutableArray.array;
    NSMutableSet<NSString *> *included = NSMutableSet.set;
    NSMutableArray<NSDictionary *> *sources = [[self orderedCallSources] mutableCopy];
    for (NSString *key in [self.policies.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
        [sources addObject:@{@"provider": key, @"label": self.policies[key][@"label"]}];
    }
    [sources addObjectsFromArray:JardMeetingProviderCatalog()];
    for (NSDictionary *entry in sources) {
        NSString *key = entry[@"provider"], *label = entry[@"label"];
        if ([included containsObject:key]) continue;
        [included addObject:key];
        BOOL browser = [key hasPrefix:@"browser:"];
        NSString *explicitPolicy = self.policies[key][@"policy"];
        NSString *policy = explicitPolicy ?: (browser ? nil : [self policyForLabel:label]) ?: self.defaultPolicy;
        if (browser && [policy isEqual:@"auto"]) policy = @"ask";
        NSString *scope = [key hasPrefix:@"site:"] ? [key substringFromIndex:5] : @"";
        // Catalog labels already identify these providers without a redundant host.
        if ([self builtInLabelForProvider:key]) scope = @"";
        [rows addObject:@{@"provider": key, @"label": label, @"scope": scope, @"policy": policy,
            @"canAlwaysRecord": @(!browser), @"inheritsPolicy": @(explicitPolicy == nil), @"lastSeen": entry[@"lastSeen"] ?: @0}];
    }
    return rows;
}

- (BOOL)snoozedCalendarCall:(JardCall *)call date:(NSDate *)date {
    for (NSString *key in self.calendarSnooze.allKeys) {
        if ([self.calendarSnooze[key] compare:date] != NSOrderedDescending) [self.calendarSnooze removeObjectForKey:key];
    }
    if (self.calendarSnooze[call.label]) return YES;
    if (!self.calendarSnooze[@""]) return NO;
    [self.calendarSnooze removeObjectForKey:@""];
    return YES;
}

- (void)snoozeCalendar:(JardCalendarMeeting *)event {
    NSDate *until = event.provider ? event.end : [event.end earlierDate:[event.start dateByAddingTimeInterval:300]];
    self.calendarSnooze[event.provider ?: @""] = until;
}

- (JardCalendarMeeting *)calendarMeetingForCall:(JardCall *)call date:(NSDate *)now {
    if (!call) return nil;
    for (JardCalendarMeeting *event in self.calendar.meetings) {
        if ([event.start timeIntervalSinceDate:now] <= 300 && [event.end compare:now] == NSOrderedDescending &&
            JardCalendarMatchesCall(event, call)) return event;
    }
    return nil;
}

- (JardCalendarMeeting *)calendarMeeting:(NSString *)identifier date:(NSDate *)now {
    for (JardCalendarMeeting *event in self.calendar.meetings) {
        if ([event.identifier isEqual:identifier] && [event.end compare:now] == NSOrderedDescending) return event;
    }
    return nil;
}

- (void)closeCalendarOffer {
    self.calendarOffer = nil;
    self.reminderExpires = nil;
    if ([self.phase isEqual:@"reminder"]) [self state:@"idle" message:@""];
}

- (void)calendarChanged {
    [self pollCalendarAt:NSDate.date];
    if (self.changed) self.changed([self status]);
}

- (NSString *)calendarMessage:(JardCalendarMeeting *)event date:(NSDate *)now {
    NSInteger mins = (NSInteger)ceil([event.start timeIntervalSinceDate:now] / 60);
    NSString *when = mins > 0 ? [NSString stringWithFormat:@"Starts in %ld min", (long)mins] : @"Starting now";
    NSString *service = self.detection && [[self policyForCalendar:event] isEqual:@"auto"] ? @"Jard records automatically" :
        (event.provider ?: @"");
    return service.length ? [NSString stringWithFormat:@"%@\n%@ · %@", event.title, when, service] :
        [NSString stringWithFormat:@"%@\n%@", event.title, when];
}

- (void)pollCalendarAt:(NSDate *)now {
    if (self.closed || self.suspended || self.busy || self.offered || self.permissionSource || self.recoveryDraft || !self.calendar.reminders) {
        if (self.calendarOffer) [self closeCalendarOffer];
        return;
    }
    for (NSString *key in self.reminded.allKeys) {
        if ([self.reminded[key] compare:now] != NSOrderedDescending) [self.reminded removeObjectForKey:key];
    }
    if (self.calendarOffer) {
        JardCalendarMeeting *event = [self calendarMeeting:self.calendarOffer.identifier date:now];
        if ([self.reminderExpires compare:now] == NSOrderedDescending && event) {
            self.calendarOffer = event;
            NSString *message = [self calendarMessage:event date:now];
            if (![self.message isEqual:message]) [self state:@"reminder" message:message];
            return;
        }
        [self closeCalendarOffer];
    }
    for (JardCalendarMeeting *event in self.calendar.meetings) {
        NSTimeInterval remaining = [event.start timeIntervalSinceDate:now];
        if (remaining > self.calendar.lead * 60 || remaining < -300 || [event.end compare:now] != NSOrderedDescending ||
            self.reminded[event.identifier]) continue;
        self.reminded[event.identifier] = event.end;
        self.calendarOffer = event;
        self.reminderExpires = [[event.start dateByAddingTimeInterval:60] laterDate:[now dateByAddingTimeInterval:30]];
        [self state:@"reminder" message:[self calendarMessage:event date:now]];
        return;
    }
}

- (void)setPolicy:(NSString *)policy provider:(NSString *)provider label:(NSString *)label {
    if ([policy isEqual:@"inherit"]) [self.policies removeObjectForKey:provider];
    else self.policies[provider] = @{@"provider": provider, @"label": label, @"policy": policy};
    [NSUserDefaults.standardUserDefaults setObject:self.policies forKey:policyKey];
}

- (void)callPoliciesChanged {
    if (self.offered && [[self policyForCall:self.offered] isEqual:@"never"]) [self dismissOffer];
    else {
        if (self.calendarOffer) [self renderPanel];
        if (self.changed) self.changed([self status]);
    }
}

- (NSString *)builtInLabelForProvider:(NSString *)key {
    for (NSDictionary *provider in JardMeetingProviderCatalog()) {
        if ([provider[@"provider"] isEqual:key]) return provider[@"label"];
    }
    return nil;
}

- (void)begin:(BOOL)prompt automatic:(BOOL)automatic languages:(NSArray<NSString *> *)languages reply:(JardDictationReply)reply {
    [self begin:prompt automatic:automatic languages:languages calendar:[self calendarMeetingForCall:self.offered date:NSDate.date] reply:reply];
}

- (void)begin:(BOOL)prompt automatic:(BOOL)automatic languages:(NSArray<NSString *> *)languages
      calendar:(JardCalendarMeeting *)event reply:(JardDictationReply)reply {
    [self begin:prompt automatic:automatic languages:languages calendar:event title:nil fromOffer:NO reply:reply];
}

- (void)begin:(BOOL)prompt automatic:(BOOL)automatic languages:(NSArray<NSString *> *)languages
      calendar:(JardCalendarMeeting *)event title:(NSString *)title fromOffer:(BOOL)fromOffer reply:(JardDictationReply)reply {
    if (self.managed) { if (reply) reply(nil, managedMessage); return; }
    if (self.closed || self.busy || self.suspended || self.recoveryDraft) {
        if (reply) reply(nil, @"Finish the active capture or update before starting a meeting.");
        return;
    }
    self.activeCall = self.offered;
    self.keptSwitch = [self.presence takePreviousIdentityForCall:self.activeCall];
    self.activeCalendar = event;
    self.activeLanguages = languages;
    self.permissionSource = nil;
    self.permissionCall = nil;
    self.permissionCalendar = nil;
    self.providerLabel = self.activeCall.label ?: event.provider ?: @"Meeting";
    self.meetingTitle = title;
    self.provisionalTitle = nil;
    self.offered = nil;
    if (event) self.reminded[event.identifier] = event.end;
    if (self.calendarOffer) [self closeCalendarOffer];
    [self.liveTurns removeAllObjects];
    self.liveCharacters = 0;
    self.excerptGeneration++;
    self.copying = NO;
    self.excerptFeedback = nil;
    self.seconds = 0;
    self.recordingRMS = self.recordingLevelAt = 0;
    self.microphoneSilent = NO;
    self.microphoneUnavailable = NO;
    self.systemAudioUnavailable = NO;
    self.systemAudioPermissionDenied = NO;
    self.microphoneName = nil;
    self.automaticRecording = automatic;
    self.autoStopAt = self.differentSince = 0;
    self.differentCall = nil;
    self.endedReason = nil;
    self.automaticUntil = automatic ? NSProcessInfo.processInfo.systemUptime + 10 : 0;
    self.sessionID = nil;
    self.endPromptAcknowledged = NO;
    self.recordingID = NSUUID.UUID.UUIDString.lowercaseString;
    self.textMeetingID = self.recordingID;
    NSString *identifier = self.recordingID;
    if (fromOffer && self.panel) {
        self.offerDepartureAt = NSProcessInfo.processInfo.systemUptime;
        self.offerDepartureSize = self.panel.frame.size;
    }
    [self state:@"starting" message:automatic ? @"Starting automatically…" : @"Starting meeting recording…"];
    NSMutableDictionary *options = [@{@"meeting": identifier, @"meetingLabel": self.providerLabel, @"languages": languages,
        @"mode": @"both", @"live": @YES, @"diarization": @YES} mutableCopy];
    if ([NSUserDefaults.standardUserDefaults boolForKey:noiseCleanupKey]) options[@"noiseCleanup"] = @YES;
    if ([NSUserDefaults.standardUserDefaults boolForKey:boostMicrophoneKey]) options[@"boostMicrophone"] = @YES;
    if (event) options[@"meetingCalendar"] = @{@"title": event.title, @"attendees": event.attendees};
    if (self.meetingTitle) options[@"meetingTitle"] = self.meetingTitle;
    if (self.activeCall.captureApplication.length) options[@"meetingApplication"] = self.activeCall.captureApplication;
    self.beginCapture(options, prompt, ^(id result, NSString *error) {
        if (self.closed || ![self.recordingID isEqual:identifier]) {
            if (reply) reply(nil, @"The meeting audio helper disconnected.");
            return;
        }
        if (error) {
            NSString *source = [result isKindOfClass:NSDictionary.class] ? result[@"permission"] : nil;
            if ([@[@"mic", @"system"] containsObject:source ?: @""]) [self blockForPermission:source];
            else if (self.activeCall && !self.suspended) {
                self.recordingID = nil;
                self.offered = self.activeCall;
                self.activeCall = nil;
                self.offeredAt = self.lastPresent = NSProcessInfo.processInfo.systemUptime;
                [self state:@"offered" message:error];
            } else {
                [self finish:error session:nil];
            }
        }
        if (reply) reply(error ? nil : [self status], error);
    });
}

- (void)finish:(NSString *)error session:(NSString *)session {
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    if (self.activeCall && now - self.lastPresent < 3) [self.presence snooze:self.activeCall absence:3 now:self.lastPresent];
    if (error || !session) [self.presence clearRememberedSwitch];
    [self clearCompletion];
    if (self.activeCalendar.title.length) self.provisionalTitle = self.activeCalendar.title;
    self.activeCall = nil;
    self.activeCalendar = nil;
    self.automaticRecording = NO;
    self.autoStopAt = 0;
    NSString *active = self.recordingID ? self.textMeetingID : nil;
    self.recordingID = nil;
    self.sessionID = session;
    if ([session isKindOfClass:NSString.class] && [[NSUUID alloc] initWithUUIDString:session]) {
        [self.delta replaceMeeting:active with:session];
        if (![self.textMeetingID isEqual:session] && !active) {
            [self.liveTurns removeAllObjects];
            self.liveCharacters = 0;
        }
        self.textMeetingID = session;
        self.lastSavedSessionID = session;
        [NSUserDefaults.standardUserDefaults setObject:session forKey:lastSessionKey];
        if (!error) self.completionSession = session;
    }
    [self state:error ? @"error" : @"finished" message:error ?: @"Meeting saved to Sessions."];
    [self updateCompletionAt:NSProcessInfo.processInfo.systemUptime pointer:NSEvent.mouseLocation];
}

- (void)handleCapture:(NSDictionary *)capture {
    if (!self.recordingID || ![capture[@"meeting"] isEqual:self.recordingID]) return;
    NSString *event = capture[@"event"];
    if ([event isEqual:@"started"]) {
        if (![self.phase isEqual:@"starting"]) return;
        self.captureUsesSharedMicrophone = [capture[@"warm"] isEqual:@YES];
        self.microphoneUnavailable = [capture[@"microphoneAvailable"] isEqual:@NO];
        self.microphoneName = [capture[@"microphone"] isKindOfClass:NSString.class] ? capture[@"microphone"] : nil;
        NSString *session = capture[@"session"];
        if ([session isKindOfClass:NSString.class] && [[NSUUID alloc] initWithUUIDString:session]) {
            [self.delta replaceMeeting:self.textMeetingID with:session];
            self.textMeetingID = session;
        }
        self.lastPresent = NSProcessInfo.processInfo.systemUptime;
        [self state:@"recording" message:@""];
    } else if ([event isEqual:@"meetingTitle"] && self.recording && !self.meetingTitle) {
        NSString *title = [capture[@"title"] isKindOfClass:NSString.class] ? capture[@"title"] : nil;
        if (!title.length || title.length > 240) return;
        self.provisionalTitle = title;
        [self updateCaption];
        if (self.changed) self.changed([self status]);
    } else if ([event isEqual:@"meetingMicWarning"] && self.recording) {
        BOOL silent = [capture[@"active"] boolValue];
        if (silent == self.microphoneSilent) return;
        self.microphoneSilent = silent;
        [self updateCaption];
        if (silent && !self.microphoneUnavailable) [self announceMeetingStatus:JardLocalized(@"Meeting microphone is silent.")];
        if (self.changed) self.changed([self status]);
    } else if ([event isEqual:@"meetingSystemAudio"] && self.recording) {
        BOOL unavailable = ![capture[@"available"] boolValue];
        BOOL denied = unavailable && [capture[@"permissionDenied"] isEqual:@YES];
        if (unavailable == self.systemAudioUnavailable && denied == self.systemAudioPermissionDenied) return;
        self.systemAudioUnavailable = unavailable;
        self.systemAudioPermissionDenied = denied;
        [self updateCaption];
        [self announceMeetingStatus:JardLocalized(denied ? @"Mac audio access was denied." :
            unavailable ? @"Mac audio reconnecting." : @"Mac audio reconnected.")];
        if (self.changed) self.changed([self status]);
    } else if ([event isEqual:@"sourceChanged"] && self.recording) {
        NSString *name = [capture[@"microphone"] isKindOfClass:NSString.class] ? capture[@"microphone"] : nil;
        BOOL unavailable = capture[@"microphoneAvailable"] ? [capture[@"microphoneAvailable"] isEqual:@NO] : self.microphoneUnavailable;
        if (!name.length || ([self.microphoneName isEqual:name] && self.microphoneUnavailable == unavailable)) return;
        BOOL availabilityChanged = unavailable != self.microphoneUnavailable;
        self.microphoneName = name;
        self.microphoneUnavailable = unavailable;
        [self updateCaption];
        if (availabilityChanged)
            [self announceMeetingStatus:JardLocalized(unavailable ? @"Meeting microphone reconnecting." : @"Meeting microphone reconnected.")];
        if (self.changed) self.changed([self status]);
    } else if ([event isEqual:@"levels"]) {
        if (!self.recording && !self.waitingForCaptureEnd) return;
        NSInteger before = (NSInteger)self.seconds;
        self.seconds = [capture[@"seconds"] doubleValue];
        NSNumber *level = [capture[@"meetingRms"] isKindOfClass:NSNumber.class] ? capture[@"meetingRms"] : nil;
        self.recordingRMS = isfinite(level.doubleValue) ? MIN(1, MAX(0, level.doubleValue)) : 0;
        self.recordingLevelAt = NSProcessInfo.processInfo.systemUptime;
        [self updateCaption];
        if ((NSInteger)self.seconds != before && self.changed) self.changed([self status]);
    } else if ([event isEqual:@"preview"]) {
        NSArray<NSDictionary *> *turns = JardMeetingTurns(capture[@"deltaSegments"]);
        if (!turns.count && [capture[@"delta"] isKindOfClass:NSString.class])
            turns = JardMeetingTurns(@[@{@"text": capture[@"delta"]}]);
        if (!turns.count) return;
        NSNumber *frame = [capture[@"through"] isKindOfClass:NSNumber.class] ? capture[@"through"] : nil;
        for (NSDictionary *turn in turns) {
            NSMutableDictionary *entry = turn.mutableCopy;
            if (frame) entry[@"through"] = frame;
            [self.liveTurns addObject:entry];
            self.liveCharacters += [turn[@"text"] length];
        }
        NSUInteger remove = 0;
        while (self.liveCharacters > 500000 && remove < self.liveTurns.count)
            self.liveCharacters -= [self.liveTurns[remove++][@"text"] length];
        if (remove) [self.liveTurns removeObjectsInRange:NSMakeRange(0, remove)];
        [self updateCaption];
        [self updateCopyControls];
        if (self.changed) self.changed([self status]);
    } else if ([event isEqual:@"finished"]) {
        if (!self.recording && !self.waitingForCaptureEnd) return;
        NSNumber *duration = [capture[@"seconds"] isKindOfClass:NSNumber.class] ? capture[@"seconds"] : nil;
        if (duration && isfinite(duration.doubleValue) && duration.doubleValue >= 0) self.seconds = duration.doubleValue;
        self.waitingForCaptureEnd = NO;
        [self state:@"saving" message:@"Saving and transcribing the full meeting…"];
    } else if ([event isEqual:@"accepted"]) {
        [self finish:nil session:capture[@"id"]];
        if ([capture[@"interruption"] isKindOfClass:NSString.class]) [self state:@"finished" message:@"Saved the audio captured before the recording was interrupted."];
    } else if ([event isEqual:@"error"]) {
        NSString *source = capture[@"permission"], *draft = capture[@"draft"];
        if ([@[@"mic", @"system"] containsObject:source ?: @""]) [self blockForPermission:source];
        else if ([draft isKindOfClass:NSString.class] && [[NSUUID alloc] initWithUUIDString:draft]) [self showRecovery:draft];
        else [self finish:capture[@"message"] ?: @"The meeting could not be saved." session:nil];
    }
}

- (void)blockForPermission:(NSString *)source {
    [self.presence clearRememberedSwitch];
    self.permissionSource = source;
    self.permissionCall = self.activeCall;
    self.permissionCalendar = self.activeCalendar;
    self.recordingID = nil;
    self.activeCall = nil;
    self.activeCalendar = nil;
    self.automaticRecording = NO;
    self.autoStopAt = 0;
    [self state:@"permission" message:[source isEqual:@"mic"] ?
        @"Allow Microphone access to record this meeting." : @"Allow Mac audio access to record this meeting."];
}

- (void)retryPermission:(JardDictationReply)reply {
    if (!self.permissionSource) { if (reply) reply(nil, @"No recording is waiting for permission."); return; }
    self.offered = self.permissionCall;
    [self begin:YES automatic:NO languages:self.activeLanguages ?: @[] calendar:self.permissionCalendar
        title:self.meetingTitle fromOffer:NO reply:reply];
}

- (void)dismissPermission {
    if (self.permissionCall) [self.presence snooze:self.permissionCall absence:60 now:NSProcessInfo.processInfo.systemUptime];
    self.permissionSource = nil;
    self.permissionCall = nil;
    self.permissionCalendar = nil;
    self.meetingTitle = nil;
    [self state:@"idle" message:@""];
}

- (void)showRecovery:(NSString *)draft {
    [self.presence clearRememberedSwitch];
    self.activeCall = nil;
    self.activeCalendar = nil;
    self.automaticRecording = NO;
    self.autoStopAt = 0;
    self.recordingID = nil;
    self.recoveryDraft = draft;
    self.sessionID = nil;
    [self state:@"interrupted" message:@"Meeting interrupted. Recover the recorded audio."];
}

- (void)scanRecovery:(BOOL)showResult {
    if (self.managed || self.closed || self.busy || self.suspended || self.scanningRecovery) return;
    self.scanningRecovery = YES;
    self.send(@"jard.meetingDrafts", @{}, ^(id result, NSString *error) {
        self.scanningRecovery = NO;
        if (self.closed || self.busy || self.suspended || self.offered || self.calendarOffer || self.permissionSource) return;
        if (error) {
            if (showResult) [self state:self.phase message:@"Could not check for interrupted meetings."];
            return;
        }
        NSArray *drafts = [result isKindOfClass:NSArray.class] ? result : @[];
        NSDictionary *first = [drafts.firstObject isKindOfClass:NSDictionary.class] ? drafts.firstObject : nil;
        NSString *identifier = first[@"id"];
        if ([identifier isKindOfClass:NSString.class] && [[NSUUID alloc] initWithUUIDString:identifier]) {
            self.providerLabel = [first[@"provider"] isKindOfClass:NSString.class] ? first[@"provider"] : @"Meeting";
            self.meetingTitle = [first[@"title"] isKindOfClass:NSString.class] ? first[@"title"] : nil;
            self.provisionalTitle = nil;
            [self showRecovery:identifier];
        } else if (self.recoveryDraft) {
            self.recoveryDraft = nil;
            self.meetingTitle = nil;
            [self state:@"idle" message:@""];
        } else if (showResult) [self state:self.phase message:@"No interrupted meetings found."];
    });
}

- (void)recoverMeeting:(JardDictationReply)reply {
    if (self.managed) { if (reply) reply(nil, managedMessage); return; }
    if (!self.recoveryDraft || self.busy || self.suspended) {
        if (reply) reply(nil, @"The meeting recording is unavailable for recovery.");
        return;
    }
    NSString *draft = self.recoveryDraft;
    self.recovering = YES;
    [self state:@"saving" message:@"Recovering recorded audio…"];
    self.send(@"jard.recoverMeeting", @{@"id": draft}, ^(id result, NSString *error) {
        self.recovering = NO;
        if (self.closed) return;
        if (error || ![result[@"id"] isEqual:draft]) {
            NSString *failure = error ?: @"The meeting could not be recovered.";
            [self state:@"interrupted" message:failure];
            if (reply) reply(nil, failure);
            return;
        }
        self.recoveryDraft = nil;
        [self finish:nil session:draft];
        [self state:@"finished" message:@"Recovered meeting saved to Sessions."];
        if (reply) reply([self status], nil);
    });
}

- (void)dismissRecovery {
    if (self.recovering) return;
    self.recoveryDraft = nil;
    [self state:@"idle" message:@""];
}

- (void)stop:(JardDictationReply)reply {
    if (!self.recording) {
        if (reply) reply(nil, @"No meeting is ready to stop.");
        return;
    }
    NSString *identifier = self.recordingID;
    if ([self.endedReason isEqual:@"switch"] && self.activeCall && self.differentCall) {
        [self.presence rememberSwitchFrom:self.activeCall to:self.differentCall
            confirmedAt:self.autoStopAt - callStopGrace lastSeen:self.lastPresent];
    }
    self.autoStopAt = 0;
    self.waitingForCaptureEnd = YES;
    [self state:@"saving" message:@"Saving the meeting…"];
    self.send(@"jard.stopMeeting", @{@"meeting": identifier}, ^(id result, NSString *error) {
        if (error && [self.recordingID isEqual:identifier]) [self finish:error session:nil];
        if (reply) reply(error ? nil : [self status], error);
    });
}

- (NSArray<NSDictionary *> *)liveTurnsThrough:(NSNumber *)frame {
    if (!frame) return self.liveTurns.copy;
    NSMutableArray *result = NSMutableArray.array;
    for (NSDictionary *turn in self.liveTurns) {
        NSNumber *end = turn[@"end"] == NSNull.null ? turn[@"through"] : @([turn[@"end"] doubleValue] * 48000);
        if (end && end.doubleValue <= frame.doubleValue + 0.01) [result addObject:turn];
    }
    return result;
}

- (void)prepareExcerpt:(BOOL)onlyNew savedSession:(NSString *)savedSession ready:(void (^)(NSString *, NSString *, void (^)(BOOL)))ready {
    if (self.copying) { ready(nil, @"Jard is already preparing a meeting excerpt.", ^(BOOL success) {}); return; }
    NSString *active = savedSession ? nil : self.recordingID;
    NSString *identifier = active ? self.textMeetingID : savedSession ?: self.lastSavedSessionID;
    if (!identifier || self.closed) { ready(nil, @"No meeting transcript is available.", ^(BOOL success) {}); return; }
    self.copying = YES;
    NSUInteger generation = ++self.excerptGeneration;
    [self updateCopyControls];
    if (self.changed) self.changed([self status]);
    JardDictationReply received = ^(id result, NSString *error) {
        if (self.closed || self.excerptGeneration != generation) {
            ready(nil, @"The meeting changed before the excerpt was ready.", ^(BOOL success) {});
            return;
        }
        NSDictionary *snapshot = [result isKindOfClass:NSDictionary.class] ? result : @{};
        NSNumber *through = [snapshot[@"through"] isKindOfClass:NSNumber.class] ? snapshot[@"through"] : nil;
        BOOL settled = !active && [snapshot[@"settled"] boolValue];
        NSString *meeting = active ? (self.textMeetingID ?: identifier) : identifier;
        NSArray *turns = active ? [self liveTurnsThrough:through] : JardMeetingTurns(snapshot[@"turns"]);
        if (!active && !settled && !error && !turns.count && [self.textMeetingID isEqual:identifier])
            turns = self.liveTurns.copy;
        NSArray *selected = onlyNew ? [self.delta unseenTurns:turns meeting:meeting] : turns;
        NSDictionary *labels = [snapshot[@"labels"] isKindOfClass:NSDictionary.class] ? snapshot[@"labels"] : nil;
        NSString *text = JardMeetingExcerptText(selected, labels);
        if (settled && !turns.count) self.emptyMeetingID = meeting;
        if (settled && !selected.count) self.exhaustedMeetingID = meeting;
        NSString *notice = active && (error || (through && ![snapshot[@"complete"] boolValue]))
            ? @"Copied the transcript available so far." : nil;
        ready(text, text.length ? notice : error ?: @"No new meeting text is available yet.", ^(BOOL success) {
            if (self.excerptGeneration != generation) return;
            if (success && selected.count) {
                [self.delta deliveredTurns:selected meeting:active ? (self.textMeetingID ?: meeting) : meeting];
                if (settled) self.exhaustedMeetingID = meeting;
            }
            self.copying = NO;
            [self updateCopyControls];
            if (self.changed) self.changed([self status]);
        });
    };
    if (self.recording) self.send(@"jard.copyMeeting", @{@"meeting": active}, received);
    else if (active) received(nil, nil);
    else self.send(@"jard.meetingExcerpt", @{@"session": identifier}, received);
}

- (void)copyNew:(BOOL)onlyNew to:(NSPasteboard *)board reply:(JardDictationReply)reply {
    [self prepareExcerpt:onlyNew savedSession:nil ready:^(NSString *text, NSString *notice, void (^didCopy)(BOOL)) {
        BOOL copied = NO;
        if (text.length) {
            [board clearContents];
            copied = [board setString:text forType:NSPasteboardTypeString];
        }
        didCopy(copied);
        if (!reply) [self confirmCopy:copied ? notice ?: @"Meeting transcript copied." : notice ?: @"No meeting text to copy yet."
            brief:copied ? @"Copied" : @"No text yet"];
        if (reply) {
            NSMutableDictionary *result = copied ? [[self status] mutableCopy] : nil;
            if (result && notice) result[@"copyPartial"] = @YES;
            reply(result, copied ? nil : notice ?: @"Could not copy the meeting excerpt.");
        }
    }];
}

- (void)preparePaste:(JardPasteExcerpt)ready {
    [self prepareExcerpt:YES savedSession:nil ready:^(NSString *text, NSString *notice, void (^didPaste)(BOOL)) {
        ready(text, didPaste);
    }];
}

- (void)dismissOffer {
    if (self.offered) [self.presence snooze:self.offered absence:60 now:NSProcessInfo.processInfo.systemUptime];
    self.offered = nil;
    [self state:@"idle" message:@""];
}

- (void)request:(NSDictionary *)request reply:(JardDictationReply)reply {
    NSString *action = request[@"action"];
    if (self.managed && ![action isEqual:@"status"] && ![action isEqual:@"copyNew"] && ![action isEqual:@"copyAll"]) {
        reply(nil, managedMessage);
        return;
    }
    if ([action isEqual:@"start"]) {
        NSArray *languages = request[@"languages"] ?: @[];
        if (![languages isKindOfClass:NSArray.class] || languages.count > 25) {
            reply(nil, @"Invalid meeting language hints."); return;
        }
        for (id language in languages) {
            if (![language isKindOfClass:NSString.class] || [language length] > 12) {
                reply(nil, @"Invalid meeting language hints."); return;
            }
        }
        [self begin:YES automatic:NO languages:languages reply:reply];
        return;
    } else if ([action isEqual:@"stop"]) {
        [self stop:reply];
        return;
    } else if ([action isEqual:@"indicator"]) {
        NSNumber *enabled = request[@"enabled"];
        if (enabled != (__bridge NSNumber *)kCFBooleanTrue && enabled != (__bridge NSNumber *)kCFBooleanFalse) {
            reply(nil, @"Choose whether to show the recording indicator."); return;
        }
        self.showIndicator = enabled.boolValue;
        [NSUserDefaults.standardUserDefaults setBool:self.showIndicator forKey:indicatorKey];
        [self renderPanel];
        if (self.changed) self.changed([self status]);
    } else if ([action isEqual:@"indicatorCorner"]) {
        NSString *corner = request[@"corner"];
        if (![corner isKindOfClass:NSString.class] || !JardValidMeetingCorner(corner)) {
            reply(nil, @"Choose a corner for the recording indicator."); return;
        }
        NSScreen *screen = self.panel.screen ?: [self indicatorScreen];
        [self saveCorner:corner screen:screen];
        [self.panel moveToCorner:corner screen:screen animated:YES];
    } else if ([action isEqual:@"retryPermission"]) {
        [self retryPermission:reply];
        return;
    } else if ([action isEqual:@"recordingPrivacy"]) {
        [self openRecordingPrivacy];
    } else if ([action isEqual:@"dismissPermission"]) {
        if (self.permissionSource) [self dismissPermission];
    } else if ([action isEqual:@"recoverMeeting"]) {
        [self recoverMeeting:reply];
        return;
    } else if ([action isEqual:@"dismissRecovery"]) {
        if (self.recoveryDraft) [self dismissRecovery];
    } else if ([action isEqual:@"refreshRecovery"]) {
        [self scanRecovery:YES];
    } else if ([action isEqual:@"keepRecording"]) {
        if ([self.phase isEqual:@"callEnded"]) [self keepButton:nil];
    } else if ([action isEqual:@"calendarConnect"]) {
        [self.calendar connect:^(NSString *error) { reply(error ? nil : [self status], error); }];
        return;
    } else if ([action isEqual:@"calendarDisconnect"]) {
        [self.calendar disconnect];
        [self.calendarSnooze removeAllObjects];
        [self.reminded removeAllObjects];
    } else if ([action isEqual:@"calendarSettings"]) {
        [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:@"x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars"]];
    } else if ([action isEqual:@"calendarLead"]) {
        NSNumber *minutes = request[@"minutes"];
        if (![minutes isKindOfClass:NSNumber.class] || ![@[@0, @1, @2, @5] containsObject:minutes]) {
            reply(nil, @"Choose a calendar reminder time."); return;
        }
        [self.calendar setLead:minutes.integerValue];
    } else if ([action isEqual:@"calendarEvent"]) {
        NSString *identifier = request[@"id"], *choice = request[@"choice"];
        if (![identifier isKindOfClass:NSString.class] || ![choice isKindOfClass:NSString.class]) {
            reply(nil, @"Choose an upcoming meeting."); return;
        }
        [self answerCalendar:identifier choice:choice reply:reply];
        return;
    } else if ([action isEqual:@"calendarReminders"] || [action isEqual:@"calendarInclude"] || [action isEqual:@"autoStop"]) {
        NSNumber *enabled = request[@"enabled"];
        if (enabled != (__bridge NSNumber *)kCFBooleanTrue && enabled != (__bridge NSNumber *)kCFBooleanFalse) {
            reply(nil, @"Choose whether to enable this meeting setting."); return;
        }
        if ([action isEqual:@"calendarReminders"]) [self.calendar setReminders:enabled.boolValue];
        else if ([action isEqual:@"calendarInclude"]) {
            NSString *identifier = request[@"id"];
            if (![identifier isKindOfClass:NSString.class] || ![self.calendar setIncluded:enabled.boolValue calendar:identifier]) {
                reply(nil, @"This calendar is unavailable."); return;
            }
        } else {
            self.autoStop = enabled.boolValue;
            [NSUserDefaults.standardUserDefaults setBool:self.autoStop forKey:autoStopKey];
            self.generation++;
            if (!self.autoStop) {
                [self.presence clearRememberedSwitch];
                if (self.autoStopAt) [self keepButton:nil];
            }
        }
    } else if ([action isEqual:@"noiseCleanup"] || [action isEqual:@"boostMicrophone"]) {
        NSNumber *enabled = request[@"enabled"];
        if (enabled != (__bridge NSNumber *)kCFBooleanTrue && enabled != (__bridge NSNumber *)kCFBooleanFalse) {
            reply(nil, @"Choose whether to enable this meeting setting."); return;
        }
        [NSUserDefaults.standardUserDefaults setBool:enabled.boolValue
            forKey:[action isEqual:@"noiseCleanup"] ? noiseCleanupKey : boostMicrophoneKey];
    } else if ([action isEqual:@"detection"]) {
        NSNumber *enabled = request[@"enabled"];
        if (enabled != (__bridge NSNumber *)kCFBooleanTrue && enabled != (__bridge NSNumber *)kCFBooleanFalse) {
            reply(nil, @"Expected a meeting detection setting."); return;
        }
        self.detection = enabled.boolValue;
        [NSUserDefaults.standardUserDefaults setBool:self.detection forKey:detectionKey];
        self.generation++;
        [self.presence resetObservations];
        if (!self.detection) {
            [self.presence clearRememberedSwitch];
            if (self.offered) [self dismissOffer];
            if ([self.phase isEqual:@"callEnded"]) [self keepButton:nil];
        }
    } else if ([action isEqual:@"copyAll"] || [action isEqual:@"copyNew"]) {
        [self copyNew:[action isEqual:@"copyNew"] to:NSPasteboard.generalPasteboard reply:reply];
        return;
    } else if ([action isEqual:@"setDefaultPolicy"]) {
        NSString *policy = request[@"policy"];
        if (!validCallPolicy(policy)) { reply(nil, @"Choose a default recording preference."); return; }
        self.defaultPolicy = policy;
        [NSUserDefaults.standardUserDefaults setObject:policy forKey:defaultPolicyKey];
        [self callPoliciesChanged];
    } else if ([action isEqual:@"setPolicy"]) {
        NSString *provider = request[@"provider"];
        NSString *policy = request[@"policy"];
        NSString *label = [provider isKindOfClass:NSString.class] ?
            [self builtInLabelForProvider:provider] ?: self.policies[provider][@"label"] ?: self.recentSources[provider][@"label"] : nil;
        if (!label.length || (!validCallPolicy(policy) && ![policy isEqual:@"inherit"]) ||
            ([policy isEqual:@"auto"] && [provider hasPrefix:@"browser:"])) {
            reply(nil, @"Choose an available recording preference for this provider."); return;
        }
        [self setPolicy:policy provider:provider label:label];
        [self callPoliciesChanged];
    } else if (![action isEqual:@"status"]) {
        reply(nil, @"Unknown meeting action."); return;
    }
    reply([self status], nil);
}

- (void)startWithEstablishedPermissionsTitle:(NSString *)title reply:(JardDictationReply)reply {
    JardCalendarMeeting *calendar = [self calendarMeetingForCall:self.offered date:NSDate.date];
    [self begin:NO automatic:NO languages:@[] calendar:calendar title:title fromOffer:NO reply:reply];
}

- (void)copyLatestTranscript:(JardDictationReply)reply {
    [self copyLatestTranscriptTo:NSPasteboard.generalPasteboard reply:reply];
}

- (void)copyLatestTranscriptTo:(NSPasteboard *)board reply:(JardDictationReply)reply {
    [self copyTranscriptForMeeting:nil to:board reply:reply];
}

- (void)copySavedTranscript:(NSString *)identifier reply:(JardDictationReply)reply {
    [self copyTranscriptForMeeting:identifier to:NSPasteboard.generalPasteboard reply:reply];
}

- (void)copyTranscriptForMeeting:(NSString *)identifier to:(NSPasteboard *)board reply:(JardDictationReply)reply {
    NSInteger before = board.changeCount;
    NSString *command = identifier ? @"jard.savedMeetingTranscript" : @"jard.latestMeetingTranscript";
    NSDictionary *payload = identifier ? @{@"id": identifier} : @{};
    self.send(command, payload, ^(id result, NSString *error) {
        if (error) { reply(nil, error); return; }
        NSString *text = [result isKindOfClass:NSDictionary.class] && [result[@"text"] isKindOfClass:NSString.class]
            ? result[@"text"] : nil;
        if (!text.length) { reply(nil, @"No meeting transcript is available to copy."); return; }
        if (self.closed || board.changeCount != before) {
            reply(nil, @"The clipboard changed while preparing the meeting transcript.");
            return;
        }
        [board clearContents];
        if (![board setString:text forType:NSPasteboardTypeString]) {
            reply(nil, @"Could not write the meeting transcript to the clipboard.");
            return;
        }
        reply(@{@"copied": @YES}, nil);
    });
}

- (void)bridgeDisconnected {
    if (!self.recordingID) return;
    [self finish:@"The recorder disconnected. Checking for recorded audio…" session:nil];
    __weak typeof(self) weak = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 500 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{ [weak scanRecovery:YES]; });
}

- (BOOL)currentOfferButton:(id)sender choices:(BOOL)choices {
    return self.offered && [self.phase isEqual:@"offered"] && self.offerChoicesOpen == choices &&
        self.panel && [sender isKindOfClass:NSButton.class] && [sender superview] == self.panel.contentView;
}
- (void)recordButton:(id)sender {
    if ([self currentOfferButton:sender choices:NO]) [self startFromOffer];
}
- (void)startFromOffer {
    JardCalendarMeeting *event = [self calendarMeetingForCall:self.offered date:NSDate.date];
    [self begin:YES automatic:NO languages:@[] calendar:event title:nil fromOffer:YES reply:nil];
}
- (void)dismissButton:(id)sender {
    if ([self currentOfferButton:sender choices:NO]) [self dismissOffer];
}
- (void)showOfferChoices:(id)sender {
    if (![self currentOfferButton:sender choices:NO]) return;
    self.offerChoicesOpen = YES;
    [self renderPanel];
}
- (void)cancelOfferChoices:(id)sender {
    if (![self currentOfferButton:sender choices:YES]) return;
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    if (now - self.offeredAt >= meetingOfferLifetime) self.offeredAt = now;
    self.offerChoicesOpen = NO;
    [self renderPanel];
}
- (void)copyAllButton:(id)sender { [self copyNew:NO to:NSPasteboard.generalPasteboard reply:nil]; }
- (void)copyNewButton:(id)sender { [self copyNew:YES to:NSPasteboard.generalPasteboard reply:nil]; }
- (void)copyCompletedMeeting:(BOOL)onlyNew to:(NSPasteboard *)board {
    NSString *session = self.completionSession;
    if (!session || !self.completionDeadline || ![self.completion[@"hasSpeech"] boolValue]) return;
    NSInteger before = board.changeCount;
    [self prepareExcerpt:onlyNew savedSession:session ready:^(NSString *text, NSString *notice, void (^didCopy)(BOOL)) {
        void (^copy)(NSString *) = ^(NSString *value) {
            BOOL fresh = !self.closed && [self.sessionID isEqual:session] && board.changeCount == before;
            BOOL copied = NO;
            if (fresh && value.length) {
                [board clearContents];
                copied = [board setString:value forType:NSPasteboardTypeString];
            }
            didCopy(copied);
            if (self.closed || ![self.sessionID isEqual:session]) return;
            [self confirmCopy:copied ? @"Meeting transcript copied." : notice ?: @"No meeting text was copied."
                         brief:copied ? @"Copied" : @"No text copied"];
        };
        if (onlyNew || !text.length || ![self.sessionID isEqual:session]) { copy(text); return; }
        self.send(@"jard.meetingMarkdown", @{@"session": session}, ^(id result, NSString *error) {
            NSString *markdown = [result isKindOfClass:NSDictionary.class] &&
                [result[@"text"] isKindOfClass:NSString.class] ? result[@"text"] : nil;
            copy(markdown.length ? markdown : text);
        });
    }];
}
- (void)copyCompletedNewButton:(id)sender { [self copyCompletedMeeting:YES to:NSPasteboard.generalPasteboard]; }
- (void)copyCompletedAllButton:(id)sender { [self copyCompletedMeeting:NO to:NSPasteboard.generalPasteboard]; }
- (void)copyMeetingAgentPrompt:(id)sender { [self copyMeetingAgentPromptTo:NSPasteboard.generalPasteboard]; }
- (void)copyMeetingAgentPromptTo:(NSPasteboard *)board {
    BOOL live = self.recording;
    NSString *meeting = live ? self.textMeetingID : self.completionDeadline ? self.completionSession : nil;
    if (![[NSUUID alloc] initWithUUIDString:meeting ?: @""] || (live && [meeting isEqual:self.recordingID])) return;
    NSString *helper = [NSBundle.mainBundle.bundlePath stringByAppendingPathComponent:@"Contents/Helpers/jardctl"];
    NSString *command = [NSFileManager.defaultManager isExecutableFileAtPath:helper] ?
        [NSString stringWithFormat:@"'%@'", [helper stringByReplacingOccurrencesOfString:@"'" withString:@"'\\''"]] : @"jardctl";
    NSString *read = [NSString stringWithFormat:@"%@ meetings show %@", command, meeting];
    NSMutableString *prompt = [NSMutableString stringWithFormat:
        @"Use the local Jaro transcript to answer my questions about this meeting. %@\n\nRead the transcript:\n  %@ --md\n",
        live ? @"The meeting is still recording." : @"The meeting has been saved.", read];
    id lastEnd = self.liveTurns.lastObject[@"end"];
    if (live && [lastEnd isKindOfClass:NSNumber.class]) {
        NSInteger through = (NSInteger)[lastEnd doubleValue];
        [prompt appendFormat:@"\nCatch up from the last transcribed turn. The first turn may repeat.\n  %@ --from %ld:%02ld --md\n",
            read, (long)through / 60, (long)through % 60];
    }
    [prompt appendFormat:@"\nSearch within this meeting, then read around the returned times:\n  %@ meetings search \"<topic>\" --meeting %@\n  %@ --from <mm:ss> --to <mm:ss> --md",
        command, meeting, read];
    [board clearContents];
    if ([board setString:prompt forType:NSPasteboardTypeString]) {
        NSButton *button = self.agentPromptButton;
        button.title = @"Prompt Copied";
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 1500 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
            button.title = @"Copy Agent Prompt";
        });
        NSAccessibilityPostNotificationWithUserInfo((id)self.caption ?: NSApp, NSAccessibilityAnnouncementRequestedNotification,
            @{NSAccessibilityAnnouncementKey: @"Meeting agent prompt copied.", NSAccessibilityPriorityKey: @(NSAccessibilityPriorityMedium)});
    }
}
- (void)stopButton:(id)sender { [self stop:nil]; }
- (void)retryPermissionButton:(id)sender { [self retryPermission:nil]; }
- (void)privacyButton:(id)sender { [self openRecordingPrivacy]; }
- (void)dismissPermissionButton:(id)sender { [self dismissPermission]; }
- (void)recoverButton:(id)sender { [self recoverMeeting:nil]; }
- (void)dismissRecoveryButton:(id)sender { [self dismissRecovery]; }
- (void)keepButton:(id)sender {
    if ([self.endedReason isEqual:@"switch"]) self.keptSwitch = self.differentCall.identity;
    self.differentCall = nil;
    self.endPromptAcknowledged = YES;
    self.endedReason = nil;
    self.autoStopAt = 0;
    [self state:@"recording" message:@""];
}

- (BOOL)openMeetingLink:(NSURL *)url { return [NSWorkspace.sharedWorkspace openURL:url]; }

- (void)answerCalendar:(NSString *)identifier choice:(NSString *)choice reply:(JardDictationReply)reply {
    void (^respond)(id, NSString *) = reply ?: ^(id result, NSString *error) {
        if (error) [self state:self.phase message:error];
    };
    JardCalendarMeeting *event = [self calendarMeeting:identifier date:NSDate.date];
    if (!event || ![@[@"join", @"record", @"joinRecord", @"dismiss"] containsObject:choice]) {
        respond(nil, @"This calendar meeting is no longer available."); return;
    }
    BOOL joining = [@[@"join", @"joinRecord"] containsObject:choice];
    BOOL record = [@[@"record", @"joinRecord"] containsObject:choice];
    if (record && (self.busy || self.suspended)) {
        respond(nil, @"Finish the active capture before recording another meeting."); return;
    }
    if (joining && (!event.link || ![self openMeetingLink:event.link])) {
        respond(nil, @"Could not open the calendar call link."); return;
    }
    self.reminded[event.identifier] = event.end;
    if ([choice isEqual:@"dismiss"] || record) [self snoozeCalendar:event];
    if ([self.calendarOffer.identifier isEqual:event.identifier]) [self closeCalendarOffer];
    if (record) {
        // A simultaneous detected-call offer can refer to another meeting.
        self.offered = nil;
        [self begin:YES automatic:NO languages:@[] calendar:event reply:respond];
    } else respond([self status], nil);
}

- (void)calendarJoin:(id)sender { [self answerCalendar:self.calendarOffer.identifier choice:@"join" reply:nil]; }
- (void)calendarRecord:(id)sender { [self answerCalendar:self.calendarOffer.identifier choice:@"record" reply:nil]; }
- (void)calendarJoinRecord:(id)sender { [self answerCalendar:self.calendarOffer.identifier choice:@"joinRecord" reply:nil]; }
- (void)calendarDismiss:(id)sender { [self answerCalendar:self.calendarOffer.identifier choice:@"dismiss" reply:nil]; }
- (void)alwaysButton:(id)sender {
    if (![self currentOfferButton:sender choices:YES] || !self.offerAllowsAutomaticRecording) return;
    JardCall *call = self.offered;
    [self setPolicy:@"auto" provider:call.provider label:call.label];
    [self startFromOffer];
}
- (void)neverButton:(id)sender {
    if (![self currentOfferButton:sender choices:YES]) return;
    JardCall *call = self.offered;
    [self setPolicy:@"never" provider:call.provider label:call.label];
    [self dismissOffer];
}

- (NSButton *)button:(NSString *)title action:(SEL)action area:(NSRect)area into:(NSView *)container {
    NSButton *button = [NSButton buttonWithTitle:title target:self action:action];
    button.frame = area;
    [button setAccessibilityLabel:title];
    [container addSubview:button];
    return button;
}

- (void)openRecordingPrivacy {
    NSString *source = self.permissionSource ?: (self.recording && self.systemAudioPermissionDenied ? @"system" : nil);
    if (!source) return;
    if ([source isEqual:@"system"] && self.showSystemAudioSettings) {
        self.showSystemAudioSettings();
        return;
    }
    NSString *pane = [source isEqual:@"mic"] ? @"Privacy_Microphone" : @"Privacy_ScreenCapture";
    [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:[@"x-apple.systempreferences:com.apple.preference.security?" stringByAppendingString:pane]]];
}

- (NSScreen *)indicatorScreen {
    NSScreen *underPointer = nil;
    NSPoint cursor = NSEvent.mouseLocation;
    for (NSScreen *screen in NSScreen.screens) {
        if ([screen.deviceDescription[@"NSScreenNumber"] isEqual:self.panelDisplay]) return screen;
        if (NSPointInRect(cursor, screen.frame)) underPointer = screen;
    }
    return underPointer ?: NSScreen.mainScreen;
}

- (void)saveCorner:(NSString *)corner screen:(NSScreen *)screen {
    self.panelCorner = corner;
    self.panelDisplay = screen.deviceDescription[@"NSScreenNumber"];
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    [defaults setObject:corner forKey:cornerKey];
    [defaults setObject:self.panelDisplay forKey:displayKey];
    if (self.changed) self.changed([self status]);
}

- (void)confirmCopy:(NSString *)message brief:(NSString *)brief {
    self.excerptFeedback = brief;
    [self updateCopyControls];
    if (self.caption || NSApp) NSAccessibilityPostNotificationWithUserInfo((id)self.caption ?: NSApp,
        NSAccessibilityAnnouncementRequestedNotification,
        @{NSAccessibilityAnnouncementKey: message, NSAccessibilityPriorityKey: @(NSAccessibilityPriorityMedium)});
    NSUInteger generation = ++self.excerptFeedbackGeneration;
    __weak typeof(self) weak = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 1500 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        if (weak.excerptFeedbackGeneration != generation) return;
        weak.excerptFeedback = nil;
        [weak updateCopyControls];
    });
}

- (void)updateCaption {
    if ((!self.recording && !self.waitingForCaptureEnd) || !self.caption) return;
    NSString *clock = meetingClock(self.seconds);
    BOOL ended = [self.phase isEqual:@"callEnded"];
    self.caption.maximumNumberOfLines = ended ? 2 : 1;
    self.caption.lineBreakMode = ended ? NSLineBreakByWordWrapping : NSLineBreakByTruncatingTail;
    NSString *notice = self.systemAudioPermissionDenied ? JardLocalized(@"Mac audio access needed") :
        self.microphoneUnavailable && self.systemAudioUnavailable ? @"Audio inputs reconnecting" :
        self.microphoneUnavailable ? @"Microphone reconnecting" : self.systemAudioUnavailable ? @"Mac audio reconnecting" :
        self.microphoneSilent ? @"Microphone silent" :
        NSProcessInfo.processInfo.systemUptime < self.automaticUntil ? @"Started automatically" : nil;
    if (self.recordingDisclosure) {
        NSString *title = self.meetingTitle ?: self.activeCalendar.title ?: self.provisionalTitle ?: self.providerLabel ?: @"Meeting";
        self.recordingNotice.stringValue = notice ?: @"";
        self.caption.stringValue = clock;
        self.caption.toolTip = title;
        [self.caption setAccessibilityLabel:[NSString stringWithFormat:@"Meeting recording. %@ · %@%@%@", title, clock,
            notice.length ? @". " : @"", notice ?: @""]];
        return;
    }
    if (ended) self.caption.stringValue = self.message;
    else {
        NSString *label = notice ?: self.meetingTitle ?: self.activeCalendar.title ?: self.provisionalTitle ?: self.providerLabel;
        NSString *caption = [NSString stringWithFormat:@"%@ · %@", label, clock];
        if ([caption sizeWithAttributes:@{NSFontAttributeName: self.caption.font}].width > NSWidth(self.caption.bounds) - 6)
            caption = [NSString stringWithFormat:@"%@ · %@", clock, label];
        self.caption.stringValue = caption;
    }
    [self.caption setAccessibilityLabel:[@"Meeting recording. " stringByAppendingString:self.caption.stringValue]];
}

- (void)updateRecordingMeterAt:(NSTimeInterval)now {
    if (!self.panel.visible || !self.recordingMeter) return;
    BOOL live = self.recording || self.waitingForCaptureEnd;
    double rms = live && now - self.recordingLevelAt <= 0.30 ? self.recordingRMS : 0;
    [self.recordingMeter advanceAt:now rms:rms];
    if (self.recordingResultTitle && self.recordingMeter.settled) {
        [self.recordingMeter removeFromSuperview];
        self.recordingMeter = nil;
    }
}

- (void)stopRecordingMeter {
    [self.meterTimer invalidate];
    self.meterTimer = nil;
    self.recordingMeter = nil;
    self.recordingActions = nil;
    self.recordingDisclosure = nil;
    self.recordingNotice = nil;
    self.recordingResultTitle = nil;
    self.recordingExpansion = 0;
    self.recordingLayoutAt = 0;
    self.recordingHoverHeld = self.recordingPinned = self.recordingControlsRevealed = NO;
}

- (CGFloat)offerDepartureFractionAt:(NSTimeInterval)now {
    if (self.panel.reduceMotion) return 1;
    double remaining = 1 - MAX(0, MIN(1, (now - self.offerDepartureAt) / offerStartingDelay));
    return 1 - remaining * remaining * remaining;
}

- (void)layoutOfferDepartureAt:(NSTimeInterval)now {
    if (self.closed || !self.panel || !self.offerDepartureAt || ![self.phase isEqual:@"starting"] || self.offerStartingVisible) return;
    CGFloat fraction = [self offerDepartureFractionAt:now];
    CGFloat width = recordingInitialWidth + (self.offerDepartureSize.width - recordingInitialWidth) * (1 - fraction);
    CGFloat height = recordingHeaderHeight + (self.offerDepartureSize.height - recordingHeaderHeight) * (1 - fraction);
    NSScreen *screen = self.panel.screen ?: [self indicatorScreen];
    NSSize old = self.panel.frame.size;
    NSRect frame = JardMeetingCornerFrame(screen.visibleFrame, NSMakeSize(width, height), self.panelCorner);
    if (!NSEqualRects(self.panel.frame, frame)) [self.panel setFrame:frame display:NO];
    NSView *surface = self.panel.contentView;
    if (now - self.offerDepartureAt >= offerStartingDelay) {
        for (NSView *view in surface.subviews.copy) [view removeFromSuperview];
        self.caption = [JardMeetingCaption labelWithString:JardLocalized(@"Starting…")];
        self.caption.alignment = NSTextAlignmentCenter;
        self.caption.font = [NSFont systemFontOfSize:13 weight:NSFontWeightMedium];
        self.caption.frame = NSMakeRect(8, 10, width - 16, 20);
        [surface addSubview:self.caption];
        self.offerStartingVisible = YES;
        return;
    }
    CGFloat dx = [self.panelCorner hasSuffix:@"Right"] ? width - old.width : 0;
    CGFloat dy = [self.panelCorner hasPrefix:@"top"] ? height - old.height : 0;
    CGFloat opacity = MAX(0, 1 - fraction * 1.4);
    for (NSView *view in surface.subviews) {
        if (dx || dy) view.frame = NSOffsetRect(view.frame, dx, dy);
        view.alphaValue = opacity;
    }
}

- (void)layoutRecordingPanelAt:(NSTimeInterval)now pointer:(NSPoint)pointer pressed:(BOOL)pressed {
    if (!self.recordingDisclosure || self.panel.dragging) return;
    BOOL inside = NSPointInRect(pointer, self.panel.frame);
    if (!inside && !pressed) self.recordingHoverHeld = NO;
    BOOL open = self.waitingForCaptureEnd || self.recordingPinned ||
        (!self.recordingHoverHeld && (inside || (pressed && self.recordingExpansion > 0)));
    CGFloat target = open ? 1 : 0;
    double dt = self.recordingLayoutAt > 0 ? MAX(0, MIN(.05, now - self.recordingLayoutAt)) : 0;
    self.recordingLayoutAt = now;
    CGFloat progress = self.recordingExpansion;
    progress += (target - progress) * (1 - exp(-dt * 18));
    if (self.panel.reduceMotion || fabs(progress - target) < .015) progress = target;
    self.recordingExpansion = progress;

    CGFloat clockWidth = meetingClockWidth(self.caption);
    NSString *notice = self.recordingNotice.stringValue;
    CGFloat noticeWidth = notice.length ? MIN(160, ceil([notice sizeWithAttributes:@{NSFontAttributeName: self.recordingNotice.font}].width) + 6) : 0;
    CGFloat restWidth = 78 + clockWidth + 54 + noticeWidth;
    CGFloat width = restWidth + (MAX(restWidth, recordingOpenWidth) - restWidth) * MIN(1, progress / .70);
    CGFloat reveal = MAX(0, (progress - .12) / .88);
    CGFloat body = (recordingRowHeight * self.recordingActions.count + 9) * reveal;
    CGFloat height = recordingHeaderHeight + body;
    if (self.offerDepartureAt) {
        CGFloat arrival = [self offerDepartureFractionAt:now];
        width += (self.offerDepartureSize.width - width) * (1 - arrival);
        height += (self.offerDepartureSize.height - height) * (1 - arrival);
        if (arrival >= 1) self.offerDepartureAt = 0;
    }
    NSString *corner = self.panelCorner;
    NSScreen *screen = self.panel.screen ?: [self indicatorScreen];
    NSRect frame = JardMeetingCornerFrame(screen.visibleFrame, NSMakeSize(width, height), corner);
    if (!NSEqualRects(self.panel.frame, frame)) [self.panel setFrame:frame display:NO];
    [self claimMeetingOverlayDockOnScreen:screen];

    BOOL top = [corner hasPrefix:@"top"];
    CGFloat header = top ? height - recordingHeaderHeight : 0;
    CGFloat clockX = width - 34 - clockWidth;
    self.caption.frame = NSMakeRect(clockX, header + 10, clockWidth, 20);
    self.recordingDisclosure.frame = NSMakeRect(width - 32, header + 7, 25, 26);
    BOOL reconnecting = self.microphoneUnavailable || self.systemAudioUnavailable;
    CGFloat noticeSpace = noticeWidth * (reconnecting ? 1 : MAX(0, 1 - progress / .65));
    self.recordingNotice.frame = NSMakeRect(clockX - noticeWidth - 4, header + 10, noticeWidth, 20);
    self.recordingNotice.alphaValue = reconnecting ? 1 : MAX(0, 1 - progress / .55);
    CGFloat meterWidth = MAX(26, clockX - noticeSpace - 20);
    self.recordingMeter.frame = NSMakeRect(11, header + 7, meterWidth, 26);
    [self.recordingMeter fitEmbeddedWidth:meterWidth anchorRight:[corner hasSuffix:@"Right"]];

    for (NSUInteger index = 0; index < self.recordingActions.count; index++) {
        NSButton *button = self.recordingActions[index];
        CGFloat offset = (index + 1) * recordingRowHeight;
        CGFloat y = recordingRowY(height, top, index);
        CGFloat visible = MAX(0, MIN(1, (body - offset + recordingRowHeight) / recordingRowHeight));
        button.hidden = visible == 0;
        button.alphaValue = visible;
        button.frame = NSMakeRect(8, y + 1, width - 16, recordingRowHeight - 2);
    }
    self.recordingActions.firstObject.enabled = open && reveal >= .24 && !self.waitingForCaptureEnd;
    BOOL revealed = open && progress >= .94;
    if (self.recordingControlsRevealed != revealed) {
        self.recordingControlsRevealed = revealed;
        [self updateCopyControls];
        NSString *action = revealed ? @"Hide meeting controls" : @"Show meeting controls";
        self.recordingDisclosure.toolTip = action;
        [self.recordingDisclosure setAccessibilityLabel:action];
        self.recordingDisclosure.image = [NSImage imageWithSystemSymbolName:revealed ? @"chevron.up" : @"chevron.down" accessibilityDescription:nil];
    }
}

- (void)openWorkspaceButton:(id)sender {
    if (self.openWorkspace) self.openWorkspace();
}

- (void)claimMeetingOverlayDockOnScreen:(NSScreen *)screen {
    if (!self.panel.visible) {
        [JardOverlayDock.shared releaseSurface:JardOverlaySurfaceMeeting];
        return;
    }
    NSScreen *target = screen ?: self.panel.screen ?: [self indicatorScreen];
    if (!target) return;
    JardOverlayEdge edge = [self.panelCorner hasPrefix:@"bottom"] ? JardOverlayEdgeBottom : JardOverlayEdgeTop;
    [JardOverlayDock.shared claimSurface:JardOverlaySurfaceMeeting frame:self.panel.frame
                                    edge:edge displayID:JardDisplayIDForScreen(target)];
}

- (void)toggleRecordingControls:(id)sender {
    BOOL close = self.recordingPinned || self.recordingControlsRevealed;
    self.recordingPinned = !close;
    self.recordingHoverHeld = close;
}

- (void)startRecordingPresentationTimer {
    if (self.meterTimer) return;
    __weak typeof(self) weak = self;
    self.meterTimer = [NSTimer timerWithTimeInterval:1.0 / 30.0 repeats:YES block:^(NSTimer *timer) {
        NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
        if (weak.offerDepartureAt && [weak.phase isEqual:@"starting"]) [weak layoutOfferDepartureAt:now];
        else if (weak.recordingResultTitle) [weak layoutRecordingResultAt:now];
        else [weak layoutRecordingPanelAt:now pointer:NSEvent.mouseLocation pressed:NSEvent.pressedMouseButtons != 0];
        [weak updateRecordingMeterAt:now];
    }];
    self.meterTimer.tolerance = 0.007;
    [NSRunLoop.mainRunLoop addTimer:self.meterTimer forMode:NSRunLoopCommonModes];
}

- (void)attachRecordingMeterTo:(JardMeetingSurface *)surface {
    if (!self.recordingMeter) {
        self.recordingMeter = [[JardDictationMeter alloc] initWithFrame:NSMakeRect(13, 53, 78, 26)];
        self.recordingMeter.embedded = YES;
        [self.recordingMeter setStage:JardIndicatorRecording label:@""];
    }
    [self startRecordingPresentationTimer];
    [self.recordingMeter removeFromSuperview];
    [surface addSubview:self.recordingMeter];
}

- (void)buildRecordingControlsIn:(JardMeetingSurface *)surface {
    self.caption.font = [NSFont monospacedDigitSystemFontOfSize:13 weight:NSFontWeightSemibold];
    self.caption.alignment = NSTextAlignmentRight;
    self.caption.usesSingleLineMode = YES;
    NSTextField *notice = [JardMeetingCaption labelWithString:@""];
    notice.font = [NSFont systemFontOfSize:11 weight:NSFontWeightMedium];
    notice.textColor = NSColor.secondaryLabelColor;
    notice.lineBreakMode = NSLineBreakByTruncatingTail;
    [notice setAccessibilityElement:NO];
    [surface addSubview:notice];
    self.recordingNotice = notice;

    NSButton *disclosure = [self button:@"" action:@selector(toggleRecordingControls:) area:NSZeroRect into:surface];
    disclosure.bordered = NO;
    disclosure.image = [NSImage imageWithSystemSymbolName:@"chevron.down" accessibilityDescription:nil];
    disclosure.toolTip = @"Show meeting controls";
    [disclosure setAccessibilityLabel:disclosure.toolTip];
    self.recordingDisclosure = disclosure;
    NSButton *stop = [self button:@"Stop & Save" action:@selector(stopButton:) area:NSZeroRect into:surface];
    NSButton *newText = [self button:@"Copy New" action:@selector(copyNewButton:) area:NSZeroRect into:surface];
    NSButton *all = [self button:@"Copy All" action:@selector(copyAllButton:) area:NSZeroRect into:surface];
    NSButton *prompt = [self button:@"Copy Agent Prompt" action:@selector(copyMeetingAgentPrompt:) area:NSZeroRect into:surface];
    NSButton *openApp = [self button:@"Open in Jaro" action:@selector(openWorkspaceButton:) area:NSZeroRect into:surface];
    self.recordingActions = @[stop, newText, all, prompt, openApp];
    self.incrementalTextButton = newText;
    self.fullTextButton = all;
    self.agentPromptButton = prompt;
    self.recordingHoverHeld = NSPointInRect(NSEvent.mouseLocation, self.panel.frame);
    [self updateCaption];
    [self updateCopyControls];
    [self layoutRecordingPanelAt:NSProcessInfo.processInfo.systemUptime pointer:NSEvent.mouseLocation pressed:NO];
}

- (void)layoutRecordingResultAt:(NSTimeInterval)now {
    if (self.panel.dragging) return;
    BOOL ready = self.completionDeadline > 0;
    CGFloat transition = self.panel.reduceMotion ? 1 : MAX(0, MIN(1, (now - self.recordingResultAt) / .24));
    CGFloat width = MAX(recordingOpenWidth, self.recordingResultOrigin.width);
    CGFloat bodyWidth = width - 32;
    CGFloat bodyHeight = 0;
    if (ready && self.completionBody.stringValue.length) {
        NSRect bounds = [self.completionBody.stringValue boundingRectWithSize:NSMakeSize(bodyWidth - 6, CGFLOAT_MAX)
            options:NSStringDrawingUsesLineFragmentOrigin | NSStringDrawingUsesFontLeading
            attributes:@{NSFontAttributeName: self.completionBody.font} context:nil];
        bodyHeight = MIN(52, ceil(NSHeight(bounds)) + 6);
    }
    CGFloat resultHeight = recordingHeaderHeight + recordingRowHeight + 9;
    if (ready) resultHeight += 3 * recordingRowHeight + bodyHeight;
    CGFloat height = resultHeight;
    if (!ready) {
        CGFloat ease = transition * transition * (3 - 2 * transition);
        height = self.recordingResultOrigin.height + (resultHeight - self.recordingResultOrigin.height) * ease;
    }

    NSScreen *screen = self.panel.screen ?: [self indicatorScreen];
    NSString *corner = self.panelCorner;
    NSRect frame = JardMeetingCornerFrame(screen.visibleFrame, NSMakeSize(width, height), corner);
    if (!NSEqualRects(self.panel.frame, frame)) [self.panel setFrame:frame display:NO];
    BOOL top = [corner hasPrefix:@"top"];
    CGFloat header = top ? height - recordingHeaderHeight : 0;
    CGFloat clockWidth = meetingClockWidth(self.caption);
    CGFloat clockX = width - 34 - clockWidth;
    self.caption.frame = NSMakeRect(clockX, header + 10, clockWidth, 20);
    self.recordingDisclosure.frame = NSMakeRect(width - 32, header + 7, 25, 26);
    self.recordingDisclosure.hidden = !ready;
    self.recordingDisclosure.enabled = ready;
    NSRect meter = self.recordingMeter.frame;
    meter.origin.y = header + 7;
    meter.size.width = MIN(NSWidth(meter), MAX(0, clockX - 20));
    self.recordingMeter.frame = meter;

    CGFloat stopY = recordingRowY(height, top, 0);
    self.recordingResultTitle.frame = NSMakeRect(16, stopY, bodyWidth, recordingRowHeight);
    self.recordingResultTitle.alphaValue = ready ? 1 : MAX(0, MIN(1, (transition - .46) / .54));
    self.recordingResultTitle.hidden = !ready && transition <= .46;
    self.completionBody.frame = NSMakeRect(16, top ? 7 : recordingHeaderHeight + 4 * recordingRowHeight + 2, bodyWidth, bodyHeight);
    self.completionBody.hidden = !ready;
    CGFloat outgoing = ready ? 0 : MAX(0, 1 - transition / .40);
    for (NSUInteger index = 0; index < self.recordingActions.count; index++) {
        NSButton *row = self.recordingActions[index];
        if (ready && index > 0) {
            row.hidden = NO;
            row.alphaValue = 1;
        } else {
            row.alphaValue = MIN(row.alphaValue, outgoing);
            row.hidden = row.hidden || outgoing == 0;
        }
        row.frame = NSMakeRect(8, recordingRowY(height, top, index) + 1, width - 16, recordingRowHeight - 2);
    }
    if ((ready || transition == 1) && !self.recordingMeter) {
        [self.meterTimer invalidate];
        self.meterTimer = nil;
    }
}

- (void)beginRecordingResult {
    if (self.recordingResultTitle) return;
    self.recordingResultAt = NSProcessInfo.processInfo.systemUptime;
    self.recordingResultOrigin = self.panel.frame.size;
    [self.recordingMeter beginDismissal];
    self.recordingNotice.hidden = YES;
    self.caption.stringValue = meetingClock(self.seconds);
    self.caption.accessibilityLabel = self.caption.stringValue;
    for (NSButton *row in self.recordingActions) {
        row.enabled = NO;
        [row setAccessibilityElement:NO];
    }
    self.recordingDisclosure.action = @selector(dismissCompletion:);
    self.recordingDisclosure.image = [NSImage imageWithSystemSymbolName:@"xmark" accessibilityDescription:nil];
    self.recordingDisclosure.toolTip = @"Close meeting capsule";
    [self.recordingDisclosure setAccessibilityLabel:self.recordingDisclosure.toolTip];
    self.incrementalTextButton.action = @selector(copyCompletedNewButton:);
    self.fullTextButton.action = @selector(copyCompletedAllButton:);

    NSTextField *title = [JardMeetingCaption wrappingLabelWithString:JardLocalized(@"Transcribing…")];
    title.font = [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold];
    title.maximumNumberOfLines = 2;
    title.lineBreakMode = NSLineBreakByWordWrapping;
    [self.panel.contentView addSubview:title];
    self.recordingResultTitle = title;
    NSTextField *body = [JardMeetingCaption wrappingLabelWithString:@""];
    body.font = [NSFont systemFontOfSize:12];
    body.maximumNumberOfLines = 3;
    body.lineBreakMode = NSLineBreakByWordWrapping;
    [self.panel.contentView addSubview:body];
    self.completionBody = body;
    [self layoutRecordingResultAt:self.recordingResultAt];
}

- (void)updateCopyControls {
    BOOL available = !self.completionSession || (self.completionDeadline && [self.completion[@"hasSpeech"] boolValue]);
    BOOL unused = !self.completionSession || ![self.exhaustedMeetingID isEqual:self.completionSession];
    BOOL availableControls = self.recordingResultTitle ? self.completionDeadline > 0 :
        !self.recordingDisclosure || (self.recordingControlsRevealed && !self.waitingForCaptureEnd);
    self.incrementalTextButton.enabled = availableControls && !self.copying && available && unused;
    self.fullTextButton.enabled = availableControls && !self.copying && available;
    BOOL livePrompt = self.recording && self.textMeetingID && ![self.textMeetingID isEqual:self.recordingID];
    self.agentPromptButton.enabled = availableControls && (livePrompt || self.completionDeadline > 0);
    self.incrementalTextButton.title = self.copying ? @"Preparing…" : self.excerptFeedback ?: @"Copy New";
    [self.incrementalTextButton setAccessibilityLabel:self.copying ? @"Preparing new meeting transcript" : @"Copy new meeting transcript"];
    [self.fullTextButton setAccessibilityLabel:@"Copy entire meeting transcript"];
}

- (void)updateCompletionContent {
    if (!self.completionSession || !self.completionBody) return;
    NSString *title = [self.completion[@"title"] isKindOfClass:NSString.class] ? self.completion[@"title"] :
        self.meetingTitle ?: self.provisionalTitle ?: self.providerLabel ?: @"Meeting";
    NSInteger seconds = [self.completion[@"duration"] isKindOfClass:NSNumber.class] ?
        [self.completion[@"duration"] integerValue] : (NSInteger)self.seconds;
    if (self.recordingResultTitle) {
        if (self.completionDeadline) {
            self.recordingResultTitle.stringValue = title;
            self.recordingResultTitle.accessibilityLabel = title;
            self.recordingResultTitle.toolTip = title;
            for (NSUInteger index = 1; index < self.recordingActions.count; index++)
                [self.recordingActions[index] setAccessibilityElement:YES];
        }
    } else {
        self.caption.stringValue = [NSString stringWithFormat:@"%@ · %@", meetingClock(seconds), title];
        self.caption.accessibilityLabel = self.caption.stringValue;
    }
    NSString *summary = [self.completion[@"summary"] isKindOfClass:NSString.class] ? self.completion[@"summary"] : nil;
    NSString *error = [self.completion[@"error"] isKindOfClass:NSString.class] ? self.completion[@"error"] : nil;
    if (!self.completionDeadline) self.completionBody.stringValue = @"Transcribing the saved meeting…";
    else if ([self.completion[@"exportStatus"] isEqual:@"error"]) self.completionBody.stringValue = @"Markdown export failed. See the meeting in Sessions.";
    else if (summary.length) self.completionBody.stringValue = summary;
    else if (error.length) self.completionBody.stringValue = error;
    else if ([self.completion[@"status"] isEqual:@"interrupted"]) self.completionBody.stringValue = @"Transcription was interrupted. Open the meeting in Sessions.";
    else if ([self.completion[@"summaryStatus"] isEqual:@"queued"]) self.completionBody.stringValue = @"Writing a title and summary with local Qwen…";
    else if ([self.completion[@"summaryStatus"] isEqual:@"error"]) self.completionBody.stringValue = @"Summary unavailable. Retry in Sessions.";
    else self.completionBody.stringValue = [self.completion[@"hasSpeech"] boolValue] ? self.message ?: @"Saved to Sessions." : @"Saved to Sessions. No speech was detected.";
    self.completionBody.accessibilityLabel = self.completionBody.stringValue;
    [self updateCopyControls];
    if (self.recordingResultTitle) {
        if (self.panel.dragging) [self startRecordingPresentationTimer];
        else [self layoutRecordingResultAt:NSProcessInfo.processInfo.systemUptime];
    }
}

- (void)renderPanel {
    BOOL offer = [self.phase isEqual:@"offered"], recording = self.recording;
    BOOL live = recording || self.waitingForCaptureEnd;
    BOOL compact = live && ![self.phase isEqual:@"callEnded"];
    BOOL completed = [self.phase isEqual:@"finished"] && self.completionSession;
    BOOL reminder = [self.phase isEqual:@"reminder"] && self.calendarOffer;
    BOOL progress = [@[@"starting", @"saving"] containsObject:self.phase];
    BOOL permission = [self.phase isEqual:@"permission"] && self.permissionSource;
    BOOL interrupted = [self.phase isEqual:@"interrupted"] && self.recoveryDraft;
    BOOL startingFromOffer = self.offerDepartureAt > 0 && [self.phase isEqual:@"starting"] && self.panel;
    BOOL hiddenRecording = !self.showIndicator && (compact || ([self.phase isEqual:@"starting"] && !startingFromOffer));
    if (self.closed || hiddenRecording || (!offer && !reminder && !live && !progress && !permission && !interrupted && !completed)) {
        if (hiddenRecording) self.offerDepartureAt = 0;
        [self stopRecordingMeter];
        [self.panel orderOut:nil];
    [JardOverlayDock.shared releaseSurface:JardOverlaySurfaceMeeting];
        self.panel = nil;
        self.caption = nil;
        return;
    }
    if (startingFromOffer) {
        [self.panel cancelDrag];
        for (NSView *view in self.panel.contentView.subviews) {
            if ([view isKindOfClass:NSButton.class]) ((NSButton *)view).enabled = NO;
            [view setAccessibilityElement:NO];
        }
        [self startRecordingPresentationTimer];
        return;
    }
    if (compact && self.recordingDisclosure) {
        if (self.waitingForCaptureEnd) {
            self.recordingActions.firstObject.title = @"Stopping…";
            [self.recordingActions.firstObject setAccessibilityLabel:@"Stopping the meeting recording"];
            self.recordingActions.firstObject.enabled = NO;
            [self updateCopyControls];
        }
        return;
    }
    if (self.recordingDisclosure && (completed || [self.phase isEqual:@"saving"])) {
        [self beginRecordingResult];
        [self updateCompletionContent];
        return;
    }
    BOOL appearing = !self.panel;
    NSSize size = compact ? NSMakeSize(recordingInitialWidth, recordingHeaderHeight) : completed ? NSMakeSize(388, 142) : NSMakeSize(360, 94);
    if (compact && self.offerDepartureAt && self.panel) size = self.panel.frame.size;
    if (offer) size = self.offerChoicesOpen ? NSMakeSize(self.offerAllowsAutomaticRecording ? 448 : 330, 96) : NSMakeSize(330, 73);
    BOOL changingOfferLayout = offer && [self.panel.contentView.identifier isEqual:@"meeting-offer"] &&
        !NSEqualSizes(self.panel.frame.size, size);
    if (appearing) {
        NSScreen *screen = [self indicatorScreen];
        NSRect frame = JardMeetingCornerFrame(screen.visibleFrame, size, self.panelCorner);
        self.panel = [[JardMeetingPanel alloc] initWithContentRect:frame
            styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
        self.panel.level = NSStatusWindowLevel;
        self.panel.opaque = NO;
        self.panel.backgroundColor = NSColor.clearColor;
        self.panel.hidesOnDeactivate = NO;
        self.panel.sharingType = NSWindowSharingNone;
        self.panel.title = @"Jard Meeting";
        __weak typeof(self) weak = self;
        self.panel.placementChanged = ^(NSString *corner, NSScreen *display) {
            [weak saveCorner:corner screen:display];
            [weak claimMeetingOverlayDockOnScreen:display];
        };
        self.panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    }
    [self.panel cancelDrag];
    if (!NSEqualSizes(self.panel.frame.size, size)) {
        NSScreen *screen = self.panel.screen ?: [self indicatorScreen];
        [self.panel setFrame:JardMeetingCornerFrame(screen.visibleFrame, size, self.panelCorner) display:YES
            animate:changingOfferLayout && !self.panel.reduceMotion];
    }
    if (!compact) [self stopRecordingMeter];
    self.panel.appearance = (live || completed) ? [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua] : nil;
    JardMeetingSurface *surface = [[JardMeetingSurface alloc] initWithFrame:NSMakeRect(0, 0, size.width, size.height)];
    surface.material = NSVisualEffectMaterialHUDWindow;
    surface.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    surface.state = NSVisualEffectStateActive;
    surface.wantsLayer = YES;
    surface.layer.cornerRadius = 14;
    surface.layer.masksToBounds = YES;
    self.panel.contentView = surface;
    self.caption = [JardMeetingCaption wrappingLabelWithString:self.message ?: @""];
    self.caption.font = [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold];
    self.caption.frame = NSMakeRect(14, 49, 332, 32);
    [surface addSubview:self.caption];
    if (completed) {
        self.caption.frame = NSMakeRect(14, 106, 327, 23);
        self.caption.maximumNumberOfLines = 1;
        self.caption.lineBreakMode = NSLineBreakByTruncatingTail;
        NSTextField *body = [JardMeetingCaption wrappingLabelWithString:@""];
        body.font = [NSFont systemFontOfSize:12];
        body.frame = NSMakeRect(14, 42, 360, 58);
        body.maximumNumberOfLines = 3;
        body.lineBreakMode = NSLineBreakByTruncatingTail;
        [surface addSubview:body];
        self.completionBody = body;
        NSButton *close = [self button:@"×" action:@selector(dismissCompletion:) area:NSMakeRect(348, 108, 28, 23) into:surface];
        [close setAccessibilityLabel:@"Close meeting capsule"];
        self.incrementalTextButton = [self button:@"Copy New" action:@selector(copyCompletedNewButton:)
            area:NSMakeRect(12, 10, 94, 27) into:surface];
        self.fullTextButton = [self button:@"Copy All" action:@selector(copyCompletedAllButton:)
            area:NSMakeRect(112, 10, 91, 27) into:surface];
        self.agentPromptButton = [self button:@"Copy Agent Prompt" action:@selector(copyMeetingAgentPrompt:)
            area:NSMakeRect(209, 10, 156, 27) into:surface];
        [self updateCompletionContent];
    } else if (permission) {
        [self button:@"Try Again" action:@selector(retryPermissionButton:) area:NSMakeRect(12, 10, 88, 28) into:surface];
        [self button:@"Open System Settings" action:@selector(privacyButton:) area:NSMakeRect(106, 10, 158, 28) into:surface];
        [self button:@"Not Now" action:@selector(dismissPermissionButton:) area:NSMakeRect(268, 10, 80, 28) into:surface];
    } else if (interrupted) {
        [self button:@"Recover Meeting" action:@selector(recoverButton:) area:NSMakeRect(12, 10, 135, 28) into:surface];
        [self button:@"Not Now" action:@selector(dismissRecoveryButton:) area:NSMakeRect(151, 10, 82, 28) into:surface];
    } else if (reminder) {
        NSString *policy = [self policyForCalendar:self.calendarOffer];
        BOOL automatic = self.detection && [policy isEqual:@"auto"], never = [policy isEqual:@"never"];
        if (automatic || never) {
            [self button:automatic ? @"Don’t Record" : @"Not Now" action:@selector(calendarDismiss:)
                area:NSMakeRect(12, 10, 105, 28) into:surface];
            if (self.calendarOffer.link) [self button:@"Join" action:@selector(calendarJoin:)
                area:NSMakeRect(126, 10, 82, 28) into:surface];
        } else {
            [self button:@"Not Now" action:@selector(calendarDismiss:) area:NSMakeRect(12, 10, 83, 28) into:surface];
            [self button:@"Record" action:@selector(calendarRecord:) area:NSMakeRect(101, 10, 78, 28) into:surface];
            if (self.calendarOffer.link) [self button:@"Join & Record" action:@selector(calendarJoinRecord:)
                area:NSMakeRect(186, 10, 146, 28) into:surface];
        }
    } else if (offer) {
        surface.identifier = @"meeting-offer";
        self.caption.frame = NSMakeRect(14, size.height - 30, size.width - 28, 19);
        self.caption.maximumNumberOfLines = 1;
        self.caption.lineBreakMode = NSLineBreakByTruncatingTail;
        if (self.offerChoicesOpen) {
            self.caption.stringValue = self.offered.label;
            self.caption.accessibilityLabel = self.offered.label;
            NSTextField *detail = [JardMeetingCaption wrappingLabelWithString:JardLocalized(@"You can change this in Settings.")];
            detail.font = [NSFont systemFontOfSize:12];
            detail.textColor = NSColor.secondaryLabelColor;
            detail.frame = NSMakeRect(14, 43, size.width - 28, 18);
            [surface addSubview:detail];
            CGFloat edge = size.width - 12;
            if (self.offerAllowsAutomaticRecording) {
                edge -= 142;
                [self button:JardLocalized(@"Always record") action:@selector(alwaysButton:) area:NSMakeRect(edge, 9, 142, 28) into:surface];
                edge -= 6;
            }
            edge -= 152;
            [self button:JardLocalized(@"Never offer") action:@selector(neverButton:) area:NSMakeRect(edge, 9, 152, 28) into:surface];
            [self button:JardLocalized(@"Cancel") action:@selector(cancelOfferChoices:) area:NSMakeRect(edge - 94, 9, 88, 28) into:surface];
        } else {
            NSButton *more = [self button:@"…" action:@selector(showOfferChoices:) area:NSMakeRect(12, 8, 36, 28) into:surface];
            more.toolTip = JardLocalized(@"Meeting recording options");
            more.accessibilityLabel = more.toolTip;
            [self button:JardLocalized(@"Not Now") action:@selector(dismissButton:) area:NSMakeRect(96, 8, 112, 28) into:surface];
            NSButton *record = [self button:JardLocalized(@"Record") action:@selector(recordButton:)
                area:NSMakeRect(216, 8, 102, 28) into:surface];
            record.font = [NSFont systemFontOfSize:NSFont.systemFontSize weight:NSFontWeightSemibold];
        }
    } else if (live) {
        [self attachRecordingMeterTo:surface];
        if (compact) {
            [self buildRecordingControlsIn:surface];
            if (self.waitingForCaptureEnd) {
                self.recordingActions.firstObject.title = @"Stopping…";
                [self.recordingActions.firstObject setAccessibilityLabel:@"Stopping the meeting recording"];
            }
        } else {
            self.caption.frame = NSMakeRect(100, 49, 246, 32);
            [self updateCaption];
            [self button:@"Keep Recording" action:@selector(keepButton:) area:NSMakeRect(12, 10, 136, 28) into:surface];
            [self button:@"Stop & Save" action:@selector(stopButton:) area:NSMakeRect(225, 10, 120, 28) into:surface];
        }
    }
    BOOL fade = appearing || (changingOfferLayout && self.panel.reduceMotion);
    if (fade) self.panel.alphaValue = 0;
    [self.panel orderFrontRegardless];
    [self claimMeetingOverlayDockOnScreen:self.panel.screen];
    if (fade) [NSAnimationContext runAnimationGroup:^(NSAnimationContext *animation) {
        animation.duration = self.panel.reduceMotion ? 0.08 : 0.16;
        self.panel.animator.alphaValue = 1;
    }];
}

- (void)shutdown {
    self.closed = YES;
    [self clearCompletion];
    [self.calendar shutdown];
    [self.timer invalidate];
    self.timer = nil;
    self.timerInterval = 0;
    [self stopRecordingMeter];
    [self.panel orderOut:nil];
    [JardOverlayDock.shared releaseSurface:JardOverlaySurfaceMeeting];
    self.panel = nil;
}
@end
