#import "../jard_annotation_access.h"
#import <assert.h>

@interface JardAnnotationAccess (TestAccess)
- (void)observeSettingsFront:(BOOL)settingsFront active:(BOOL)active;
@end

@interface AnnotationAccessFixture : JardAnnotationAccess
@property(nonatomic) NSTimeInterval time;
@property(nonatomic) BOOL allowed;
@property(nonatomic) NSUInteger reads, probes, visits, resets;
@property(nonatomic, copy) void (^readAnswer)(NSNumber *);
@property(nonatomic, copy) void (^probeAnswer)(BOOL);
@property(nonatomic, copy) void (^resetAnswer)(BOOL);
@end

@implementation AnnotationAccessFixture
- (NSTimeInterval)now { return self.time; }
- (BOOL)preflight { return self.allowed; }
- (NSString *)bundleIdentifier { return @"local.fixture.annotation"; }
- (void)readFreshGrant:(void (^)(NSNumber *))reply { self.reads++; self.readAnswer = reply; }
- (void)probeCurrentProcess:(void (^)(BOOL))reply { self.probes++; self.probeAnswer = reply; }
- (BOOL)openSettings { self.visits++; return YES; }
- (void)resetBundle:(NSString *)bundle reply:(void (^)(BOOL))reply {
    assert([bundle isEqual:@"local.fixture.annotation"]);
    self.resets++;
    self.resetAnswer = reply;
}
- (void)finishRead:(NSNumber *)answer {
    void (^reply)(NSNumber *) = self.readAnswer;
    self.readAnswer = nil;
    assert(reply);
    reply(answer);
}
- (void)finishProbe:(BOOL)answer {
    void (^reply)(BOOL) = self.probeAnswer;
    self.probeAnswer = nil;
    assert(reply);
    reply(answer);
}
- (void)finishReset:(BOOL)answer {
    void (^reply)(BOOL) = self.resetAnswer;
    self.resetAnswer = nil;
    assert(reply);
    reply(answer);
}
@end

static NSDictionary *status(JardAnnotationAccess *access, BOOL refresh) {
    __block NSDictionary *state;
    [access request:@"status" refresh:refresh reply:^(id result, NSString *error) { assert(!error); state = result; }];
    return state;
}

int main(void) {
    @autoreleasepool {
        AnnotationAccessFixture *access = AnnotationAccessFixture.new;
        NSMutableArray<NSNumber *> *settingsEvents = NSMutableArray.new;
        access.settingsVisitChanged = ^(BOOL granted) { [settingsEvents addObject:@(granted)]; };
        access.time = 1;
        assert([status(access, NO)[@"authorization"] isEqual:@"checking"] && access.reads == 0);
        assert([status(access, YES)[@"authorization"] isEqual:@"checking"] && access.reads == 1);
        assert([status(access, YES)[@"authorization"] isEqual:@"checking"] && access.reads == 1);
        [access finishRead:nil];
        assert([status(access, YES)[@"authorization"] isEqual:@"unavailable"]);
        assert(!access.visits && !access.probes && !access.resets);
        [access request:@"allow" refresh:NO reply:^(id result, NSString *error) { assert(result && !error); }];
        assert(access.visits == 1);
        access.time = 3;
        assert([access pollSettingsVisit]);
        access.time = 4;
        [access observeSettingsFront:YES active:NO];
        [access observeSettingsFront:NO active:YES];
        access.time = 6;
        [access finishRead:@NO];
        assert(![status(access, NO)[@"canRepair"] boolValue]);
        access.time = 8;
        status(access, YES);
        [access finishRead:@NO];
        assert([status(access, NO)[@"canRepair"] boolValue]);
        [access request:@"repair" refresh:NO reply:^(id result, NSString *error) { assert(!result && error.length); }];
        assert(access.resets == 1 && access.resetting && [status(access, YES)[@"busy"] boolValue]);
        [access finishReset:NO];
        [access request:@"repair" refresh:NO reply:^(id result, NSString *error) {
            assert(!error && [result[@"authorization"] isEqual:@"checking"]);
        }];
        [access finishReset:YES];
        assert(access.resets == 2 && access.visits == 2 && !access.resetting);

        access.mayProbe = ^BOOL{ return NO; };
        status(access, YES);
        [access finishRead:@YES];
        assert(access.probes == 0);
        access.mayProbe = ^BOOL{ return YES; };
        status(access, YES);
        [access finishRead:@YES];
        assert(access.probes == 1);
        [access finishProbe:NO];
        assert([status(access, NO)[@"authorization"] isEqual:@"restart"] && access.needsRestart);
        assert(([settingsEvents isEqual:@[@NO, @NO, @YES]]) && ![access pollSettingsVisit]);

        access.time = 10;
        status(access, YES);
        [access finishRead:@YES];
        [access finishProbe:YES];
        assert([status(access, NO)[@"authorization"] isEqual:@"granted"] && !access.needsRestart);
        access.time = 12;
        status(access, YES);
        [access finishRead:@YES];
        assert(access.probes == 2);
        access.time = 14;
        status(access, YES);
        access.allowed = YES;
        assert([status(access, YES)[@"authorization"] isEqual:@"granted"]);
        [access finishRead:@NO];
        assert([status(access, NO)[@"authorization"] isEqual:@"granted"]);
        assert(settingsEvents.count == 3);

        AnnotationAccessFixture *newVisit = AnnotationAccessFixture.new;
        newVisit.time = 20;
        newVisit.settingsVisitChanged = access.settingsVisitChanged;
        status(newVisit, YES);
        [newVisit request:@"allow" refresh:NO reply:^(id result, NSString *error) { assert(result && !error); }];
        [newVisit finishRead:@YES]; // A check begun before opening Settings cannot settle this visit.
        assert(newVisit.probes == 0 && settingsEvents.count == 4);
        assert([newVisit pollSettingsVisit]);
        [newVisit finishRead:@YES];
        [newVisit finishProbe:YES];
        assert(![newVisit pollSettingsVisit] && settingsEvents.count == 5 && settingsEvents.lastObject.boolValue);
        puts("Screen Recording verified guarded restart, stale repair, and one grant event for the current System Settings visit.");
    }
}
