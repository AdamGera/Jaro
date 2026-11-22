#import "../jard_recording_environment.h"
#import "../jard_dictation_media.h"
#import <IOKit/pwr_mgt/IOPMLib.h>
#import <assert.h>
#import <math.h>

@interface FixtureOutput : NSObject <JardRecordingOutput>
@property(atomic, copy) NSArray<NSNumber *> *volumes;
@property(atomic) BOOL rejectWrite;
@end

@interface JardRecordingEnvironment (MediaFixture)
@property(nonatomic, strong) JardDictationMedia *media;
@end

@interface FixtureMedia : JardDictationMedia
@property(atomic) NSUInteger starts, stops;
@end

@implementation FixtureMedia
- (void)begin { self.starts++; }
- (void)finish { self.stops++; }
- (void)shutdown { }
@end

@implementation FixtureOutput
- (NSArray<NSNumber *> *)readVolumes { return self.volumes; }
- (BOOL)writeVolumes:(NSArray<NSNumber *> *)volumes {
    if (self.rejectWrite) return NO;
    self.volumes = volumes;
    return YES;
}
@end

static void waitFor(BOOL (^condition)(void)) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:3];
    while (!condition() && deadline.timeIntervalSinceNow > 0)
        [NSRunLoop.mainRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:.01]];
    assert(condition());
}

static BOOL preventsIdleSleep(void) {
    CFDictionaryRef active = NULL;
    assert(IOPMCopyAssertionsByProcess(&active) == kIOReturnSuccess);
    NSDictionary *byProcess = CFBridgingRelease(active);
    for (NSDictionary *assertion in byProcess[@(NSProcessInfo.processInfo.processIdentifier)]) {
        if ([assertion[(__bridge NSString *)kIOPMAssertionNameKey] isEqual:@"Jard dictation"] &&
            [assertion[(__bridge NSString *)kIOPMAssertionTypeKey]
                isEqual:(__bridge NSString *)kIOPMAssertionTypePreventUserIdleSystemSleep]) return YES;
    }
    return NO;
}

static BOOL volumesMatch(FixtureOutput *output, double left, double right) {
    NSArray<NSNumber *> *values = output.volumes;
    return fabs(values[0].doubleValue - left) < .002 && fabs(values[1].doubleValue - right) < .002;
}

static void testOverlappingCaptures(void) {
    FixtureOutput *output = FixtureOutput.new;
    output.volumes = @[@.8, @.5];
    JardRecordingEnvironment *environment = [[JardRecordingEnvironment alloc] initWithOutput:^{ return output; }];
    FixtureMedia *media = [[FixtureMedia alloc] initWithPlayers:^{ return @[]; }];
    environment.media = media;
    __block BOOL completed = NO;

    [environment setAnnotationRecording:YES mode:@"mute" percent:20 completion:nil];
    waitFor(^{ return volumesMatch(output, 0, 0); });
    assert(!preventsIdleSleep());
    [environment beginWithMode:@"mute" percent:20];
    waitFor(^{ return preventsIdleSleep(); });
    [environment finish:^{ completed = YES; }];
    waitFor(^{ return completed; });
    assert(volumesMatch(output, 0, 0) && !preventsIdleSleep());
    [environment setAnnotationRecording:NO mode:nil percent:0 completion:nil];
    waitFor(^{ return volumesMatch(output, .8, .5); });

    [environment beginWithMode:@"lower" percent:50];
    [environment setAnnotationRecording:YES mode:@"lower" percent:20 completion:nil];
    waitFor(^{ return volumesMatch(output, .16, .10); });
    completed = NO;
    [environment setAnnotationRecording:NO mode:nil percent:0 completion:^{ completed = YES; }];
    waitFor(^{ return completed; });
    assert(volumesMatch(output, .4, .25) && preventsIdleSleep());
    [environment finish:nil];
    waitFor(^{ return volumesMatch(output, .8, .5); });

    [environment setAnnotationRecording:YES mode:@"lower" percent:20 completion:nil];
    waitFor(^{ return volumesMatch(output, .16, .10); });
    completed = NO;
    __block BOOL laterCue = NO;
    NSTimeInterval stopped = NSProcessInfo.processInfo.systemUptime;
    [environment setAnnotationRecording:NO mode:nil percent:0 completion:^{ completed = YES; }];
    [environment setAnnotationRecording:NO mode:nil percent:0 completion:^{ laterCue = YES; }];
    waitFor(^{ return laterCue; });
    assert(completed && volumesMatch(output, .8, .5) && NSProcessInfo.processInfo.systemUptime - stopped >= .95);

    [environment setAnnotationRecording:YES mode:@"pause" percent:20 completion:nil];
    waitFor(^BOOL{ return media.starts == 1; });
    [environment beginWithMode:@"mute" percent:20];
    waitFor(^{ return volumesMatch(output, 0, 0); });
    [environment finish:nil];
    waitFor(^{ return volumesMatch(output, .8, .5); });
    assert(media.stops == 0);
    [environment beginWithMode:@"pause" percent:20];
    [environment setAnnotationRecording:NO mode:nil percent:0 completion:nil];
    completed = NO;
    [environment finish:^{ completed = YES; }];
    waitFor(^{ return completed; });
    assert(media.starts == 1 && media.stops == 1);

    [environment setAnnotationRecording:YES mode:@"lower" percent:20 completion:nil];
    waitFor(^{ return volumesMatch(output, .16, .10); });
    output.volumes = @[@.63, @.32];
    [environment beginWithMode:@"mute" percent:20];
    [environment shutdown];
    assert(volumesMatch(output, .63, .32) && !preventsIdleSleep());
    puts("Annotate and dictation kept their shared volume and media effects until the last owner released them.");
}

