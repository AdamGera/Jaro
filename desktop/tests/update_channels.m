#import "../jard_updater.h"
#import <Security/Security.h>
#import <assert.h>
#import <fcntl.h>
#import <sys/file.h>
#import <sys/stat.h>
#import <unistd.h>

@interface JardUpdater (FixtureCallbacks)
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error;
- (void)setState:(NSString *)phase message:(NSString *)message;
- (NSTimeInterval)updateUptime;
- (void)pruneAbandonedStages;
- (void)cleanPreviousUpdate;
- (BOOL)verifyCandidate:(NSURL *)candidate version:(NSString *)version channel:(NSString *)channel reason:(NSString **)reason;
@end

@interface ChannelFixtureUpdater : JardUpdater
@property(nonatomic, strong) NSData *certificate;
@property(nonatomic, copy) dispatch_block_t onStaging;
@property(nonatomic, copy) dispatch_block_t beforeBundleVerification;
@property(nonatomic, strong) NSMutableArray<NSString *> *restorePhases;
@property(nonatomic) NSTimeInterval simulatedUptime;
@end

@implementation ChannelFixtureUpdater
- (BOOL)verifyCandidate:(NSURL *)candidate version:(NSString *)version channel:(NSString *)channel reason:(NSString **)reason {
    assert(!NSThread.isMainThread);
    if (self.beforeBundleVerification) self.beforeBundleVerification();
    return [super verifyCandidate:candidate version:version channel:channel reason:reason];
}

- (NSTimeInterval)updateUptime {
    return self.simulatedUptime ?: [super updateUptime];
}

- (void)setState:(NSString *)phase message:(NSString *)message {
    [self.restorePhases addObject:phase];
    [super setState:phase message:message];
    if ([phase isEqual:@"staging"] && self.onStaging) {
        dispatch_block_t callback = self.onStaging;
        self.onStaging = nil;
        callback();
    }
}

