#import <AppKit/AppKit.h>
#import <Carbon/Carbon.h>
#import <CoreAudio/CoreAudio.h>
#import <WebKit/WebKit.h>
#import <objc/runtime.h>
#import <signal.h>
#import <unistd.h>
#import "../jard_interface_language.h"
#import "../jard_meetings.h"
#import "../jard_updater.h"
#import "../system_dictation.h"

@interface JardHost : NSObject
@property(nonatomic, readonly) NSWindow *window;
@property(nonatomic, readonly) BOOL showingApp;
@property(nonatomic, readonly) NSStatusItem *meetingStatusItem;
@property(nonatomic, readonly) NSDictionary<NSString *, NSMenuItem *> *meetingMenuItems;
@property(nonatomic, readonly) NSMenuItem *updateMenu;
@property(nonatomic, readonly) JardUpdater *updater;
@property(nonatomic, readonly) JardSystemDictation *systemDictation;
@property(nonatomic, readonly) BOOL annotationManaged;
@property(nonatomic, readonly) AudioObjectPropertyListenerBlock inputDeviceListener;
@property(nonatomic, readonly) NSUInteger inputDeviceNotifications;
- (void)completeWindowLaunch:(NSAppleEventDescriptor *)event;
- (void)setShowDockIcon:(BOOL)enabled;
@property(nonatomic, readonly) NSDictionary *windowNavigation;
- (void)replayDictationSetupShowingWindow:(BOOL)show reply:(JardDictationReply)reply;
- (void)controlWindowAction:(NSString *)action pane:(NSString *)pane reply:(void (^)(id result, NSString *error))reply;
- (NSDictionary *)dockIconStatus;
- (void)refreshInterfaceLanguage;
- (void)applyControlSettings:(NSDictionary *)request reply:(JardDictationReply)reply;
- (NSDictionary *)workspaceUpdateStatus:(NSDictionary *)status;
- (void)performUpdateAction:(NSString *)action payload:(NSDictionary *)payload reply:(JardDictationReply)reply;
- (void)sendToHelper:(NSString *)command payload:(NSDictionary *)payload request:(NSString *)request completion:(JardDictationReply)completion;
- (void)refreshMeetingAvailability;
- (void)quitApp:(id)sender;
- (void)showUpdateNotice:(NSString *)title detail:(NSString *)message;
- (void)waitForMenuQuitUntil:(NSTimeInterval)deadline meeting:(BOOL)meeting annotation:(BOOL)annotation
               stagedUpdate:(BOOL)staged;
- (void)updateChanged:(NSDictionary *)status;
- (void)updateMeetingMenu:(NSDictionary *)status;
- (void)menuNeedsUpdate:(NSMenu *)menu;
- (BOOL)canStartAnnotation;
- (void)controlAnnotation:(NSString *)action parameters:(NSDictionary *)parameters reply:(JardDictationReply)reply;
- (void)controlAnnotationAccess:(NSString *)action reply:(JardDictationReply)reply;
@end

@interface JardMeetings (WindowFixture)
- (void)copyLatestTranscriptTo:(NSPasteboard *)board reply:(JardDictationReply)reply;
@end

static NSString *scenario;
static NSURL *fixtureRoot;
static __weak JardHost *fixtureHost;
static void (*normalLaunch)(JardHost *, SEL, NSNotification *);
static void (*normalMeetingMenu)(JardHost *, SEL);
static NSDictionary *(*normalUpdateStatus)(JardUpdater *, SEL);
static BOOL stagedRefresh;
static dispatch_block_t finalUpdateCheck;
static JardDictationReply updateDrain, updateReopen;
static JardUpdateInstallReply updateInstall;
static NSUInteger meetingStopsOnQuit, menuQuitTerminations;
static NSString *menuQuitNotice;
static NSMutableArray<NSNumber *> *systemQuitReplies;
static void (*normalSend)(JardHost *, SEL, NSString *, NSDictionary *, NSString *, JardDictationReply);

static NSDictionary *fixtureUpdateStatus(JardUpdater *updater, SEL selector) {
    if ([scenario isEqual:@"update-restart"] || [scenario isEqual:@"quit-capture"])
        return @{@"phase": @"ready", @"staged": @YES, @"version": @"2"};
    if (stagedRefresh) return @{@"phase": @"checking", @"staged": @YES, @"version": @"2"};
    return normalUpdateStatus(updater, selector);
}

static BOOL fixtureCanInstall(JardUpdater *updater, SEL selector) { return YES; }

static void fixtureFinalUpdateCheck(JardUpdater *updater, SEL selector, dispatch_block_t finish) {
    finalUpdateCheck = finish;
}

static void fixtureRestart(JardUpdater *updater, SEL selector, JardUpdateInstallReply completion) {
    updateInstall = completion;
}

static void fixtureSend(JardHost *host, SEL selector, NSString *command, NSDictionary *payload, NSString *request,
                        JardDictationReply completion) {
    if ([command isEqual:@"jard.updateQuiesce"]) updateDrain = completion;
    else if ([command isEqual:@"jard.updateAbort"]) updateReopen = completion;
    else if ([scenario isEqual:@"quit-capture"] && [command isEqual:@"jard.stopMeeting"]) {
        meetingStopsOnQuit++;
        completion(@{}, nil);
    }
    else normalSend(host, selector, command, payload, request, completion);
}

static void fixtureMenuQuitNotice(JardHost *host, SEL selector, NSString *title, NSString *message) {
    menuQuitNotice = message;
}

static void fixtureMenuTerminate(NSApplication *app, SEL selector, id sender) {
    menuQuitTerminations++;
}

static void fixtureSystemQuitReply(NSApplication *app, SEL selector, BOOL terminate) {
    [systemQuitReplies addObject:@(terminate)];
}