int main(void) {
    @autoreleasepool {
        FixtureOutput *builtIn = FixtureOutput.new;
        FixtureOutput *switched = FixtureOutput.new;
        builtIn.volumes = @[@.8, @.5];
        switched.volumes = @[@.9, @.7];
        __block FixtureOutput *route = builtIn;
        JardRecordingEnvironment *environment = [[JardRecordingEnvironment alloc] initWithOutput:^{ return route; }];

        [environment beginWithMode:@"none" percent:20];
        waitFor(^{ return preventsIdleSleep(); });
        assert(volumesMatch(builtIn, .8, .5));
        [environment finish:nil];
        waitFor(^BOOL{ return !preventsIdleSleep(); });

        [environment beginWithMode:@"mute" percent:20];
        waitFor(^{ return volumesMatch(builtIn, 0, 0); });
        route = switched;
        __block BOOL restored = NO;
        [environment finish:^{ restored = YES; }];
        waitFor(^{ return restored; });
        assert(volumesMatch(builtIn, .8, .5) && volumesMatch(switched, .9, .7));

        [environment beginWithMode:@"lower" percent:20];
        waitFor(^{ return volumesMatch(switched, .18, .14); });
        restored = NO;
        NSTimeInterval stopped = NSProcessInfo.processInfo.systemUptime;
        [environment finish:^{ restored = YES; }];
        waitFor(^{ return restored; });
        assert(NSProcessInfo.processInfo.systemUptime - stopped >= .95);
        assert(volumesMatch(switched, .9, .7) && !preventsIdleSleep());

        [environment beginWithMode:@"lower" percent:20];
        waitFor(^{ return volumesMatch(switched, .18, .14); });
        switched.volumes = @[@.62, @.32];
        restored = NO;
        [environment finish:^{ restored = YES; }];
        waitFor(^{ return restored; });
        assert(volumesMatch(switched, .62, .32));

        [environment beginWithMode:@"lower" percent:20];
        waitFor(^{ return volumesMatch(switched, .124, .064); });
        restored = NO;
        [environment finish:^{ restored = YES; }];
        waitFor(^BOOL{ return switched.volumes[0].doubleValue > .2; });
        switched.volumes = @[@.71, @.34];
        waitFor(^{ return restored; });
        assert(volumesMatch(switched, .71, .34));

        [environment beginWithMode:@"lower" percent:20];
        waitFor(^{ return volumesMatch(switched, .142, .068); });
        [environment finish:nil];
        [environment beginWithMode:@"mute" percent:20];
        waitFor(^{ return volumesMatch(switched, 0, 0); });
        [environment shutdown];
        assert(volumesMatch(switched, .71, .34) && !preventsIdleSleep());
        testOverlappingCaptures();

        JardRecordingEnvironment *unsupported = [[JardRecordingEnvironment alloc] initWithOutput:^{ return switched; }];
        switched.rejectWrite = YES;
        [unsupported beginWithMode:@"mute" percent:20];
        [unsupported shutdown];
        assert(volumesMatch(switched, .71, .34) && !preventsIdleSleep());
        puts("Recording environment held and released an IOKit assertion, preserved stereo balance, and restored only its own output edits.");
    }
}
