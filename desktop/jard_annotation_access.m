#import "jard_annotation_access.h"
#import "jard_permission_return.h"
#import <ScreenCaptureKit/ScreenCaptureKit.h>

static void runCheckedTask(NSURL *executable, NSArray<NSString *> *arguments, void (^reply)(NSData *)) {
    NSTask *task = NSTask.new;
    NSPipe *output = NSPipe.pipe;
    task.executableURL = executable;
    task.arguments = arguments;
    task.standardOutput = output;
    task.standardError = NSFileHandle.fileHandleWithNullDevice;
    __block BOOL finished = NO;
    void (^complete)(NSData *) = ^(NSData *data) {
        if (finished) return;
        finished = YES;
        reply(data);
    };
    task.terminationHandler = ^(NSTask *process) {
        NSData *data = process.terminationStatus == 0 ? output.fileHandleForReading.readDataToEndOfFile : nil;
        dispatch_async(dispatch_get_main_queue(), ^{ complete(data); });
    };
    if (![task launchAndReturnError:NULL]) { complete(nil); return; }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        if (finished) return;
        if (task.running) [task terminate];
        complete(nil);
    });
}

@interface JardAnnotationAccess ()
@property(nonatomic, copy) NSString *authorization;
@property(nonatomic) BOOL reading, resetting, visiting, sawSettings, processUsable;
@property(nonatomic) NSUInteger revision;
@property(nonatomic) NSTimeInterval checkedAt, readStartedAt, checkedReadStartedAt, returnedAt;
@end

@implementation JardAnnotationAccess

- (instancetype)init {
    if (!(self = [super init])) return nil;
    _authorization = @"checking";
    [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(applicationActivated:)
        name:NSWorkspaceDidActivateApplicationNotification object:nil];
    return self;
}

- (void)dealloc { [NSWorkspace.sharedWorkspace.notificationCenter removeObserver:self]; }

- (BOOL)preflight { return CGPreflightScreenCaptureAccess(); }
- (NSTimeInterval)now { return NSProcessInfo.processInfo.systemUptime; }
- (NSString *)bundleIdentifier { return NSBundle.mainBundle.bundleIdentifier; }

- (void)readFreshGrant:(void (^)(NSNumber *))reply {
    NSURL *helper = [NSBundle.mainBundle.bundleURL URLByAppendingPathComponent:@"Contents/Helpers/jard-dialog"];
    runCheckedTask(helper, @[@"screen-recording-preflight"], ^(NSData *data) {
        id value = data.length ? [NSJSONSerialization JSONObjectWithData:data options:0 error:NULL] : nil;
        id granted = [value isKindOfClass:NSDictionary.class] ? value[@"granted"] : nil;
        reply([granted isKindOfClass:NSNumber.class] ? granted : nil);
    });
}

- (void)probeCurrentProcess:(void (^)(BOOL))reply {
    // Called only after the signed helper confirms the grant. Never retain the image.
    __block BOOL finished = NO;
    void (^complete)(BOOL) = ^(BOOL usable) {
        dispatch_async(dispatch_get_main_queue(), ^{
            if (finished) return;
            finished = YES;
            reply(usable);
        });
    };
    [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES
        completionHandler:^(SCShareableContent *content, NSError *error) {
            SCDisplay *display = content.displays.firstObject;
            if (error || !display) { complete(NO); return; }
            SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:display excludingWindows:@[]];
            SCStreamConfiguration *configuration = SCStreamConfiguration.new;
            configuration.width = 64;
            configuration.height = 40;
            [SCScreenshotManager captureImageWithFilter:filter configuration:configuration completionHandler:^(CGImageRef image, NSError *failure) {
                complete(!failure && image && CGImageGetWidth(image) && CGImageGetHeight(image));
            }];
        }];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 4 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{ complete(NO); });
}

- (void)observeSettingsFront:(BOOL)settingsFront active:(BOOL)active {
    if (!self.visiting) return;
    if (settingsFront) self.sawSettings = YES;
    if (!active || settingsFront) self.returnedAt = 0;
    else if (self.sawSettings && !self.returnedAt) self.returnedAt = [self now];
}

- (void)applicationActivated:(NSNotification *)note {
    NSRunningApplication *app = note.userInfo[NSWorkspaceApplicationKey];
    [self observeSettingsFront:[app.bundleIdentifier isEqual:JardSystemSettingsBundle]
        active:app.processIdentifier == NSProcessInfo.processInfo.processIdentifier];
}

