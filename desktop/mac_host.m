#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <Carbon/Carbon.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreGraphics/CoreGraphics.h>
#import <IOKit/IOKitLib.h>
#import <IOKit/pwr_mgt/IOPM.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <WebKit/WebKit.h>
#import <signal.h>
#import <errno.h>
#import <unistd.h>
#import "appkit_host.h"
#import "system_dictation.h"
#import "jard_login_item.h"
#import "jard_updater.h"
#import "jard_annotation.h"
#import "jard_annotation_access.h"
#import "jard_annotation_result_panel.h"
#import "jard_meetings.h"
#import "jard_meeting_audio_access.h"
#import "jard_interface_language.h"
#import "jard_permission_return.h"

#define SDK_BYTES(text) (text).UTF8String, [(text) lengthOfBytesUsingEncoding:NSUTF8StringEncoding]

static char statusItemVisibilityContext;
static NSString *const updatesManagedMessage = @"Updates managed by your organization";
static NSString *const annotationManagedMessage = @"Screen annotation is managed by your organization.";
static NSString *const updateRollbackMessage = @"The update could not start. Jaro restored the previous version.";
static const AudioObjectPropertyAddress inputDeviceProperties[] = {
    {kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain},
    {kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain},
    {kAudioHardwarePropertyServiceRestarted, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain},
};

typedef NS_ENUM(NSUInteger, JardCapturePermission) {
    JardNoCapturePermission, JardScreenRecordingPermission, JardMacAudioPermission
};

@class JardHost;

@interface JardTerminationDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, weak) JardHost *host;
@property(nonatomic, weak) id<NSApplicationDelegate> upstream;
@end

static BOOL JardForcedBoolean(CFStringRef key) {
    CFStringRef domain = CFSTR("local.jardaudio.desktop");
    if (!CFPreferencesAppValueIsForced(key, domain)) return NO;
    CFPropertyListRef value = CFPreferencesCopyAppValue(key, domain);
    if (!value) return NO;
    BOOL enabled = CFGetTypeID(value) == CFBooleanGetTypeID() && CFBooleanGetValue(value);
    CFRelease(value);
    return enabled;
}

@interface JardHost : NSObject <NSMenuDelegate>
@property(nonatomic, assign) native_sdk_appkit_host_t *host;
@property(nonatomic, strong) NSURL *bridgeURL;
@property(nonatomic, copy) NSString *origin;
@property(nonatomic, strong) NSTask *bridge;
@property(nonatomic, strong) NSFileHandle *input;
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSString *> *pending;
@property(nonatomic, strong) NSMutableDictionary<NSString *, JardDictationReply> *nativePending;
@property(nonatomic) BOOL preparing;
@property(nonatomic, strong) NSMutableArray<JardDictationReply> *prepareWaiters;
@property(nonatomic, strong) JardSystemDictation *systemDictation;
@property(nonatomic, strong) JardLoginItem *loginItem;
@property(nonatomic, strong) JardUpdater *updater;
@property(nonatomic, strong) JardAnnotation *annotation;
@property(nonatomic, strong) JardAnnotationAccess *annotationAccess;
@property(nonatomic) BOOL annotationManaged;
@property(nonatomic, strong) JardAnnotationResultPanel *annotationResultPanel;
@property(nonatomic, strong) JardAnnotationResultPanel *annotationRoutePanel;
@property(nonatomic, strong) JardMeetings *meetings;
@property(nonatomic, strong) JardMeetingAudioAccess *meetingAudioAccess;
@property(nonatomic, strong) NSMenuItem *updateMenu;
@property(nonatomic, strong) NSMenuItem *automaticUpdatesMenu;
@property(nonatomic, strong) NSStatusItem *meetingStatusItem;
@property(nonatomic, strong) NSDictionary<NSString *, NSMenuItem *> *meetingMenuItems;
@property(nonatomic, copy) NSDictionary *meetingMenuStatus;
@property(nonatomic, strong) NSTimer *updateTimer;
@property(nonatomic, strong) NSTimer *capturePermissionTimer;
@property(nonatomic, copy) AudioObjectPropertyListenerBlock inputDeviceListener;
@property(nonatomic) NSUInteger inputDeviceNotifications;
@property(nonatomic) IONotificationPortRef lidPort;
@property(nonatomic) io_object_t lidNotification;
@property(nonatomic) BOOL inputDeviceEventPending;
@property(nonatomic) JardCapturePermission capturePermissionVisit;
@property(nonatomic) NSTimeInterval capturePermissionOpenedAt, capturePermissionReturnDeadline;
@property(nonatomic) BOOL updating, manualUpdateCheck, manualUpdateResult;
@property(nonatomic) BOOL updatesDisabledAtLaunch, updateRestartBlocked;
@property(nonatomic) BOOL updateRestartInProgress;
@property(nonatomic, copy) NSString *updateRestartError;
@property(nonatomic, copy) NSString *updateSettingsRequest;
@property(nonatomic, copy) NSDictionary *windowNavigation;
@property(nonatomic, copy) JardDictationReply pendingUpdateRestart;
@property(nonatomic) BOOL showingApp, closing;
@property(nonatomic) BOOL updateStartupReady, closingFailedUpdate;
@property(nonatomic) BOOL menuQuitPending;
@property(nonatomic) BOOL systemQuitPending, terminationApproved;
@property(nonatomic, strong) JardTerminationDelegate *terminationDelegate;
@property(nonatomic) BOOL launchHandled, quietStartupPending, hiddenLaunchRequested;
@property(nonatomic) NSTimeInterval startupActivationDeadline;
@property(nonatomic) BOOL otherRecording, otherRecordingCapturesSystemAudio;
@property(nonatomic, copy) NSString *sdkCaptureID;
@property(nonatomic, copy) NSString *controlExitID;
@property(nonatomic, weak) NSWindow *window;
@property(nonatomic, copy) NSString *workspaceLaunchURL;
@property(nonatomic) NSTimeInterval lastWorkspaceReload;
@property(nonatomic, weak) WKWebView *failedWorkspace;
@property(nonatomic) BOOL workspaceRecoveryShowing;
@property(nonatomic, strong) id dragMonitor;
@property(nonatomic) CGFloat workspaceToolbarInset;
@property(nonatomic, strong) NSMapTable<NSMenuItem *, NSString *> *menuLabels;
@property(nonatomic, copy) NSString *menuLanguage, *selectedInterfaceLanguage;
- (NSApplicationTerminateReply)requestSystemTermination;
- (void)terminateAfterCleanup;
- (void)queueInputDeviceEvent;
- (void)registerInputDeviceProperties;
- (void)removeInputDeviceProperties;
@end

static void lidStateChanged(void *context, io_service_t service, natural_t message, void *argument) {
    if (message == kIOPMMessageClamshellStateChange) [(__bridge JardHost *)context queueInputDeviceEvent];
}

@implementation JardTerminationDelegate

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)sender {
    return self.host ? [self.host requestSystemTermination] : NSTerminateNow;
}

- (BOOL)respondsToSelector:(SEL)selector {
    return [super respondsToSelector:selector] || [self.upstream respondsToSelector:selector];
}

- (id)forwardingTargetForSelector:(SEL)selector {
    if ([self.upstream respondsToSelector:selector]) return self.upstream;
    return [super forwardingTargetForSelector:selector];
}

@end

@implementation JardHost

- (void)respond:(NSDictionary *)response {
    if ([response[@"id"] isEqual:@"native.browser"]) {
        if (![response[@"ok"] boolValue]) {
            NSAlert *alert = [[NSAlert alloc] init];
            alert.messageText = @"Could not open the browser";
            alert.informativeText = response[@"error"][@"message"] ?: @"The local engine is unavailable.";
            [alert beginSheetModalForWindow:NSApp.mainWindow completionHandler:nil];
        }
        return;
    }
    NSData *json = [NSJSONSerialization dataWithJSONObject:response options:0 error:nil];
    if (json && !self.closing) native_sdk_appkit_bridge_respond(self.host, json.bytes, json.length);
}

- (void)reject:(NSString *)request reason:(NSString *)reason {
    [self respond:@{@"id": request, @"ok": @NO, @"error": @{@"code": @"bridge_error", @"message": reason}}];
}

- (void)received:(NSDictionary *)line {
    if (self.closing || ![line isKindOfClass:NSDictionary.class]) return;
    if ([self.controlExitID isEqual:line[@"desktopExitAccepted"]]) {
        [self terminateAfterCleanup];
        return;
    }
    if ([self.sdkCaptureID isEqual:line[@"sdkCaptureEnded"]]) {
        [self setOwnedCapture:nil];
        return;
    }
    NSDictionary *request = [line[@"nativeRequest"] isKindOfClass:NSDictionary.class] ? line[@"nativeRequest"] : nil;
    if (request) {
        NSString *identifier = request[@"id"];
        NSString *action = request[@"action"];
        if (![identifier isKindOfClass:NSString.class] || ![action isKindOfClass:NSString.class]) return;
        JardDictationReply reply = ^(id result, NSString *error) {
            if (!error && [action isEqual:@"status"]) {
                NSMutableDictionary *status = [result mutableCopy];
                [status addEntriesFromDictionary:[self desktopStatus]];
                result = status;
            }
            NSDictionary *response = error ? @{@"ok": @NO, @"error": error} : @{@"ok": @YES, @"result": result ?: @{}};
            [self sendToHelper:@"jard.controlReply" payload:@{@"id": identifier, @"response": response} request:nil completion:nil];
        };
        if ([action isEqual:@"setup"]) {
            [self replayDictationSetupShowingWindow:YES reply:reply];
        } else if ([action isEqual:@"sdk-start"]) {
            [self approveSDKCapture:identifier];
        } else if ([action isEqual:@"desktop-status"]) {
            reply([self desktopStatus], nil);
        } else if ([action isEqual:@"desktop-quit"] || [action isEqual:@"desktop-restart"]) {
            [self prepareControlExit:identifier restart:[action isEqual:@"desktop-restart"]
                               force:[request[@"force"] isEqual:@YES] reply:reply];
        } else if ([action isEqual:@"window-show"] || [action isEqual:@"window-hide"]) {
            [self controlWindowAction:action pane:request[@"pane"] reply:reply];
        } else if ([action hasPrefix:@"settings-"] || [action hasPrefix:@"vocabulary-"] || [action isEqual:@"launch-at-login"]) {
            if (self.updating) { reply(nil, @"Jard is preparing to restart."); return; }
            if ([action isEqual:@"launch-at-login"]) {
                NSDictionary *operation = request[@"enabled"] ? @{@"action": @"set", @"enabled": request[@"enabled"]} : @{@"action": @"status"};
                [self.loginItem request:operation reply:^(NSDictionary *result, NSString *error) {
                    NSString *failure = error ?: result[@"message"];
                    reply(failure ? nil : @{@"enabled": @([result[@"state"] isEqual:@"enabled"])}, failure);
                }];
            } else {
                if ([action isEqual:@"settings-apply"] && JardChangesAnnotationSettings(request[@"changes"])) {
                    if (self.annotationManaged) { reply(nil, annotationManagedMessage); return; }
                    if (self.closing || (JardChangesAnnotationInputSettings(request[@"changes"]) && [self hasActiveCapture])) {
                        reply(nil, @"Finish the current recording before changing Annotate settings.");
                        return;
                    }
                }
                [self applyControlSettings:request reply:reply];
            }
            [self updateWindowClosePolicy];
        } else if ([action isEqual:@"check-for-updates"]) {
            [self performUpdateAction:@"check" payload:request reply:reply];
        } else if ([action isEqual:@"update-channel"]) {
            NSString *operation = request[@"channel"] ? @"channel" : request[@"offer"] ? @"confirmSwitch" : @"status";
            [self performUpdateAction:operation payload:request reply:reply];
        } else if ([action isEqual:@"history-checkpoint"]) {
            NSString *path = request[@"path"];
            if (![path isKindOfClass:NSString.class] || !path.isAbsolutePath) {
                reply(nil, @"The private dictation history file is unavailable.");
                return;
            }
            [self.systemDictation writeHistorySnapshot:path reply:reply];
        } else if ([action isEqual:@"history-clear"]) {
            if (self.updating) { reply(nil, @"Jaro is preparing to restart."); return; }
            [self.systemDictation control:action reply:reply];
        } else if ([action isEqual:@"history"]) {
            [self.systemDictation request:@{@"action": @"history", @"query": request[@"query"] ?: @"",
                @"offset": request[@"offset"] ?: @0} reply:reply];
        } else if ([action hasPrefix:@"annotation-"]) {
            [self controlAnnotation:[action substringFromIndex:@"annotation-".length] parameters:request reply:reply];
        } else if ([action isEqual:@"meeting-record"]) {
            [self.meetings startWithEstablishedPermissionsTitle:request[@"title"] reply:reply];
        } else if ([action isEqual:@"meeting-copy-last"]) {
            [self.meetings copyLatestTranscript:reply];
        } else {
            NSString *meetingAction = @{@"meeting-status": @"status", @"meeting-stop": @"stop"}[action];
            if (meetingAction) [self.meetings request:@{@"action": meetingAction} reply:reply];
            else [self.systemDictation control:action reply:reply];
        }
        return;
    }
    NSDictionary *response = line[@"response"];
    if ([response isKindOfClass:NSDictionary.class] && [response[@"id"] isKindOfClass:NSString.class]) {
        NSString *key = response[@"id"];
        JardDictationReply completion = self.nativePending[key];
        if (completion) {
            [self.nativePending removeObjectForKey:key];
            completion(response[@"result"], [response[@"ok"] boolValue] ? nil :
                (response[@"error"][@"message"] ?: @"The local helper could not finish dictation."));
            return;
        }
        NSString *original = self.pending[key];
        if (!original) return;
        [self.pending removeObjectForKey:key];
        NSMutableDictionary *reply = [response mutableCopy];
        reply[@"id"] = original;
        [self respond:reply];
    } else if ([line[@"navigate"] isEqual:@"app"] && !self.showingApp) {
        // The helper proves engine identity and library ownership before sending this signal.
        // Build the destination locally. A helper message never supplies the navigation URL.
        NSString *key = [line[@"workspaceKey"] isKindOfClass:NSString.class] ? line[@"workspaceKey"] : nil;
        NSCharacterSet *invalid = [NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdef"].invertedSet;
        if (key.length != 64 || [key rangeOfCharacterFromSet:invalid].location != NSNotFound) return;
        self.showingApp = YES;
        [self.systemDictation workspaceReady];
        [self.meetings workspaceReady];
        [self updateMeetingMenu:nil];
        if (self.quietStartupPending && (!self.systemDictation.quietAtLogin || ![self canOpenFromMenuBar]))
            [self showStartupWindowIfHidden];
        self.quietStartupPending = NO;
        [self.pending removeAllObjects];
        self.workspaceLaunchURL = [self.origin stringByAppendingFormat:@"/_desktop/connect#%@", key];
        native_sdk_appkit_load_webview(self.host, SDK_BYTES(self.workspaceLaunchURL), 1, NULL, 0, NULL, 0, NULL, 0, 0);
    } else if ([line[@"event"] isEqual:@"jard-microphone"]) {
        id detail = line[@"detail"];
        if ([detail isKindOfClass:NSDictionary.class]) [self.systemDictation updateWarmInputStatus:detail];
    } else if ([line[@"event"] isEqual:@"jard-annotation-confirmation"]) {
        [self confirmAnnotationDelivery:line[@"detail"]];
    } else if ([line[@"event"] isEqual:@"setup"] || [line[@"event"] isEqual:@"jard-capture"] ||
               [line[@"event"] isEqual:@"jard-annotation-routing"] || [line[@"event"] isEqual:@"jard-annotation-delivery-changed"]) {
        NSString *event = line[@"event"];
        NSDictionary *capture = [line[@"detail"] isKindOfClass:NSDictionary.class] ? line[@"detail"] : nil;
        if ([event isEqual:@"jard-annotation-routing"] && capture &&
            ![self.annotationResultPanel updateRouting:capture] && ![self.annotationRoutePanel updateRouting:capture])
            [self.annotationRoutePanel showRoute:capture];
        if ([event isEqual:@"jard-capture"]) {
            [self.meetings handleCapture:capture];
            if (!capture[@"meeting"] && !capture[@"dictation"]) {
                NSString *kind = capture[@"event"];
                if ([kind isEqual:@"started"]) {
                    self.otherRecording = YES;
                    NSString *mode = [capture[@"mode"] isKindOfClass:NSString.class] ? capture[@"mode"] : @"";
                    self.otherRecordingCapturesSystemAudio = [mode isEqual:@"system"] || [mode isEqual:@"both"];
                } else if ([@[@"accepted", @"error"] containsObject:kind ?: @""]) {
                    self.otherRecording = NO;
                    self.otherRecordingCapturesSystemAudio = NO;
                }
                [self.systemDictation setSystemAudioRecordingActive:[self isRecordingSystemAudio]];
                [self refreshMeetingAvailability];
            }
            if ([self.systemDictation handleCapture:capture]) return;
        }
        NSData *detail = [NSJSONSerialization dataWithJSONObject:line[@"detail"] ?: NSNull.null
                                                         options:NSJSONWritingFragmentsAllowed error:nil];
        if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(event), detail.bytes, detail.length);
    }
}

