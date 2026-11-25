#import "../jard_updater.h"
#import <assert.h>

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc > 1 && !strcmp(argv[1], "--jaro-relaunch-update")) return JardRunUpdateRelaunch(argc, argv);
        NSBundle *bundle = NSBundle.mainBundle;
        NSURL *installed = bundle.bundleURL.URLByStandardizingPath;
        NSURL *root = installed.URLByDeletingLastPathComponent;
        NSString *version = [bundle objectForInfoDictionaryKey:@"CFBundleVersion"];
        NSDictionary *environment = NSProcessInfo.processInfo.environment;
        NSURL *marker = [root URLByAppendingPathComponent:@"launched"];
        NSString *stagePath = [root URLByAppendingPathComponent:@".jard-update-fixture"].path;
        NSURL *expectedLibrary = [root URLByAppendingPathComponent:@"Library" isDirectory:YES].URLByResolvingSymlinksInPath;
        NSURL *openedLibrary = [NSURL fileURLWithPath:environment[@"RUMBLE_DATA_DIR"] isDirectory:YES].URLByResolvingSymlinksInPath;
        if ([environment[@"JARD_UPDATE_ROLLED_BACK"] isEqual:@"1"]) {
            assert([version isEqual:@"1.1.0"]);
            assert([openedLibrary.path isEqual:expectedLibrary.path]);
            [@"old version restored" writeToURL:marker atomically:YES encoding:NSUTF8StringEncoding error:nil];
            return 0;
        }
        NSDictionary *configuration = @{@"feed_url": @"https://example.invalid/feed", @"public_key": @"unused"};
        if ([version isEqual:@"1.2.0"]) {
            assert([openedLibrary.path isEqual:expectedLibrary.path]);
            JardUpdater *updater = [[JardUpdater alloc] initWithConfiguration:configuration bundle:bundle stateChanged:nil];
            assert(updater);
            BOOL quitOnly = [NSFileManager.defaultManager fileExistsAtPath:[root URLByAppendingPathComponent:@"quit-only"].path];
            assert(JardIsUpdateStartupProbe() != quitOnly);
            [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.3]];
            assert([NSFileManager.defaultManager fileExistsAtPath:stagePath]);
            if ([[bundle objectForInfoDictionaryKey:@"FixtureFailsAtLaunch"] boolValue]) return 2;
            [updater confirmStartup];
            NSDate *until = [NSDate dateWithTimeIntervalSinceNow:8];
            while ([NSFileManager.defaultManager fileExistsAtPath:stagePath] && until.timeIntervalSinceNow > 0)
                [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
            BOOL backup = [NSFileManager.defaultManager fileExistsAtPath:stagePath];
            NSString *proof = backup ? @"new version launched; backup remains" : @"new version launched; backup cleaned";
            [proof writeToURL:marker atomically:YES encoding:NSUTF8StringEncoding error:nil];
            return 0;
        }
        NSURL *stage = [root URLByAppendingPathComponent:@".jard-update-fixture" isDirectory:YES];
        NSDictionary *pending = @{@"installed": installed.path, @"runningVersion": version,
            @"stage": stage.path, @"candidate": @"Fixture.app", @"version": @"1.2.0", @"notes": @"Isolated test"};
        [NSUserDefaults.standardUserDefaults setObject:pending forKey:@"JardPendingUpdate"];
        JardUpdater *updater = [[JardUpdater alloc] initWithConfiguration:configuration bundle:bundle stateChanged:nil];
        __block NSNumber *restored = nil;
        [updater restoreStagedUpdateWithCompletion:^(BOOL ready) { restored = @(ready); }];
        NSDate *until = [NSDate dateWithTimeIntervalSinceNow:8];
        while (!restored && until.timeIntervalSinceNow > 0)
            [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        assert(restored.boolValue);
        assert([[updater status][@"phase"] isEqual:@"ready"]);
        __block NSString *failure = nil;
        __block NSNumber *applied = nil;
        BOOL updateOnQuit = argc > 1 && [@(argv[1]) isEqual:@"quit"];
        JardUpdateInstallReply complete = ^(BOOL success, NSString *reason) { applied = @(success); failure = reason; };
        if (updateOnQuit) [updater installOnQuitWithCompletion:complete];
        else [updater restartWithCompletion:complete];
        until = [NSDate dateWithTimeIntervalSinceNow:8];
        while (!applied && until.timeIntervalSinceNow > 0)
            [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.02]];
        if (!applied.boolValue) {
            fprintf(stderr, "%s\n", (failure ?: @"The installation fixture timed out.").UTF8String);
            return 1;
        }
        return 0;
    }
}
