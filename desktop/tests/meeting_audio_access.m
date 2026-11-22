#import "../jard_meeting_audio_access.h"
#import <assert.h>

@interface JardMeetingAudioAccess (TestAccess)
- (void)observeSettingsFront:(BOOL)settingsFront active:(BOOL)active;
@end

@interface AudioAccessFixture : JardMeetingAudioAccess
@property(nonatomic) int32_t tap;
@property(nonatomic) BOOL screen, promptAvailable;
@property(nonatomic) NSTimeInterval time;
@property(nonatomic) NSUInteger prompts, settings, resets;
@property(nonatomic, copy) void (^answer)(BOOL);
@property(nonatomic, copy) void (^resetDone)(NSString *);
@end

@implementation AudioAccessFixture
- (int32_t)tapStatus { return self.tap; }
- (BOOL)screenAllowed { return self.screen; }
- (NSTimeInterval)now { return self.time; }
- (void)observeSettings {}
- (NSString *)bundleIdentifier { return @"local.fixture.meeting"; }
- (BOOL)openSettings { self.settings++; return YES; }
- (BOOL)requestTap:(void (^)(BOOL))reply {
    if (!self.promptAvailable) return NO;
    self.prompts++;
    self.answer = reply;
    return YES;
}
- (void)resetBundle:(NSString *)bundle reply:(void (^)(NSString *))reply {
    assert([bundle isEqual:@"local.fixture.meeting"]);
    self.resets++;
    self.resetDone = reply;
}
- (void)finishPrompt:(BOOL)granted {
    self.tap = granted ? 0 : 1;
    void (^answer)(BOOL) = self.answer;
    self.answer = nil;
    answer(granted);
}
- (void)finishReset:(NSString *)error {
    if (!error) self.tap = 2;
    void (^done)(NSString *) = self.resetDone;
    self.resetDone = nil;
    done(error);
}
@end

static NSDictionary *status(JardMeetingAudioAccess *access) {
    __block NSDictionary *state = nil;
    [access request:@{@"action": @"status"} reply:^(NSDictionary *result, NSString *error) {
        assert(!error);
        state = result;
    }];
    assert(state);
    return state;
}

int main(void) {
    @autoreleasepool {
        AudioAccessFixture *fixture = AudioAccessFixture.new;
        fixture.tap = 2;
        fixture.promptAvailable = YES;
        fixture.time = 1;
        assert([status(fixture)[@"authorization"] isEqual:@"request"] && !fixture.prompts && !fixture.settings);
        __block BOOL allowed = NO, started = NO;
        [fixture request:@{@"action": @"allow"} reply:^(NSDictionary *result, NSString *error) {
            assert(!error && [result[@"authorization"] isEqual:@"denied"]);
            allowed = YES;
        }];
        assert([status(fixture)[@"busy"] boolValue] && fixture.prompts == 1);
        fixture.screen = YES;
        [fixture checkBackendWithPrompt:YES screenRequest:nil reply:^(NSString *backend) {
            assert([backend isEqual:@"screen"]);
            started = YES;
        }];
        assert(fixture.prompts == 1 && !started && !allowed);
        [fixture finishPrompt:NO];
        assert(started && allowed && ![status(fixture)[@"busy"] boolValue]);

        JardDictationReply refused = ^(id result, NSString *error) { assert(!result && [error containsString:@"returning"]); };
        [fixture request:@{@"action": @"repair"} reply:refused];
        [fixture request:@{@"action": @"allow"} reply:^(NSDictionary *result, NSString *error) {
            assert(!error && ![result[@"canRepair"] boolValue] && [result[@"screen"] boolValue]);
        }];
        assert(fixture.settings == 1 && fixture.prompts == 1);
        fixture.time = 10;
        [fixture observeSettingsFront:NO active:YES];
        assert(![status(fixture)[@"canRepair"] boolValue]);
        [fixture observeSettingsFront:YES active:NO];
        [fixture observeSettingsFront:NO active:NO];
        assert(![status(fixture)[@"canRepair"] boolValue]);
        [fixture observeSettingsFront:NO active:YES];
        fixture.time = 11;
        [fixture request:@{@"action": @"repair"} reply:refused];
        fixture.time = 12;
        assert([status(fixture)[@"canRepair"] boolValue]);
        [fixture request:@{@"action": @"repair"} reply:^(id result, NSString *error) {
            assert(!result && [error isEqual:@"fixture error"]);
        }];
        assert(fixture.resets == 1 && [status(fixture)[@"busy"] boolValue]);
        [fixture finishReset:@"fixture error"];
        assert([status(fixture)[@"canRepair"] boolValue]);
        __block BOOL reset = NO;
        [fixture request:@{@"action": @"repair"} reply:^(NSDictionary *result, NSString *error) {
            assert(!error && [result[@"authorization"] isEqual:@"granted"] && [result[@"screen"] boolValue]);
            assert(![result[@"canRepair"] boolValue]);
            reset = YES;
        }];
        [fixture finishReset:nil];
        assert(fixture.prompts == 2 && fixture.resets == 2 && !reset);
        [fixture finishPrompt:YES];
        assert(reset);
        [fixture request:@{@"action": @"repair"} reply:refused];
        [fixture checkBackendWithPrompt:NO screenRequest:nil reply:^(NSString *backend) { assert([backend isEqual:@"tapAndScreen"]); }];
        fixture.screen = NO;
        [fixture checkBackendWithPrompt:NO screenRequest:nil reply:^(NSString *backend) { assert([backend isEqual:@"tap"]); }];
        fixture.tap = -1;
        [fixture checkBackendWithPrompt:NO screenRequest:nil reply:^(NSString *backend) { assert(!backend); }];
        [fixture checkBackendWithPrompt:YES screenRequest:^(void (^reply)(BOOL)) { reply(YES); }
                                 reply:^(NSString *backend) { assert([backend isEqual:@"screen"]); }];

        AudioAccessFixture *visit = AudioAccessFixture.new;
        NSMutableArray<NSNumber *> *settingsEvents = NSMutableArray.new;
        visit.settingsVisitChanged = ^(BOOL granted) { [settingsEvents addObject:@(granted)]; };
        visit.tap = 1;
        visit.screen = YES;
        assert([visit showSettings] && [visit pollSettingsVisit]);
        assert([settingsEvents isEqual:@[@NO]]); // Screen fallback alone does not authorize the audio tap.
        visit.tap = 0;
        assert(![visit pollSettingsVisit] && ([settingsEvents isEqual:@[@NO, @YES]]));
        assert([visit showSettings] && ![visit pollSettingsVisit]);
        assert(settingsEvents.count == 2);
        puts("Meeting audio verified consent repair, screen fallback, and one grant event for an audio-only System Settings visit.");
    }
}