- (void)applyControlSettings:(NSDictionary *)request reply:(JardDictationReply)reply {
    [self.systemDictation controlSettings:request reply:^(id result, NSString *error) {
        NSDictionary *changes = [request[@"action"] isEqual:@"settings-apply"] && !error ? request[@"changes"] : nil;
        if (changes[JardShowDockIconSettingKey]) [self refreshDockIconPreference];
        if (changes[JardInterfaceLanguageSettingKey]) {
            [self refreshInterfaceLanguageWithCompletion:^{ reply(result, nil); }];
        } else {
            reply(result, error);
        }
    }];
}

- (BOOL)startBridge:(NSError **)error {
    if (self.bridge.running) return YES;
    NSTask *child = [[NSTask alloc] init];
    child.executableURL = self.bridgeURL;
    NSMutableDictionary *environment = [NSProcessInfo.processInfo.environment mutableCopy];
    environment[@"JARD_UI_EXECUTABLE"] = self.bridgeURL.path;
    environment[@"JARD_UI_INSTANCE"] = @(NSProcessInfo.processInfo.processIdentifier).stringValue;
    environment[@"JARD_MEETING_CLOCK"] = [JardInterfaceLanguageStatus()[@"resolved"] isEqual:@"en"] ? @"12" : @"24";
    child.environment = environment;
    NSPipe *input = NSPipe.pipe;
    NSPipe *output = NSPipe.pipe;
    child.standardInput = input;
    child.standardOutput = output;
    if (![child launchAndReturnError:error]) return NO;
    self.bridge = child;
    self.input = input.fileHandleForWriting;
    NSFileHandle *reader = output.fileHandleForReading;
    // Keep reading status messages off the window thread, including during long model installs.
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSMutableData *buffer = NSMutableData.data;
        while (YES) {
            @autoreleasepool {
                uint8_t chunk[16384];
                ssize_t count = read(reader.fileDescriptor, chunk, sizeof(chunk));
                if (count < 0 && errno == EINTR) continue;
                if (count <= 0) break;
                [buffer appendBytes:chunk length:(NSUInteger)count];
                const uint8_t *bytes = buffer.bytes;
                NSUInteger consumed = 0;
                for (NSUInteger i = 0; i < buffer.length; i++) {
                    if (bytes[i] != '\n') continue;
                    NSData *line = [buffer subdataWithRange:NSMakeRange(consumed, i - consumed)];
                    consumed = i + 1;
                    id data = [NSJSONSerialization JSONObjectWithData:line options:0 error:nil];
                    if (data) dispatch_async(dispatch_get_main_queue(), ^{ if (self.bridge == child) [self received:data]; });
                }
                if (consumed) [buffer replaceBytesInRange:NSMakeRange(0, consumed) withBytes:NULL length:0];
                if (buffer.length > 4 * 1024 * 1024) break;
            }
        }
        dispatch_async(dispatch_get_main_queue(), ^{
            if (self.bridge != child || self.closing) return;
            self.bridge = nil;
            [self.input closeAndReturnError:nil];
            self.input = nil;
            for (NSString *request in self.pending.allValues) [self reject:request reason:@"The local helper stopped. Try the action again."];
            [self.pending removeAllObjects];
            NSArray *completions = self.nativePending.allValues;
            [self.nativePending removeAllObjects];
            for (JardDictationReply completion in completions) completion(nil, @"The local helper stopped. Try again.");
            if (self.preparing || self.prepareWaiters.count)
                [self finishPrepareWithResult:nil error:@"The local helper stopped. Try again."];
            [self.annotationResultPanel dismiss];
            [self.annotationRoutePanel dismiss];
            [self.systemDictation helperDisconnected];
            [self.meetings bridgeDisconnected];
            self.otherRecording = NO;
            self.otherRecordingCapturesSystemAudio = NO;
            [self.systemDictation setSystemAudioRecordingActive:NO];
            if (self.controlExitID) { [self terminateAfterCleanup]; return; }
            [self setOwnedCapture:nil];
            [self refreshMeetingAvailability];
        });
    });
    return YES;
}

- (NSDictionary *)chrome {
    double top, left, bottom, right, x, y, width, height;
    native_sdk_appkit_window_chrome_insets(self.host, 1, &top, &left, &bottom, &right, &x, &y, &width, &height);
    return @{@"controlsEnd": @(left), @"controlsCenterY": @(y + height / 2),
             @"fullscreen": @((self.window.styleMask & NSWindowStyleMaskFullScreen) != 0)};
}

- (void)chromeChanged:(NSNotification *)notification {
    NSDictionary *geometry = [self chrome];
    NSData *detail = [NSJSONSerialization dataWithJSONObject:geometry options:0 error:nil];
    native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-chrome"), detail.bytes, detail.length);
}

- (void)completeWindowLaunch:(NSAppleEventDescriptor *)event {
    if (!self.terminationDelegate && NSApp.delegate) {
        self.terminationDelegate = JardTerminationDelegate.new;
        self.terminationDelegate.host = self;
        self.terminationDelegate.upstream = NSApp.delegate;
        NSApp.delegate = self.terminationDelegate;
    }
    if (self.launchHandled) return;
    self.launchHandled = YES;
    BOOL login = event.eventClass == kCoreEventClass && event.eventID == kAEOpenApplication &&
        [event paramDescriptorForKeyword:keyAEPropData].enumCodeValue == keyAELaunchedAsLogInItem;
    BOOL menuOnly = self.hiddenLaunchRequested || ![self showDockIcon];
    if (menuOnly) [self updateMeetingMenu:nil];
    BOOL canStayQuiet = !menuOnly || [self canOpenFromMenuBar];
    BOOL quiet = canStayQuiet && (self.hiddenLaunchRequested || ((login || menuOnly) && self.systemDictation.quietAtLogin));
    if (quiet && !self.window.visible) {
        self.quietStartupPending = !self.hiddenLaunchRequested && !self.showingApp;
        [self updateWindowClosePolicy];
        // AppKit can send an activation along with a background launch.
        self.startupActivationDeadline = NSProcessInfo.processInfo.systemUptime + 1;
    } else if (!self.window.visible) {
        [self showWorkspace];
    }
}

- (void)applicationStarted:(NSNotification *)notification {
    [self completeWindowLaunch:NSAppleEventManager.sharedAppleEventManager.currentAppleEvent];
}

- (void)showStartupWindowIfHidden {
    if (!self.window.visible && !self.window.miniaturized && !NSApp.hidden)
        [self showWorkspace];
}

- (void)setDockIconVisible:(BOOL)visible {
    NSApplicationActivationPolicy policy = visible ? NSApplicationActivationPolicyRegular : NSApplicationActivationPolicyAccessory;
    if (NSApp.activationPolicy != policy && [NSApp setActivationPolicy:policy])
        [self.systemDictation applicationActivationPolicyChanged];
}

- (void)finishBackgroundStartup {
    self.quietStartupPending = NO;
    if (self.hiddenLaunchRequested) {
        self.hiddenLaunchRequested = NO;
        self.meetingMenuItems[@"quit"].hidden = [self showDockIcon];
        [self updateWindowClosePolicy];
    }
}

- (void)showWorkspace {
    [self finishBackgroundStartup];
    [self setDockIconVisible:YES];
    native_sdk_appkit_show_window(self.host, 1);
}

- (void)replayDictationSetupShowingWindow:(BOOL)show reply:(JardDictationReply)reply {
    if (self.updating || self.updateRestartInProgress || self.closing) {
        reply(nil, @"Jaro is preparing to close.");
        return;
    }
    if ([self hasActiveCapture]) {
        reply(nil, @"Finish the current recording before replaying dictation setup.");
        return;
    }
    if (show && !self.window) {
        reply(nil, @"The Jaro workspace is unavailable.");
        return;
    }
    [self.systemDictation request:@{@"action": @"set", @"key": @"onboarding", @"value": @"welcome"}
                           reply:^(id result, NSString *error) {
        if (!error) {
            [self updateWindowClosePolicy];
            if (show) {
                [self showWorkspace];
                [NSApp activate];
                if (self.showingApp) {
                    NSData *detail = [NSJSONSerialization dataWithJSONObject:result options:0 error:nil];
                    if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-dictation-setup"), detail.bytes, detail.length);
                }
            }
        }
        reply(result, error);
    }];
}

- (BOOL)canReturnFromPermissionSettings {
    NSWindow *window = self.window;
    return self.showingApp && window.visible && !window.miniaturized && !NSApp.hidden && !self.closing &&
        !self.updating && !self.updateRestartInProgress && !self.systemDictation.sessionUnavailable && ![self hasActiveCapture];
}

- (void)returnFromPermissionSettings {
    if (![self canReturnFromPermissionSettings] || NSApp.active) return;
    NSString *foreground = NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier;
    if (![foreground isEqual:JardSystemSettingsBundle]) return;
    [NSApp activate];
    [self.window makeKeyAndOrderFront:nil];
}

- (void)endCapturePermissionWatch {
    [self.capturePermissionTimer invalidate];
    self.capturePermissionTimer = nil;
    self.capturePermissionVisit = JardNoCapturePermission;
    self.capturePermissionReturnDeadline = 0;
}

- (void)pollCapturePermission {
    if (![self canReturnFromPermissionSettings]) { [self endCapturePermissionWatch]; return; }
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    BOOL settingsOpen = [NSRunningApplication runningApplicationsWithBundleIdentifier:JardSystemSettingsBundle].count > 0;
    if ((!settingsOpen || NSApp.active) && now - self.capturePermissionOpenedAt > JardPermissionReturnGrace) {
        [self endCapturePermissionWatch];
        return;
    }
    BOOL visiting = self.capturePermissionVisit == JardScreenRecordingPermission ?
        [self.annotationAccess pollSettingsVisit] : [self.meetingAudioAccess pollSettingsVisit];
    if (self.capturePermissionReturnDeadline) {
        BOOL settingsFront = [NSWorkspace.sharedWorkspace.frontmostApplication.bundleIdentifier isEqual:JardSystemSettingsBundle];
        if (JardTakePermissionReturn(&_capturePermissionReturnDeadline, now, settingsOpen, settingsFront, NSApp.active))
            [self returnFromPermissionSettings];
    }
    if (!visiting && !self.capturePermissionReturnDeadline) [self endCapturePermissionWatch];
}

- (void)capturePermissionVisitChanged:(JardCapturePermission)permission granted:(BOOL)granted {
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    if (granted) {
        if (self.capturePermissionVisit == permission) self.capturePermissionReturnDeadline = now + JardPermissionReturnGrace;
        return;
    }
    if (![self canReturnFromPermissionSettings]) { [self endCapturePermissionWatch]; return; }
    self.capturePermissionVisit = permission;
    self.capturePermissionOpenedAt = now;
    self.capturePermissionReturnDeadline = 0;
    if (self.capturePermissionTimer) return;
    __weak typeof(self) owner = self;
    self.capturePermissionTimer = [NSTimer timerWithTimeInterval:0.25 repeats:YES block:^(NSTimer *timer) {
        [owner pollCapturePermission];
    }];
    self.capturePermissionTimer.tolerance = 0.04;
    [NSRunLoop.mainRunLoop addTimer:self.capturePermissionTimer forMode:NSRunLoopCommonModes];
}

- (void)controlWindowAction:(NSString *)action pane:(NSString *)pane reply:(JardDictationReply)reply {
    if (self.updating || self.closing) { reply(nil, @"Jaro is preparing to close."); return; }
    if (!self.window) { reply(nil, @"The Jaro workspace is unavailable."); return; }
    if ([action isEqual:@"window-hide"]) {
        BOOL wasVisible = self.window.visible && !self.window.miniaturized;
        if (!self.window.miniaturized) native_sdk_appkit_hide_window(self.host, 1);
        [self updateDockPresentation];
        reply(@{@"windowVisible": @NO, @"changed": @(wasVisible)}, nil);
        return;
    }
    if ([pane isEqual:@"live-meeting"] && !self.meetings.recording) {
        reply(nil, @"There is no live meeting transcript.");
        return;
    }
    if (![@[@"settings", @"general", @"dictation", @"history", @"output", @"meetings", @"annotate", @"home", @"live-meeting", @"meeting-library", @"vocabulary", @"add-term"] containsObject:pane ?: NSNull.null]) {
        reply(nil, @"Unknown Jaro workspace pane.");
        return;
    }
    self.windowNavigation = @{@"id": NSUUID.UUID.UUIDString, @"pane": pane};
    [self showWorkspace];
    [NSApp activate];
    if (self.showingApp) {
        NSData *detail = [NSJSONSerialization dataWithJSONObject:self.windowNavigation options:0 error:nil];
        if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-window-navigation"), detail.bytes, detail.length);
    }
    reply(@{@"windowVisible": @(self.window.visible), @"requestedPane": pane}, nil);
}

- (void)applicationActivated {
    [self refreshInterfaceLanguage];
    BOOL startupActivation = self.startupActivationDeadline > NSProcessInfo.processInfo.systemUptime;
    self.startupActivationDeadline = 0;
    if (!self.launchHandled || startupActivation || self.closing || self.updating || NSApp.hidden ||
        NSApp.activationPolicy != NSApplicationActivationPolicyRegular || NSApp.keyWindow ||
        !self.window || self.window.visible || self.window.miniaturized) return;
    [self showWorkspace];
}

- (void)workspaceHidden:(NSNotification *)notification {
    [self.systemDictation stopSoundPreview];
}

- (void)checkForScheduledUpdate:(BOOL)workspaceFocused {
    if (!self.closing && !self.updating && ![self.updateRestartError isEqual:updateRollbackMessage] &&
        [NSUserDefaults.standardUserDefaults boolForKey:@"JardCheckForUpdatesOnLaunch"])
        [self.updater checkAutomaticallyOnActivation:workspaceFocused];
}

- (void)workspaceFocused:(NSNotification *)notification {
    [self checkForScheduledUpdate:YES];
    [self queueInputDeviceEvent];
    [self offerWorkspaceRecovery];
}