static void verifyQuitDuringCapture(JardHost *host) {
    JardMeetings *meeting = [host valueForKey:@"meetings"];
    id annotation = [host valueForKey:@"annotation"];
    NSString *identifier = NSUUID.UUID.UUIDString;
    id<NSApplicationDelegate> delegate = NSApp.delegate;
    assert([delegate respondsToSelector:@selector(applicationShouldHandleReopen:hasVisibleWindows:)]);
    systemQuitReplies = NSMutableArray.new;

    [host setValue:identifier forKey:@"sdkCaptureID"];
    assert([delegate applicationShouldTerminate:NSApp] == NSTerminateCancel);
    [host setValue:nil forKey:@"sdkCaptureID"];

    [meeting setValue:identifier forKey:@"recordingID"];
    [meeting setValue:@"saving" forKey:@"phase"];
    assert([delegate applicationShouldTerminate:NSApp] == NSTerminateLater);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.05]];
    assert([[host valueForKey:@"menuQuitPending"] boolValue] && !updateDrain);
    [host waitForMenuQuitUntil:0 meeting:YES annotation:NO stagedUpdate:YES];
    assert(![[host valueForKey:@"updating"] boolValue] && [menuQuitNotice containsString:@"finish saving"]);
    assert(![[host.updater valueForKey:@"quitting"] boolValue] && !updateDrain);
    assert([systemQuitReplies isEqual:@[@NO]]);
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.15]];
    [meeting setValue:nil forKey:@"recordingID"];
    [meeting setValue:@"idle" forKey:@"phase"];
    menuQuitNotice = nil;

    [meeting setValue:identifier forKey:@"recordingID"];
    [meeting setValue:@"saving" forKey:@"phase"];
    [host quitApp:nil];
    [meeting handleCapture:@{@"event": @"error", @"meeting": identifier, @"message": @"Fixture could not save the meeting."}];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.15]];
    assert([menuQuitNotice isEqual:@"Fixture could not save the meeting."] && !updateDrain);
    assert(![[host valueForKey:@"updating"] boolValue] && ![[host.updater valueForKey:@"quitting"] boolValue]);
    menuQuitNotice = nil;

    __block BOOL superseded = NO;
    [host performUpdateAction:@"restart" payload:@{} reply:^(id result, NSString *error) { superseded = result && !error; }];
    assert(finalUpdateCheck && !superseded);
    [meeting setValue:identifier forKey:@"recordingID"];
    [meeting setValue:@"starting" forKey:@"phase"];
    [annotation setValue:NSUUID.UUID.UUIDString forKey:@"identifier"];
    [annotation setValue:@"saving" forKey:@"phase"];
    [host quitApp:nil];
    assert(superseded && !updateDrain && !meetingStopsOnQuit);
    assert([delegate applicationShouldTerminate:NSApp] == NSTerminateLater);
    assert([delegate applicationShouldTerminate:NSApp] == NSTerminateLater);
    finalUpdateCheck();
    [meeting handleCapture:@{@"event": @"started", @"meeting": identifier}];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.15]];
    assert(meetingStopsOnQuit == 1 && !meeting.recording && meeting.busy && !updateDrain);
    [host quitApp:nil];
    assert(meetingStopsOnQuit == 1);
    [meeting handleCapture:@{@"event": @"accepted", @"meeting": identifier, @"id": NSUUID.UUID.UUIDString}];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.15]];
    assert(!meeting.busy && !updateDrain);
    [annotation setValue:nil forKey:@"identifier"];
    [annotation setValue:@"finished" forKey:@"phase"];
    [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.15]];
    assert(updateDrain && !updateInstall && !menuQuitTerminations && !menuQuitNotice);
    updateDrain(@{@"ready": @YES}, nil);
    assert(updateInstall && !menuQuitTerminations);
    updateInstall(YES, nil);
    assert(!menuQuitTerminations && ([systemQuitReplies isEqual:@[@NO, @YES]]));
    assert([delegate applicationShouldTerminate:NSApp] == NSTerminateNow);
}

static void verifyRestartTransaction(JardHost *host) {
    NSMenuItem *menu = host.meetingMenuItems[@"update"];
    __block NSString *failure;
    JardDictationReply completed = ^(id result, NSString *error) { assert(!result); failure = error; };

    [host performUpdateAction:@"restart" payload:@{} reply:completed];
    assert(finalUpdateCheck && !failure && !menu.enabled && [menu.title isEqual:@"Restarting…"]);
    assert([[[host workspaceUpdateStatus:host.updater.status] objectForKey:@"restartInProgress"] boolValue]);
    [host setValue:@YES forKey:@"otherRecording"];
    finalUpdateCheck();
    NSDictionary *state = [host workspaceUpdateStatus:host.updater.status];
    assert([failure containsString:@"recording"] && !updateDrain);
    assert([state[@"restartBlocked"] boolValue] && ![state[@"restartInProgress"] boolValue]);
    [host setValue:@NO forKey:@"otherRecording"];

    failure = nil;
    [host performUpdateAction:@"restart" payload:@{} reply:completed];
    assert(!failure && finalUpdateCheck);
    finalUpdateCheck();
    assert(updateDrain && !failure);
    updateDrain(@{@"ready": @YES}, nil);
    assert(updateInstall && !updateReopen && !failure && !menu.enabled);
    assert([[host.systemDictation valueForKey:@"updatePaused"] boolValue]);
    updateInstall(NO, @"Fixture could not replace the application.");
    assert(updateReopen && !failure && !menu.enabled);
    assert([[host.systemDictation valueForKey:@"updatePaused"] boolValue]);
    assert([[[host workspaceUpdateStatus:host.updater.status] objectForKey:@"restartInProgress"] boolValue]);
    [host performUpdateAction:@"restart" payload:@{} reply:^(id result, NSString *error) { assert(!result && error); }];
    updateReopen(@{@"ready": @YES}, nil);
    state = [host workspaceUpdateStatus:host.updater.status];
    assert([failure isEqual:@"Fixture could not replace the application."] && [state[@"restartError"] isEqual:failure]);
    assert([state[@"staged"] boolValue] && ![state[@"restartInProgress"] boolValue] && menu.enabled);
    assert(![[host.systemDictation valueForKey:@"updatePaused"] boolValue]);
    finalUpdateCheck = nil;
    updateDrain = nil;
    updateReopen = nil;
    updateInstall = nil;
}