- (void)URLSession:(NSURLSession *)session didReceiveChallenge:(NSURLAuthenticationChallenge *)challenge
    completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential *))complete {
    SecTrustRef trust = challenge.protectionSpace.serverTrust;
    if (!trust || ![challenge.protectionSpace.host isEqual:@"localhost"]) {
        complete(NSURLSessionAuthChallengeCancelAuthenticationChallenge, nil);
        return;
    }
    SecCertificateRef anchor = SecCertificateCreateWithData(NULL, (__bridge CFDataRef)self.certificate);
    assert(anchor);
    SecTrustSetAnchorCertificates(trust, (__bridge CFArrayRef)@[(__bridge id)anchor]);
    SecTrustSetAnchorCertificatesOnly(trust, true);
    CFRelease(anchor);
    CFErrorRef failure = NULL;
    if (SecTrustEvaluateWithError(trust, &failure))
        complete(NSURLSessionAuthChallengeUseCredential, [NSURLCredential credentialForTrust:trust]);
    else {
        NSLog(@"Fixture TLS trust failed: %@", CFBridgingRelease(failure));
        complete(NSURLSessionAuthChallengeCancelAuthenticationChallenge, nil);
    }
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error {
    if (error) NSLog(@"Fixture HTTPS request failed: %@", error);
    [super URLSession:session task:task didCompleteWithError:error];
}
@end

static NSDictionary *awaitPhase(JardUpdater *updater, NSString *phase) {
    NSDate *until = [NSDate dateWithTimeIntervalSinceNow:22];
    while ([until timeIntervalSinceNow] > 0) {
        NSDictionary *status = [updater status];
        if ([status[@"phase"] isEqual:phase]) return status;
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
    }
    NSLog(@"Expected %@. Reached %@", phase, [updater status]);
    abort();
}

static BOOL awaitRestore(JardUpdater *updater) {
    __block NSNumber *restored = nil;
    [updater restoreStagedUpdateWithCompletion:^(BOOL ready) { restored = @(ready); }];
    NSDate *until = [NSDate dateWithTimeIntervalSinceNow:15];
    while (!restored && until.timeIntervalSinceNow > 0)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(restored);
    return restored.boolValue;
}

static BOOL awaitInstall(JardUpdater *updater, BOOL relaunch, NSString **reason) {
    __block NSNumber *applied = nil;
    __block NSString *failure = nil;
    JardUpdateInstallReply complete = ^(BOOL success, NSString *error) {
        assert(NSThread.isMainThread);
        applied = @(success);
        failure = error;
    };
    if (relaunch) [updater restartWithCompletion:complete];
    else [updater installOnQuitWithCompletion:complete];
    NSDate *until = [NSDate dateWithTimeIntervalSinceNow:15];
    while (!applied && until.timeIntervalSinceNow > 0)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(applied);
    if (reason) *reason = failure;
    return applied.boolValue;
}

static void writeAt(NSURL *root, NSString *name, NSString *value) {
    assert([value writeToURL:[root URLByAppendingPathComponent:name] atomically:YES encoding:NSUTF8StringEncoding error:nil]);
}

static ChannelFixtureUpdater *makeUpdater(NSBundle *bundle, NSURL *root) {
    ChannelFixtureUpdater *updater = [[ChannelFixtureUpdater alloc] initWithConfiguration:
        [bundle objectForInfoDictionaryKey:@"JardUpdates"] bundle:bundle stateChanged:nil];
    assert(updater);
    updater.certificate = [NSData dataWithContentsOfURL:[root URLByAppendingPathComponent:@"server.cer"]];
    assert(updater.certificate.length);
    return updater;
}

static NSUInteger countStages(NSURL *root) {
    NSArray *files = [NSFileManager.defaultManager contentsOfDirectoryAtURL:root includingPropertiesForKeys:nil options:0 error:nil];
    NSUInteger count = 0;
    for (NSURL *file in files) if ([file.lastPathComponent hasPrefix:@".jard-update-"]) count++;
    return count;
}

static void assertNoStage(NSURL *root) {
    NSDate *until = [NSDate dateWithTimeIntervalSinceNow:10];
    while (countStages(root) && until.timeIntervalSinceNow > 0)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
    assert(countStages(root) == 0);
}

static NSURL *fixtureStage(NSURL *parent, NSString *name, NSInteger mode) {
    NSURL *stage = [parent URLByAppendingPathComponent:name isDirectory:YES];
    assert([NSFileManager.defaultManager createDirectoryAtURL:stage withIntermediateDirectories:NO
        attributes:@{NSFilePosixPermissions: @(mode)} error:nil]);
    return stage;
}

static void ageStage(NSURL *stage) {
    assert([NSFileManager.defaultManager setAttributes:@{NSFileModificationDate: [NSDate dateWithTimeIntervalSinceNow:-3600]}
        ofItemAtPath:stage.path error:nil]);
}

static void exerciseAbandonedStages(JardUpdater *updater, NSURL *root) {
    NSDictionary *receipt = [NSUserDefaults.standardUserDefaults dictionaryForKey:@"JardPendingUpdate"];
    NSURL *ready = [NSURL fileURLWithPath:receipt[@"stage"] isDirectory:YES];
    NSString *readyName = ready.lastPathComponent;
    NSString *prefix = [readyName substringToIndex:readyName.length - NSUUID.UUID.UUIDString.length];
    assert(prefix.length > @".jard-update-".length && [prefix hasPrefix:@".jard-update-"]);
    NSString *(^name)(void) = ^{ return [prefix stringByAppendingString:NSUUID.UUID.UUIDString]; };
    NSURL *abandoned = fixtureStage(root, name(), 0700);
    NSURL *locked = fixtureStage(root, name(), 0700);
    NSURL *recent = fixtureStage(root, name(), 0700);
    NSURL *public = fixtureStage(root, name(), 0755);
    NSURL *legacy = fixtureStage(root, [@".jard-update-" stringByAppendingString:NSUUID.UUID.UUIDString], 0700);
    NSURL *other = fixtureStage(root, [@".jard-update-00000000000000000000000000000000-" stringByAppendingString:NSUUID.UUID.UUIDString], 0700);
    NSURL *malformed = fixtureStage(root, [prefix stringByAppendingString:@"not-a-stage"], 0700);
    NSURL *link = [root URLByAppendingPathComponent:name()];
    NSFileManager *manager = NSFileManager.defaultManager;
    assert([manager createSymbolicLinkAtURL:link withDestinationURL:other error:nil]);
    int lease = open(locked.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    assert(lease >= 0 && flock(lease, LOCK_EX | LOCK_NB) == 0);
    for (NSURL *stage in @[ready, abandoned, locked, public, legacy, other, malformed]) ageStage(stage);

    [updater pruneAbandonedStages];
    assert(![manager fileExistsAtPath:abandoned.path]);
    NSDictionary<NSString *, NSURL *> *protected = @{@"ready": ready, @"leased": locked, @"recent": recent,
        @"public": public, @"legacy": legacy, @"other": other, @"malformed": malformed, @"link": link};
    for (NSString *kind in protected) {
        NSURL *stage = protected[kind];
        struct stat info;
        int result = lstat(stage.fileSystemRepresentation, &info);
        if (result != 0) NSLog(@"Missing %@ stage %@", kind, stage.lastPathComponent);
        assert(result == 0);
    }

    close(lease);
    ageStage(recent);
    [updater pruneAbandonedStages];
    assert(![manager fileExistsAtPath:locked.path] && ![manager fileExistsAtPath:recent.path]);
    for (NSURL *stage in @[link, public, legacy, other, malformed]) assert([manager removeItemAtURL:stage error:nil]);
    assert(countStages(root) == 1 && [manager fileExistsAtPath:ready.path]);
    writeAt(root, @"stage-cleanup", @"Removed only aged, unlocked folders belonging to this installation and preserved the ready stage.");
}

static void exerciseStaleReceipts(JardUpdater *updater, NSBundle *bundle, NSURL *root) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *pendingKey = @"JardPendingUpdate";
    NSString *cleanupKey = @"JardInstalledUpdateBackup";
    NSDictionary *readyReceipt = [defaults dictionaryForKey:pendingKey];
    NSURL *ready = [NSURL fileURLWithPath:readyReceipt[@"stage"] isDirectory:YES];
    NSString *prefix = [ready.lastPathComponent substringToIndex:ready.lastPathComponent.length - NSUUID.UUID.UUIDString.length];
    NSURL *(^stage)(void) = ^{ return fixtureStage(root, [prefix stringByAppendingString:NSUUID.UUID.UUIDString], 0700); };
    NSFileManager *manager = NSFileManager.defaultManager;
    NSString *version = [bundle objectForInfoDictionaryKey:@"CFBundleVersion"];
    int lease = open(ready.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    assert(lease >= 0 && flock(lease, LOCK_EX | LOCK_NB) == 0);

    NSURL *obsolete = stage();
    NSURL *prior = stage();
    [defaults setObject:@{@"installed": bundle.bundlePath, @"stage": obsolete.path, @"runningVersion": @"1.0.0",
        @"version": version, @"candidate": @"Jaro.app", @"channel": @"stable"} forKey:pendingKey];
    [defaults setObject:@{@"installed": bundle.bundlePath, @"stage": prior.path, @"version": @"2.0.0"} forKey:cleanupKey];
    ageStage(obsolete);
    ageStage(prior);
    [updater pruneAbandonedStages];
    assert(![manager fileExistsAtPath:obsolete.path] && ![manager fileExistsAtPath:prior.path]);

    // An exchange between channels can retain the version and leave the other channel in staging.
    NSURL *switched = stage();
    NSURL *contents = [switched URLByAppendingPathComponent:@"Jaro.app/Contents" isDirectory:YES];
    assert([manager createDirectoryAtURL:contents withIntermediateDirectories:YES attributes:nil error:nil]);
    assert(([@{@"CFBundleIdentifier": bundle.bundleIdentifier, @"CFBundleVersion": version,
        @"JardUpdates": @{@"channel": @"dev"}} writeToURL:[contents URLByAppendingPathComponent:@"Info.plist"] atomically:YES]));
    [defaults setObject:@{@"installed": bundle.bundlePath, @"stage": switched.path, @"runningVersion": version,
        @"version": version, @"candidate": @"Jaro.app", @"channel": @"stable"} forKey:pendingKey];
    ageStage(switched);
    [updater pruneAbandonedStages];
    assert(![manager fileExistsAtPath:switched.path]);

    NSURL *backup = stage();
    [defaults setObject:readyReceipt forKey:pendingKey];
    [defaults setObject:@{@"installed": bundle.bundlePath, @"stage": backup.path, @"version": version} forKey:cleanupKey];
    ageStage(backup);
    [updater pruneAbandonedStages];
    assert([manager fileExistsAtPath:backup.path]);
    [updater cleanPreviousUpdate];
    assert(![manager fileExistsAtPath:backup.path] && [[defaults dictionaryForKey:pendingKey] isEqual:readyReceipt]);

    NSURL *otherBackup = stage();
    NSMutableDictionary *otherPending = [readyReceipt mutableCopy];
    otherPending[@"installed"] = [root URLByAppendingPathComponent:@"Other.app"].path;
    [defaults setObject:otherPending forKey:pendingKey];
    [defaults setObject:@{@"installed": bundle.bundlePath, @"stage": otherBackup.path, @"version": version} forKey:cleanupKey];
    [updater cleanPreviousUpdate];
    assert(![manager fileExistsAtPath:otherBackup.path] && [[defaults dictionaryForKey:pendingKey] isEqual:otherPending]);

    [defaults setObject:readyReceipt forKey:pendingKey];
    close(lease);
    writeAt(root, @"receipt-recovery", @"Obsolete receipts did not keep interrupted swaps. Cleanup preserved newer and other-install receipts.");
}

static void pauseVerificationFor(ChannelFixtureUpdater *updater, dispatch_block_t action) {
    updater.beforeBundleVerification = ^{
        dispatch_semaphore_t complete = dispatch_semaphore_create(0);
        dispatch_async(dispatch_get_main_queue(), ^{
            action();
            dispatch_semaphore_signal(complete);
        });
        assert(dispatch_semaphore_wait(complete, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)) == 0);
    };
}

static void exerciseRestoreAdmission(ChannelFixtureUpdater *updater, NSBundle *bundle, NSURL *root) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSDictionary *receipt = [defaults dictionaryForKey:@"JardPendingUpdate"];
    NSMutableDictionary *changed = [receipt mutableCopy];
    changed[@"notes"] = @"Updated receipt fixture";
    __weak ChannelFixtureUpdater *owner = updater;
    pauseVerificationFor(updater, ^{
        assert(![owner canInstallStagedUpdate]);
        [defaults setObject:changed forKey:@"JardPendingUpdate"];
    });
    assert(!awaitRestore(updater) && ![updater canInstallStagedUpdate]);
    assert([[defaults dictionaryForKey:@"JardPendingUpdate"] isEqual:changed]);
    [defaults setObject:receipt forKey:@"JardPendingUpdate"];

    pauseVerificationFor(updater, ^{
        [defaults setBool:YES forKey:@"DisableUpdates"];
        [owner check];
        assert([owner.status[@"phase"] isEqual:@"unavailable"]);
    });
    assert(!awaitRestore(updater) && ![updater canInstallStagedUpdate]);
    assert([[defaults dictionaryForKey:@"JardPendingUpdate"] isEqual:receipt]);
    [defaults removeObjectForKey:@"DisableUpdates"];

    ChannelFixtureUpdater *closing = makeUpdater(bundle, root);
    __weak ChannelFixtureUpdater *quitOwner = closing;
    pauseVerificationFor(closing, ^{
        assert(![quitOwner prepareToInstallOnQuit]);
    });
    assert(!awaitRestore(closing) && ![closing canInstallStagedUpdate]);
    assert([[defaults dictionaryForKey:@"JardPendingUpdate"] isEqual:receipt]);

    updater.restorePhases = NSMutableArray.array;
    pauseVerificationFor(updater, ^{
        [owner check];
        assert([owner.status[@"phase"] isEqual:@"checking"] && ![owner canInstallStagedUpdate]);
    });
    assert(awaitRestore(updater));
    updater.beforeBundleVerification = nil;
    assert([awaitPhase(updater, @"ready")[@"version"] isEqual:@"4.0.0"]);
    assert(updater.restorePhases.count >= 2 && [updater.restorePhases.lastObject isEqual:@"ready"]);
    assert([updater.restorePhases indexOfObject:@"ready"] == updater.restorePhases.count - 1);
    updater.restorePhases = nil;
    assert([[updater status][@"notes"] isEqual:@"Notes for stable-four"]);
    writeAt(root, @"restore-admission", @"AppKit handled checks, policy changes, and Quit while signature verification ran off the main thread.");
}

