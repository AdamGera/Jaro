#import "jard_meeting_audio_access.h"
#import "jard_permission_return.h"
#import <dlfcn.h>

typedef int32_t (*JardAudioPreflight)(CFStringRef, CFDictionaryRef);
typedef void (*JardAudioRequest)(CFStringRef, CFDictionaryRef, void (^)(BOOL));

typedef struct {
    JardAudioPreflight preflight;
    JardAudioRequest request;
} JardAudioConsent;

static JardAudioConsent audioConsent(void) {
    static dispatch_once_t once;
    static JardAudioConsent consent;
    dispatch_once(&once, ^{
        // CoreAudio has no public preflight for audio-only recording. Preserve
        // the public ScreenCaptureKit path when the consent API is unavailable.
        void *library = dlopen("/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC", RTLD_LAZY | RTLD_LOCAL);
        if (!library) return;
        consent.preflight = (JardAudioPreflight)dlsym(library, "TCCAccessPreflight");
        consent.request = (JardAudioRequest)dlsym(library, "TCCAccessRequest");
    });
    return consent;
}

typedef void (^AudioConsentReply)(BOOL);

@interface JardMeetingAudioAccess ()
@property(nonatomic) BOOL visitingSettings, sawSettings, resetting;
@property(nonatomic) NSTimeInterval returnedAt;
@property(nonatomic, strong) NSMutableArray<AudioConsentReply> *waiting;
@end

@implementation JardMeetingAudioAccess

- (instancetype)init {
    if (!(self = [super init])) return nil;
    [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(applicationActivated:)
        name:NSWorkspaceDidActivateApplicationNotification object:nil];
    return self;
}

- (void)dealloc {
    [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self];
}

- (int32_t)tapStatus {
    JardAudioPreflight preflight = audioConsent().preflight;
    return preflight ? preflight(CFSTR("kTCCServiceAudioCapture"), NULL) : -1;
}

- (BOOL)screenAllowed { return CGPreflightScreenCaptureAccess(); }
- (NSTimeInterval)now { return NSProcessInfo.processInfo.systemUptime; }
- (NSString *)bundleIdentifier { return NSBundle.mainBundle.bundleIdentifier; }

- (BOOL)requestTap:(AudioConsentReply)reply {
    JardAudioRequest request = audioConsent().request;
    if (!request) return NO;
    request(CFSTR("kTCCServiceAudioCapture"), NULL, ^(BOOL granted) {
        dispatch_async(dispatch_get_main_queue(), ^{ reply(granted); });
    });
    return YES;
}

- (BOOL)askForTap:(AudioConsentReply)reply {
    if (self.waiting) { [self.waiting addObject:[reply copy]]; return YES; }
    self.waiting = [NSMutableArray arrayWithObject:[reply copy]];
    if ([self requestTap:^(BOOL granted) {
        NSArray<AudioConsentReply> *waiting = self.waiting;
        self.waiting = nil;
        for (AudioConsentReply answer in waiting) answer(granted);
    }]) return YES;
    self.waiting = nil;
    return NO;
}

- (void)checkBackendWithPrompt:(BOOL)prompt screenRequest:(JardScreenAudioRequest)screenRequest
                        reply:(void (^)(NSString *))reply {
    if (self.resetting) prompt = NO;
    int32_t status = [self tapStatus];
    if (status == 0) { reply([self screenAllowed] ? @"tapAndScreen" : @"tap"); return; }
    if (prompt && status > 1 && [self askForTap:^(BOOL granted) {
        BOOL screen = [self screenAllowed];
        reply(granted ? (screen ? @"tapAndScreen" : @"tap") : (screen ? @"screen" : nil));
    }]) return;
    if ([self screenAllowed]) { reply(@"screen"); return; }
    if (prompt && status != 1 && screenRequest) {
        screenRequest(^(BOOL granted) { reply(granted ? @"screen" : nil); });
    } else reply(nil);
}

- (void)observeSettingsFront:(BOOL)settingsFront active:(BOOL)active {
    if (!self.visitingSettings) return;
    if (settingsFront) self.sawSettings = YES;
    if (!active || settingsFront) self.returnedAt = 0;
    else if (self.sawSettings && !self.returnedAt) self.returnedAt = [self now];
}