- (void)reloadWorkspace:(WKWebView *)web {
    if (!web || web.window != self.window || self.closing || self.updating || self.menuQuitPending) return;
    self.failedWorkspace = nil;
    self.lastWorkspaceReload = NSProcessInfo.processInfo.systemUptime;
    [self.pending removeAllObjects];
    NSURL *origin = [NSURL URLWithString:self.origin];
    NSURL *page = web.URL;
    // Reload the current session route with the existing workspace cookie.
    // The original bootstrap recovers a crash before the first page committed.
    if ([page.scheme isEqual:origin.scheme] && [page.host isEqual:origin.host] &&
        [page.port isEqual:origin.port] && ![page.path isEqual:@"/_desktop/connect"] && [web reloadFromOrigin]) return;
    native_sdk_appkit_load_webview(self.host, SDK_BYTES(self.workspaceLaunchURL), 1, NULL, 0, NULL, 0, NULL, 0, 0);
}

- (void)offerWorkspaceRecovery {
    WKWebView *web = self.failedWorkspace;
    NSWindow *window = self.window;
    if (!web || self.workspaceRecoveryShowing || !window.isKeyWindow || window.attachedSheet ||
        self.closing || self.updating || self.menuQuitPending) return;
    NSAlert *alert = NSAlert.new;
    alert.messageText = JardLocalized(@"Jaro’s workspace stopped responding");
    alert.informativeText = JardLocalized(@"Reload the workspace to continue. Native recordings keep running.");
    [alert addButtonWithTitle:JardLocalized(@"Reload workspace")];
    [alert addButtonWithTitle:JardLocalized(@"Later")];
    self.workspaceRecoveryShowing = YES;
    __weak typeof(self) owner = self;
    [alert beginSheetModalForWindow:window completionHandler:^(NSModalResponse response) {
        owner.workspaceRecoveryShowing = NO;
        if (response == NSAlertFirstButtonReturn) [owner reloadWorkspace:web];
    }];
}

- (void)workspaceContentTerminated:(NSNotification *)notification {
    WKWebView *web = notification.object;
    if (![web isKindOfClass:WKWebView.class] || web.window != self.window ||
        !self.workspaceLaunchURL || self.closing || self.terminationApproved) return;
    self.failedWorkspace = web;
    if (self.updating || self.menuQuitPending) return;
    [self.systemDictation stopSoundPreview];
    NSLog(@"Jaro's WebKit content process terminated.");
    if (!self.lastWorkspaceReload || NSProcessInfo.processInfo.systemUptime - self.lastWorkspaceReload >= 30) {
        // Let WebKit finish retiring the failed process before asking it to load again.
        __weak typeof(self) owner = self;
        dispatch_async(dispatch_get_main_queue(), ^{ [owner reloadWorkspace:web]; });
        return;
    }
    // Repeated terminations require an explicit retry. Do not focus a hidden window.
    [self offerWorkspaceRecovery];
}

- (void)queueInputDeviceEvent {
    if (self.closing || !self.showingApp || !self.window.visible || self.window.miniaturized || self.inputDeviceEventPending) return;
    self.inputDeviceEventPending = YES;
    __weak typeof(self) owner = self;
    // A device arrival can change the list and the default input together.
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 120 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        JardHost *app = owner;
        app.inputDeviceEventPending = NO;
        if (!app || app.closing || !app.showingApp || !app.window.visible || app.window.miniaturized) return;
        native_sdk_appkit_emit_window_event(app.host, 1, SDK_BYTES(@"jard-input-devices"), "{}", 2);
    });
}

- (void)registerInputDeviceProperties {
    for (NSUInteger i = 0; i < sizeof(inputDeviceProperties) / sizeof(inputDeviceProperties[0]); i++) {
        OSStatus result = AudioObjectAddPropertyListenerBlock(kAudioObjectSystemObject, &inputDeviceProperties[i],
                                                              dispatch_get_main_queue(), self.inputDeviceListener);
        if (result == noErr) self.inputDeviceNotifications |= 1UL << i;
    }
}

- (void)observeInputDevices {
    __weak typeof(self) owner = self;
    self.inputDeviceListener = ^(UInt32 count, const AudioObjectPropertyAddress *addresses) {
        JardHost *app = owner;
        if (!app || app.closing) return;
        for (UInt32 i = 0; i < count; i++) {
            if (addresses[i].mSelector == kAudioHardwarePropertyServiceRestarted) {
                dispatch_async(dispatch_get_main_queue(), ^{
                    JardHost *host = owner;
                    if (!host || host.closing) return;
                    [host removeInputDeviceProperties];
                    [host registerInputDeviceProperties];
                });
                break;
            }
        }
        [app queueInputDeviceEvent];
    };
    [self registerInputDeviceProperties];

    // The lid can disable the built-in mic without changing either Core Audio property.
    io_service_t root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"));
    if (!root) return;
    IONotificationPortRef port = IONotificationPortCreate(kIOMainPortDefault);
    io_object_t notification = IO_OBJECT_NULL;
    if (port && IOServiceAddInterestNotification(port, root, kIOGeneralInterest, lidStateChanged,
                                                 (__bridge void *)self, &notification) == kIOReturnSuccess) {
        self.lidPort = port;
        self.lidNotification = notification;
        IONotificationPortSetDispatchQueue(port, dispatch_get_main_queue());
    } else if (port) {
        IONotificationPortDestroy(port);
    }
    IOObjectRelease(root);
}

- (void)removeInputDeviceProperties {
    for (NSUInteger i = 0; i < sizeof(inputDeviceProperties) / sizeof(inputDeviceProperties[0]); i++) {
        if (self.inputDeviceNotifications & (1UL << i)) {
            AudioObjectRemovePropertyListenerBlock(kAudioObjectSystemObject, &inputDeviceProperties[i],
                                                   dispatch_get_main_queue(), self.inputDeviceListener);
        }
    }
    self.inputDeviceNotifications = 0;
}

- (void)stopObservingInputDevices {
    [self removeInputDeviceProperties];
    self.inputDeviceListener = nil;
    if (self.lidNotification) IOObjectRelease(self.lidNotification);
    if (self.lidPort) IONotificationPortDestroy(self.lidPort);
    self.lidNotification = IO_OBJECT_NULL;
    self.lidPort = NULL;
}

- (void)installWindowChrome {
    self.window = NSApp.mainWindow ?: NSApp.windows.firstObject;
    self.window.titlebarSeparatorStyle = NSTitlebarSeparatorStyleNone;
    self.window.toolbarStyle = NSWindowToolbarStyleUnifiedCompact;
    self.window.collectionBehavior |= NSWindowCollectionBehaviorMoveToActiveSpace;
    [self observeInputDevices];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(applicationStarted:)
        name:NSApplicationDidFinishLaunchingNotification object:NSApp];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(workspaceHidden:)
        name:NSApplicationDidHideNotification object:NSApp];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(workspaceHidden:)
        name:NSWindowDidMiniaturizeNotification object:self.window];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(workspaceFocused:)
        name:NSWindowDidBecomeKeyNotification object:self.window];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(workspaceContentTerminated:)
        name:@"JaroWorkspaceContentTerminated" object:nil];
    for (NSString *name in @[NSWindowDidEnterFullScreenNotification, NSWindowDidExitFullScreenNotification]) {
        [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(chromeChanged:) name:name object:self.window];
    }
    __weak JardHost *weakSelf = self;
    self.dragMonitor = [NSEvent addLocalMonitorForEventsMatchingMask:NSEventMaskLeftMouseDown handler:^NSEvent *(NSEvent *event) {
        JardHost *app = weakSelf;
        NSWindow *window = app.window;
        if (!window || event.window != window || window.attachedSheet || NSApp.modalWindow) return event;
        NSPoint point = [window.contentView convertPoint:event.locationInWindow fromView:nil];
        NSRect bounds = window.contentView.bounds;
        CGFloat y = window.contentView.isFlipped ? point.y : NSMaxY(bounds) - point.y;
        // The workspace reports its measured action area so wider toolbars keep receiving clicks.
        CGFloat toolbarInset = app.showingApp ? app.workspaceToolbarInset : 250;
        if (toolbarInset <= 0 || point.x < 208 || point.x >= NSWidth(bounds) - toolbarInset || y < 2 || y > 42) return event;
        if (event.clickCount >= 2) native_sdk_appkit_start_window_drag(app.host, 1);
        else [window performWindowDragWithEvent:event];
        return nil;
    }];
}

// Ask from the signed foreground application so macOS associates the grant with
// the same app the user opened. A status check never requests access.
- (void)checkPermission:(NSString *)source prompt:(BOOL)prompt reply:(void (^)(NSString *))reply {
    if ([source isEqual:@"system"]) {
        if (CGPreflightScreenCaptureAccess()) { reply(@"granted"); return; }
        // A negative CoreGraphics preflight is inconclusive. On explicit request,
        // ask the same ScreenCaptureKit API that the recorder uses. Discard the
        // content list immediately; this check never creates a recording stream.
        if (!prompt) { reply(@"unavailable"); return; }
        [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:NO
            completionHandler:^(SCShareableContent *content, NSError *error) {
                NSString *status = nil;
                if (content && !error) status = @"granted";
                else if ([error.domain isEqual:SCStreamErrorDomain] && error.code == SCStreamErrorUserDeclined)
                    status = @"denied";
                dispatch_async(dispatch_get_main_queue(), ^{ reply(status); });
            }];
        return;
    }
    switch ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio]) {
        case AVAuthorizationStatusAuthorized: reply(@"granted"); return;
        case AVAuthorizationStatusDenied: reply(@"denied"); return;
        case AVAuthorizationStatusRestricted: reply(@"restricted"); return;
        case AVAuthorizationStatusNotDetermined:
            if (!prompt) { reply(@"unavailable"); return; }
            [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) {
                dispatch_async(dispatch_get_main_queue(), ^{ reply(granted ? @"granted" : @"denied"); });
            }];
            return;
    }
    reply(@"unavailable");
}

- (void)checkMeetingSystemAudio:(BOOL)prompt reply:(void (^)(NSString *backend))reply {
    [self.meetingAudioAccess checkBackendWithPrompt:prompt screenRequest:^(void (^answer)(BOOL granted)) {
        [self checkPermission:@"system" prompt:YES reply:^(NSString *result) { answer([result isEqual:@"granted"]); }];
    } reply:reply];
}

- (void)startMeeting:(NSDictionary *)options prompt:(BOOL)prompt reply:(JardDictationReply)reply {
    [self checkPermission:@"mic" prompt:prompt reply:^(NSString *microphone) {
        if (![microphone isEqual:@"granted"]) {
            reply(@{@"permission": @"mic"}, @"Allow Microphone access to record this meeting.");
            return;
        }
        [self checkMeetingSystemAudio:prompt reply:^(NSString *systemAudio) {
            if (!systemAudio) {
                reply(@{@"permission": @"system"}, @"Allow Mac audio access to record this meeting.");
                return;
            }
            if (self.closing || self.updating || [self annotationBlocksMeeting] || self.otherRecording || self.sdkCaptureID) {
                reply(nil, @"Finish your current capture or update before starting a meeting.");
                return;
            }
            NSMutableDictionary *capture = [options mutableCopy];
            capture[@"inputDevice"] = [self.systemDictation sdkMicrophoneSettings][@"inputDevice"];
            capture[@"meetingSystemAudio"] = systemAudio;
            [self sendToHelper:@"jard.record" payload:capture request:nil completion:reply];
        }];
    }];
}

- (void)setOwnedCapture:(NSString *)identifier {
    self.sdkCaptureID = identifier;
    [self.systemDictation setSDKCaptureActive:identifier != nil];
    [self updateWindowClosePolicy];
    [self refreshMeetingAvailability];
}

- (void)approveSDKCapture:(NSString *)identifier {
    void (^respond)(NSDictionary *) = ^(NSDictionary *result) {
        [self sendToHelper:@"jard.controlReply" payload:@{@"id": identifier, @"response": result} request:nil completion:nil];
    };
    if (!self.showingApp || self.updating || self.systemDictation.hasActivity || self.annotation.busy || self.meetings.busy ||
        self.otherRecording || self.sdkCaptureID) {
        respond(@{@"ok": @NO, @"code": @"dictation-busy", @"status": @409, @"error": @"A recording is already active or Jard is restarting."});
        return;
    }
    [self checkPermission:@"mic" prompt:NO reply:^(NSString *status) {
        if (![status isEqual:@"granted"]) {
            respond(@{@"ok": @NO, @"code": @"microphone-permission-required", @"status": @403, @"error": @"Allow Microphone access in Jard first."});
            return;
        }
        [self setOwnedCapture:identifier];
        respond(@{@"ok": @YES, @"result": [self.systemDictation sdkMicrophoneSettings]});
    }];
}