static void exerciseInstallAdmission(ChannelFixtureUpdater *updater, NSBundle *bundle, NSURL *root) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSDictionary *receipt = [defaults dictionaryForKey:@"JardPendingUpdate"];
    NSString *version = [bundle objectForInfoDictionaryKey:@"CFBundleVersion"];
    __weak ChannelFixtureUpdater *owner = updater;
    pauseVerificationFor(updater, ^{
        [owner restartWithCompletion:^(BOOL applied, NSString *reason) { assert(!applied && reason.length); }];
        [defaults setBool:YES forKey:@"DisableUpdates"];
    });
    assert(!awaitInstall(updater, YES, nil));
    updater.beforeBundleVerification = nil;
    [defaults removeObjectForKey:@"DisableUpdates"];
    assert([updater.status[@"staged"] boolValue] && [[defaults dictionaryForKey:@"JardPendingUpdate"] isEqual:receipt]);
    assert([[NSDictionary dictionaryWithContentsOfURL:[bundle.bundleURL URLByAppendingPathComponent:@"Contents/Info.plist"]]
        [@"CFBundleVersion"] isEqual:version]);

    NSURL *stage = [NSURL fileURLWithPath:receipt[@"stage"] isDirectory:YES];
    NSURL *candidate = [stage URLByAppendingPathComponent:receipt[@"candidate"]];
    NSURL *original = [stage URLByAppendingPathComponent:@"Original.app"];
    NSFileManager *manager = NSFileManager.defaultManager;
    pauseVerificationFor(updater, ^{
        assert([manager moveItemAtURL:candidate toURL:original error:nil]);
        assert([manager copyItemAtURL:original toURL:candidate error:nil]);
    });
    NSString *failure = nil;
    assert(!awaitInstall(updater, YES, &failure) && [failure containsString:@"changed during verification"]);
    updater.beforeBundleVerification = nil;
    assert([manager removeItemAtURL:candidate error:nil]);
    assert([manager moveItemAtURL:original toURL:candidate error:nil]);
    writeAt(root, @"install-admission", @"Final verification ran off-thread and refused duplicate restart, a policy change, and a substituted candidate directory.");
}