static void fixtureMeetingMenu(JardHost *host, SEL selector) {
    if ([scenario hasSuffix:@"-unavailable"]) return;
    normalMeetingMenu(host, selector);
}

static void report(NSString *name, NSDictionary *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
    [data writeToURL:[fixtureRoot URLByAppendingPathComponent:name] atomically:YES];
}

static WKWebView *webView(NSView *view) {
    if ([view isKindOfClass:WKWebView.class]) return (WKWebView *)view;
    for (NSView *child in view.subviews) {
        WKWebView *web = webView(child);
        if (web) return web;
    }
    return nil;
}

static void fixtureLaunch(JardHost *host, SEL selector, NSNotification *notification) {
    fixtureHost = host;
    BOOL visibleBeforeLaunch = host.window.visible;
    if ([scenario isEqual:@"normal"] || [scenario isEqual:@"annotation-managed"] || [scenario isEqual:@"managed-updates"] ||
        [scenario isEqual:@"launch-update"] || [scenario isEqual:@"focus-update"] || [scenario isEqual:@"update-restart"] ||
        [scenario isEqual:@"application-settings"] || [scenario isEqual:@"web-recovery"] || [scenario isEqual:@"quit-capture"] ||
        [scenario hasPrefix:@"locale-"] || [scenario hasPrefix:@"dock-cold-"] || [scenario hasPrefix:@"hidden-"] ||
        [scenario isEqual:@"dock-setting"] || [scenario isEqual:@"dock-unavailable"]) {
        normalLaunch(host, selector, notification);
    } else {
        NSAppleEventDescriptor *event = [NSAppleEventDescriptor appleEventWithEventClass:kCoreEventClass
            eventID:kAEOpenApplication targetDescriptor:[NSAppleEventDescriptor nullDescriptor]
            returnID:kAutoGenerateReturnID transactionID:kAnyTransactionID];
        [event setParamDescriptor:[NSAppleEventDescriptor descriptorWithEnumCode:keyAELaunchedAsLogInItem]
                       forKeyword:keyAEPropData];
        [host completeWindowLaunch:event];
        if (![scenario isEqual:@"login-idle"]) {
            [NSNotificationCenter.defaultCenter postNotificationName:NSApplicationDidBecomeActiveNotification object:NSApp];
        }
    }
    report(@"launched.json", @{@"visibleBeforeLaunch": @(visibleBeforeLaunch), @"visible": @(host.window.visible)});
}

static BOOL fixtureQuietAtLogin(id self, SEL selector) {
    BOOL badListener = [scenario isEqual:@"login-input"] || [scenario isEqual:@"dock-cold-input"];
    return !badListener || ![[self valueForKey:@"workspace"] boolValue];
}

static BOOL fixtureAnnotationManaged(id self, SEL selector) { return YES; }

static void fixtureCopyLatestTranscript(JardMeetings *meeting, SEL selector, JardDictationReply reply) {
    NSPasteboard *board = NSPasteboard.pasteboardWithUniqueName;
    [meeting copyLatestTranscriptTo:board reply:^(id result, NSString *error) {
        report(@"latest-meeting-copy.json", @{@"text": [board stringForType:NSPasteboardTypeString] ?: @"",
            @"error": error ?: @"", @"copied": result[@"copied"] ?: @NO});
        [board releaseGlobally];
        reply(result, error);
    }];
}

@interface JardWindowFixture : NSObject
@end

@implementation JardWindowFixture