- (void)endVisit {
    self.visiting = self.sawSettings = NO;
    self.returnedAt = 0;
}

- (void)finishGrantedVisit {
    BOOL requested = self.visiting;
    [self endVisit];
    if (requested && self.settingsVisitChanged) self.settingsVisitChanged(YES);
}

- (void)finishRead:(NSString *)authorization generation:(NSUInteger)generation {
    self.reading = NO;
    if (generation != self.revision) return;
    self.authorization = authorization;
    self.checkedAt = [self now];
    self.checkedReadStartedAt = self.readStartedAt;
    self.processUsable = [authorization isEqual:@"granted"];
    if ([authorization isEqual:@"granted"] || [authorization isEqual:@"restart"]) [self finishGrantedVisit];
}

- (void)refresh {
    if (self.resetting) return;
    if ([self preflight]) {
        self.authorization = @"granted";
        self.processUsable = YES;
        self.revision++;
        [self finishGrantedVisit];
        return;
    }
    if (self.reading || (self.checkedAt && [self now] - self.checkedAt < 2)) return;
    self.reading = YES;
    self.readStartedAt = [self now];
    NSUInteger generation = ++self.revision;
    [self readFreshGrant:^(NSNumber *granted) {
        if (generation != self.revision || !granted.boolValue) {
            [self finishRead:granted ? @"off" : @"unavailable" generation:generation];
            return;
        }
        if (self.processUsable) { [self finishRead:@"granted" generation:generation]; return; }
        if (self.mayProbe && !self.mayProbe()) { self.reading = NO; return; }
        [self probeCurrentProcess:^(BOOL usable) {
            [self finishRead:usable ? @"granted" : @"restart" generation:generation];
        }];
    }];
}

- (BOOL)pollSettingsVisit {
    if (self.visiting) [self refresh];
    return self.visiting;
}

- (BOOL)needsRestart { return ![self preflight] && [self.authorization isEqual:@"restart"]; }

- (NSDictionary *)status {
    BOOL canRepair = [self.authorization isEqual:@"off"] && self.returnedAt &&
        self.checkedReadStartedAt > self.returnedAt && !self.resetting && [self now] - self.returnedAt >= 2;
    return @{@"authorization": self.authorization, @"canRepair": @(canRepair), @"busy": @(self.resetting)};
}

- (BOOL)openSettings {
    return [NSWorkspace.sharedWorkspace openURL:[NSURL URLWithString:
        @"x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"]];
}

- (void)showSettings:(JardDictationReply)reply {
    [self endVisit];
    self.visiting = YES;
    self.revision++;
    self.checkedAt = 0;
    if (![self openSettings]) {
        [self endVisit];
        reply(nil, @"Open Privacy & Security in System Settings to allow Screen Recording.");
    } else {
        if (![self.authorization isEqual:@"restart"] && self.settingsVisitChanged) self.settingsVisitChanged(NO);
        reply([self status], nil);
    }
}

- (void)resetBundle:(NSString *)bundle reply:(void (^)(BOOL))reply {
    runCheckedTask([NSURL fileURLWithPath:@"/usr/bin/tccutil"], @[@"reset", @"ScreenCapture", bundle],
        ^(NSData *data) { reply(data != nil); });
}

- (void)request:(NSString *)action refresh:(BOOL)refresh reply:(JardDictationReply)reply {
    if ([action isEqual:@"status"]) {
        if (refresh) [self refresh];
        reply([self status], nil);
        return;
    }
    if (![@[@"allow", @"repair"] containsObject:action]) { reply(nil, @"Unknown Screen Recording action."); return; }
    if (self.resetting) { reply(nil, @"Screen Recording access is being reset."); return; }
    if ([self preflight] || [self.authorization isEqual:@"granted"]) {
        if ([self preflight]) [self refresh];
        reply([self status], nil);
        return;
    }
    if ([action isEqual:@"allow"]) { [self showSettings:reply]; return; }
    NSString *bundle = [self bundleIdentifier];
    if (![[self status][@"canRepair"] boolValue] || !bundle.length) {
        reply(nil, @"Screen Recording repair is available after returning from System Settings with access still off.");
        return;
    }
    self.resetting = YES;
    self.revision++;
    [self resetBundle:bundle reply:^(BOOL success) {
        self.resetting = NO;
        self.checkedAt = 0;
        if (!success) { reply(nil, @"Could not reset Screen Recording access. Check Jaro in System Settings."); return; }
        self.authorization = @"checking";
        [self showSettings:reply];
    }];
}

@end