static void automaticCheck(ChannelFixtureUpdater *updater, NSTimeInterval uptime, BOOL focus, NSString *result) {
    updater.simulatedUptime = uptime;
    [updater checkAutomaticallyOnActivation:focus];
    assert([updater.status[@"phase"] isEqual:@"checking"] == (result != nil));
    if (result) awaitPhase(updater, result);
}

static void exerciseSchedule(ChannelFixtureUpdater *updater, NSURL *root) {
    NSUInteger otherStages = countStages(root);
    writeAt(root, @"development-feed", @"missing");
    automaticCheck(updater, 100, NO, @"error");
    automaticCheck(updater, 159, YES, nil);
    automaticCheck(updater, 1299, NO, nil);
    automaticCheck(updater, 1300, NO, @"error");
    automaticCheck(updater, 3699, NO, nil);
    automaticCheck(updater, 3700, NO, @"error");
    automaticCheck(updater, 7299, NO, nil);
    automaticCheck(updater, 7300, NO, @"error");

    updater.simulatedUptime = 7301;
    [updater check];
    awaitPhase(updater, @"error");
    writeAt(root, @"development-feed", @"first");
    automaticCheck(updater, 7360, YES, nil);
    automaticCheck(updater, 7361, YES, @"current");
    automaticCheck(updater, 7960, NO, nil);
    writeAt(root, @"development-feed", @"missing");
    automaticCheck(updater, 7961, NO, @"error");
    automaticCheck(updater, 9160, NO, nil);

    writeAt(root, @"development-feed", @"later");
    automaticCheck(updater, 9161, NO, @"ready");
    assert(countStages(root) == otherStages + 1);
    writeAt(root, @"development-feed", @"missing");
    __block BOOL finished = NO;
    [updater checkBeforeRestart:^{ finished = YES; }];
    assert([updater.status[@"phase"] isEqual:@"checking"]);
    NSDictionary *stage = awaitPhase(updater, @"ready");
    assert(finished && [stage[@"staged"] boolValue] && [stage[@"version"] isEqual:@"2.10.0"] && [stage[@"message"] length]);

    writeAt(root, @"stable-feed", @"missing");
    assert([updater selectChannel:@"stable" reason:NULL]);
    stage = awaitPhase(updater, @"ready");
    assert([stage[@"staged"] boolValue] && [stage[@"stageChannel"] isEqual:@"dev"] && [stage[@"message"] length]);
    assert(countStages(root) == otherStages + 1);
    automaticCheck(updater, 12760, NO, nil);
    automaticCheck(updater, 12761, YES, nil);
    automaticCheck(updater, 12761, NO, @"ready");

    writeAt(root, @"development-feed", @"invalid-notes");
    updater.onStaging = ^{
        [NSUserDefaults.standardUserDefaults setObject:@"stable" forKey:@"JardUpdateChannel"];
    };
    assert([updater selectChannel:@"dev" reason:NULL]);
    stage = awaitPhase(updater, @"ready");
    assert([stage[@"channel"] isEqual:@"stable"] && [stage[@"version"] isEqual:@"2.10.0"]);
    assert([updater canInstallStagedUpdate] && countStages(root) == otherStages + 1);
    [updater cancel];
}

static NSDictionary *checkRollout(ChannelFixtureUpdater *updater, NSURL *root, NSString *mode, NSString *phase) {
    writeAt(root, @"rollout-stable", mode);
    [updater check];
    return awaitPhase(updater, phase);
}