+ (void)load {
    if (![NSBundle.mainBundle.bundleIdentifier hasPrefix:@"local.jardaudio.window-fixture."]) return;
    NSDictionary *env = NSProcessInfo.processInfo.environment;
    NSString *root = env[@"JARD_WINDOW_FIXTURE_ROOT"];
    scenario = env[@"JARD_WINDOW_FIXTURE_SCENARIO"];
    if (!root.length || !scenario.length) return;
    fixtureRoot = [NSURL fileURLWithPath:root isDirectory:YES];
    Method launch = class_getInstanceMethod(NSClassFromString(@"JardHost"), NSSelectorFromString(@"applicationStarted:"));
    normalLaunch = (void (*)(JardHost *, SEL, NSNotification *))method_setImplementation(launch, (IMP)fixtureLaunch);
    if ([scenario isEqual:@"annotation-managed"]) {
        Method policy = class_getInstanceMethod(NSClassFromString(@"JardHost"), @selector(annotationManaged));
        method_setImplementation(policy, (IMP)fixtureAnnotationManaged);
    }
    if ([scenario isEqual:@"normal"]) {
        Method copy = class_getInstanceMethod(JardMeetings.class, @selector(copyLatestTranscript:));
        method_setImplementation(copy, (IMP)fixtureCopyLatestTranscript);
    }
    if ([scenario hasSuffix:@"-unavailable"]) {
        Method menu = class_getInstanceMethod(NSClassFromString(@"JardHost"), NSSelectorFromString(@"createMeetingMenu"));
        normalMeetingMenu = (void (*)(JardHost *, SEL))method_setImplementation(menu, (IMP)fixtureMeetingMenu);
    }
    if ([@[@"dock-updates", @"update-restart", @"quit-capture"] containsObject:scenario]) {
        Method status = class_getInstanceMethod(JardUpdater.class, @selector(status));
        normalUpdateStatus = (NSDictionary *(*)(JardUpdater *, SEL))method_setImplementation(status, (IMP)fixtureUpdateStatus);
    }
    if ([@[@"update-restart", @"quit-capture"] containsObject:scenario]) {
        Class host = NSClassFromString(@"JardHost");
        Method send = class_getInstanceMethod(host, @selector(sendToHelper:payload:request:completion:));
        normalSend = (void (*)(JardHost *, SEL, NSString *, NSDictionary *, NSString *, JardDictationReply))
            method_setImplementation(send, (IMP)fixtureSend);
        method_setImplementation(class_getInstanceMethod(JardUpdater.class, @selector(canInstallStagedUpdate)), (IMP)fixtureCanInstall);
        method_setImplementation(class_getInstanceMethod(JardUpdater.class, @selector(checkBeforeRestart:)), (IMP)fixtureFinalUpdateCheck);
        method_setImplementation(class_getInstanceMethod(JardUpdater.class, @selector(restartWithCompletion:)), (IMP)fixtureRestart);
        if ([scenario isEqual:@"quit-capture"]) {
            method_setImplementation(class_getInstanceMethod(JardUpdater.class, @selector(installOnQuitWithCompletion:)), (IMP)fixtureRestart);
            method_setImplementation(class_getInstanceMethod(host, @selector(showUpdateNotice:detail:)), (IMP)fixtureMenuQuitNotice);
            method_setImplementation(class_getInstanceMethod(NSApplication.class, @selector(terminate:)), (IMP)fixtureMenuTerminate);
            method_setImplementation(class_getInstanceMethod(NSApplication.class, @selector(replyToApplicationShouldTerminate:)), (IMP)fixtureSystemQuitReply);
        }
    }
    if (![scenario isEqual:@"login-setup"]) {
        Method quiet = class_getInstanceMethod(NSClassFromString(@"JardSystemDictation"), NSSelectorFromString(@"quietAtLogin"));
        method_setImplementation(quiet, (IMP)fixtureQuietAtLogin);
    }

    dispatch_async(dispatch_get_main_queue(), ^{
        NSURL *path = [fixtureRoot URLByAppendingPathComponent:@"command"];
        __block NSPanel *overlay;
        NSTimer *poll = [NSTimer timerWithTimeInterval:.03 repeats:YES block:^(NSTimer *timer) {
            JardHost *host = fixtureHost;
            if (!host) return;
            NSString *command = [NSString stringWithContentsOfURL:path encoding:NSUTF8StringEncoding error:nil];
            if (!command.length || ![NSFileManager.defaultManager removeItemAtURL:path error:nil]) return;
            NSWindow *window = host.window;
            if ([command isEqual:@"close"]) [window performClose:nil];
            else if ([command isEqual:@"activate"]) {
                [NSNotificationCenter.defaultCenter postNotificationName:NSApplicationDidBecomeActiveNotification object:NSApp];
            } else if ([command isEqual:@"dock"]) {
                [NSApp.delegate applicationShouldHandleReopen:NSApp hasVisibleWindows:NO];
            } else if ([command isEqual:@"dock-off"]) [host setShowDockIcon:NO];
            else if ([command isEqual:@"dock-on"]) [host setShowDockIcon:YES];
            else if ([command isEqual:@"menu-off"]) host.meetingStatusItem.visible = NO;
            else if ([command isEqual:@"menu-on"]) host.meetingStatusItem.visible = YES;
            else if ([command isEqual:@"menu-copy-label-fr"]) {
                assert(JardSetInterfaceLanguage(@"fr"));
                [host refreshInterfaceLanguage];
                report(@"reply.json", @{@"title": host.meetingMenuItems[@"copyLatest"].title ?: @""});
                return;
            }
            else if ([command isEqual:@"menu-copy-latest"]) {
                NSMenuItem *item = host.meetingMenuItems[@"copyLatest"];
                NSInteger index = [item.menu indexOfItem:item];
                BOOL available = item && !item.hidden && item.enabled && index >= 0;
                if (available) [item.menu performActionForItemAtIndex:index];
                report(@"reply.json", @{@"available": @(available), @"title": item.title ?: @""});
                return;
            }
            else if ([command isEqual:@"menu-live-meeting"]) {
                __block NSString *idleFailure = nil;
                [host controlWindowAction:@"window-show" pane:@"live-meeting" reply:^(id result, NSString *error) { idleFailure = error; }];
                id meeting = [host valueForKey:@"meetings"];
                [meeting setValue:@"recording" forKey:@"phase"];
                NSMenuItem *open = host.meetingMenuItems[@"open"];
                NSInteger index = [open.menu indexOfItem:open];
                if (index >= 0) [open.menu performActionForItemAtIndex:index];
                report(@"reply.json", @{@"idleFailure": idleFailure ?: @"", @"pane": host.windowNavigation[@"pane"] ?: @"",
                    @"recording": @([[meeting valueForKey:@"phase"] isEqual:@"recording"]), @"visible": @(window.visible)});
                [meeting setValue:@"idle" forKey:@"phase"];
                return;
            }
            else if ([command isEqual:@"menu-open"]) {
                NSMenuItem *open = host.meetingMenuItems[@"open"];
                NSInteger index = [open.menu indexOfItem:open];
                if (index >= 0) [open.menu performActionForItemAtIndex:index];
            } else if ([command isEqual:@"application-check"] || [command isEqual:@"status-check"]) {
                NSMenuItem *item = [command isEqual:@"application-check"] ? host.updateMenu : host.meetingMenuItems[@"update"];
                NSMenu *menu = item.menu;
                [menu update];
                NSInteger index = item && menu ? [menu indexOfItem:item] : -1;
                if (index >= 0 && !item.hidden && item.enabled) [menu performActionForItemAtIndex:index];
            } else if ([command isEqual:@"annotation-toggle"]) {
                NSMutableDictionary *observed = NSMutableDictionary.dictionary;
                [host.systemDictation request:@{@"action": @"setAnnotationEnabled", @"enabled": @NO} reply:^(id state, NSString *error) {
                    observed[@"off"] = error ?: state[@"annotationEnabled"];
                }];
                [host controlAnnotation:@"start" parameters:@{@"source": @"display"} reply:^(id state, NSString *error) {
                    observed[@"start"] = error ?: @"Unexpected capture";
                }];
                [host controlAnnotation:@"toggle" parameters:@{} reply:^(id state, NSString *error) {
                    observed[@"toggleDisabled"] = error ?: @"Unexpected capture";
                }];
                [host controlAnnotation:@"status" parameters:@{} reply:^(id state, NSString *error) {
                    observed[@"status"] = error ?: state[@"enabled"];
                }];
                [host.systemDictation request:@{@"action": @"setAnnotationEnabled", @"enabled": @YES} reply:^(id state, NSString *error) {
                    observed[@"on"] = error ?: state[@"annotationEnabled"];
                }];
                id annotation = [host valueForKey:@"annotation"];
                id notify = [annotation valueForKey:@"changed"];
                [annotation setValue:nil forKey:@"changed"];
                [annotation setValue:NSUUID.UUID.UUIDString forKey:@"identifier"];
                [annotation setValue:@"picking" forKey:@"phase"];
                [host controlAnnotation:@"toggle" parameters:@{} reply:^(id state, NSString *error) {
                    observed[@"togglePicker"] = error ?: @((BOOL)([state[@"phase"] isEqual:@"picking"] && ![state[@"changed"] boolValue]));
                }];
                [annotation setValue:@"recording" forKey:@"phase"];
                [annotation setValue:@YES forKey:@"narrationFinished"];
                [host controlAnnotation:@"toggle" parameters:@{} reply:^(id state, NSString *error) {
                    observed[@"toggleRecording"] = error ?: @((BOOL)([state[@"phase"] isEqual:@"saving"] && [state[@"changed"] boolValue]));
                }];
                [annotation setValue:@NO forKey:@"narrationFinished"];
                [annotation setValue:nil forKey:@"identifier"];
                [annotation setValue:@"idle" forKey:@"phase"];
                [annotation setValue:nil forKey:@"message"];
                [annotation setValue:notify forKey:@"changed"];
                report(@"reply.json", observed);
                return;
            } else if ([command isEqual:@"annotation-policy"]) {
                NSMutableDictionary *failures = NSMutableDictionary.dictionary;
                NSMutableDictionary *permissionFailures = NSMutableDictionary.dictionary;
                if (!host.annotationManaged) [host setValue:@YES forKey:@"otherRecording"];
                for (NSString *action in @[@"status", @"allow", @"repair", @"restart"]) {
                    [host controlAnnotationAccess:action reply:^(id state, NSString *error) {
                        if (error) permissionFailures[action] = error;
                        else if (![action isEqual:@"status"] || ![state[@"authorization"] isEqual:@"checking"])
                            permissionFailures[action] = @"Permission command changed state unexpectedly";
                    }];
                }
                if (!host.annotationManaged) [host setValue:@NO forKey:@"otherRecording"];
                if (host.annotationManaged) {
                    for (NSString *source in @[@"display", @"window", @"area", @"phone"]) {
                        [host controlAnnotation:@"start" parameters:@{@"source": source} reply:^(id state, NSString *error) {
                            failures[source] = error ?: @"Capture started unexpectedly";
                        }];
                    }
                }
                [host controlAnnotation:@"status" parameters:@{} reply:^(id state, NSString *error) {
                    report(@"reply.json", @{@"state": state ?: @{}, @"startFailures": failures,
                        @"permissionFailures": permissionFailures,
                        @"shortcutRegistered": @([host.systemDictation valueForKey:@"annotationEvents"] != nil),
                        @"canStart": @([host canStartAnnotation])});
                }];
                return;
            } else if ([command isEqual:@"vocabulary-menu"]) {
                NSMenuItem *item = host.meetingMenuItems[@"vocabulary"];
                BOOL hidden = item.hidden;
                __block NSString *setFailure;
                void (^set)(NSString *, id) = ^(NSString *key, id value) {
                    [host.systemDictation request:@{@"action": @"set", @"key": key, @"value": value}
                        reply:^(id result, NSString *error) { if (error) setFailure = error; }];
                };
                set(@"vocabularyMenu", @YES);
                [host menuNeedsUpdate:item.menu];
                NSUInteger emptyCount = item.submenu.numberOfItems;
                if (setFailure || emptyCount != 2) {
                    report(@"reply.json", @{@"error": setFailure ?: @"Missing initial actions", @"count": @(emptyCount), @"hidden": @(item.hidden)});
                    return;
                }
                NSMutableArray *terms = NSMutableArray.array;
                for (NSUInteger index = 1; index <= 32; index++) [terms addObject:[NSString stringWithFormat:@"Name %lu", index]];
                set(@"vocabulary", [terms componentsJoinedByString:@"\n"]);
                [host menuNeedsUpdate:item.menu];
                if (setFailure) { report(@"reply.json", @{@"error": setFailure}); return; }
                BOOL full = ![item.submenu itemAtIndex:0].enabled;
                NSMutableArray *recent = NSMutableArray.array;
                for (NSMenuItem *entry in item.submenu.itemArray) {
                    if (!entry.separatorItem && !entry.action && !entry.enabled) [recent addObject:entry.title];
                }
                [item.submenu performActionForItemAtIndex:item.submenu.numberOfItems - 1];
                NSString *opened = host.windowNavigation[@"pane"] ?: @"";
                set(@"vocabulary", @"One");
                [host menuNeedsUpdate:item.menu];
                BOOL addEnabled = [item.submenu itemAtIndex:0].enabled && !item.hidden;
                [item.submenu performActionForItemAtIndex:0];
                NSString *added = host.windowNavigation[@"pane"] ?: @"";
                set(@"vocabularyMenu", @NO);
                report(@"reply.json", @{@"initiallyHidden": @(hidden), @"emptyCount": @(emptyCount),
                    @"recent": recent, @"full": @(full), @"opened": opened, @"added": added,
                    @"addEnabled": @(addEnabled), @"hidden": @(item.hidden)});
                return;
            } else if ([command isEqual:@"paste-menu"]) {
                NSMenuItem *item = host.meetingMenuItems[@"pasteLast"];
                [host.meetingStatusItem.menu update];
                report(@"reply.json", @{@"title": item.title ?: @"", @"shortcut": item.keyEquivalent ?: @"",
                    @"shown": @(item && !item.hidden), @"enabled": @(item.enabled),
                    @"dockVisible": @(NSApp.activationPolicy == NSApplicationActivationPolicyRegular)});
                return;
            } else if ([command isEqual:@"record-menu"]) {
                NSMenuItem *item = host.meetingMenuItems[@"start"];
                report(@"reply.json", @{@"shown": @(item && !item.hidden), @"enabled": @(item.enabled)});
                return;
            } else if ([command isEqual:@"annotation-shared-meeting"]) {
                id annotation = [host valueForKey:@"annotation"];
                id meeting = [host valueForKey:@"meetings"];
                [annotation setValue:NSUUID.UUID.UUIDString forKey:@"identifier"];
                [annotation setValue:@"picking" forKey:@"phase"];
                [host refreshMeetingAvailability];
                BOOL pickerBlocked = !host.meetingMenuItems[@"start"].enabled && [[meeting valueForKey:@"suspended"] boolValue];
                [annotation setValue:@"recording" forKey:@"phase"];
                [annotation setValue:@YES forKey:@"narrationStarted"];
                [host refreshMeetingAvailability];
                BOOL sharedReady = host.meetingMenuItems[@"start"].enabled && ![[meeting valueForKey:@"suspended"] boolValue];
                [annotation setValue:@NO forKey:@"narrationStarted"];
                [annotation setValue:nil forKey:@"identifier"];
                [annotation setValue:@"idle" forKey:@"phase"];
                [host refreshMeetingAvailability];
                report(@"reply.json", @{@"pickerBlocked": @(pickerBlocked), @"sharedReady": @(sharedReady)});
                return;
            } else if ([command isEqual:@"annotation-start-over-shared-microphone"]) {
                JardMeetings *meeting = [host valueForKey:@"meetings"];
                JardSystemDictation *dictation = [host valueForKey:@"systemDictation"];
                NSString *identifier = NSUUID.UUID.UUIDString;
                [meeting setValue:identifier forKey:@"recordingID"];
                [meeting setValue:@"starting" forKey:@"phase"];
                [meeting handleCapture:@{@"meeting": identifier, @"event": @"started", @"warm": @YES}];
                BOOL sharedMeetingAllowed = [host canStartAnnotation];
                [meeting setValue:@NO forKey:@"captureUsesSharedMicrophone"];
                BOOL otherMeetingBlocked = ![host canStartAnnotation];
                [meeting setValue:nil forKey:@"recordingID"];
                [meeting setValue:@"idle" forKey:@"phase"];
                [dictation setValue:identifier forKey:@"captureID"];
                [dictation setValue:@YES forKey:@"captureMicReady"];
                [dictation setValue:@YES forKey:@"captureUsesSharedMicrophone"];
                BOOL sharedDictationAllowed = [host canStartAnnotation];
                [dictation setAnnotationActive:YES microphoneShared:NO];
                BOOL pickerPreserved = [[dictation valueForKey:@"annotationPreservesDictation"] boolValue] &&
                    [[dictation valueForKey:@"captureID"] isEqual:identifier];
                [dictation setAnnotationActive:NO microphoneShared:NO];
                [dictation setValue:@NO forKey:@"captureUsesSharedMicrophone"];
                BOOL otherDictationBlocked = ![host canStartAnnotation];
                [dictation setValue:nil forKey:@"captureID"];
                [dictation setValue:@NO forKey:@"captureMicReady"];
                [host refreshMeetingAvailability];
                report(@"reply.json", @{@"sharedMeetingAllowed": @(sharedMeetingAllowed), @"otherMeetingBlocked": @(otherMeetingBlocked),
                    @"sharedDictationAllowed": @(sharedDictationAllowed), @"otherDictationBlocked": @(otherDictationBlocked),
                    @"pickerPreserved": @(pickerPreserved)});
                return;
            } else if ([command isEqual:@"background-check"]) {
                [host.updater check];
            } else if ([command isEqual:@"focus-update"]) {
                NSNotificationCenter *center = NSNotificationCenter.defaultCenter;
                [center postNotificationName:NSWindowDidBecomeKeyNotification object:window];
                NSString *disabled = host.updater.status[@"phase"];
                [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"JardCheckForUpdatesOnLaunch"];
                [center postNotificationName:NSWindowDidBecomeKeyNotification object:NSWindow.new];
                NSString *other = host.updater.status[@"phase"];
                [center postNotificationName:NSWindowDidBecomeKeyNotification object:window];
                report(@"reply.json", @{@"disabled": disabled, @"other": other, @"workspace": host.updater.status[@"phase"]});
                return;
            } else if ([command isEqual:@"cli-replay"]) {
                NSDictionary *before = [host.systemDictation status];
                __block NSString *busyError = nil, *updateError = nil;
                [host setValue:@YES forKey:@"otherRecording"];
                [host replayDictationSetupShowingWindow:YES reply:^(id result, NSString *error) { busyError = error; }];
                [host setValue:@NO forKey:@"otherRecording"];
                [host setValue:@YES forKey:@"updateRestartInProgress"];
                [host replayDictationSetupShowingWindow:YES reply:^(id result, NSString *error) { updateError = error; }];
                [host setValue:@NO forKey:@"updateRestartInProgress"];
                NSDictionary *blocked = [host.systemDictation status];
                BOOL blockedWindowVisible = window.visible;
                [host replayDictationSetupShowingWindow:YES reply:^(id result, NSString *error) {
                    NSDictionary *after = [host.systemDictation status];
                    BOOL unchanged = YES;
                    for (NSString *key in @[@"microphone", @"accessibility", @"shortcutLabel", @"enabled", @"retention"]) {
                        if (![before[key] isEqual:after[key]]) unchanged = NO;
                    }
                    report(@"reply.json", @{@"busyError": busyError ?: @"", @"updateError": updateError ?: @"",
                        @"blockedStep": blocked[@"onboarding"], @"blockedWindowVisible": @(blockedWindowVisible),
                        @"step": after[@"onboarding"], @"unchanged": @(unchanged), @"visible": @(window.visible), @"error": error ?: @""});
                }];
                return;
            } else if ([command isEqual:@"cli-hide"] || [command isEqual:@"cli-show"]) {
                BOOL hide = [command isEqual:@"cli-hide"];
                [host controlWindowAction:hide ? @"window-hide" : @"window-show" pane:hide ? nil : @"history"
                    reply:^(id result, NSString *error) {
                        report(@"reply.json", @{@"visible": @(window.visible), @"minimized": @(window.miniaturized),
                            @"dockVisible": @(NSApp.activationPolicy == NSApplicationActivationPolicyRegular),
                            @"result": result ?: @{}, @"error": error ?: @"", @"navigation": host.windowNavigation ?: @{}});
                    }];
                return;
            } else if ([command isEqual:@"update-policy"]) {
                NSMenu *menu = NSApp.mainMenu.itemArray.firstObject.submenu;
                NSMenuItem *item = host.meetingMenuItems[@"update"];
                [host.meetingStatusItem.menu update];
                NSUInteger visibleActions = 0;
                for (NSMenuItem *choice in menu.itemArray) {
                    if (!choice.hidden && (choice.action == NSSelectorFromString(@"updateFromMenu:") ||
                        choice.action == NSSelectorFromString(@"toggleAutomaticUpdates:"))) visibleActions++;
                }
                NSMutableDictionary *failures = NSMutableDictionary.dictionary;
                for (NSString *action in @[@"check", @"channel", @"confirmSwitch", @"restart", @"download", @"releaseNotes"]) {
                    [host performUpdateAction:action payload:@{} reply:^(id result, NSString *error) {
                        failures[action] = error ?: @"Unexpectedly accepted";
                    }];
                }
                report(@"reply.json", @{@"updaterCreated": @(host.updater != nil), @"update": [host workspaceUpdateStatus:host.updater.status],
                    @"statusMenuTitle": item.title ?: @"", @"statusMenuShown": @(item && !item.hidden),
                    @"statusMenuEnabled": @(item.enabled), @"visibleActions": @(visibleActions), @"failures": failures});
                return;
            } else if ([command isEqual:@"meeting-input-status"]) {
                JardMeetings *meetings = [host valueForKey:@"meetings"];
                NSString *previousPhase = [meetings valueForKey:@"phase"];
                NSDictionary *previousStatus = [host valueForKey:@"meetingMenuStatus"];
                [meetings setValue:@"recording" forKey:@"phase"];
                NSMutableDictionary *status = [@{@"busy": @YES, @"seconds": @17, @"title": @"Test call",
                    @"microphoneUnavailable": @YES} mutableCopy];
                [host updateMeetingMenu:status];
                assert([host.meetingStatusItem.button.toolTip isEqual:@"Microphone is reconnecting."]);
                assert([host.meetingStatusItem.button.accessibilityLabel containsString:@"0:17, Microphone is reconnecting."]);
                status[@"systemAudioUnavailable"] = @YES;
                [host updateMeetingMenu:status];
                assert([host.meetingStatusItem.button.toolTip isEqual:@"Microphone and Mac audio are reconnecting."]);
                assert(!host.meetingMenuItems[@"stop"].hidden);
                status[@"microphoneUnavailable"] = @NO;
                [host updateMeetingMenu:status];
                assert([host.meetingStatusItem.button.toolTip isEqual:@"Mac audio is reconnecting."]);
                status[@"systemAudioUnavailable"] = @NO;
                [host updateMeetingMenu:status];
                assert([host.meetingStatusItem.button.toolTip isEqual:@"Test call"]);
                [meetings setValue:previousPhase forKey:@"phase"];
                [host updateMeetingMenu:previousStatus ?: @{}];
                report(@"reply.json", @{@"passed": @YES});
                return;
            } else if ([command isEqual:@"restart-transaction"]) {
                verifyRestartTransaction(host);
                report(@"reply.json", @{@"passed": @YES});
                return;
            } else if ([command isEqual:@"quit-capture"]) {
                verifyQuitDuringCapture(host);
                report(@"reply.json", @{@"passed": @YES});
                return;
            } else if ([command isEqual:@"update-hold"] || [command isEqual:@"update-unhold"]) {
                if (!stagedRefresh) {
                    stagedRefresh = YES;
                    [host updateChanged:host.updater.status];
                }
                [host setValue:@([command isEqual:@"update-hold"]) forKey:@"otherRecording"];
                [host refreshMeetingAvailability];
                NSMenuItem *item = host.meetingMenuItems[@"update"];
                report(@"reply.json", @{@"update": [host workspaceUpdateStatus:host.updater.status],
                    @"publishedHold": [host valueForKey:@"updateRestartBlocked"], @"statusMenuEnabled": @(item.enabled)});
                return;
            } else if ([command isEqual:@"update-status"]) {
                report(@"reply.json", @{@"update": [host workspaceUpdateStatus:host.updater.status],
                    @"visible": @(window.visible), @"sheet": @(window.attachedSheet != nil),
                    @"statusMenuTitle": host.meetingMenuItems[@"update"].title ?: @""});
                return;
            } else if ([command isEqual:@"preview-active"]) {
                [host.systemDictation setValue:NSMutableArray.array forKey:@"previewSounds"];
            } else if ([command isEqual:@"minimize"]) [window miniaturize:nil];
            else if ([command isEqual:@"overlay"]) {
                overlay = [[NSPanel alloc] initWithContentRect:NSMakeRect(40, 40, 160, 90)
                    styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
                [overlay makeKeyAndOrderFront:nil];
            } else if ([command isEqual:@"dismiss-overlay"]) {
                [overlay orderOut:nil];
                overlay = nil;
            } else if ([command isEqual:@"setup-title"]) {
                [webView(window.contentView) evaluateJavaScript:@"({title: document.getElementById('title')?.textContent ?? '', lang: document.documentElement.lang})"
                    completionHandler:^(id result, NSError *error) {
                        report(@"reply.json", [result isKindOfClass:NSDictionary.class] ? result : @{@"title": @""});
                    }];
                return;
            } else if ([command isEqual:@"input-device-events"]) {
                WKWebView *web = webView(window.contentView);
                [web evaluateJavaScript:@"window.inputEvents = 0; window.addEventListener('native-sdk:jard-input-devices', () => ++window.inputEvents); 0"
                       completionHandler:^(id result, NSError *error) {
                    assert(!error && host.inputDeviceListener && (host.inputDeviceNotifications & 3) == 3);
                    const AudioObjectPropertyAddress properties[] = {
                        {kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain},
                        {kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain},
                        {kAudioHardwarePropertyServiceRestarted, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain},
                    };
                    for (NSUInteger i = 0; i < 3; i++) host.inputDeviceListener(1, &properties[i]);
                    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 350 * NSEC_PER_MSEC), dispatch_get_main_queue(), ^{
                        [web evaluateJavaScript:@"window.inputEvents" completionHandler:^(id count, NSError *readError) {
                            report(@"reply.json", @{@"events": count ?: NSNull.null, @"registered": @((host.inputDeviceNotifications & 3) == 3)});
                        }];
                    });
                }];
                return;
            } else if ([command isEqual:@"locale-test"]) {
                NSDictionary *status = JardInterfaceLanguageStatus(), *choices = status[@"choices"];
                NSArray *tags = @[@"zz-YY", @"pl-PL", @"zh_TW.UTF-8", @"zh-Hans-HK", @"zh-Hant", @"pt-PT"];
                NSMutableArray *resolved = NSMutableArray.new;
                for (NSString *tag in tags) [resolved addObject:JardResolveInterfaceLanguage(tag, choices)];
                BOOL frenchMenu = [NSApp.mainMenu itemWithTitle:@"Fichier"] != nil;
                BOOL invalidRejected = !JardSetInterfaceLanguage(@"zz");
                BOOL japaneseAccepted = JardSetInterfaceLanguage(@"ja");
                [host refreshInterfaceLanguage];
                report(@"reply.json", @{@"frMenu": @(frenchMenu), @"jaMenu": @([NSApp.mainMenu itemWithTitle:@"ファイル"] != nil),
                    @"resolved": resolved, @"invalidRejected": @(invalidRejected), @"accepted": @(japaneseAccepted),
                    @"selected": JardInterfaceLanguageStatus()[@"selected"]});
                return;
            } else if ([command isEqual:@"application-settings"]) {
                NSData *payload = [NSData dataWithContentsOfURL:[fixtureRoot URLByAppendingPathComponent:@"settings.json"]];
                NSDictionary *request = [NSJSONSerialization JSONObjectWithData:payload options:0 error:nil];
                [host applyControlSettings:request reply:^(id result, NSString *error) {
                    NSDictionary *language = JardInterfaceLanguageStatus();
                    report(@"reply.json", @{@"result": result ?: NSNull.null, @"error": error ?: NSNull.null,
                        @"language": language[@"selected"], @"resolved": language[@"resolved"],
                        @"frenchMenu": @([NSApp.mainMenu itemWithTitle:@"Fichier"] != nil), @"dock": [host dockIconStatus]});
                }];
                return;
            } else if ([command hasPrefix:@"web-recovery-"] && [scenario isEqual:@"web-recovery"]) {
                WKWebView *web = webView(window.contentView);
                if ([command isEqual:@"web-recovery-prepare"]) {
                    [window makeKeyAndOrderFront:nil];
                    [NSApp activate];
                    [host setValue:@"fixture-capture" forKey:@"sdkCaptureID"];
                    [web evaluateJavaScript:@"localStorage.setItem('fixture-last-session', '#fixture-notes'); history.replaceState(null, '', '#fixture-notes')"
                        completionHandler:^(id result, NSError *error) { report(@"reply.json", @{@"ok": @(!error)}); }];
                } else if ([command isEqual:@"web-recovery-state"]) {
                    NSMutableDictionary *state = [@{@"sheet": @(window.attachedSheet != nil),
                        @"recording": @([[host valueForKey:@"sdkCaptureID"] isEqual:@"fixture-capture"]),
                        @"helperRunning": @([[host valueForKey:@"bridge"] isRunning]), @"visible": @(window.visible),
                        @"key": @(window.isKeyWindow), @"active": @(NSApp.active),
                        @"failed": @([host valueForKey:@"failedWorkspace"] != nil)} mutableCopy];
                    if ([state[@"failed"] boolValue] || window.attachedSheet) {
                        report(@"reply.json", state);
                        return;
                    }
                    [web evaluateJavaScript:@"({ loads: window.loadCount ?? 0, route: location.hash })"
                        completionHandler:^(id result, NSError *error) {
                            if ([result isKindOfClass:NSDictionary.class]) [state addEntriesFromDictionary:result];
                            report(@"reply.json", state);
                        }];
                } else if ([command isEqual:@"web-recovery-kill"]) {
                    SEL processIdentifier = NSSelectorFromString(@"_webProcessIdentifier");
                    pid_t pid = [web respondsToSelector:processIdentifier] ?
                        ((pid_t (*)(id, SEL))[web methodForSelector:processIdentifier])(web, processIdentifier) : 0;
                    // Terminate only the renderer explicitly owned by this disposable fixture.
                    report(@"reply.json", @{@"ok": @(pid > 1 && pid != getpid() && kill(pid, SIGKILL) == 0)});
                } else if ([command isEqual:@"web-recovery-retry"]) {
                    NSWindow *sheet = window.attachedSheet;
                    if (sheet) [window endSheet:sheet returnCode:NSAlertFirstButtonReturn];
                    report(@"reply.json", @{@"ok": @(sheet != nil)});
                } else {
                    [host setValue:nil forKey:@"sdkCaptureID"];
                    report(@"reply.json", @{@"ok": @YES});
                }
                return;
            } else if ([command isEqual:@"settings-events"]) {
                [webView(window.contentView) evaluateJavaScript:@"window.settingsEvents ?? null"
                    completionHandler:^(id result, NSError *error) {
                        report(@"reply.json", @{@"events": [result isKindOfClass:NSArray.class] ? result : NSNull.null});
                    }];
                return;
            }
            report(@"reply.json", @{@"visible": @(window.visible), @"ready": @(host.showingApp),
                @"minimized": @(window.miniaturized), @"overlayFocused": @(overlay && NSApp.keyWindow == overlay),
                @"soundPreview": host.systemDictation.status[@"soundPreview"],
                @"dockVisible": @(NSApp.activationPolicy == NSApplicationActivationPolicyRegular),
                @"dockSetting": [host dockIconStatus], @"quitAvailable": @(host.meetingMenuItems[@"quit"] && !host.meetingMenuItems[@"quit"].hidden)});
        }];
        [NSRunLoop.mainRunLoop addTimer:poll forMode:NSRunLoopCommonModes];
    });
}

@end