- (void)invoke:(NSDictionary *)message origin:(NSString *)origin {
    if (self.closing || ![message isKindOfClass:NSDictionary.class]) return;
    NSString *request = message[@"id"];
    NSString *command = message[@"command"];
    if (![request isKindOfClass:NSString.class] || request.length > 100 || ![command isKindOfClass:NSString.class]) return;
    if ([command isEqual:@"jard.chrome"] && [origin isEqual:(self.showingApp ? self.origin : @"zero://inline")]) {
        [self respond:@{@"id": request, @"ok": @YES, @"result": [self chrome]}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.workspaceReady"]) {
        if (!self.updateStartupReady) [self confirmUpdateStartup];
        [self respond:@{@"id": request, @"ok": @YES, @"result": NSNull.null}];
        return;
    }
    if ([command isEqual:@"jard.interfaceLanguage"] && [origin isEqual:(self.showingApp ? self.origin : @"zero://inline")]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        if ([payload[@"action"] isEqual:@"set"] && self.showingApp && !self.updating) {
            if (!JardSetInterfaceLanguage(payload[@"selected"])) {
                [self reject:request reason:JardLocalized(@"Choose an available interface language.")];
                return;
            }
            [self refreshInterfaceLanguageWithCompletion:^{
                [self respond:@{@"id": request, @"ok": @YES, @"result": JardInterfaceLanguageStatus()}];
            }];
            return;
        } else if (![payload[@"action"] isEqual:@"status"]) {
            [self reject:request reason:JardLocalized(@"The interface language cannot be changed right now.")];
            return;
        }
        [self respond:@{@"id": request, @"ok": @YES, @"result": JardInterfaceLanguageStatus()}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.dockIcon"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        if ([payload[@"action"] isEqual:@"set"]) {
            if (self.updating) { [self reject:request reason:@"Jard is preparing to restart."]; return; }
            id enabled = payload[@"enabled"];
            if (![enabled isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)enabled) != CFBooleanGetTypeID()) {
                [self reject:request reason:@"Choose whether to show the Dock icon."];
                return;
            }
            [self setShowDockIcon:[enabled boolValue]];
        } else if (![payload[@"action"] isEqual:@"status"]) {
            [self reject:request reason:@"Unknown Dock icon action."];
            return;
        }
        [self respond:@{@"id": request, @"ok": @YES, @"result": [self dockIconStatus]}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.agentSetup"]) {
        NSString *cli = [NSBundle.mainBundle.bundlePath stringByAppendingPathComponent:@"Contents/Helpers/jardctl"];
        if (![NSFileManager.defaultManager isExecutableFileAtPath:cli]) {
            [self reject:request reason:@"Jaro’s command line tool is unavailable."];
            return;
        }
        [self respond:@{@"id": request, @"ok": @YES, @"result": @{@"cli": cli}}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] &&
        ([command isEqual:@"jard.permission"] || [command isEqual:@"jard.openPrivacy"])) {
        NSDictionary *payload = message[@"payload"];
        NSString *source = [payload isKindOfClass:NSDictionary.class] ? payload[@"source"] : nil;
        if (![@[@"mic", @"system"] containsObject:source ?: @""]) {
            [self reject:request reason:@"Unknown recording permission."];
            return;
        }
        if ([command isEqual:@"jard.permission"]) {
            [self checkPermission:source prompt:[payload[@"prompt"] isEqual:@YES] reply:^(NSString *status) {
                if (status) [self respond:@{@"id": request, @"ok": @YES, @"result": @{@"status": status}}];
                else [self reject:request reason:@"Mac audio access could not be checked. Try again."];
            }];
        } else {
            NSString *pane = [source isEqual:@"mic"] ? @"Privacy_Microphone" : @"Privacy_ScreenCapture";
            NSString *address = [@"x-apple.systempreferences:com.apple.preference.security?" stringByAppendingString:pane];
            if ([NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:address]]) {
                [self respond:@{@"id": request, @"ok": @YES, @"result": @{}}];
            } else {
                [self reject:request reason:@"Open System Settings and select Privacy & Security."];
            }
        }
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.revealApp"]) {
        [NSWorkspace.sharedWorkspace activateFileViewerSelectingURLs:@[NSBundle.mainBundle.bundleURL]];
        [self respond:@{@"id": request, @"ok": @YES, @"result": @{}}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.meetingAudioAccess"]) {
        if (self.meetings.managed) { [self reject:request reason:@"Meetings are managed by your organization."]; return; }
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        if (self.closing || (![payload[@"action"] isEqual:@"status"] && (self.updating || [self hasActiveCapture]))) {
            [self reject:request reason:@"Finish your current capture or update before changing Mac audio access."];
            return;
        }
        [self.meetingAudioAccess request:payload reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.copyMeetingTranscript"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        NSString *key = [payload[@"id"] isKindOfClass:NSString.class] ? payload[@"id"] : @"";
        if (![[NSUUID alloc] initWithUUIDString:key]) { [self reject:request reason:@"Invalid meeting ID."]; return; }
        [self.meetings copySavedTranscript:key reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.meetings"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        if ([payload[@"action"] isEqual:@"showLiveTranscript"]) {
            [self controlWindowAction:@"window-show" pane:@"live-meeting" reply:^(id result, NSString *error) {
                if (error) [self reject:request reason:error];
                else [self.meetings request:@{@"action": @"status"} reply:^(id status, NSString *failure) {
                    [self respond:@{@"id": request, @"ok": @YES, @"result": status}];
                }];
            }];
            return;
        }
        [self.meetings request:payload reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else {
                [self updateWindowClosePolicy];
                [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
            }
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.annotation"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        [self controlAnnotation:payload[@"action"] parameters:payload reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.sessionScreenshot"]) {
        if (self.annotationManaged) { [self reject:request reason:annotationManagedMessage]; return; }
        if (!self.systemDictation.annotationEnabled) { [self reject:request reason:@"Annotate is turned off in Settings."]; return; }
        if (![self canStartAnnotation]) { [self reject:request reason:@"Finish the current recording or update first."]; return; }
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        [self.annotation takeScreenshot:payload[@"source"] reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.annotationAccess"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        NSString *action = [payload[@"action"] isKindOfClass:NSString.class] ? payload[@"action"] : @"";
        JardDictationReply finish = ^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        };
        [self controlAnnotationAccess:action reply:finish];
        return;
    }
    BOOL pasteAnnotation = [command isEqual:@"jard.pasteAnnotation"];
    if (self.showingApp && [origin isEqual:self.origin] && (pasteAnnotation || [command isEqual:@"jard.copyAnnotation"])) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        NSString *key = [payload[@"id"] isKindOfClass:NSString.class] ? payload[@"id"] : @"";
        [self shareAnnotation:key paste:pasteAnnotation fromPanel:NO reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.windowNavigation"]) {
        NSDictionary *snapshot = self.windowNavigation ? @{@"navigation": self.windowNavigation} : @{};
        [self respond:@{@"id": request, @"ok": @YES, @"result": snapshot}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.workspaceToolbar"]) {
        id inset = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"][@"inset"] : nil;
        double width = [inset isKindOfClass:NSNumber.class] ? [inset doubleValue] : -1;
        if (!isfinite(width) || width < 0) {
            [self reject:request reason:@"The workspace toolbar bounds are invalid."];
            return;
        }
        self.workspaceToolbarInset = width;
        [self respond:@{@"id": request, @"ok": @YES, @"result": NSNull.null}];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.loginItem"]) {
        if (self.updating || self.closing) { [self reject:request reason:@"Jaro is preparing to close."]; return; }
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        [self.loginItem request:payload reply:^(NSDictionary *result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.update"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        [self performUpdateAction:payload[@"action"] payload:payload reply:^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result}];
        }];
        return;
    }
    if (self.showingApp && [origin isEqual:self.origin] && [command isEqual:@"jard.systemDictation"]) {
        NSDictionary *payload = [message[@"payload"] isKindOfClass:NSDictionary.class] ? message[@"payload"] : @{};
        if ([payload[@"action"] isEqual:@"setAnnotationOverlays"] && self.annotationManaged) {
            [self reject:request reason:annotationManagedMessage];
            return;
        }
        if (([payload[@"action"] isEqual:@"chooseShortcut"] && [payload[@"target"] isEqual:@"annotate"]) ||
            [payload[@"action"] isEqual:@"resetAnnotationShortcut"] || [payload[@"action"] isEqual:@"setAnnotationEnabled"]) {
            if (self.annotationManaged) { [self reject:request reason:annotationManagedMessage]; return; }
            if (self.closing || self.updating || [self hasActiveCapture]) {
                [self reject:request reason:@"Finish the current recording or update before changing the Annotate shortcut."];
                return;
            }
        }
        if ([message[@"payload"] isKindOfClass:NSDictionary.class] && [message[@"payload"][@"action"] isEqual:@"reopen"]) {
            [self prepareControlExit:nil restart:YES force:NO reply:^(id result, NSString *error) {
                if (error) [self reject:request reason:error];
                else [self respond:@{@"id": request, @"ok": @YES, @"result": [self.systemDictation status]}];
            }];
            return;
        }
        JardDictationReply reply = ^(id result, NSString *error) {
            if (error) [self reject:request reason:error];
            else {
                [self updateWindowClosePolicy];
                [self respond:@{@"id": request, @"ok": @YES, @"result": result ?: NSNull.null}];
            }
        };
        if ([payload[@"action"] isEqual:@"set"] && [payload[@"key"] isEqual:@"onboarding"] &&
            [payload[@"value"] isEqual:@"welcome"]) {
            [self replayDictationSetupShowingWindow:NO reply:reply];
        } else {
            [self.systemDictation request:message[@"payload"] reply:reply];
        }
        return;
    }
    NSArray *commands = self.showingApp
        ? @[@"jard.devices", @"jard.inputDevices", @"jard.microphoneLevel", @"jard.record", @"jard.stop", @"jard.dictation",
            @"jard.dictationCleanup", @"jard.live", @"jard.openAudio", @"jard.revealSessionMedia", @"jard.export", @"jard.meetingExports", @"jard.annotationStorage", @"jard.annotationAgentSkill", @"jard.annotationRoutingPolicy", @"jard.annotationRules", @"jard.annotationDestinations", @"jard.annotationRoute", @"jard.rememberAnnotationDestination", @"jard.forgetAnnotationDestination", @"jard.sendAnnotation", @"jard.annotationAgentDestinations", @"jard.sendAnnotationToAgent", @"jard.sendSessionToAgent", @"jard.annotationDeliveries", @"jard.undoAnnotationDelivery", @"jard.correctAnnotationDelivery", @"jard.recover", @"jard.discard", @"jard.open"]
        : @[@"jard.prepare", @"jard.setup"];
    if (![origin isEqual:(self.showingApp ? self.origin : @"zero://inline")] || ![commands containsObject:command]) {
        [self reject:request reason:@"This command is unavailable in this view."];
        return;
    }
    if ([command isEqual:@"jard.microphoneLevel"] && (!self.window.visible || self.window.miniaturized)) {
        [self respond:@{@"id": request, @"ok": @YES, @"result": @{@"peak": NSNull.null}}];
        return;
    }
    if (self.updating) { [self reject:request reason:@"Jard is preparing to restart."]; return; }
    id payload = message[@"payload"] ?: @{};
    if ([command isEqual:@"jard.prepare"]) {
        [self sendToHelper:command payload:payload request:nil completion:^(id result, NSString *error) {
            if (self.showingApp || self.closing) return;
            BOOL ready = [result isKindOfClass:NSDictionary.class] && [result[@"ready"] isEqual:@YES];
            if (!error && [result isKindOfClass:NSDictionary.class] && [result[@"ready"] isEqual:@NO])
                [self confirmUpdateStartup];  // The working app is showing its model setup.
            if (!self.hiddenLaunchRequested && (!self.launchHandled || self.quietStartupPending) && (error || !ready)) {
                self.quietStartupPending = NO;
                [self showStartupWindowIfHidden];
            }
            if (error) [self reject:request reason:error];
            else [self respond:@{@"id": request, @"ok": @YES, @"result": result ?: NSNull.null}];
        }];
        return;
    }
    if (([command isEqual:@"jard.record"] || [command isEqual:@"jard.dictation"]) && [payload isKindOfClass:NSDictionary.class]) {
        NSMutableDictionary *publicOptions = [payload mutableCopy];
        [publicOptions removeObjectsForKeys:@[@"global", @"keep", @"preRoll", @"inputDevice", @"avoidBluetooth",
            @"outputProfile", @"cleanupOnFailure", @"releaseTail", @"noiseCleanup", @"boostMicrophone", @"meeting", @"meetingLabel", @"meetingCalendar", @"meetingTitle",
            @"meetingApplication", @"meetingSystemAudio"]];
        if ([command isEqual:@"jard.record"] && ![publicOptions[@"mode"] isEqual:@"system"]) {
            NSDictionary *microphone = [self.systemDictation sdkMicrophoneSettings];
            publicOptions[@"inputDevice"] = microphone[@"inputDevice"];
            publicOptions[@"avoidBluetooth"] = microphone[@"avoidBluetooth"];
        }
        payload = publicOptions;
    }
    [self sendToHelper:command payload:payload request:request completion:nil];
}

- (void)finishPrepareWithResult:(id)result error:(NSString *)error {
    self.preparing = NO;
    NSArray<JardDictationReply> *waiters = [self.prepareWaiters copy];
    [self.prepareWaiters removeAllObjects];
    for (JardDictationReply waiter in waiters) waiter(result, error);
}

- (void)sendToHelper:(NSString *)command payload:(id)payload request:(NSString *)request completion:(JardDictationReply)completion {
    void (^reject)(NSString *) = ^(NSString *reason) {
        if (completion) completion(nil, reason);
        if (request) [self reject:request reason:reason];
    };
    if (self.closing) { reject(@"Jard is closing."); return; }
    if ((self.preparing || !self.bridge.running) && self.showingApp && ![command isEqual:@"jard.prepare"] &&
        ![command isEqual:@"jard.updateQuiesce"]) {
        if (!self.prepareWaiters) self.prepareWaiters = NSMutableArray.array;
        if (self.prepareWaiters.count >= 64) {
            reject(@"The app is busy or the request is too large.");
            return;
        }
        [self.prepareWaiters addObject:^(id result, NSString *error) {
            if (self.closing) {
                reject(@"Jard is closing.");
            } else if (error || ![result isKindOfClass:NSDictionary.class] || ![result[@"ready"] boolValue]) {
                reject(error ?: @"The local model is unavailable. Reopen Jard and check model setup.");
            } else {
                [self sendToHelper:command payload:payload request:request completion:completion];
            }
        }];
        if (!self.preparing) {
            self.preparing = YES;
            [self sendToHelper:@"jard.prepare" payload:@{} request:nil completion:^(id result, NSString *error) {
                [self finishPrepareWithResult:result error:error];
            }];
        }
        return;
    }
    NSError *error = nil;
    if (![self startBridge:&error]) {
        reject(error.localizedDescription ?: @"The app could not start its local helper.");
        return;
    }
    NSString *wireID = NSUUID.UUID.UUIDString;
    NSData *json = [NSJSONSerialization dataWithJSONObject:@{@"id": wireID, @"command": command,
                                                        @"payload": payload} options:0 error:&error];
    if (!json || json.length > 262144 || self.pending.count + self.nativePending.count >= 64) {
        reject(@"The app is busy or the request is too large.");
        return;
    }
    if (request) self.pending[wireID] = request;
    else self.nativePending[wireID] = completion ?: ^(id result, NSString *failure) {};
    NSMutableData *line = [json mutableCopy];
    [line appendBytes:"\n" length:1];
    if (![self.input writeData:line error:&error]) {
        [self.pending removeObjectForKey:wireID];
        [self.nativePending removeObjectForKey:wireID];
        reject(error.localizedDescription ?: @"The local helper disconnected.");
    }
}

- (void)updateWindowClosePolicy {
    native_sdk_appkit_set_window_close_policy(self.host, 1,
        (self.updating || self.hiddenLaunchRequested || self.systemDictation.keepsRunning || self.systemDictation.vocabularyMenuEnabled || self.annotation.busy || self.meetings.keepsRunning || self.sdkCaptureID ||
            (self.showingApp && ![self showDockIcon])) ? 1 : 0);
}

- (BOOL)showDockIcon {
    return JardShowDockIconEnabled();
}

- (BOOL)canOpenFromMenuBar {
    return self.meetingStatusItem.visible && self.meetingStatusItem.button.enabled && self.meetingMenuItems[@"open"].enabled;
}

- (NSDictionary *)dockIconStatus {
    return @{@"enabled": @([self showDockIcon]), @"menuBarAvailable": @([self canOpenFromMenuBar])};
}

- (void)setShowDockIcon:(BOOL)enabled {
    [NSUserDefaults.standardUserDefaults setBool:enabled forKey:JardShowDockIconPreferenceKey];
    [self refreshDockIconPreference];
}

- (void)refreshDockIconPreference {
    [self updateMeetingMenu:nil];
    [self emitDockIconStatus];
}

- (void)updateDockPresentation {
    if (!self.launchHandled || self.closing || !self.window) return;
    if (self.window.visible || self.window.miniaturized) [self finishBackgroundStartup];
    if (self.hiddenLaunchRequested) {
        if ([self canOpenFromMenuBar]) [self setDockIconVisible:NO];
        else [self showStartupWindowIfHidden];
        return;
    }
    BOOL visible = !self.showingApp || self.window.visible || self.window.miniaturized || NSApp.hidden ||
        [self showDockIcon] || ![self canOpenFromMenuBar];
    [self setDockIconVisible:visible];
}

- (void)observeValueForKeyPath:(NSString *)keyPath ofObject:(id)object change:(NSDictionary *)change context:(void *)context {
    if (context != &statusItemVisibilityContext) {
        [super observeValueForKeyPath:keyPath ofObject:object change:change context:context];
        return;
    }
    [self updateWindowClosePolicy];
    [self updateDockPresentation];
    [self emitDockIconStatus];
}

- (void)emitDockIconStatus {
    if (self.showingApp && !self.closing) {
        NSData *detail = [NSJSONSerialization dataWithJSONObject:[self dockIconStatus] options:0 error:nil];
        if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-dock-icon"), detail.bytes, detail.length);
    }
}

- (BOOL)annotationBlocksMeeting {
    return self.annotation.busy && !self.annotation.sharesMicrophone;
}

- (void)refreshMeetingAvailability {
    [self.meetings suspendOffers:[self annotationBlocksMeeting] || self.updating || self.otherRecording || self.sdkCaptureID != nil];
    if (self.showingApp) [self.meetings request:@{@"action": @"status"} reply:^(id result, NSString *error) {
        [self updateMeetingMenu:result];
    }];
}

- (BOOL)isRecordingSystemAudio {
    NSDictionary *annotation = self.annotation.status;
    BOOL annotationAudio = [annotation[@"phase"] isEqual:@"recording"] && [annotation[@"audio"][@"computerAudio"] boolValue];
    return self.meetings.recording || self.otherRecordingCapturesSystemAudio || annotationAudio;
}

- (void)meetingChanged:(NSDictionary *)status {
    if (self.closing) return;
    [self.systemDictation setMeetingPasteAvailable:self.meetings.canPaste];
    [self.systemDictation setSystemAudioRecordingActive:[self isRecordingSystemAudio]];
    [self updateWindowClosePolicy];
    if (self.showingApp) [self updateMeetingMenu:status];
    NSData *detail = [NSJSONSerialization dataWithJSONObject:status options:0 error:nil];
    if (detail && self.showingApp) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-meetings"), detail.bytes, detail.length);
}

- (void)createMeetingMenu {
    self.meetingStatusItem = [NSStatusBar.systemStatusBar statusItemWithLength:NSVariableStatusItemLength];
    if (!self.meetingStatusItem) return;
    NSMenu *menu = [[NSMenu alloc] initWithTitle:@"Jard Meetings"];
    menu.autoenablesItems = NO;
    menu.delegate = self;
    NSMutableDictionary *items = NSMutableDictionary.dictionary;
    for (NSArray<NSString *> *entry in @[
        @[@"open", @"Open Jaro"], @[@"start", @"Record Meeting"], @[@"stop", @"Stop & Save"],
        @[@"keepRecording", @"Keep Recording"], @[@"copyNew", @"Copy New"], @[@"copyAll", @"Copy All"],
        @[@"indicator", @"Show Recording Indicator"], @[@"privacy", @"Open Recording Privacy Settings"],
        @[@"recoverMeeting", @"Recover Meeting"], @[@"pasteLast", @"Paste Last Dictation"], @[@"vocabulary", @"Vocabulary"],
        @[@"copyLatest", @"Copy Last Meeting Transcript"],
        @[@"update", @"Check for Updates…"], @[@"quit", @"Quit Jaro"]
    ]) {
        NSMenuItem *item = [menu addItemWithTitle:entry[1] action:@selector(meetingMenuAction:) keyEquivalent:@""];
        item.target = self;
        item.representedObject = entry[0];
        items[entry[0]] = item;
    }
    NSMenu *vocabulary = [[NSMenu alloc] initWithTitle:@"Vocabulary"];
    vocabulary.autoenablesItems = NO;
    [items[@"vocabulary"] setSubmenu:vocabulary];
    self.meetingMenuItems = items;
    self.meetingStatusItem.menu = menu;
    [self rememberMenuLabels:menu];
    [self translateMenuLabels];
    self.meetingStatusItem.button.imagePosition = NSImageLeft;
    [self.meetingStatusItem addObserver:self forKeyPath:@"visible" options:0 context:&statusItemVisibilityContext];
}

- (void)menuNeedsUpdate:(NSMenu *)menu {
    if (menu != self.meetingStatusItem.menu) return;
    [self.systemDictation configurePasteLastMenuItem:self.meetingMenuItems[@"pasteLast"]];
    NSMenuItem *vocabulary = self.meetingMenuItems[@"vocabulary"];
    if (vocabulary && !vocabulary.hidden) {
        NSMenu *submenu = vocabulary.submenu;
        [submenu removeAllItems];
        NSArray *entries = self.systemDictation.menuVocabularyEntries;
        NSMenuItem *add = [submenu addItemWithTitle:JardLocalized(@"Add Term…") action:@selector(meetingMenuAction:) keyEquivalent:@""];
        add.target = self;
        add.representedObject = @"add-term";
        add.enabled = entries.count < 32;
        if (entries.count) {
            [submenu addItem:NSMenuItem.separatorItem];
            NSUInteger shown = 0;
            for (NSDictionary *entry in entries.reverseObjectEnumerator) {
                NSString *line = [[entry[@"term"] componentsSeparatedByCharactersInSet:NSCharacterSet.newlineCharacterSet] componentsJoinedByString:@" "];
                [submenu addItemWithTitle:line action:NULL keyEquivalent:@""].enabled = NO;
                if (++shown == 5) break;
            }
            [submenu addItem:NSMenuItem.separatorItem];
        }
        NSMenuItem *open = [submenu addItemWithTitle:JardLocalized(@"Open Vocabulary…") action:@selector(meetingMenuAction:) keyEquivalent:@""];
        open.target = self;
        open.representedObject = @"vocabulary";
    }
    [self updateMenuTitle:self.updater.status];
}

- (void)removeMeetingMenu {
    if (!self.meetingStatusItem) return;
    [self.meetingStatusItem removeObserver:self forKeyPath:@"visible" context:&statusItemVisibilityContext];
    [NSStatusBar.systemStatusBar removeStatusItem:self.meetingStatusItem];
    self.meetingStatusItem = nil;
    self.meetingMenuItems = nil;
}

- (void)updateMeetingMenu:(NSDictionary *)incomingStatus {
    [self refreshUpdateRestartAvailability];
    if (incomingStatus) self.meetingMenuStatus = incomingStatus;
    NSDictionary *status = self.meetingMenuStatus;
    BOOL busy = [status[@"busy"] boolValue], recording = self.meetings.recording;
    BOOL permission = [status[@"phase"] isEqual:@"permission"], recovery = [status[@"recoveryAvailable"] boolValue];
    BOOL meetingControls = busy || permission || recovery || [status[@"detection"] boolValue];
    BOOL dictationEnabled = self.showingApp && self.systemDictation.keepsRunning;
    BOOL canCopyLatest = self.showingApp && [status[@"hasSavedMeeting"] boolValue];
    BOOL dockRequested = [self showDockIcon] && !self.hiddenLaunchRequested;
    BOOL vocabularyMenu = self.showingApp && self.systemDictation.vocabularyMenuEnabled;
    if (!meetingControls && !dictationEnabled && !vocabularyMenu && !canCopyLatest && dockRequested) {
        [self setDockIconVisible:YES];
        [self removeMeetingMenu];
        [self updateWindowClosePolicy];
        return;
    }
    if (!self.meetingStatusItem) [self createMeetingMenu];
    NSDictionary<NSString *, NSMenuItem *> *items = self.meetingMenuItems;
    items[@"start"].hidden = !self.showingApp || self.meetings.managed || busy || permission || recovery;
    items[@"vocabulary"].hidden = !vocabularyMenu;
    items[@"pasteLast"].hidden = !dictationEnabled;
    [self.systemDictation configurePasteLastMenuItem:items[@"pasteLast"]];
    items[@"quit"].hidden = dockRequested;
    items[@"start"].enabled = ![self annotationBlocksMeeting] && !self.otherRecording && !self.updating && !self.sdkCaptureID;
    items[@"stop"].hidden = items[@"indicator"].hidden = !recording;
    items[@"keepRecording"].hidden = ![status[@"phase"] isEqual:@"callEnded"];
    items[@"copyNew"].hidden = items[@"copyAll"].hidden = !recording;
    items[@"copyNew"].enabled = ![status[@"copying"] boolValue];
    items[@"copyAll"].enabled = items[@"copyNew"].enabled && [status[@"hasText"] boolValue];
    items[@"copyLatest"].hidden = !canCopyLatest || recording;
    items[@"indicator"].state = [status[@"showIndicator"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
    BOOL systemPermissionDenied = recording && [status[@"systemAudioPermissionDenied"] boolValue];
    items[@"privacy"].hidden = !permission && !systemPermissionDenied;
    items[@"recoverMeeting"].hidden = !recovery || busy;
    items[@"recoverMeeting"].enabled = items[@"start"].enabled;
    long seconds = [status[@"seconds"] longValue];
    NSString *elapsed = recording ? [NSString stringWithFormat:@"%ld:%02ld", seconds / 60, seconds % 60] : @"";
    NSStatusBarButton *button = self.meetingStatusItem.button;
    button.title = elapsed;
    BOOL silent = recording && [status[@"microphoneSilent"] boolValue];
    BOOL microphoneReconnecting = recording && [status[@"microphoneUnavailable"] boolValue];
    BOOL systemReconnecting = recording && [status[@"systemAudioUnavailable"] boolValue];
    NSString *symbol = microphoneReconnecting && systemReconnecting ? @"exclamationmark.circle" :
        systemReconnecting ? @"speaker.slash" : microphoneReconnecting || silent ? @"mic.slash" :
        recording ? @"record.circle" : @"waveform";
    button.image = [NSImage imageWithSystemSymbolName:symbol accessibilityDescription:nil];
    button.image.template = YES;
    NSString *title = [status[@"title"] isKindOfClass:NSString.class] ? status[@"title"] : nil;
    NSString *warning = systemPermissionDenied ? JardLocalized(@"Mac audio access was denied.") :
        microphoneReconnecting && systemReconnecting ? @"Microphone and Mac audio are reconnecting." :
        microphoneReconnecting ? @"Microphone is reconnecting." :
        systemReconnecting && silent ? @"Mac audio is reconnecting. The microphone is silent." :
        systemReconnecting ? @"Mac audio is reconnecting." :
        silent ? @"Microphone silent. Mac audio is still recording." : nil;
    button.toolTip = warning ?: (recording && title.length ? title : nil);
    [button setAccessibilityLabel:recording ? [NSString stringWithFormat:@"Jaro meeting recording, %@%@%@",
        title.length ? [title stringByAppendingString:@", "] : @"", elapsed,
        warning ? [@", " stringByAppendingString:warning] : @""] : meetingControls ? @"Jaro meetings" : @"Jaro"];
    if (recording && title.length) items[@"open"].title = [JardLocalized(@"Open Jaro · {title}") stringByReplacingOccurrencesOfString:@"{title}" withString:title];
    else if (busy && !recording) items[@"open"].title = JardLocalized(@"Open Jaro · Meeting in progress");
    else items[@"open"].title = JardLocalized(@"Open Jaro");
    [self updateMenuTitle:self.updater.status];
    [self updateWindowClosePolicy];
    [self updateDockPresentation];
}

- (void)meetingMenuAction:(NSMenuItem *)sender {
    NSString *action = sender.representedObject;
    if ([action isEqual:@"open"]) {
        if (self.meetings.recording) {
            [self controlWindowAction:@"window-show" pane:@"live-meeting" reply:^(id result, NSString *error) {}];
            return;
        }
        [self showWorkspace];
        [NSApp activate];
        return;
    }
    if ([action isEqual:@"quit"]) {
        [self quitApp:sender];
        return;
    }
    if ([action isEqual:@"update"]) {
        [self updateFromMenu:sender];
        return;
    }
    if ([action isEqual:@"vocabulary"] || [action isEqual:@"add-term"]) {
        [self controlWindowAction:@"window-show" pane:action reply:^(id result, NSString *error) {}];
        return;
    }
    if ([action isEqual:@"pasteLast"]) {
        [self.systemDictation pasteLastFromMenu];
        return;
    }
    JardDictationReply complete = ^(id result, NSString *error) {
        if (error && ![@[@"start", @"recoverMeeting"] containsObject:action]) [self showUpdateNotice:@"Meeting action unavailable" detail:error];
        else if (!error && self.meetingStatusItem.button && [@[@"copyNew", @"copyAll", @"copyLatest"] containsObject:action])
            NSAccessibilityPostNotificationWithUserInfo(self.meetingStatusItem.button, NSAccessibilityAnnouncementRequestedNotification,
                @{NSAccessibilityAnnouncementKey: @"Meeting transcript copied.", NSAccessibilityPriorityKey: @(NSAccessibilityPriorityMedium)});
    };
    if ([action isEqual:@"copyLatest"]) {
        [self.meetings copyLatestTranscript:complete];
        return;
    }
    NSMutableDictionary *request = [@{@"action": [action isEqual:@"privacy"] ? @"recordingPrivacy" : action} mutableCopy];
    if ([action isEqual:@"indicator"]) request[@"enabled"] = sender.state == NSControlStateValueOn ? @NO : @YES;
    [self.meetings request:request reply:complete];
}

- (void)openBrowser:(id)sender {
    [self invoke:@{@"id": @"native.browser", @"command": @"jard.open", @"payload": @{}} origin:self.origin];
}

- (void)performUpdateAction:(NSString *)action payload:(NSDictionary *)payload reply:(JardDictationReply)reply {
    if ([action isEqual:@"status"]) {
        reply([self workspaceUpdateStatus:self.updater.status], nil);
        return;
    }
    if ([self updatesManaged]) {
        reply(nil, JardLocalized(updatesManagedMessage));
        return;
    }
    if (!self.updater || self.updating || self.closing) {
        reply(nil, @"The updater is unavailable.");
        return;
    }
    if ([action isEqual:@"restart"]) {
        [self restartUpdate:reply];
        return;
    }
    NSString *error = nil;
    if ([action isEqual:@"check"]) [self checkForUpdatesOpeningSettings:NO];
    else if ([action isEqual:@"channel"] || [action isEqual:@"confirmSwitch"]) {
        BOOL accepted = [action isEqual:@"channel"] ? [self.updater selectChannel:payload[@"channel"] reason:&error] :
            [self.updater confirmChannelSwitch:payload[@"offer"] reason:&error];
        if (accepted && self.updateRestartError) {
            self.updateRestartError = nil;
            [self emitUpdateStatus:self.updater.status];
        }
    }
    else if ([action isEqual:@"download"] && [[self.updater status][@"phase"] isEqual:@"download"]) [self.updater openRelease];
    else if ([action isEqual:@"releaseNotes"]) [self.updater openStagedReleaseNotesForVersion:payload[@"version"] channel:payload[@"channel"] reason:&error];
    else error = @"Unknown update action.";
    reply(error ? nil : [self workspaceUpdateStatus:self.updater.status], error);
}

- (void)rememberMenuLabels:(NSMenu *)menu {
    if (!self.menuLabels) self.menuLabels = [NSMapTable weakToStrongObjectsMapTable];
    for (NSMenuItem *item in menu.itemArray) {
        if (item.title.length) [self.menuLabels setObject:item.title forKey:item];
        if (item.submenu) [self rememberMenuLabels:item.submenu];
    }
}

- (void)translateMenuLabels {
    for (NSMenuItem *item in self.menuLabels) item.title = JardLocalized([self.menuLabels objectForKey:item] ?: item.title);
}

- (void)refreshInterfaceLanguage {
    [self refreshInterfaceLanguageWithCompletion:nil];
}

- (void)refreshInterfaceLanguageWithCompletion:(dispatch_block_t)completion {
    NSDictionary *status = JardInterfaceLanguageStatus();
    NSString *resolved = status[@"resolved"], *selected = status[@"selected"];
    BOOL resolvedChanged = ![resolved isEqual:self.menuLanguage];
    if (!resolvedChanged && [selected isEqual:self.selectedInterfaceLanguage]) {
        if (completion) completion();
        return;
    }
    self.selectedInterfaceLanguage = selected;
    if (resolvedChanged) {
        self.menuLanguage = resolved;
        [self translateMenuLabels];
        [self updateMenuTitle:[self.updater status]];
    }
    if (self.showingApp && !self.closing) {
        if (resolvedChanged) [self updateMeetingMenu:nil];
        NSData *detail = [NSJSONSerialization dataWithJSONObject:status options:0 error:nil];
        if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-interface-language"), detail.bytes, detail.length);
        if (resolvedChanged && self.bridge.running && !self.updating) {
            [self sendToHelper:@"jard.meetingClock" payload:@{@"hours": [resolved isEqual:@"en"] ? @"12" : @"24"}
                       request:nil completion:^(id result, NSString *error) { if (completion) completion(); }];
            return;
        }
    }
    if (completion) completion();
}

- (BOOL)updatesManaged {
    return self.updatesDisabledAtLaunch || [NSUserDefaults.standardUserDefaults boolForKey:@"DisableUpdates"];
}

- (void)updateMenuTitle:(NSDictionary *)status {
    BOOL managed = [self updatesManaged];
    NSString *phase = status[@"phase"];
    NSString *title;
    if (self.updateRestartInProgress) {
        title = JardLocalized(@"Restarting…");
    } else if ([phase isEqual:@"switch"]) {
        NSString *name = JardLocalized([status[@"channel"] isEqual:@"dev"] ? @"Development" : @"Stable");
        title = [[JardLocalized(@"Switch to {channel} {version}") stringByReplacingOccurrencesOfString:@"{channel}" withString:name]
            stringByReplacingOccurrencesOfString:@"{version}" withString:status[@"version"] ?: @""];
    } else title = JardLocalized([status[@"staged"] boolValue] ? @"Restart to Update" :
        [phase isEqual:@"download"] ? @"Download Update…" : @"Check for Updates…");
    self.updateMenu.title = title;
    self.updateMenu.hidden = self.automaticUpdatesMenu.hidden = managed;
    NSMenuItem *statusItem = self.meetingMenuItems[@"update"];
    statusItem.title = managed ? JardLocalized(updatesManagedMessage) : title;
    statusItem.hidden = !managed && !self.updater;
    statusItem.enabled = [self canChooseUpdate:status];
}

- (BOOL)canChooseUpdate:(NSDictionary *)status {
    if (!self.updater || self.closing || self.updating || [self updatesManaged]) return NO;
    if ([status[@"staged"] boolValue]) return ![self hasActiveCapture];
    return ![@[@"checking", @"downloading", @"staging", @"applying", @"unavailable"] containsObject:status[@"phase"] ?: @"idle"];
}

- (NSDictionary *)workspaceUpdateStatus:(NSDictionary *)status {
    if ([self updatesManaged]) {
        return @{@"phase": @"unavailable", @"updatesDisabled": @YES, @"manualCheck": @NO,
            @"currentVersion": [NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleVersion"] ?: @""};
    }
    NSMutableDictionary *result = [status mutableCopy] ?: [@{@"phase": @"unavailable"} mutableCopy];
    result[@"restartBlocked"] = @([status[@"staged"] boolValue] && [self hasActiveCapture]);
    result[@"restartInProgress"] = @(self.updateRestartInProgress);
    result[@"restartError"] = self.updateRestartError;
    result[@"manualCheck"] = @(self.manualUpdateCheck || self.manualUpdateResult);
    result[@"openUpdateSettings"] = self.updateSettingsRequest;
    return result;
}

- (void)emitUpdateStatus:(NSDictionary *)status {
    NSDictionary *workspaceStatus = [self workspaceUpdateStatus:status];
    self.updateRestartBlocked = [workspaceStatus[@"restartBlocked"] boolValue];
    if (self.showingApp && !self.closing) {
        NSData *detail = [NSJSONSerialization dataWithJSONObject:workspaceStatus options:0 error:nil];
        if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-update"), detail.bytes, detail.length);
    }
}

- (void)refreshUpdateRestartAvailability {
    if (!self.updater || self.closing) return;
    NSDictionary *status = self.updater.status;
    BOOL blocked = [status[@"staged"] boolValue] && [self hasActiveCapture];
    if (blocked != self.updateRestartBlocked) {
        [self updateMenuTitle:status];
        [self emitUpdateStatus:status];
    }
}

- (void)updateChanged:(NSDictionary *)status {
    self.manualUpdateResult = self.manualUpdateCheck;
    if (![@[@"checking", @"downloading", @"staging"] containsObject:status[@"phase"] ?: @""])
        self.manualUpdateCheck = NO;
    [self updateMenuTitle:status];
    [self emitUpdateStatus:status];
}

- (void)checkForUpdatesOpeningSettings:(BOOL)openSettings {
    if (openSettings) {
        self.updateSettingsRequest = NSUUID.UUID.UUIDString;
        [self showWorkspace];
        [NSApp activate];
    }
    self.manualUpdateCheck = YES;
    self.manualUpdateResult = NO;
    self.updateRestartError = nil;
    [self.updater check];
    // A check that joins an existing transfer need not trigger a state change.
    [self emitUpdateStatus:self.updater.status];
}

- (void)showUpdateNotice:(NSString *)title detail:(NSString *)message {
    NSAlert *alert = NSAlert.new;
    alert.messageText = JardLocalized(title);
    alert.informativeText = JardLocalized(message ?: @"");
    NSWindow *window = self.window;
    if (window.visible) [alert beginSheetModalForWindow:window completionHandler:nil];
    else [alert runModal];
}

- (void)updateFromMenu:(id)sender {
    NSDictionary *status = [self.updater status];
    if (![self canChooseUpdate:status]) return;
    NSString *phase = status[@"phase"];
    if ([status[@"staged"] boolValue]) {
        [self restartUpdate:^(id result, NSString *error) {
            if (error) [self showUpdateNotice:@"Jard could not restart." detail:error];
        }];
    } else if ([phase isEqual:@"download"]) {
        [self.updater openRelease];
    } else if ([phase isEqual:@"switch"]) {
        NSString *failure = nil;
        if (![self.updater confirmChannelSwitch:status[@"offer"] reason:&failure])
            [self showUpdateNotice:@"Could not switch update channels." detail:failure];
    } else {
        [self checkForUpdatesOpeningSettings:YES];
    }
}

- (BOOL)hasActiveCapture {
    return self.systemDictation.hasActivity || self.annotation.busy || self.annotationAccess.resetting ||
        self.meetings.busy || self.otherRecording || self.sdkCaptureID;
}

- (BOOL)canStartAnnotation {
    return !self.annotationManaged && self.systemDictation.annotationEnabled && self.showingApp &&
        !self.closing && !self.updating && !self.annotation.busy && !self.annotationAccess.resetting &&
        !self.otherRecording && !self.sdkCaptureID &&
        (!self.systemDictation.hasActivity || self.systemDictation.sharesMicrophone) &&
        (!self.meetings.busy || self.meetings.sharesMicrophone);
}

- (void)controlAnnotationAccess:(NSString *)action reply:(JardDictationReply)reply {
    if (self.annotationManaged) { reply(nil, annotationManagedMessage); return; }
    if (!self.systemDictation.annotationEnabled) { reply(nil, @"Annotate is turned off in Settings."); return; }
    BOOL blocked = self.closing || self.updating || [self hasActiveCapture];
    if (![action isEqual:@"status"] && blocked) { reply(nil, @"Finish the current recording or update first."); return; }
    if ([action isEqual:@"restart"]) {
        if (![self.annotationAccess needsRestart]) { reply(nil, @"Screen Recording does not need a restart right now."); return; }
        [self prepareControlExit:nil restart:YES force:NO reply:reply];
        return;
    }
    [self.annotationAccess request:action refresh:!blocked reply:reply];
}

- (void)controlAnnotation:(NSString *)action parameters:(NSDictionary *)parameters reply:(JardDictationReply)reply {
    [self controlAnnotation:action parameters:parameters quartzPointer:nil reply:reply];
}

- (void)controlAnnotation:(NSString *)action parameters:(NSDictionary *)parameters quartzPointer:(NSValue *)pointer
    reply:(JardDictationReply)reply {
    if ([action isEqual:@"toggle"]) {
        if ([self.annotation.status[@"phase"] isEqual:@"recording"]) action = @"stop";
        else action = self.annotation.busy ? @"status" : @"start";
    }
    if ([action isEqual:@"setAudio"]) {
        if (self.annotationManaged) { reply(nil, annotationManagedMessage); return; }
        id microphone = parameters[@"microphone"], computerAudio = parameters[@"computerAudio"];
        if (![microphone isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)microphone) != CFBooleanGetTypeID() ||
            ![computerAudio isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)computerAudio) != CFBooleanGetTypeID()) {
            reply(nil, @"Choose which audio sources to include.");
            return;
        }
        if (![self.annotation setRecordingMicrophone:[microphone boolValue] computerAudio:[computerAudio boolValue]]) {
            reply(nil, @"Finish the current annotation before changing its audio.");
            return;
        }
        reply(self.annotation.status, nil);
        return;
    }
    if ([action isEqual:@"start"]) {
        if (self.annotationManaged) { reply(nil, annotationManagedMessage); return; }
        if (!self.systemDictation.annotationEnabled) { reply(nil, @"Annotate is turned off in Settings."); return; }
        id source = parameters[@"source"] ?: @"display";
        if (![@[@"display", @"window", @"area", @"phone"] containsObject:source]) {
            reply(nil, @"Choose a display, window, area, or wired iPhone or iPad to annotate.");
            return;
        }
        NSDictionary *microphone = [self.systemDictation sdkMicrophoneSettings];
        id languages = parameters[@"languages"] ?: microphone[@"languages"] ?: @[];
        if (![languages isKindOfClass:NSArray.class] || [languages count] > 25) {
            reply(nil, @"Invalid annotation language hints.");
            return;
        }
        for (id language in languages) {
            if (![language isKindOfClass:NSString.class] || ![language length] || [language length] > 12) {
                reply(nil, @"Invalid annotation language hints.");
                return;
            }
        }
        NSMutableDictionary *current = [[self.annotation status] mutableCopy];
        if (self.annotation.busy && [current[@"source"] isEqual:source]) {
            current[@"changed"] = @NO;
            reply(current, nil);
            return;
        }
        if (![self canStartAnnotation]) {
            reply(nil, @"Finish the current recording or update first.");
            return;
        }
        if (!pointer && !parameters[@"source"]) {
            CGEventRef location = CGEventCreate(NULL);
            if (location) {
                pointer = [NSValue valueWithPoint:CGEventGetLocation(location)];
                CFRelease(location);
            }
        }
        [self.annotation start:languages source:source microphone:microphone
            origin:[source isEqual:@"phone"] ? nil : [self.systemDictation annotationOriginSnapshotAllowingPrevious:[source isEqual:@"window"]]
            quartzPointer:pointer reply:^(id result, NSString *error) {
            if (error) { reply(nil, error); return; }
            NSMutableDictionary *accepted = [result mutableCopy];
            accepted[@"changed"] = @YES;
            reply(accepted, nil);
        }];
        return;
    }
    BOOL changed = NO;
    if ([action isEqual:@"stop"]) changed = [self.annotation stop];
    else if ([action isEqual:@"cancel"]) changed = [self.annotation cancel];
    else if ([action isEqual:@"reveal"]) [self.annotation reveal];
    else if (![action isEqual:@"status"]) { reply(nil, @"Unknown annotation action."); return; }
    NSMutableDictionary *result = [[self.annotation status] mutableCopy];
    result[@"changed"] = @(changed);
    result[@"managed"] = @(self.annotationManaged);
    result[@"enabled"] = @((BOOL)(!self.annotationManaged && self.systemDictation.annotationEnabled));
    if (self.annotationManaged) result[@"message"] = annotationManagedMessage;
    reply(result, nil);
}

- (NSDictionary *)desktopStatus {
    return @{@"pid": @(NSProcessInfo.processInfo.processIdentifier), @"bundle": NSBundle.mainBundle.bundlePath,
        @"version": [NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleVersion"] ?: @"",
        @"windowVisible": @(self.window.visible), @"dockIconVisible": @(NSApp.activationPolicy == NSApplicationActivationPolicyRegular),
        @"busy": @((BOOL)([self hasActiveCapture] || self.updating)),
        @"update": [self.updater status][@"phase"] ?: @"unavailable"};
}

- (void)resumeAfterControlExitFailure {
    self.updating = NO;
    [self.systemDictation pauseForUpdate:NO];
    [self updateWindowClosePolicy];
    [self refreshMeetingAvailability];
    [self offerWorkspaceRecovery];
}

- (void)confirmUpdateStartup {
    self.updateStartupReady = YES;
    if (self.closingFailedUpdate) return;
    if (self.updater) [self.updater confirmStartup];
    else JardConfirmUpdateStartup();
}

- (void)watchUpdateStartup {
    if (!JardIsUpdateStartupProbe()) return;
    __weak JardHost *owner = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 45 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        JardHost *app = owner;
        if (!app || app.updateStartupReady || app.closing || app.updating ||
            app.menuQuitPending || app.systemQuitPending || [app hasActiveCapture]) return;
        app.closingFailedUpdate = app.updating = YES;
        [app.systemDictation pauseForUpdate:YES];
        [app sendToHelper:@"jard.updateQuiesce" payload:@{} request:nil completion:^(id result, NSString *error) {
            if (error || ![result[@"ready"] boolValue]) {
                app.closingFailedUpdate = NO;
                [app resumeAfterControlExitFailure];
                if (app.updateStartupReady) [app confirmUpdateStartup];
                return;
            }
            // The supervisor sees the exit without a receipt and restores the backup.
            app.terminationApproved = YES;
            [NSApp terminate:nil];
        }];
    });
}

- (void)waitForAnnotationUntil:(NSTimeInterval)deadline completion:(void (^)(BOOL))completion {
    if (!self.annotation.busy) { completion(YES); return; }
    if (NSProcessInfo.processInfo.systemUptime >= deadline) { completion(NO); return; }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [self waitForAnnotationUntil:deadline completion:completion];
    });
}

- (void)prepareControlExit:(NSString *)identifier restart:(BOOL)restart force:(BOOL)force reply:(JardDictationReply)reply {
    if (self.updating || (!force && [self hasActiveCapture])) {
        reply(nil, @"Finish your recording or dictation before closing Jard.");
        return;
    }
    self.updating = YES;
    [self.systemDictation pauseForUpdate:YES];
    [self updateWindowClosePolicy];
    [self refreshMeetingAvailability];
    if (force && self.annotation.busy) {
        [self.annotation stop];
        [self waitForAnnotationUntil:NSProcessInfo.processInfo.systemUptime + 40 completion:^(BOOL stopped) {
            if (stopped) [self completeControlExit:identifier restart:restart force:force reply:reply];
            else {
                [self resumeAfterControlExitFailure];
                reply(nil, @"Jard is still saving the annotation. Try again once it finishes.");
            }
        }];
        return;
    }
    [self completeControlExit:identifier restart:restart force:force reply:reply];
}

- (void)completeControlExit:(NSString *)identifier restart:(BOOL)restart force:(BOOL)force reply:(JardDictationReply)reply {
    [self sendToHelper:@"jard.desktopQuiesce" payload:force ? @{@"force": @YES} : @{} request:nil completion:^(id result, NSString *error) {
        if (error || ![result[@"ready"] boolValue]) {
            [self resumeAfterControlExitFailure];
            reply(nil, error ?: @"The desktop could not close safely.");
            return;
        }
        NSString *reason = nil;
        if (restart && !JardScheduleRelaunch(NSBundle.mainBundle.bundleURL, &reason)) {
            [self sendToHelper:@"jard.updateAbort" payload:@{} request:nil completion:^(id reopened, NSString *abortError) {
                [self resumeAfterControlExitFailure];
                reply(nil, reason ?: @"Jard could not schedule its restart.");
            }];
            return;
        }
        void (^ready)(BOOL) = ^(BOOL applied) {
            self.controlExitID = identifier;
            NSMutableDictionary *status = [[self desktopStatus] mutableCopy];
            status[@"updateApplied"] = @(applied);
            reply(status, nil);
            // The workspace has no private CLI connection to acknowledge this reply.
            if (!identifier) [self terminateAfterCleanup];
        };
        if (![self.updater prepareToInstallOnQuit]) {
            ready(NO);
            return;
        }
        [self sendToHelper:@"jard.updateQuiesce" payload:@{} request:nil completion:^(id drain, NSString *drainError) {
            if (drainError || ![drain[@"ready"] boolValue]) { ready(NO); return; }
            [self.updater installOnQuitWithCompletion:^(BOOL applied, NSString *failure) {
                if (failure) NSLog(@"Jard deferred the staged update: %@", failure);
                ready(applied);
            }];
        }];
    }];
}

- (void)restartUpdate:(JardDictationReply)reply {
    if (self.updating || ![self.updater canInstallStagedUpdate]) {
        reply(nil, @"There is no verified update ready to restart.");
        return;
    }
    if (!self.showingApp || [self hasActiveCapture]) {
        reply(nil, @"Finish your recording or dictation before restarting Jard.");
        return;
    }
    self.updating = YES;
    self.updateRestartInProgress = YES;
    self.updateRestartError = nil;
    self.pendingUpdateRestart = reply;
    [self refreshMeetingAvailability];
    [self updateMenuTitle:self.updater.status];
    [self emitUpdateStatus:self.updater.status];
    [self.updater checkBeforeRestart:^{
        JardDictationReply reply = self.pendingUpdateRestart;
        self.pendingUpdateRestart = nil;
        if (!reply || self.closing) return;
        NSDictionary *status = [self.updater status];
        if ([self hasActiveCapture] || ![status[@"phase"] isEqual:@"ready"] || ![status[@"staged"] boolValue]) {
            NSString *failure = [self hasActiveCapture] ? @"Finish your recording or dictation before restarting Jard." :
                ([status[@"phase"] isEqual:@"switch"] ? @"The channel release changed. Confirm the new release before restarting." :
                @"There is no verified update ready to restart.");
            [self finishFailedUpdateRestart:failure reply:reply];
            return;
        }
        [self quiesceAndRestart:reply];
    }];
}

- (void)finishFailedUpdateRestart:(NSString *)failure reply:(JardDictationReply)reply {
    self.updateRestartInProgress = NO;
    self.updateRestartError = failure;
    [self resumeAfterControlExitFailure];
    [self updateMenuTitle:self.updater.status];
    [self emitUpdateStatus:self.updater.status];
    reply(nil, failure);
}

- (void)quiesceAndRestart:(JardDictationReply)reply {
    [self.systemDictation pauseForUpdate:YES];
    [self sendToHelper:@"jard.updateQuiesce" payload:@{} request:nil completion:^(id result, NSString *error) {
        if (error || ![result[@"ready"] boolValue]) {
            [self finishFailedUpdateRestart:error ?: @"The local engine is busy. Try again." reply:reply];
            return;
        }
        [self.updater restartWithCompletion:^(BOOL applied, NSString *failure) {
            if (applied) {
                reply([self.updater status], nil);
                [self terminateAfterCleanup];
                return;
            }
            [self sendToHelper:@"jard.updateAbort" payload:@{} request:nil completion:^(id reopened, NSString *abortError) {
                NSString *message = failure ?: @"The application could not be replaced.";
                if (abortError || ![reopened[@"ready"] boolValue])
                    message = [message stringByAppendingString:@" The local engine could not reopen."];
                [self finishFailedUpdateRestart:message reply:reply];
            }];
        }];
    }];
}

- (BOOL)annotationResultAvailable {
    return self.showingApp && self.bridge.running && !self.closing && !self.updating &&
        !self.annotationManaged && !self.annotation.busy && [self.systemDictation annotationRoutingDisplayAvailable];
}

- (void)confirmAnnotationDelivery:(id)detail {
    if (![detail isKindOfClass:NSDictionary.class]) return;
    NSString *capture = detail[@"capture"], *nonce = detail[@"nonce"];
    if (![capture isKindOfClass:NSString.class] || ![[NSUUID alloc] initWithUUIDString:capture] ||
        ![nonce isKindOfClass:NSString.class] || ![[NSUUID alloc] initWithUUIDString:nonce]) return;
    NSDictionary *session = CFBridgingRelease(CGSessionCopyCurrentDictionary());
    BOOL unlocked = [session[(__bridge NSString *)kCGSessionOnConsoleKey] boolValue] && ![session[@"CGSSessionScreenIsLocked"] boolValue];
    NSString *state = unlocked ? ([self.annotationResultPanel confirmationStateForCapture:capture] ?:
        [self.annotationRoutePanel confirmationStateForCapture:capture]) : nil;
    [self sendToHelper:@"jard.annotationRouteConfirm" payload:@{@"capture": capture, @"nonce": nonce, @"state": state ?: @"unavailable"}
                 request:nil completion:nil];
}

- (NSURL *)annotationContactSheet:(id)result {
    if (![result isKindOfClass:NSDictionary.class]) return nil;
    NSString *path = [result[@"imagePath"] isKindOfClass:NSString.class] ? result[@"imagePath"] : nil;
    return path.isAbsolutePath && [path.lastPathComponent isEqual:@"contact-sheet.png"] ? [NSURL fileURLWithPath:path] : nil;
}

- (void)inspectAnnotation:(NSString *)key reply:(JardDictationReply)reply {
    [self sendToHelper:@"jard.annotationReview" payload:@{@"id": key} request:nil completion:^(id result, NSString *error) {
        if (![self annotationResultAvailable]) { reply(nil, @"The annotation panel is unavailable."); return; }
        NSURL *sheet = [self annotationContactSheet:result];
        if (error || !sheet || ![NSWorkspace.sharedWorkspace openURL:sheet]) {
            reply(nil, error ?: @"The contact sheet is unavailable. Open the annotation in Sessions.");
            return;
        }
        reply(@YES, nil);
    }];
}

- (void)shareAnnotation:(NSString *)key paste:(BOOL)paste fromPanel:(BOOL)fromPanel reply:(JardDictationReply)reply {
    if (![[NSUUID alloc] initWithUUIDString:key]) { reply(nil, @"Invalid annotation ID."); return; }
    if (self.updating || self.systemDictation.hasActivity || (paste && self.annotation.busy)) {
        reply(nil, @"Finish the current action before sharing this annotation.");
        return;
    }
    NSRunningApplication *destination = paste ? [self.systemDictation annotationPasteDestinationFromPanel:fromPanel] : nil;
    if (paste && !destination) {
        reply(nil, fromPanel ? @"Focus a field in another app, then click Paste." :
            @"Focus a field in another app, then return to Jaro to paste the annotation.");
        return;
    }
    NSUInteger preparationRevision = [self.systemDictation annotationPreparationRevision];
    NSPasteboard *board = NSPasteboard.generalPasteboard;
    NSInteger clipboardRevision = board.changeCount;
    [self sendToHelper:@"jard.annotationHandoff" payload:@{@"id": key, @"waitForNarration": @(fromPanel)}
                 request:nil completion:^(id result, NSString *error) {
        if (error) { reply(nil, error); return; }
        if (self.updating || self.systemDictation.hasActivity || (paste && self.annotation.busy) ||
            (fromPanel && ![self annotationResultAvailable])) {
            reply(nil, @"Finish the current action before sharing this annotation.");
            return;
        }
        NSString *markdown = [result[@"markdown"] isKindOfClass:NSString.class] ? result[@"markdown"] : nil;
        NSURL *sheet = [self annotationContactSheet:result];
        NSData *image = nil;
        if (sheet) {
            NSFileHandle *file = [NSFileHandle fileHandleForReadingFromURL:sheet error:nil];
            const NSUInteger limit = 12 * 1024 * 1024;
            image = [file readDataUpToLength:limit + 1 error:nil];
            [file closeFile];
            if (image.length > limit) image = nil;
        }
        if (paste) {
            [self.systemDictation pasteAnnotation:markdown image:image path:sheet.path destination:destination fromPanel:fromPanel
                preparationRevision:preparationRevision clipboardRevision:clipboardRevision reply:reply];
            return;
        }
        BOOL includedImage = NO;
        if (board.changeCount != clipboardRevision) {
            reply(nil, @"The clipboard changed while Jaro prepared the annotation. Copy it again to replace the current clipboard.");
            return;
        }
        if (!JardCopyAnnotationHandoff(board, clipboardRevision, markdown, image, &includedImage)) {
            reply(nil, @"Could not copy the annotation.");
            return;
        }
        reply(@{@"includedImage": @(includedImage)}, nil);
    }];
}

- (void)annotationChanged:(NSDictionary *)status {
    if (self.closing) return;
    if ([status[@"busy"] boolValue]) [self.annotationResultPanel endForNextCapture];
    [self.systemDictation setSystemAudioRecordingActive:[self isRecordingSystemAudio]];
    [self.systemDictation setAnnotationPhase:status[@"phase"]];
    [self.systemDictation setAnnotationActive:[status[@"busy"] boolValue] microphoneShared:[status[@"sharedMicrophone"] boolValue]];
    [self refreshMeetingAvailability];
    [self updateWindowClosePolicy];
    NSData *detail = [NSJSONSerialization dataWithJSONObject:status options:0 error:nil];
    if (detail) native_sdk_appkit_emit_window_event(self.host, 1, SDK_BYTES(@"jard-annotation"), detail.bytes, detail.length);
}

- (NSApplicationTerminateReply)requestSystemTermination {
    if (self.terminationApproved || self.closing) return NSTerminateNow;
    if (self.systemQuitPending) return NSTerminateLater;
    if (self.sdkCaptureID || (self.updating && !self.pendingUpdateRestart && !self.menuQuitPending))
        return NSTerminateCancel;
    if (!self.menuQuitPending && !self.meetings.busy && !self.annotation.busy && ![self.updater canInstallStagedUpdate])
        return NSTerminateNow;

    self.systemQuitPending = YES;
    if (!self.menuQuitPending) {
        __weak typeof(self) owner = self;
        // AppKit pumps a modal run loop while it waits for the reply to Quit.
        [NSRunLoop.mainRunLoop performInModes:@[NSRunLoopCommonModes, NSModalPanelRunLoopMode] block:^{
            if (owner.systemQuitPending) [owner quitApp:nil];
        }];
    }
    return NSTerminateLater;
}

- (void)replyToSystemQuit:(BOOL)allow {
    self.systemQuitPending = NO;
    [NSApp replyToApplicationShouldTerminate:allow];
}

- (void)terminateAfterCleanup {
    // A completed Quit is intentional even if model startup is still pending.
    [self confirmUpdateStartup];
    self.terminationApproved = YES;
    self.menuQuitPending = NO;
    if (self.systemQuitPending) [self replyToSystemQuit:YES];
    else [NSApp terminate:nil];
}

- (void)finishMenuQuitWithStagedUpdate:(BOOL)installable {
    if (self.showingApp && ![self hasActiveCapture] && installable) {
        self.updating = YES;
        [self refreshMeetingAvailability];
        [self.systemDictation pauseForUpdate:YES];
        [self sendToHelper:@"jard.updateQuiesce" payload:@{} request:nil completion:^(id result, NSString *error) {
            if (error || ![result[@"ready"] boolValue]) { [self terminateAfterCleanup]; return; }
            [self.updater installOnQuitWithCompletion:^(BOOL applied, NSString *failure) {
                if (!applied) NSLog(@"Jard deferred the staged update: %@", failure);
                [self terminateAfterCleanup];
            }];
        }];
        return;
    }
    [self terminateAfterCleanup];
}

- (void)cancelMenuQuitWhileSaving:(NSString *)message {
    self.menuQuitPending = NO;
    [self.updater resumeAfterCancelledQuit];
    [self resumeAfterControlExitFailure];
    [self updateMenuTitle:self.updater.status];
    [self emitUpdateStatus:self.updater.status];
    if (self.systemQuitPending) [self replyToSystemQuit:NO];
    [self showUpdateNotice:@"Jaro is still open." detail:message];
}

- (void)waitForMenuQuitUntil:(NSTimeInterval)deadline meeting:(BOOL)meeting annotation:(BOOL)annotation
               stagedUpdate:(BOOL)installable {
    if (self.closing || !self.menuQuitPending) return;
    if (meeting && !self.meetings.busy) {
        NSDictionary *status = self.meetings.status;
        if (![status[@"phase"] isEqual:@"finished"] || ![status[@"session"] length]) {
            [self cancelMenuQuitWhileSaving:status[@"message"] ?: @"The meeting could not be saved before quitting."];
            return;
        }
    }
    if (annotation && !self.annotation.busy) {
        NSDictionary *status = self.annotation.status;
        if (![@[@"finished", @"cancelled"] containsObject:status[@"phase"] ?: @""]) {
            [self cancelMenuQuitWhileSaving:status[@"message"] ?: @"The annotation could not be saved before quitting."];
            return;
        }
    }
    if (![self hasActiveCapture]) {
        [self finishMenuQuitWithStagedUpdate:installable];
        return;
    }
    if (NSProcessInfo.processInfo.systemUptime >= deadline) {
        [self cancelMenuQuitWhileSaving:@"Wait for the recording to finish saving before quitting."];
        return;
    }
    if (meeting && self.meetings.recording)
        [self.meetings request:@{@"action": @"stop"} reply:^(id result, NSString *error) {}];
    __weak typeof(self) owner = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
        [owner waitForMenuQuitUntil:deadline meeting:meeting annotation:annotation stagedUpdate:installable];
    });
}

- (void)quitApp:(id)sender {
    if (self.closing || self.terminationApproved || self.menuQuitPending) return;
    if (self.updating && !self.pendingUpdateRestart) {
        if (self.systemQuitPending) [self replyToSystemQuit:NO];
        return;
    }
    if (self.sdkCaptureID) {
        if (self.systemQuitPending) { [self replyToSystemQuit:NO]; return; }
        [self showUpdateNotice:@"Jard is handling an SDK recording." detail:@"Finish or cancel the recording in the connected app before quitting Jard."];
        return;
    }
    JardDictationReply superseded = self.pendingUpdateRestart;
    self.pendingUpdateRestart = nil;
    self.updateRestartInProgress = NO;
    self.menuQuitPending = YES;
    BOOL installable = [self.updater prepareToInstallOnQuit];
    if (superseded) superseded([self.updater status], nil);
    BOOL meeting = self.meetings.busy, annotation = self.annotation.busy;
    if (!meeting && !annotation) {
        [self finishMenuQuitWithStagedUpdate:installable];
        return;
    }
    self.updating = YES;
    [self refreshMeetingAvailability];
    [self updateWindowClosePolicy];
    [self updateMenuTitle:self.updater.status];
    [self emitUpdateStatus:self.updater.status];
    if (!self.systemDictation.hasActivity) [self.systemDictation pauseForUpdate:YES];
    if (annotation) [self.annotation stop];
    NSTimeInterval deadline = NSProcessInfo.processInfo.systemUptime + (annotation ? 40 : 30);
    [self waitForMenuQuitUntil:deadline meeting:meeting annotation:annotation stagedUpdate:installable];
}

- (void)toggleAutomaticUpdates:(NSMenuItem *)item {
    if ([self updatesManaged]) return;
    BOOL enabled = item.state != NSControlStateValueOn;
    [NSUserDefaults.standardUserDefaults setBool:enabled forKey:@"JardCheckForUpdatesOnLaunch"];
    item.state = enabled ? NSControlStateValueOn : NSControlStateValueOff;
}

- (BOOL)validateMenuItem:(NSMenuItem *)item {
    if (item.action == @selector(toggleAutomaticUpdates:)) return ![self updatesManaged];
    if (item.action == @selector(quitApp:)) return YES;
    if (item.action == @selector(updateFromMenu:)) return [self canChooseUpdate:self.updater.status];
    return self.showingApp && !self.closing;
}

- (void)shutdown {
    if (self.closing) return;
    [self endCapturePermissionWatch];
    [self.annotationResultPanel dismiss];
    [self.annotationRoutePanel dismiss];
    [self.annotation shutdown];
    [self.meetings shutdown];
    [self removeMeetingMenu];
    [self.systemDictation shutdown];
    [self.updater cancel];
    self.pendingUpdateRestart = nil;
    [self.updateTimer invalidate];
    self.updateTimer = nil;
    self.closing = YES;
    [self stopObservingInputDevices];
    [self.nativePending removeAllObjects];
    self.preparing = NO;
    [self.prepareWaiters removeAllObjects];
    if (self.dragMonitor) { [NSEvent removeMonitor:self.dragMonitor]; self.dragMonitor = nil; }
    [NSNotificationCenter.defaultCenter removeObserver:self];
    if (NSApp.delegate == self.terminationDelegate) NSApp.delegate = self.terminationDelegate.upstream;
    self.terminationDelegate = nil;
    [self.input closeAndReturnError:nil];
    self.input = nil;
    // EOF lets the helper finalize any accepted audio and release its client lease.
}
@end

static void bridgeMessage(void *context, uint64_t window, const char *label, size_t labelLength,
                          const char *data, size_t length, const char *origin, size_t originLength) {
    if (window != 1 || labelLength != 4 || memcmp(label, "main", 4) || length > 131072) return;
    JardHost *app = (__bridge JardHost *)context;
    NSData *json = [NSData dataWithBytes:data length:length];
    NSString *source = [[NSString alloc] initWithBytes:origin length:originLength encoding:NSUTF8StringEncoding];
    [app invoke:[NSJSONSerialization JSONObjectWithData:json options:0 error:nil] origin:source];
}

static void hostEvent(void *context, const native_sdk_appkit_event_t *event) {
    JardHost *app = (__bridge JardHost *)context;
    if (event->kind == NATIVE_SDK_APPKIT_EVENT_SHUTDOWN) [app shutdown];
    else if (event->kind == NATIVE_SDK_APPKIT_EVENT_APP_ACTIVATED) [app applicationActivated];
    else if (event->kind == NATIVE_SDK_APPKIT_EVENT_APP_DEACTIVATED) app.startupActivationDeadline = 0;
    else if (event->kind == NATIVE_SDK_APPKIT_EVENT_WINDOW_FRAME && event->window_id == 1) {
        if (!event->open || event->hidden) [app.systemDictation stopSoundPreview];
        if (event->open) [app updateDockPresentation];
    }
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        signal(SIGPIPE, SIG_IGN);
        if (argc > 1 && !strcmp(argv[1], "--jaro-relaunch-update")) return JardRunUpdateRelaunch(argc, argv);
        if (argc == 2 && !strcmp(argv[1], "--settings-apply-offline"))
            return JardApplyOfflineDictationSettings(JardForcedBoolean(CFSTR("DisableAnnotate")));
        int preferencesLease = JardLockDictationPreferences(NO);
        if (preferencesLease < 0) {
            NSLog(@"Jard could not lock its settings for startup.");
            return 1;
        }
        NSBundle *bundle = NSBundle.mainBundle;
        NSURL *resources = bundle.resourceURL;
        NSString *title = [bundle objectForInfoDictionaryKey:@"CFBundleDisplayName"] ?: @"Jaro";
        NSString *identifier = bundle.bundleIdentifier ?: @"local.jardaudio.desktop";
        JardHost *app = [[JardHost alloc] init];
        if ([NSProcessInfo.processInfo.environment[@"JARD_UPDATE_ROLLED_BACK"] isEqual:@"1"])
            app.updateRestartError = updateRollbackMessage;
        app.hiddenLaunchRequested = [NSProcessInfo.processInfo.arguments containsObject:@"--hidden"];
        app.pending = NSMutableDictionary.dictionary;
        app.nativePending = NSMutableDictionary.dictionary;
        app.loginItem = JardLoginItem.new;
        __weak JardHost *weakApp = app;
        app.systemDictation = [[JardSystemDictation alloc] initWithSend:^(NSString *command, NSDictionary *payload,
                JardDictationReply completion) {
            [weakApp sendToHelper:command payload:payload request:nil completion:completion];
        }];
        app.systemDictation.activityChanged = ^{
            JardHost *owner = weakApp;
            if (owner.menuQuitPending && !owner.systemDictation.hasActivity)
                [owner.systemDictation pauseForUpdate:YES];
            [owner refreshUpdateRestartAvailability];
        };
        app.systemDictation.accessibilityGrantedInSettings = ^{
            [weakApp returnFromPermissionSettings];
        };
        app.systemDictation.phaseChanged = ^(NSString *phase) {
            JardHost *owner = weakApp;
            if (!owner.showingApp || !owner.bridge.running || owner.closing || owner.updating || [phase isEqual:@"idle"]) return;
            [owner sendToHelper:@"jard.diagnosticPhase" payload:@{@"phase": phase} request:nil completion:nil];
        };
        app.systemDictation.preferencesChanged = ^(NSDictionary *status) {
            JardHost *owner = weakApp;
            if (owner.closing) return;
            BOOL show = [status[@"annotationShowOverlays"] boolValue];
            owner.annotation.showOverlaysInRecordings = show;
            owner.annotationResultPanel.showOverlaysInRecordings = show;
            owner.annotationRoutePanel.showOverlaysInRecordings = show;
            if (!owner.showingApp) return;
            [owner updateMeetingMenu:nil];
            NSData *detail = [NSJSONSerialization dataWithJSONObject:status options:0 error:nil];
            if (detail) native_sdk_appkit_emit_window_event(owner.host, 1, SDK_BYTES(@"jard-dictation-preferences"), detail.bytes, detail.length);
        };
        app.systemDictation.microphonePreferenceChanged = ^(NSString *uid) {
            JardHost *owner = weakApp;
            if (!owner.bridge.running || owner.closing || owner.updating) return;
            [owner sendToHelper:@"jard.meetingInput" payload:@{@"inputDevice": uid ?: @""} request:nil completion:nil];
        };
        app.bridgeURL = [[bundle.bundleURL URLByAppendingPathComponent:@"Contents/Helpers"] URLByAppendingPathComponent:@"jard-ui"];
        app.annotationManaged = JardForcedBoolean(CFSTR("DisableAnnotate"));
        app.annotationAccess = JardAnnotationAccess.new;
        app.annotationAccess.settingsVisitChanged = ^(BOOL granted) {
            [weakApp capturePermissionVisitChanged:JardScreenRecordingPermission granted:granted];
        };
        app.annotationAccess.mayProbe = ^BOOL{
            JardHost *owner = weakApp;
            return owner.systemDictation.annotationEnabled && owner.window.visible && !owner.closing && !owner.updating && ![owner hasActiveCapture];
        };
        app.annotation = [[JardAnnotation alloc] initWithSend:^(NSString *command, NSDictionary *payload, JardDictationReply completion) {
            [weakApp sendToHelper:command payload:payload request:nil completion:completion];
        } stateChanged:^(NSDictionary *status) {
            [weakApp annotationChanged:status];
        }];
        void (^annotationAction)(NSString *, BOOL, JardDictationReply) = ^(NSString *session, BOOL paste, JardDictationReply reply) {
            JardHost *owner = weakApp;
            if (paste) [owner shareAnnotation:session paste:YES fromPanel:YES reply:reply];
            else [owner inspectAnnotation:session reply:reply];
        };
        void (^routeAction)(NSString *, NSString *, BOOL, JardDictationReply) = ^(NSString *capture, NSString *action,
                                                                                  BOOL held, JardDictationReply reply) {
            if ([action isEqual:@"cancel"] && reply) {
                [weakApp sendToHelper:@"jard.annotationRoute" payload:@{@"id": capture, @"action": @"cancel"} request:nil completion:reply];
            } else if ([action isEqual:@"undo"] && reply) {
                [weakApp sendToHelper:@"jard.annotationRouteUndo" payload:@{@"id": capture} request:nil completion:reply];
            } else {
                [weakApp sendToHelper:@"jard.annotationRoutePanel" payload:@{@"capture": capture, @"action": action, @"held": @(held)}
                             request:nil completion:nil];
            }
        };
        app.annotationResultPanel = [[JardAnnotationResultPanel alloc] initWithAvailability:^BOOL{
            return [weakApp annotationResultAvailable];
        } action:annotationAction routeAction:routeAction];
        app.annotation.saved = ^(NSString *session, NSImage *preview, NSDictionary *routing) {
            [weakApp.annotationResultPanel showSession:session preview:preview routing:routing];
        };
        app.annotationRoutePanel = [[JardAnnotationResultPanel alloc] initWithAvailability:^BOOL{
            JardHost *owner = weakApp;
            return owner.showingApp && owner.bridge.running && !owner.closing && !owner.updating &&
                !owner.annotationManaged && [owner.systemDictation annotationRoutingDisplayAvailable];
        } action:annotationAction routeAction:routeAction];
        void (^annotationCue)(NSString *) = ^(NSString *name) { [weakApp.systemDictation playAnnotationCue:name]; };
        app.annotationResultPanel.playCue = annotationCue;
        app.annotationRoutePanel.playCue = annotationCue;
        BOOL showAnnotationOverlays = [app.systemDictation.status[@"annotationShowOverlays"] boolValue];
        app.annotation.showOverlaysInRecordings = showAnnotationOverlays;
        app.annotationResultPanel.showOverlaysInRecordings = showAnnotationOverlays;
        app.annotationRoutePanel.showOverlaysInRecordings = showAnnotationOverlays;
        if (!JardForcedBoolean(CFSTR("DisableOpenCodeProcessing"))) {
            JardDictationSend destinationRequest = ^(NSString *command, NSDictionary *payload, JardDictationReply reply) {
                [weakApp sendToHelper:command payload:payload request:nil completion:reply];
            };
            app.annotationResultPanel.destinationRequest = destinationRequest;
            app.annotationRoutePanel.destinationRequest = destinationRequest;
        }
        if (!app.annotationManaged) {
            [app.systemDictation setAnnotationInput:^BOOL(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL chordFree) {
                JardHost *owner = weakApp;
                BOOL allowStart = chordFree && [owner canStartAnnotation];
                return [owner.annotation handleEvent:type event:event shortcut:shortcut allowStart:allowStart start:^(CGPoint pointer) {
                    JardHost *target = weakApp;
                    [target controlAnnotation:@"start" parameters:@{} quartzPointer:[NSValue valueWithPoint:pointer]
                        reply:^(id result, NSString *error) {}];
                }];
            }];
        }
        app.meetingAudioAccess = JardMeetingAudioAccess.new;
        app.meetingAudioAccess.settingsVisitChanged = ^(BOOL granted) {
            [weakApp capturePermissionVisitChanged:JardMacAudioPermission granted:granted];
        };
        app.meetings = [[JardMeetings alloc] initWithManaged:JardForcedBoolean(CFSTR("DisableMeetings"))
                start:^(NSDictionary *options, BOOL prompt, JardDictationReply completion) {
            [weakApp startMeeting:options prompt:prompt reply:completion];
        } send:^(NSString *command, NSDictionary *payload, JardDictationReply completion) {
            [weakApp sendToHelper:command payload:payload request:nil completion:completion];
        } changed:^(NSDictionary *status) {
            [weakApp meetingChanged:status];
        }];
        app.meetings.showSystemAudioSettings = ^{ [weakApp.meetingAudioAccess showSettings]; };
        app.meetings.openWorkspace = ^{
            JardHost *owner = weakApp;
            if (!owner || owner.closing) return;
            [owner.window makeKeyAndOrderFront:nil];
            [NSApp activateIgnoringOtherApps:YES];
        };
        [app.systemDictation setMeetingPasteAction:^{
            JardHost *owner = weakApp;
            [owner.systemDictation enqueueMeetingPaste:^(JardPasteExcerpt ready) {
                [owner.meetings preparePaste:ready];
            }];
        }];
        NSString *port = NSProcessInfo.processInfo.environment[@"RUMBLE_PORT"] ?: @"8765";
        NSInteger portNumber = port.integerValue;
        if (portNumber < 1 || portNumber > 65535 || ![port isEqual:@(portNumber).stringValue]) return 1;
        app.origin = [NSString stringWithFormat:@"http://127.0.0.1:%ld", (long)portNumber];
        NSURL *setupURL = [resources URLByAppendingPathComponent:@"setup.html"];
#ifdef JARD_TESTING
        NSDictionary *env = NSProcessInfo.processInfo.environment;
        if (env[@"JARD_TEST_HELPER"]) app.bridgeURL = [NSURL fileURLWithPath:env[@"JARD_TEST_HELPER"]];
        if (env[@"JARD_TEST_SETUP"]) setupURL = [NSURL fileURLWithPath:env[@"JARD_TEST_SETUP"]];
#endif
        NSString *setup = [NSString stringWithContentsOfURL:setupURL encoding:NSUTF8StringEncoding error:nil];
        if (!setup) setup = @"<p>The setup page is missing. Reinstall Jaro.</p>";
        NSString *version = [bundle objectForInfoDictionaryKey:@"CFBundleVersion"] ?: @"0.0.0";
        app.host = native_sdk_appkit_create(SDK_BYTES(title), SDK_BYTES(title), SDK_BYTES(version),
            SDK_BYTES(@"Private local audio transcription"), 1, 0, 1, SDK_BYTES(title), SDK_BYTES(identifier),
            NULL, 0, SDK_BYTES(@"main"), 0, 0, 1050, 760, 0, 2, 1, 1, 2 /* hidden_inset_tall */, 2 /* hidden until launch */, 0);
        if (!app.host) return 1;
        NSDictionary *updates = [bundle objectForInfoDictionaryKey:@"JardUpdates"];
        app.updatesDisabledAtLaunch = [NSUserDefaults.standardUserDefaults boolForKey:@"DisableUpdates"];
        if (!app.updatesDisabledAtLaunch) {
            app.updater = [[JardUpdater alloc] initWithConfiguration:updates bundle:bundle stateChanged:^(NSDictionary *status) {
                [weakApp updateChanged:status];
            }];
        }
        if (app.updater) {
            NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
            [defaults registerDefaults:@{@"JardCheckForUpdatesOnLaunch": updates[@"check_on_start"] ?: @NO}];
            BOOL automatic = [defaults boolForKey:@"JardCheckForUpdatesOnLaunch"];
            NSMenu *menu = NSApp.mainMenu.itemArray.firstObject.submenu;
            app.updateMenu = [menu insertItemWithTitle:@"Check for Updates…" action:@selector(updateFromMenu:) keyEquivalent:@"" atIndex:1];
            app.updateMenu.target = app;
            NSMenuItem *toggle = [menu insertItemWithTitle:@"Check for Updates on Launch"
                action:@selector(toggleAutomaticUpdates:) keyEquivalent:@"" atIndex:2];
            app.automaticUpdatesMenu = toggle;
            toggle.target = app;
            toggle.state = automatic ? NSControlStateValueOn : NSControlStateValueOff;
            [app.updater restoreStagedUpdateWithCompletion:nil];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                [weakApp checkForScheduledUpdate:NO];
            });
            app.updateTimer = [NSTimer scheduledTimerWithTimeInterval:60 repeats:YES block:^(NSTimer *timer) {
                [weakApp checkForScheduledUpdate:NO];
            }];
        }
        NSMenu *applicationMenu = NSApp.mainMenu.itemArray.firstObject.submenu;
        for (NSMenuItem *item in applicationMenu.itemArray) {
            if (item.action != @selector(terminate:)) continue;
            item.target = app;
            item.action = @selector(quitApp:);
        }
        [app installWindowChrome];
        NSMenu *file = [NSApp.mainMenu itemWithTitle:@"File"].submenu;
        NSMenuItem *open = [file insertItemWithTitle:@"Open in Browser" action:@selector(openBrowser:) keyEquivalent:@"b" atIndex:0];
        open.keyEquivalentModifierMask = NSEventModifierFlagCommand | NSEventModifierFlagShift;
        open.target = app;
        [app rememberMenuLabels:NSApp.mainMenu];
        [app refreshInterfaceLanguage];
        native_sdk_appkit_set_window_content_min_size(app.host, 1, 740, 500);
        NSString *origins = [@"zero://inline\n" stringByAppendingString:app.origin];
        native_sdk_appkit_set_security_policy(app.host, SDK_BYTES(origins), NULL, 0, 0);
        native_sdk_appkit_set_bridge_callback(app.host, bridgeMessage, (__bridge void *)app);
        [app watchUpdateStartup];
        native_sdk_appkit_load_webview(app.host, SDK_BYTES(setup), 0, NULL, 0, NULL, 0, NULL, 0, 0);
        native_sdk_appkit_run(app.host, hostEvent, (__bridge void *)app);
        [app shutdown];
        native_sdk_appkit_destroy(app.host);
        close(preferencesLease);
        return 0;
    }
}