static void exerciseRollout(ChannelFixtureUpdater *updater, NSURL *root) {
    assert([updater.status[@"channel"] isEqual:@"stable"]);
    NSURL *receipt = [root URLByAppendingPathComponent:@"fixture-rollout-bucket"];
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *saved = [NSString stringWithContentsOfURL:receipt encoding:NSUTF8StringEncoding error:nil];
    if (!saved) {
        assert(![defaults objectForKey:@"JardUpdateRolloutBucket"]);
        [updater check];
        assert(![awaitPhase(updater, @"current")[@"staged"] boolValue]);
        NSNumber *generated = [defaults objectForKey:@"JardUpdateRolloutBucket"];
        assert(generated && generated.integerValue >= 0 && generated.integerValue < 100);
        assert([defaults synchronize]);
        writeAt(root, @"fixture-rollout-bucket", generated.stringValue);
        return;
    }
    assert([[[defaults objectForKey:@"JardUpdateRolloutBucket"] stringValue] isEqual:saved]);
    [defaults setInteger:37 forKey:@"JardUpdateRolloutBucket"];
    NSUInteger otherStages = countStages(root);

    writeAt(root, @"stable-feed", @"stable-four");
    NSDictionary *stage = checkRollout(updater, root, @"missing", @"ready");
    assert([stage[@"version"] isEqual:@"4.0.0"] && countStages(root) == otherStages + 1);
    writeAt(root, @"stable-feed", @"stalled-feed");
    writeAt(root, @"rollout-stable", @"delayed-404");
    __block NSUInteger finalChecks = 0;
    NSTimeInterval started = NSProcessInfo.processInfo.systemUptime;
    [updater checkBeforeRestart:^{ finalChecks++; }];
    stage = awaitPhase(updater, @"ready");
    NSTimeInterval elapsed = NSProcessInfo.processInfo.systemUptime - started;
    assert(finalChecks == 1 && elapsed >= 13 && elapsed < 19 && [stage[@"message"] length]);
    assert([stage[@"version"] isEqual:@"4.0.0"] && [stage[@"staged"] boolValue]);

    for (NSUInteger i = 0; i < 2; i++) {
        stage = checkRollout(updater, root, @"closed-boundary.json", @"ready");
        assert([stage[@"version"] isEqual:@"4.0.0"] && ![stage[@"message"] length]);
    }
    [updater checkBeforeRestart:^{ finalChecks++; }];
    stage = awaitPhase(updater, @"ready");
    assert(finalChecks == 2 && [stage[@"version"] isEqual:@"4.0.0"] && ![stage[@"message"] length]);

    for (NSString *mode in @[@"invalid-policy.json", @"wrong-channel.json", @"wrong-key.json", @"stable-five.json", @"unavailable"]) {
        stage = checkRollout(updater, root, mode, @"ready");
        assert([stage[@"version"] isEqual:@"4.0.0"] && [stage[@"staged"] boolValue] && [stage[@"message"] length]);
    }
    stage = checkRollout(updater, root, @"older-required.json", @"ready");
    assert([stage[@"version"] isEqual:@"4.0.0"] && ![stage[@"message"] length]);
    stage = checkRollout(updater, root, @"open-boundary.json", @"ready");
    assert([stage[@"version"] isEqual:@"5.0.0"] && countStages(root) == otherStages + 1);
    stage = checkRollout(updater, root, @"required.json", @"ready");
    assert([stage[@"version"] isEqual:@"6.0.0"] && countStages(root) == otherStages + 1);

    assert([updater selectChannel:@"dev" reason:NULL]);
    NSDictionary *offer = awaitPhase(updater, @"ready");
    assert([offer[@"version"] isEqual:@"6.0.0"] && [offer[@"stageChannel"] isEqual:@"stable"]);
    assert([offer[@"notes"] isEqual:@"Notes for stable-six"] && [offer[@"offerVersion"] isEqual:@"2.0.0"]);
    assert([offer[@"staged"] boolValue] && countStages(root) == otherStages + 1);
    assert(![updater confirmChannelSwitch:@"stale-offer" reason:NULL]);
    [updater cancel];

    updater = makeUpdater(NSBundle.mainBundle, root);
    assert(awaitRestore(updater) && [updater canInstallStagedUpdate]);
    assert([updater.status[@"channel"] isEqual:@"dev"] && [updater.status[@"version"] isEqual:@"6.0.0"]);
    [updater checkBeforeRestart:^{ finalChecks++; }];
    offer = awaitPhase(updater, @"ready");
    assert(finalChecks == 3 && [offer[@"version"] isEqual:@"6.0.0"] && [offer[@"offerVersion"] isEqual:@"2.0.0"]);
    assert([updater confirmChannelSwitch:offer[@"offer"] reason:NULL]);
    stage = awaitPhase(updater, @"ready");
    assert([stage[@"version"] isEqual:@"2.0.0"] && [stage[@"stageChannel"] isEqual:@"dev"]);
    assert(!stage[@"offer"] && countStages(root) == otherStages + 1);
    [updater cancel];
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc > 1 && !strcmp(argv[1], "--jaro-relaunch-update")) return JardRunUpdateRelaunch(argc, argv);
        NSBundle *bundle = NSBundle.mainBundle;
        NSURL *root = bundle.bundleURL.URLByStandardizingPath.URLByDeletingLastPathComponent;
        NSString *version = [bundle objectForInfoDictionaryKey:@"CFBundleVersion"];
        NSString *flavor = [bundle objectForInfoDictionaryKey:@"FixtureFlavor"];
        if ([flavor isEqual:@"broken-update"]) return 2;
        ChannelFixtureUpdater *updater = makeUpdater(bundle, root);
        NSDictionary *state = [updater status];
        assert(![state[@"staged"] boolValue] && ![state[@"updatesDisabled"] boolValue]);
        NSString *channel = state[@"installedChannel"];
        assert(([state[@"channels"] isEqual:@[@"stable", @"dev"]]));
        NSString *failure = nil;
        if ([flavor isEqual:@"rollback-installed"]) {
            [updater check];
            if ([NSProcessInfo.processInfo.environment[@"JARD_UPDATE_ROLLED_BACK"] isEqual:@"1"]) {
                assert([version isEqual:@"3.0.0"]);
                state = awaitPhase(updater, @"error");
                assert(![state[@"staged"] boolValue] && [state[@"message"] containsString:@"failed to start"]);
                writeAt(root, @"rollback-reoffer-checked", @"restored the previous bundle and skipped the failed archive");
            } else {
                assert([awaitPhase(updater, @"ready")[@"version"] isEqual:@"4.0.0"]);
                if (argc == 1) assert(awaitInstall(updater, YES, &failure));
            }
            return 0;
        }
        if ([[bundle objectForInfoDictionaryKey:@"FixtureFlavor"] isEqual:@"rollout-installed"]) {
            exerciseRollout(updater, root);
            return 0;
        }
        if ([[bundle objectForInfoDictionaryKey:@"FixtureFlavor"] isEqual:@"schedule-installed"]) {
            assert([state[@"channel"] isEqual:@"dev"]);
            exerciseSchedule(updater, root);
            return 0;
        }
        if ([[bundle objectForInfoDictionaryKey:@"FixtureFlavor"] isEqual:@"preserved-stage-installed"]) {
            [updater check];
            assert([awaitPhase(updater, @"ready")[@"version"] isEqual:@"4.0.0"]);
            assert([updater selectChannel:@"dev" reason:&failure]);
            state = awaitPhase(updater, @"ready");
            assert([state[@"offerVersion"] isEqual:@"2.0.0"] && [state[@"stageChannel"] isEqual:@"stable"]);
            assert(awaitInstall(updater, NO, &failure));
            return 0;
        }
        if ([[bundle objectForInfoDictionaryKey:@"FixtureFlavor"] isEqual:@"cache-installed"]) {
            writeAt(root, @"cache-mode", @"unsolicited");
            [updater check];
            assert([awaitPhase(updater, @"error")[@"message"] containsString:@"matching signed copy"]);

            writeAt(root, @"cache-mode", @"current");
            for (NSUInteger i = 0; i < 2; ++i) {
                [updater check];
                assert(![awaitPhase(updater, @"current")[@"message"] length]);
            }

            writeAt(root, @"cache-mode", @"stable");
            [updater check];
            assert([awaitPhase(updater, @"ready")[@"version"] isEqual:@"4.0.0"]);
            [updater check];
            state = awaitPhase(updater, @"ready");
            assert([state[@"version"] isEqual:@"4.0.0"] && ![state[@"message"] length]);
            __block BOOL checked = NO;
            [updater checkBeforeRestart:^{ checked = YES; }];
            state = awaitPhase(updater, @"ready");
            assert(checked && [state[@"version"] isEqual:@"4.0.0"] && ![state[@"message"] length]);

            writeAt(root, @"cache-mode", @"redirect");
            [updater check];
            assert([awaitPhase(updater, @"ready")[@"version"] isEqual:@"5.0.0"]);
            [updater check];
            state = awaitPhase(updater, @"ready");
            assert([state[@"version"] isEqual:@"5.0.0"] && ![state[@"message"] length]);

            writeAt(root, @"cache-mode", @"wrong-tag");
            [updater check];
            assert([awaitPhase(updater, @"ready")[@"message"] containsString:@"matching signed copy"]);
            writeAt(root, @"cache-mode", @"malformed");
            for (NSUInteger i = 0; i < 2; ++i) {
                [updater check];
                assert(![awaitPhase(updater, @"ready")[@"message"] length]);
            }

            writeAt(root, @"cache-mode", @"stable");
            [updater check];
            assert(![awaitPhase(updater, @"ready")[@"message"] length]);
            writeAt(root, @"cache-mode", @"invalid-signature");
            for (NSUInteger i = 0; i < 2; ++i) {
                [updater check];
                assert([awaitPhase(updater, @"ready")[@"message"] containsString:@"signed update feed"]);
            }
            writeAt(root, @"cache-mode", @"stable");
            [updater check];
            awaitPhase(updater, @"ready");
            writeAt(root, @"cache-mode", @"unsolicited");
            checked = NO;
            [updater checkBeforeRestart:^{ checked = YES; }];
            state = awaitPhase(updater, @"ready");
            assert(checked && [state[@"staged"] boolValue] && [state[@"message"] containsString:@"matching signed copy"]);
            writeAt(root, @"cache-mode", @"stable");
            [updater check];
            awaitPhase(updater, @"ready");
            writeAt(root, @"cache-mode", @"switch-channel");
            assert([updater selectChannel:@"dev" reason:&failure]);
            state = awaitPhase(updater, @"ready");
            assert([state[@"channel"] isEqual:@"dev"] && [state[@"stageChannel"] isEqual:@"stable"]);
            [updater cancel];
            return 0;
        }
        if ([[bundle objectForInfoDictionaryKey:@"FixtureFlavor"] isEqual:@"resume-installed"]) {
            NSString *behavior = [NSString stringWithContentsOfURL:[root URLByAppendingPathComponent:@"resume-behavior"]
                encoding:NSUTF8StringEncoding error:nil];
            BOOL succeeds = [behavior isEqual:@"resume"] || [behavior isEqual:@"full"];
            [updater check];
            state = awaitPhase(updater, succeeds ? @"ready" : @"error");
            if (succeeds) assert([state[@"staged"] boolValue] && [state[@"version"] isEqual:[behavior isEqual:@"full"] ? @"7.0.0" : @"6.0.0"]);
            else {
                NSString *reason = [behavior isEqual:@"tamper"] ? @"signed size and digest" : @"resume";
                assert(![state[@"staged"] boolValue] && [state[@"message"] containsString:reason]);
            }
            [updater cancel];
            return 0;
        }
        if ([version isEqual:@"3.0.0"]) {
            assert([channel isEqual:@"stable"] && [state[@"channel"] isEqual:@"stable"]);
            assert(![updater selectChannel:@"other" reason:&failure]);
            [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"DisableUpdates"];
            [updater check];
            assert([awaitPhase(updater, @"unavailable")[@"updatesDisabled"] boolValue]);
            assert(![updater selectChannel:@"dev" reason:&failure]);
            [NSUserDefaults.standardUserDefaults removeObjectForKey:@"DisableUpdates"];
            [updater check];
            awaitPhase(updater, @"current");  // The older release may only be used on a different channel.
            writeAt(root, @"stable-feed", @"stable-four");
            [updater check];
            NSDictionary *stableStage = awaitPhase(updater, @"ready");
            assert([stableStage[@"version"] isEqual:@"4.0.0"]);
            assert([stableStage[@"stageChannel"] isEqual:@"stable"] && [stableStage[@"notes"] isEqual:@"Notes for stable-four"]);
            assert(![stableStage[@"releaseNotesAvailable"] boolValue]);
            ChannelFixtureUpdater *observer = makeUpdater(bundle, root);
            assert(!awaitRestore(observer));  // The ready candidate is leased to the first updater.
            [updater cancel];
            updater = observer;
            exerciseAbandonedStages(updater, root);  // The saved receipt protects the unlocked ready stage.
            exerciseStaleReceipts(updater, bundle, root);
            exerciseRestoreAdmission(updater, bundle, root);
            exerciseInstallAdmission(updater, bundle, root);
            assert([[updater status][@"notes"] isEqual:@"Notes for stable-four"]);
            // A final check must supersede even a stable release that was just verified.
            writeAt(root, @"stable-feed", @"stable-five");
            __block NSUInteger finalChecks = 0;
            [updater check];
            assert([updater canInstallStagedUpdate]);
            [updater checkBeforeRestart:^{ finalChecks++; }];
            assert([[[updater status] objectForKey:@"phase"] isEqual:@"checking"] && finalChecks == 0);
            stableStage = awaitPhase(updater, @"ready");
            assert([stableStage[@"version"] isEqual:@"5.0.0"] && finalChecks == 1);
            assert([stableStage[@"notes"] isEqual:@"Notes for stable-five"]);

            writeAt(root, @"stable-feed", @"stalled-feed");
            NSTimeInterval start = NSProcessInfo.processInfo.systemUptime;
            [updater checkBeforeRestart:^{ finalChecks++; }];
            NSDictionary *offline = awaitPhase(updater, @"ready");
            NSTimeInterval elapsed = NSProcessInfo.processInfo.systemUptime - start;
            assert(elapsed >= 13 && elapsed < 20 && finalChecks == 2);
            assert([offline[@"version"] isEqual:@"5.0.0"] && [offline[@"staged"] boolValue] && [offline[@"message"] length]);
            assert([updater prepareToInstallOnQuit]);
            [updater resumeAfterCancelledQuit];
            NSDictionary *resumed = [updater status];
            assert([resumed[@"phase"] isEqual:@"ready"] && [resumed[@"staged"] boolValue]);
            assert([resumed[@"version"] isEqual:offline[@"version"]] && [resumed[@"notes"] isEqual:offline[@"notes"]]);
            writeAt(root, @"stable-feed", @"stable");
            printf("A fresh Stable release replaced a recent stage. A trickling feed timed out in %.1f seconds and kept that stage.\n", elapsed);

            writeAt(root, @"development-feed", @"invalid-key");
            assert([updater selectChannel:@"dev" reason:&failure]);
            assert([awaitPhase(updater, @"ready")[@"message"] containsString:@"signed update feed"]);
            assert(countStages(root) == 1);

            writeAt(root, @"development-feed", @"first");
            assert(chmod(root.fileSystemRepresentation, 0500) == 0);
            [updater check];
            assert([awaitPhase(updater, @"download")[@"version"] isEqual:@"2.0.0"]);
            assert(!awaitInstall(updater, NO, &failure));
            assert(chmod(root.fileSystemRepresentation, 0700) == 0);
            [updater check];
            NSDictionary *first = awaitPhase(updater, @"ready");
            assert([first[@"offerVersion"] isEqual:@"2.0.0"] && [first[@"version"] isEqual:@"5.0.0"]);
            assert(![updater confirmChannelSwitch:@"stale" reason:&failure]);
            assert([updater canInstallStagedUpdate] && countStages(root) == 1);

            writeAt(root, @"development-feed", @"second");
            assert([updater confirmChannelSwitch:first[@"offer"] reason:&failure]);
            NSDictionary *second = awaitPhase(updater, @"ready");
            assert([second[@"offerVersion"] isEqual:first[@"offerVersion"]]);
            assert(![second[@"offer"] isEqual:first[@"offer"]]);
            assert([second[@"version"] isEqual:first[@"version"]] && countStages(root) == 1);

            writeAt(root, @"development-feed", @"wrong-bundle");
            [updater check];
            NSDictionary *foreign = awaitPhase(updater, @"ready");
            assert([updater confirmChannelSwitch:foreign[@"offer"] reason:&failure]);
            state = awaitPhase(updater, @"ready");
            assert([state[@"message"] containsString:@"different update channel"]);
            assert([state[@"version"] isEqual:@"5.0.0"] && [state[@"stageChannel"] isEqual:@"stable"] && countStages(root) == 1);

            writeAt(root, @"development-feed", @"second");
            [updater check];
            assert([updater confirmChannelSwitch:awaitPhase(updater, @"ready")[@"offer"] reason:&failure]);
            NSDictionary *devStage = awaitPhase(updater, @"ready");
            assert([devStage[@"staged"] boolValue] && [devStage[@"stageChannel"] isEqual:@"dev"]);
            NSString *switchNotes = @"Current change\nShared change\nOlder change";
            assert([devStage[@"releaseNotesAvailable"] boolValue] && [devStage[@"notes"] isEqual:switchNotes]);
            assert(![updater openStagedReleaseNotesForVersion:@"5.0.0" channel:@"dev" reason:&failure]);
            assert(![updater openStagedReleaseNotesForVersion:devStage[@"version"] channel:@"stable" reason:&failure]);
            writeAt(root, @"development-feed", @"first");
            __block BOOL checked = NO;
            [updater checkBeforeRestart:^{ checked = YES; }];
            state = [updater status];
            assert([state[@"phase"] isEqual:@"checking"] && [state[@"staged"] boolValue]);
            assert([state[@"notes"] isEqual:switchNotes] && [state[@"releaseNotesAvailable"] boolValue]);
            NSDictionary *updated = awaitPhase(updater, @"switch");
            assert(!updated[@"stageChannel"] && !updated[@"releaseNotesAvailable"]);
            assert(checked && [updated[@"offer"] isEqual:first[@"offer"]] && ![updated[@"staged"] boolValue]);
            assert(!awaitInstall(updater, YES, &failure));
            assertNoStage(root);
            assert([updater confirmChannelSwitch:updated[@"offer"] reason:&failure]);
            awaitPhase(updater, @"ready");

            [updater cancel];
            updater = makeUpdater(bundle, root);  // Model a new updater after an interrupted process.
            assert(!awaitRestore(updater));
            assertNoStage(root);
            [updater check];
            assert([updater confirmChannelSwitch:awaitPhase(updater, @"switch")[@"offer"] reason:&failure]);
            awaitPhase(updater, @"ready");
            [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"DisableUpdates"];
            assert(!awaitInstall(updater, NO, &failure));
            [NSUserDefaults.standardUserDefaults removeObjectForKey:@"DisableUpdates"];
            writeAt(root, @"development-feed", @"missing");
            checked = NO;
            [updater checkBeforeRestart:^{ checked = YES; }];
            NSDictionary *confirmedOffline = awaitPhase(updater, @"ready");
            assert(checked && [confirmedOffline[@"staged"] boolValue] && [confirmedOffline[@"message"] length]);
            writeAt(root, @"development-feed", @"first");
            assert(awaitInstall(updater, YES, &failure));
            return 0;
        }
        assert([version isEqual:@"2.0.0"]);
        assertNoStage(root);  // Initializing the new build removes the prior same-path backup.
        assert([state[@"channel"] isEqual:channel]);
        if ([channel isEqual:@"dev"]) {
            [updater check];
            awaitPhase(updater, @"current");
            writeAt(root, @"development-feed", @"invalid-notes");
            [updater check];
            assert([awaitPhase(updater, @"ready")[@"notes"] isEqual:@"Notes for dev-invalid-notes"]);
            writeAt(root, @"development-feed", @"later");
            [updater check];
            NSDictionary *skipped = awaitPhase(updater, @"ready");
            NSString *skippedNotes = @"Latest change\nShared change\nSkipped change";
            assert([skipped[@"version"] isEqual:@"2.10.0"] && [skipped[@"notes"] isEqual:skippedNotes]);
            [updater cancel];
            updater = makeUpdater(bundle, root);
            assert(awaitRestore(updater) && [updater.status[@"notes"] isEqual:skippedNotes]);
            writeAt(root, @"development-feed", @"first");
            assert([updater selectChannel:@"stable" reason:&failure]);
            NSDictionary *offer = awaitPhase(updater, @"ready");
            assert([offer[@"offerVersion"] isEqual:version]);  // Same version, different channel.
            assert([offer[@"version"] isEqual:@"2.10.0"]);
            assert([updater confirmChannelSwitch:offer[@"offer"] reason:&failure]);
            awaitPhase(updater, @"ready");
            assert(awaitInstall(updater, NO, &failure));
            writeAt(root, @"installed-on-quit", @"Development restarted at the same path. Stable installed on quit.");
        } else {
            assert([channel isEqual:@"stable"]);
            writeAt(root, @"stable-reopened", @"Stable opened at the same path and removed the previous backup.");
            writeAt(root, @"stable-feed", @"stable-four");
            [updater check];
            assert([awaitPhase(updater, @"ready")[@"version"] isEqual:@"4.0.0"]);
            writeAt(root, @"stable-feed", @"stable-five");
            __weak ChannelFixtureUpdater *weakUpdater = updater;
            __block BOOL quitting = NO;
            updater.onStaging = ^{
                assert([weakUpdater prepareToInstallOnQuit]);
                quitting = YES;
            };
            [updater check];
            NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:10];
            while ((!quitting || countStages(root) != 1) && [deadline timeIntervalSinceNow] > 0) {
                [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.03]];
            }
            assert(quitting && countStages(root) == 1);
            assert([updater.status[@"version"] isEqual:@"4.0.0"]);
            assert(awaitInstall(updater, NO, &failure));
            NSDictionary *installed = [NSDictionary dictionaryWithContentsOfURL:
                [bundle.bundleURL URLByAppendingPathComponent:@"Contents/Info.plist"]];
            assert([installed[@"CFBundleVersion"] isEqual:@"4.0.0"]);
            writeAt(root, @"late-stage-fenced", @"Quit kept the verified stage and discarded a later verification result.");
        }
        return 0;
    }
}