- (void)observeSettings {
    if (!self.visitingSettings) return;
    NSString *front = NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier;
    [self observeSettingsFront:[front isEqual:JardSystemSettingsBundle] active:NSApp.active];
}

- (void)applicationActivated:(NSNotification *)note {
    NSRunningApplication *application = note.userInfo[NSWorkspaceApplicationKey];
    BOOL settingsFront = [application.bundleIdentifier isEqual:JardSystemSettingsBundle];
    BOOL active = application.processIdentifier == NSProcessInfo.processInfo.processIdentifier;
    [self observeSettingsFront:settingsFront active:active];
}

- (void)endSettingsVisit {
    self.visitingSettings = self.sawSettings = NO;
    self.returnedAt = 0;
}

- (int32_t)observeTapPermission {
    int32_t tap = [self tapStatus];
    if (tap == 0) {
        BOOL requested = self.visitingSettings;
        [self endSettingsVisit];
        if (requested && self.settingsVisitChanged) self.settingsVisitChanged(YES);
    } else [self observeSettings];
    return tap;
}

- (BOOL)pollSettingsVisit {
    if (self.visitingSettings && !self.resetting) [self observeTapPermission];
    return self.visitingSettings;
}

- (NSDictionary *)status {
    int32_t tap = [self observeTapPermission];
    NSString *authorization = tap == 0 ? @"granted" : tap == 1 ? @"denied" : tap > 1 ? @"request" : @"unknown";
    BOOL canRepair = tap > 0 && self.returnedAt && [self now] - self.returnedAt >= 2;
    return @{@"authorization": authorization, @"screen": @([self screenAllowed]),
        @"canRepair": @(canRepair), @"busy": @(self.waiting != nil || self.resetting)};
}

- (BOOL)openSettings {
    return [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:
        @"x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"]];
}

- (BOOL)showSettings {
    [self endSettingsVisit];
    BOOL missing = [self tapStatus] != 0;
    if (![self openSettings]) return NO;
    self.visitingSettings = missing;
    if (missing && self.settingsVisitChanged) self.settingsVisitChanged(NO);
    return YES;
}

- (void)allow:(JardDictationReply)reply {
    int32_t tap = [self tapStatus];
    if (tap == 0) { reply([self status], nil); return; }
    if (tap > 1 && [self askForTap:^(__unused BOOL granted) { reply([self status], nil); }]) return;
    if ([self showSettings]) reply([self status], nil);
    else reply(nil, @"Open Privacy & Security in System Settings to allow Mac audio.");
}

- (void)resetBundle:(NSString *)bundle reply:(void (^)(NSString *error))reply {
    NSTask *task = NSTask.new;
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/tccutil"];
    task.arguments = @[@"reset", @"AudioCapture", bundle];
    task.standardOutput = task.standardError = NSFileHandle.fileHandleWithNullDevice;
    task.terminationHandler = ^(NSTask *process) {
        NSString *error = process.terminationStatus == 0 ? nil : @"Could not reset Mac audio access. Check the audio-only list in System Settings.";
        dispatch_async(dispatch_get_main_queue(), ^{ reply(error); });
    };
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) reply(error.localizedDescription ?: @"Could not reset Mac audio access.");
}

- (void)request:(NSDictionary *)parameters reply:(JardDictationReply)reply {
    NSString *action = parameters[@"action"];
    NSDictionary *status = [self status];
    if ([action isEqual:@"status"]) { reply(status, nil); return; }
    if ([status[@"busy"] boolValue]) { reply(nil, @"A Mac audio access request is still open."); return; }
    if ([action isEqual:@"allow"]) { [self allow:reply]; return; }
    if (![action isEqual:@"repair"]) { reply(nil, @"Unknown Mac audio access action."); return; }
    NSString *bundle = [self bundleIdentifier];
    if (![status[@"canRepair"] boolValue] || !bundle.length) {
        reply(nil, @"Mac audio repair is available after returning from System Settings with audio-only access still off.");
        return;
    }
    self.resetting = YES;
    [self resetBundle:bundle reply:^(NSString *error) {
        self.resetting = NO;
        if (error) { reply(nil, error); return; }
        [self endSettingsVisit];
        [self allow:reply];
    }];
}

@end
