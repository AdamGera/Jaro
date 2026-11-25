#import "../jard_meetings.h"
#import "../meeting_presence.h"
#import "../jard_calendar.h"
#import <assert.h>

@interface JardMeetings (Test)
@property(nonatomic) NSTimeInterval lastPresent;
@property(nonatomic, strong) JardMeetingPresence *presence;
@property(nonatomic, readonly) JardCall *offered;
@property(nonatomic, strong) JardCalendar *calendar;
@property(nonatomic, strong) NSMutableDictionary *policies;
@property(nonatomic) BOOL autoStop;
- (NSDictionary *)status;
- (void)observed:(NSArray<JardCall *> *)calls;
- (void)observeCalls:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now date:(NSDate *)date;
- (void)pollCalendarAt:(NSDate *)date;
- (void)dismissOffer;
- (NSString *)policyForCall:(JardCall *)call;
- (NSString *)policyForCalendar:(JardCalendarMeeting *)event;
- (void)renderPanel;
- (void)copyNew:(BOOL)onlyNew to:(NSPasteboard *)board reply:(JardDictationReply)reply;
@end

@interface TestMeetings : JardMeetings
@property(nonatomic, copy) NSURL *joined;
@end
@implementation TestMeetings
- (void)renderPanel {}
- (BOOL)openMeetingLink:(NSURL *)url { self.joined = url; return YES; }
@end

@interface TestCalendar : JardCalendar
@property(nonatomic, copy) NSArray<JardCalendarMeeting *> *events;
@property(nonatomic) NSUInteger requests;
@end
@implementation TestCalendar
- (BOOL)connected { return self.requests > 0; }
- (BOOL)reminders { return YES; }
- (NSInteger)lead { return 1; }
- (NSString *)access { return self.connected ? @"granted" : @"notDetermined"; }
- (NSArray<JardCalendarMeeting *> *)meetings { return self.connected ? self.events ?: @[] : @[]; }
- (void)connect:(void (^)(NSString *))reply { self.requests++; reply(nil); }
@end

static void exerciseCalendar(void) {
    NSDate *now = NSDate.date;
    JardCalendarMeeting *event = JardCalendarOccurrence(@"repeat", @"Design sync", @"work", [now dateByAddingTimeInterval:100],
        [now dateByAddingTimeInterval:1700], NO, @[@"Sam", @"Sam"],
        @[@"https://meet.google.com.example.com/abc-defg-hij", @"Join <https://meet.google.com/abc-defg-hij>."]);
    assert([event.provider isEqual:@"Google Meet"] && [event.link.host isEqual:@"meet.google.com"] && event.attendees.count == 1);
    assert(!JardCalendarOccurrence(@"hidden", @"Ignore", @"work", event.start, event.end, YES, @[@"Sam"], @[]));
    assert(!JardCalendarOccurrence(@"fake", @"Ignore", @"work", event.start, event.end, NO, @[],
        @[@"https://meet.google.com.evil.example/abc-defg-hij", @"https://meet.google.com@evil.example/abc-defg-hij",
          @"https://zoom.us/download", @"https://zoom.us:8888/j/123", @"https://teams.microsoft.com/home"]));
    assert(!JardCalendarMatchesCall(event, JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/xyz-wxyz-xyz"], @"com.google.chrome")));

    __block NSDictionary *take = nil;
    TestMeetings *meeting = [[TestMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(prompt); take = options; reply(@{}, nil);
    } send:^(NSString *command, NSDictionary *options, JardDictationReply reply) { assert(NO); } changed:nil];
    [meeting.policies removeAllObjects];
    TestCalendar *calendar = [[TestCalendar alloc] initWithChanged:nil];
    calendar.events = @[event];
    meeting.calendar = calendar;
    [meeting request:@{@"action": @"status"} reply:^(id result, NSString *error) {
        assert(!error && ![result[@"calendar"][@"connected"] boolValue] && calendar.requests == 0);
    }];
    [meeting request:@{@"action": @"calendarConnect"} reply:^(id result, NSString *error) {
        assert(!error && [result[@"calendar"][@"connected"] boolValue] && calendar.requests == 1);
    }];
    [meeting pollCalendarAt:now];
    assert([[meeting status][@"phase"] isEqual:@"idle"]);
    [meeting pollCalendarAt:[now dateByAddingTimeInterval:41]];
    assert([[meeting status][@"phase"] isEqual:@"reminder"]);
    [meeting request:@{@"action": @"calendarEvent", @"id": event.identifier, @"choice": @"dismiss"}
        reply:^(id result, NSString *error) { assert(!error && !meeting.joined); }];
    JardCall *browser = JardMeetingBrowserCall(event.link, @"com.google.chrome");
    [meeting observeCalls:@[browser] now:10 date:now];
    [meeting observeCalls:@[browser] now:12 date:now];
    [meeting pollCalendarAt:[now dateByAddingTimeInterval:50]];
    assert([[meeting status][@"phase"] isEqual:@"idle"]);

    JardCalendarMeeting *moved = JardCalendarOccurrence(@"repeat", event.title, @"work", [now dateByAddingTimeInterval:200],
        event.end, NO, event.attendees, @[event.link.absoluteString]);
    assert(![event.identifier isEqual:moved.identifier]);
    calendar.events = @[moved];
    [meeting pollCalendarAt:[now dateByAddingTimeInterval:145]];
    assert([[meeting status][@"phase"] isEqual:@"reminder"]);
    [meeting request:@{@"action": @"calendarEvent", @"id": moved.identifier, @"choice": @"join"}
        reply:^(id result, NSString *error) { assert(!error && [meeting.joined isEqual:moved.link] && !take); }];
    [meeting request:@{@"action": @"calendarEvent", @"id": moved.identifier, @"choice": @"joinRecord"}
        reply:^(id result, NSString *error) {
            assert(!error && [take[@"meetingCalendar"][@"title"] isEqual:moved.title]);
            assert([take[@"meetingCalendar"][@"attendees"] isEqual:@[@"Sam"]]);
        }];
    [meeting shutdown];
}

static void exerciseAutoStop(BOOL manualStop) {
    NSString *chrome = @"com.google.chrome";
    JardCall *original = JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/abc-defg-hij"], chrome);
    JardCall *named = JardMeetingBrowserTitleCall(@"Meet – Design sync", chrome);
    JardCall *changed = JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/abc-defg-xyz"], chrome);
    JardCall *third = JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/xyz-wxyz-xyz"], chrome);
    __block NSString *identifier;
    __block NSUInteger starts = 0, stops = 0;
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *previous = [defaults stringForKey:@"JardLastMeetingSession"];
    TestMeetings *meeting = [[TestMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(!prompt && [options[@"meetingApplication"] isEqual:chrome]);
        starts++;
        identifier = options[@"meeting"];
        reply(@{}, nil);
    } send:^(NSString *command, NSDictionary *options, JardDictationReply reply) {
        if ([command isEqual:@"jard.meetingCompletion"]) { reply(@{@"status": @"processing"}, nil); return; }
        assert([command isEqual:@"jard.stopMeeting"] && [options[@"meeting"] isEqual:identifier]);
        stops++; reply(nil, nil);
    } changed:nil];
    [meeting.policies removeAllObjects];
    meeting.policies[original.provider] = @{@"label": original.label, @"policy": @"auto"};
    meeting.autoStop = YES;
    NSDate *date = NSDate.date;
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    [meeting observeCalls:@[original] now:now date:date];
    [meeting observeCalls:@[original] now:now + 2 date:date];
    assert(identifier && starts == 1);
    [meeting handleCapture:@{@"meeting": identifier, @"event": @"started"}];
    JardCall *unverifiedChange = JardMeetingBrowserTitleCall(@"Meet - abc-defg-xyz", chrome);
    [meeting observeCalls:@[unverifiedChange] now:now + 3 date:date];
    [meeting observeCalls:@[unverifiedChange] now:now + 7 date:date];
    assert([[meeting status][@"phase"] isEqual:@"recording"]);
    now += 5;
    [meeting observeCalls:@[original] now:now + 3 date:date];
    [meeting observeCalls:@[named] now:now + 4 date:date];
    assert([[meeting status][@"phase"] isEqual:@"recording"]);
    original.inputActive = NO;
    [meeting observeCalls:@[original] now:now + 6 date:date];
    assert([[meeting status][@"phase"] isEqual:@"recording"]);
    [meeting observeCalls:@[] now:now + 9 date:date];
    assert([[meeting status][@"phase"] isEqual:@"callEnded"]);
    [meeting observeCalls:@[original] now:now + 10 date:date];
    assert([[meeting status][@"phase"] isEqual:@"recording"]);
    [meeting observeCalls:@[changed] now:now + 11 date:date];
    [meeting observeCalls:@[changed] now:now + 14 date:date];
    assert([[meeting status][@"phase"] isEqual:@"recording"]);
    [meeting observeCalls:@[changed] now:now + 15 date:date];
    assert([[meeting status][@"phase"] isEqual:@"callEnded"]);
    [meeting request:@{@"action": @"keepRecording"} reply:^(id result, NSString *error) { assert(!error); }];
    [meeting observeCalls:@[original] now:now + 16 date:date];
    [meeting observeCalls:@[named] now:now + 17 date:date];
    [meeting observeCalls:@[changed] now:now + 18 date:date];
    [meeting observeCalls:@[changed] now:now + 48 date:date];
    assert(stops == 0 && [[meeting status][@"phase"] isEqual:@"recording"]);
    [meeting observeCalls:@[third] now:now + 49 date:date];
    [meeting observeCalls:@[third] now:now + 53 date:date];
    assert([[meeting status][@"phase"] isEqual:@"callEnded"]);
    [meeting observeCalls:@[named] now:now + 54 date:date];
    assert([[meeting status][@"phase"] isEqual:@"callEnded"]);
    [meeting observeCalls:@[original] now:now + 55 date:date];
    assert([[meeting status][@"phase"] isEqual:@"recording"]);
    [meeting observeCalls:@[third] now:now + 56 date:date];
    [meeting observeCalls:@[third] now:now + 60 date:date];
    assert([[meeting status][@"phase"] isEqual:@"callEnded"]);
    [meeting observeCalls:@[named] now:now + 89 date:date];
    assert(stops == 0 && [[meeting status][@"phase"] isEqual:@"callEnded"]);
    if (manualStop) [meeting request:@{@"action": @"stop"} reply:^(id result, NSString *error) { assert(!error); }];
    else [meeting observeCalls:@[named] now:now + 90 date:date];
    assert(stops == 1 && [[meeting status][@"phase"] isEqual:@"saving"]);
    NSString *prior = identifier;
    original.inputActive = YES;
    for (NSUInteger second = 91; second < 100; ++second)
        [meeting observeCalls:@[original] now:now + second date:date];
    assert(starts == 1 && meeting.lastPresent == now + 99);
    [meeting handleCapture:@{@"meeting": identifier, @"event": @"accepted", @"id": NSUUID.UUID.UUIDString.lowercaseString}];
    [meeting observeCalls:@[third] now:now + 100 date:date];
    assert(starts == 2 && ![identifier isEqual:prior]);
    [meeting handleCapture:@{@"meeting": identifier, @"event": @"started"}];
    [meeting observeCalls:@[original] now:now + 101 date:date];
    [meeting observeCalls:@[original] now:now + 106 date:date];
    assert(stops == 1 && [[meeting status][@"phase"] isEqual:@"recording"]);
    [meeting observeCalls:@[changed] now:now + 107 date:date];
    [meeting observeCalls:@[changed] now:now + 111 date:date];
    assert(stops == 1 && [[meeting status][@"phase"] isEqual:@"callEnded"]);
    [meeting shutdown];
    [defaults setObject:previous forKey:@"JardLastMeetingSession"];
}

static void exerciseBrowserTitles(void) {
    NSString *arc = @"company.thebrowser.browser";
    JardCall *address = JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/meet/abc-defg-hij?authuser=1"], arc);
    JardCall *codeTitle = JardMeetingBrowserTitleCall(@"Meet – ABC-DEFG-HIJ - Arc", arc);
    JardCall *namedTitle = JardMeetingBrowserTitleCall(@"Meet — Design sync - Arc", arc);
    JardCall *different = JardMeetingBrowserTitleCall(@"Meet - xyz-wxyz-xyz", arc);
    assert([codeTitle.identity isEqual:address.identity] && [codeTitle.googleMeetCode isEqual:@"abc-defg-hij"]);
    assert([namedTitle.label isEqual:@"Google Meet"] && !namedTitle.googleMeetCode && !namedTitle.slow);
    assert(![namedTitle.identity containsString:@"Design sync"] && [address matchesObservation:namedTitle]);
    assert(![address matchesObservation:different]);
    assert(!JardMeetingBrowserTitleCall(@"Google Meet - Arc", arc));
    assert(!JardMeetingBrowserTitleCall(@"Meet -  - Arc", arc));
    assert(!JardMeetingBrowserTitleCall(@"Meet - abc-defg-hij", @"com.example.editor"));

    JardMeetingPresence *watch = JardMeetingPresence.new;
    assert(![watch offerFromCalls:@[address] now:10]);
    assert([watch offerFromCalls:@[codeTitle] now:12] == codeTitle);
    [watch snooze:codeTitle absence:3 now:12];
    assert(![watch offerFromCalls:@[namedTitle] now:14]);
    assert(![watch offerFromCalls:@[namedTitle] now:16]);
    assert(![watch offerFromCalls:@[address] now:18]);
    assert(![watch offerFromCalls:@[different] now:20]);
    assert([watch offerFromCalls:@[different] now:22] == different);
    puts("Google Meet title and URL evidence shared call identity and snooze, with unresolved titles preserving an active call.");
}

static void exerciseSwitchMemory(void) {
    NSString *arc = @"company.thebrowser.browser";
    JardCall *first = JardMeetingBrowserTitleCall(@"Meet - abc-defg-hij", arc);
    JardCall *next = JardMeetingBrowserTitleCall(@"Meet - xyz-wxyz-xyz", arc);
    JardCall *third = JardMeetingBrowserTitleCall(@"Meet - qqq-qqqq-qqq", arc);
    JardCall *unresolved = JardMeetingUnnamedBrowser(arc, @"Arc");
    JardMeetingPresence *watch = JardMeetingPresence.new;
    [watch rememberSwitchFrom:first to:next confirmedAt:10 lastSeen:12];
    assert([[[watch callsResolvingSwitch:@[first] now:13] firstObject].identity isEqual:next.identity]);
    assert([[[watch callsResolvingSwitch:@[unresolved] now:14] firstObject].identity isEqual:next.identity]);
    assert([[watch callsResolvingSwitch:@[third] now:15] firstObject] == third);
    assert([[watch callsResolvingSwitch:@[first] now:16] firstObject] == first);
    assert(![watch takePreviousIdentityForCall:next]);

    [watch rememberSwitchFrom:first to:next confirmedAt:20 lastSeen:20];
    first.inputActive = NO;
    assert([[watch callsResolvingSwitch:@[first] now:21] firstObject] == first);
    assert(![watch callsResolvingSwitch:@[] now:23].count);
    first.inputActive = YES;
    assert([[watch callsResolvingSwitch:@[first] now:24] firstObject] == first);

    [watch rememberSwitchFrom:first to:next confirmedAt:30 lastSeen:30];
    for (NSUInteger second = 31; second < 150; ++second)
        assert([[[watch callsResolvingSwitch:@[first] now:second] firstObject].identity isEqual:next.identity]);
    assert([[watch callsResolvingSwitch:@[first] now:150] firstObject] == first);
    puts("Confirmed call-switch context expired after microphone loss, a different code, or two minutes.");
}

static void exerciseOfferLifecycle(void) {
    NSString *chrome = @"com.google.chrome";
    JardCall *first = JardMeetingBrowserTitleCall(@"Meet - abc-defg-hij", chrome);
    JardCall *next = JardMeetingBrowserTitleCall(@"Meet - xyz-wxyz-xyz", chrome);
    JardCall *unnamed = JardMeetingUnnamedBrowser(chrome, @"Chrome");
    TestMeetings *meeting = [[TestMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
        assert(NO);
    } send:nil changed:nil];
    [meeting.policies removeAllObjects];
    NSDate *date = NSDate.date;
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    [meeting observeCalls:@[unnamed] now:now date:date];
    [meeting observeCalls:@[unnamed] now:now + 1 date:date];
    assert(!meeting.offered);
    [meeting observeCalls:@[first] now:now + 2 date:date];
    assert(meeting.offered == first); // The first two seconds are from the same browser input.
    for (NSUInteger second = 3; second < 22; ++second) {
        [meeting observeCalls:@[unnamed] now:now + second date:date];
        assert(meeting.offered == first);
    }
    [meeting observeCalls:@[unnamed] now:now + 22 date:date];
    assert(!meeting.offered);
    [meeting observeCalls:@[first] now:now + 23 date:date];
    assert(!meeting.offered);
    [meeting observeCalls:@[] now:now + 24 date:date];
    [meeting observeCalls:@[first] now:now + 25 date:date];
    assert(!meeting.offered);
    [meeting observeCalls:@[first] now:now + 27 date:date];
    assert(meeting.offered == first);
    [meeting observeCalls:@[next] now:now + 28 date:date];
    assert(meeting.offered == first);
    [meeting observeCalls:@[next] now:now + 30 date:date];
    assert(meeting.offered == next);
    [meeting dismissOffer];
    for (NSUInteger second = 31; second < 65; ++second) {
        [meeting observeCalls:@[second % 2 ? unnamed : next] now:now + second date:date];
        assert(!meeting.offered);
    }
    [meeting shutdown];

    JardMeetingPresence *watch = JardMeetingPresence.new;
    [watch deferOfferForCall:first now:10.5];
    assert(![watch offerFromCalls:@[unnamed] now:11]);
    assert(![watch offerFromCalls:@[next] now:12]);
    assert([watch offerFromCalls:@[next] now:13] == next);
    watch = JardMeetingPresence.new;
    [watch snooze:unnamed absence:60 now:20];
    assert(![watch offerFromCalls:@[first] now:21]);
    assert(![watch offerFromCalls:@[first] now:23]);

    watch = JardMeetingPresence.new;
    assert(![watch offerFromCalls:@[unnamed] now:30]);
    assert(![watch offerFromCalls:@[unnamed] now:31]);
    assert(![watch offerFromCalls:@[first] now:34]);
    assert([watch offerFromCalls:@[first] now:36] == first);
    puts("Meeting offers kept microphone continuity across tabs and rearmed after unanswered pre-join released the mic.");
}

static void exerciseProviderSettings(void) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSString *key = @"JardMeetingProviderPolicies";
    NSString *defaultKey = @"JardMeetingDefaultPolicy";
    NSString *recentKey = @"JardMeetingRecentCallSources";
    id previous = [defaults objectForKey:key];
    id previousDefault = [defaults objectForKey:defaultKey];
    id previousRecent = [defaults objectForKey:recentKey];
    [defaults removeObjectForKey:defaultKey];
    [defaults removeObjectForKey:recentKey];
    [defaults setObject:@{
        @"site:zoom.us": @{@"provider": @"site:zoom.us", @"label": @"Zoom", @"policy": @"never"},
        @"browser:com.google.chrome": @{@"provider": @"browser:com.google.chrome", @"label": @"Google Chrome", @"policy": @"never"},
        @"app:webex": @[@"invalid"],
    } forKey:key];
    __block NSUInteger automaticStarts = 0;
    TestMeetings *meeting = [[TestMeetings alloc] initWithManaged:NO
        start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
            assert(!prompt && [options[@"meetingLabel"] isEqual:@"Zoom"]);
            automaticStarts++;
            reply(@{}, nil);
        } send:nil changed:nil];
    NSArray *rows = [meeting status][@"providerPolicies"];
    assert(rows.count == 20);
    NSDictionary *(^row)(NSArray *, NSString *) = ^NSDictionary *(NSArray *sources, NSString *provider) {
        for (NSDictionary *source in sources) if ([source[@"provider"] isEqual:provider]) return source;
        return nil;
    };
    assert([[meeting status][@"defaultPolicy"] isEqual:@"ask"]);
    assert([row(rows, @"app:zoom")[@"policy"] isEqual:@"never"] && [row(rows, @"app:zoom")[@"inheritsPolicy"] boolValue]);
    assert([row(rows, @"site:meet.google.com")[@"policy"] isEqual:@"ask"]);
    NSString *chrome = @"com.google.chrome";
    JardCall *zoom = JardMeetingDirectCall(@"us.zoom.xos");
    JardCall *webZoom = JardMeetingBrowserCall([NSURL URLWithString:@"https://zoom.us/j/123"], chrome);
    JardCall *zoomAlias = JardMeetingBrowserCall([NSURL URLWithString:@"https://zoomgov.com/j/123"], @"com.apple.safari");
    JardCall *unnamed = JardMeetingUnnamedBrowser(chrome, @"Google Chrome");
    NSDate *date = NSDate.date;
    JardCalendarMeeting *invite = JardCalendarOccurrence(@"zoom", @"Design sync", @"work", date,
        [date dateByAddingTimeInterval:1800], NO, @[], @[@"https://zoom.us/j/123"]);
    assert(invite.link);
    void (^save)(NSString *, NSString *) = ^(NSString *provider, NSString *policy) {
        [meeting request:@{@"action": @"setPolicy", @"provider": provider, @"policy": policy}
            reply:^(id result, NSString *error) { assert(result && !error); }];
    };
    void (^setDefault)(NSString *) = ^(NSString *policy) {
        [meeting request:@{@"action": @"setDefaultPolicy", @"policy": policy}
            reply:^(id result, NSString *error) { assert(!error && [result[@"defaultPolicy"] isEqual:policy]); }];
    };
    JardCall *unnamedSafari = JardMeetingUnnamedBrowser(@"com.apple.safari", @"Safari");
    JardCall *slack = JardMeetingDirectCall(@"com.tinyspeck.slackmacgap");
    assert(slack && [[meeting policyForCall:slack] isEqual:@"ask"]);
    setDefault(@"auto");
    assert(automaticStarts == 0 && [[meeting policyForCall:slack] isEqual:@"auto"]);
    assert([[meeting policyForCall:unnamedSafari] isEqual:@"ask"]);
    assert([[meeting policyForCall:unnamed] isEqual:@"never"]);
    save(slack.provider, @"ask");
    assert([[meeting policyForCall:slack] isEqual:@"ask"]);
    save(slack.provider, @"inherit");
    assert([[meeting policyForCall:slack] isEqual:@"auto"]);
    setDefault(@"never");
    assert([[meeting policyForCall:unnamedSafari] isEqual:@"never"]);
    setDefault(@"auto");
    save(zoom.provider, @"auto");
    assert(automaticStarts == 0 && [[meeting policyForCall:zoomAlias] isEqual:@"auto"]);
    assert([[meeting policyForCall:webZoom] isEqual:@"never"]);
    assert([[meeting policyForCalendar:invite] isEqual:@"never"]);
    save(webZoom.provider, @"ask");
    assert([[meeting policyForCall:webZoom] isEqual:@"ask"] && [[meeting policyForCall:unnamed] isEqual:@"never"]);
    assert([[meeting policyForCalendar:invite] isEqual:@"ask"]);
    for (NSDictionary *change in @[
        @{@"provider": unnamed.provider, @"policy": @"auto"},
        @{@"provider": @"site:unrecognized", @"policy": @"never"},
        @{@"provider": zoom.provider, @"policy": @"unexpected"},
    ]) {
        NSMutableDictionary *request = [change mutableCopy];
        request[@"action"] = @"setPolicy";
        [meeting request:request reply:^(id result, NSString *error) { assert(!result && error); }];
    }
    [meeting observeCalls:@[webZoom] now:30 date:date];
    [meeting observeCalls:@[webZoom] now:32 date:date];
    assert(automaticStarts == 0 && [[[meeting status] objectForKey:@"phase"] isEqual:@"offered"]);
    rows = [meeting status][@"providerPolicies"];
    assert([rows.firstObject[@"provider"] isEqual:webZoom.provider] && [rows.firstObject[@"lastSeen"] doubleValue] > 0);
    assert(![rows.firstObject[@"inheritsPolicy"] boolValue]);
    save(webZoom.provider, @"never");
    assert([[[meeting status] objectForKey:@"phase"] isEqual:@"idle"]);
    [meeting observeCalls:@[zoom] now:35 date:date];
    [meeting observeCalls:@[zoom] now:37 date:date];
    assert(automaticStarts == 1);
    [meeting shutdown];
    TestMeetings *reopened = [[TestMeetings alloc] initWithManaged:NO start:nil send:nil changed:nil];
    assert([[reopened policyForCall:zoom] isEqual:@"auto"] && [[reopened policyForCall:webZoom] isEqual:@"never"]);
    assert([[reopened status][@"defaultPolicy"] isEqual:@"auto"]);
    assert([row([reopened status][@"providerPolicies"], webZoom.provider)[@"lastSeen"] doubleValue] > 0);
    for (NSTimeInterval sample = 100; sample <= 120; sample += 2) {
        [reopened observeCalls:@[unnamedSafari] now:sample date:[date dateByAddingTimeInterval:sample]];
    }
    assert([reopened.offered.provider isEqual:unnamedSafari.provider]);
    NSArray *recentRows = [reopened status][@"providerPolicies"];
    assert([recentRows.firstObject[@"provider"] isEqual:unnamedSafari.provider]);
    assert([recentRows.firstObject[@"policy"] isEqual:@"ask"]);
    assert(![recentRows.firstObject[@"canAlwaysRecord"] boolValue]);
    for (NSUInteger index = 0; index < 80; ++index) {
        NSString *provider = [NSString stringWithFormat:@"site:previous-%lu.test", (unsigned long)index];
        reopened.policies[provider] = @{@"provider": provider, @"label": provider, @"policy": @"ask"};
    }
    [reopened request:@{@"action": @"setPolicy", @"provider": unnamedSafari.provider, @"policy": @"never"}
        reply:^(id result, NSString *error) { assert(!error && [result[@"phase"] isEqual:@"idle"]); }];
    [reopened shutdown];
    TestMeetings *full = [[TestMeetings alloc] initWithManaged:NO start:nil send:nil changed:nil];
    assert([[full policyForCall:unnamedSafari] isEqual:@"never"]);
    assert([full.policies[@"site:previous-79.test"][@"policy"] isEqual:@"ask"]);
    [full shutdown];
    TestMeetings *managed = [[TestMeetings alloc] initWithManaged:YES start:nil send:nil changed:nil];
    [managed request:@{@"action": @"setPolicy", @"provider": zoom.provider, @"policy": @"ask"}
        reply:^(id result, NSString *error) { assert(!result && [error containsString:@"managed"]); }];
    [managed request:@{@"action": @"setDefaultPolicy", @"policy": @"never"}
        reply:^(id result, NSString *error) { assert(!result && [error containsString:@"managed"]); }];
    [managed shutdown];
    assert([[[defaults dictionaryForKey:key][zoom.provider] objectForKey:@"policy"] isEqual:@"auto"]);
    assert([[defaults stringForKey:defaultKey] isEqual:@"auto"]);
    [defaults setObject:previous forKey:key];
    [defaults setObject:previousDefault forKey:defaultKey];
    [defaults setObject:previousRecent forKey:recentKey];
    puts("Provider settings listed built-in and scoped options, retained legacy choices, and applied only authorized recording policies.");
}

int main(void) {
    @autoreleasepool {
        exerciseCalendar();
        exerciseBrowserTitles();
        exerciseSwitchMemory();
        exerciseOfferLifecycle();
        exerciseAutoStop(NO);
        exerciseAutoStop(YES);
        exerciseProviderSettings();
        JardCall *zoom = JardMeetingDirectCall(@"us.zoom.CptHost");
        assert([zoom.identity isEqual:JardMeetingDirectCall(@"us.zoom.xos.helper").identity]);
        assert(!JardMeetingDirectCall(@"org.mozilla.firefox"));
        JardCall *signal = JardMeetingDirectCall(@"org.whispersystems.signal-desktop");
        assert(signal.slow);
        NSString *chrome = @"com.google.chrome";
        JardCall *meet = JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/meet/abc-defg-hij"], chrome);
        JardCall *other = JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/abc-defg-xyz"], chrome);
        assert([meet.label isEqual:@"Google Meet"] && ![meet.identity isEqual:other.identity]);
        assert(!JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com/lookup/name"], chrome));
        assert(!JardMeetingBrowserCall([NSURL URLWithString:@"https://meet.google.com.example.com/abc-defg-hij"], chrome));
        assert(!JardMeetingBrowserCall([NSURL URLWithString:@"http://meet.google.com/abc-defg-hij"], chrome));
        JardCall *teams = JardMeetingBrowserCall([NSURL URLWithString:@"https://teams.cloud.microsoft/meet/123"], chrome);
        assert([teams.provider isEqual:JardMeetingDirectCall(@"com.microsoft.teams2").provider]);
        JardCall *unnamed = JardMeetingUnnamedBrowser(chrome, @"Chrome");
        assert(unnamed.unnamedBrowser && unnamed.slow);

        JardMeetingPresence *watch = JardMeetingPresence.new;
        assert(![watch offerFromCalls:@[zoom] now:10]);
        assert(![watch offerFromCalls:@[zoom] now:11]);
        assert([watch offerFromCalls:@[zoom] now:12] == zoom);
        [watch snooze:zoom absence:60 now:12];
        assert(![watch offerFromCalls:@[] now:35]);
        assert(![watch offerFromCalls:@[zoom] now:36]);
        assert(![watch offerFromCalls:@[zoom] now:37]);
        assert(![watch offerFromCalls:@[] now:96]);
        assert(![watch offerFromCalls:@[zoom] now:98]);
        assert([watch offerFromCalls:@[zoom] now:100] == zoom);
        [watch resetObservations];
        for (NSUInteger now = 120; now < 140; now++) assert(![watch offerFromCalls:@[unnamed] now:now]);
        assert([watch offerFromCalls:@[unnamed] now:140] == unnamed);
        assert(![watch offerFromCalls:@[unnamed] now:151]);

        __block NSDictionary *take;
        __block BOOL refuse = YES;
        __block NSUInteger cutoff = 96000;
        __block BOOL complete = YES;
        __block BOOL settled = NO;
        NSString *saved = NSUUID.UUID.UUIDString.lowercaseString;
        NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
        NSString *previous = [defaults stringForKey:@"JardLastMeetingSession"];
        NSArray *finalTurns = @[
            @{@"speaker": @2, @"start": @0.5, @"end": @1.7, @"text": @"First phrase rewritten."},
            @{@"speaker": @1, @"start": @0.9, @"end": @1.6, @"text": @"Overlapping local note."},
            @{@"speaker": @2, @"start": @2.8, @"end": @3.2, @"text": @"A recovered gap."},
            @{@"speaker": @2, @"start": @4.0, @"end": @4.9, @"text": @"Second phrase rewritten."},
            @{@"speaker": @2, @"start": @6.0, @"end": @7.0, @"text": @"Tail."}
        ];
        TestMeetings *meeting = [[TestMeetings alloc] initWithManaged:NO start:^(NSDictionary *options, BOOL prompt, JardDictationReply reply) {
            assert(prompt);
            assert([options[@"mode"] isEqual:@"both"] && [options[@"live"] boolValue]);
            take = options;
            reply(@{}, refuse ? @"Microphone denied" : nil);
        } send:^(NSString *command, NSDictionary *options, JardDictationReply reply) {
            if ([command isEqual:@"jard.meetingCompletion"]) {
                assert([options[@"session"] isEqual:saved]);
                reply(@{@"status": @"complete", @"hasSpeech": @YES}, nil);
                return;
            }
            if ([command isEqual:@"jard.meetingExcerpt"]) {
                assert([options[@"session"] isEqual:saved]);
                reply(@{@"turns": settled ? finalTurns : @[finalTurns[0]], @"settled": @(settled)}, nil);
                return;
            }
            assert([options[@"meeting"] isEqual:take[@"meeting"]]);
            if ([command isEqual:@"jard.copyMeeting"]) reply(@{@"through": @(cutoff), @"complete": @(complete)}, nil);
            else { assert([command isEqual:@"jard.stopMeeting"]); reply(nil, nil); }
        } changed:nil];
        NSDictionary *start = @{@"action": @"start", @"languages": @[@"en"]};
        [meeting request:start reply:^(id result, NSString *error) { assert(error); }];
        assert(!meeting.busy && [[meeting status][@"phase"] isEqual:@"error"]);
        refuse = NO;
        [meeting suspendOffers:YES];
        [meeting request:start reply:^(id result, NSString *error) { assert(error); }];
        [meeting suspendOffers:NO];

        // Use a recognized call that the real watcher already held for two seconds.
        NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
        [meeting.presence offerFromCalls:@[zoom] now:now - 2];
        [meeting observed:@[zoom]];
        assert([[meeting status][@"phase"] isEqual:@"offered"]);
        [meeting request:start reply:^(id result, NSString *error) { assert(!error && [result[@"busy"] boolValue]); }];
        assert([take[@"meetingApplication"] isEqual:@"us.zoom.cpthost"]);
        NSString *identifier = take[@"meeting"];
        assert([identifier isEqual:identifier.lowercaseString]);
        [meeting handleCapture:@{@"meeting": @"stale", @"event": @"started"}];
        assert(!meeting.recording);
        [meeting handleCapture:@{@"meeting": identifier, @"event": @"started", @"session": saved}];
        assert(meeting.recording);
        [meeting request:start reply:^(id result, NSString *error) { assert(error); }];
        assert(meeting.canPaste);
        [meeting handleCapture:@{@"meeting": identifier, @"event": @"preview", @"through": @96000,
            @"deltaSegments": @[@{@"speaker": @2, @"start": @0.4, @"end": @1.8, @"text": @"First phrase."}]}];
        [meeting handleCapture:@{@"meeting": identifier, @"event": @"preview", @"through": @240000,
            @"deltaSegments": @[@{@"speaker": @2, @"start": @4.1, @"end": @4.8, @"text": @"Second phrase."}]}];
        NSPasteboard *pasteboard = NSPasteboard.pasteboardWithUniqueName;
        [meeting copyNew:YES to:pasteboard reply:^(id result, NSString *error) { assert(result && !error); }];
        assert([[pasteboard stringForType:NSPasteboardTypeString] isEqual:@"Speaker 2: First phrase."]);
        [meeting copyNew:YES to:pasteboard reply:^(id result, NSString *error) { assert(!result && error); }];
        cutoff = 240000;
        [meeting preparePaste:^(NSString *text, void (^didPaste)(BOOL)) {
            assert([text isEqual:@"Speaker 2: Second phrase."]);
            assert([[meeting status][@"copying"] boolValue]);
            didPaste(NO);
        }];
        [meeting preparePaste:^(NSString *text, void (^didPaste)(BOOL)) {
            assert([text isEqual:@"Speaker 2: Second phrase."]);
            didPaste(YES);
        }];
        [meeting copyNew:YES to:pasteboard reply:^(id result, NSString *error) { assert(!result && error); }];
        complete = NO;
        [meeting copyNew:NO to:pasteboard reply:^(id result, NSString *error) { assert([result[@"copyPartial"] boolValue] && !error); }];
        assert([[pasteboard stringForType:NSPasteboardTypeString] isEqual:@"Speaker 2: First phrase. Second phrase."]);
        meeting.lastPresent = now - 4;
        [meeting observed:@[]];
        assert([[meeting status][@"phase"] isEqual:@"callEnded"]);
        [meeting observed:@[zoom]];
        assert([[meeting status][@"phase"] isEqual:@"recording"]);
        [meeting request:@{@"action": @"stop"} reply:^(id result, NSString *error) { assert(!error); }];
        assert([[meeting status][@"phase"] isEqual:@"saving"]);
        [meeting handleCapture:@{@"meeting": identifier, @"event": @"accepted", @"id": saved}];
        assert(!meeting.busy && [[meeting status][@"session"] isEqual:saved] && meeting.canPaste);
        [meeting copyNew:YES to:pasteboard reply:^(id result, NSString *error) { assert(!result && error); }];
        // A draft with nothing new must leave the shortcut ready for final publication.
        assert(meeting.canPaste);
        settled = YES;
        NSString *expected = @"Speaker 1: Overlapping local note.\n\nSpeaker 2: A recovered gap. Tail.";
        [meeting preparePaste:^(NSString *text, void (^didPaste)(BOOL)) {
            assert([text isEqual:expected]);
            didPaste(NO);
        }];
        [meeting copyNew:YES to:pasteboard reply:^(id result, NSString *error) { assert(result && !error); }];
        assert([[pasteboard stringForType:NSPasteboardTypeString] isEqual:expected]);
        assert(!meeting.canPaste);
        [meeting copyNew:YES to:pasteboard reply:^(id result, NSString *error) { assert(!result && error); }];
        [meeting copyNew:NO to:pasteboard reply:^(id result, NSString *error) { assert(result && !error); }];
        assert([[pasteboard stringForType:NSPasteboardTypeString] containsString:@"First phrase rewritten."]);
        [pasteboard releaseGlobally];
        [meeting shutdown];

        NSString *recovered = NSUUID.UUID.UUIDString.lowercaseString;
        TestMeetings *reopened = [[TestMeetings alloc] initWithManaged:NO start:nil
            send:^(NSString *command, NSDictionary *options, JardDictationReply reply) {
                if ([command isEqual:@"jard.meetingDrafts"]) reply(@[], nil);
                else if ([command isEqual:@"jard.latestMeeting"]) reply(@{@"session": recovered, @"recovering": @NO}, nil);
                else {
                    assert([command isEqual:@"jard.meetingExcerpt"] && [options[@"session"] isEqual:recovered]);
                    reply(@{@"turns": @[finalTurns[0]], @"labels": @{@"2": @"Them"}, @"settled": @YES}, nil);
                }
            } changed:nil];
        [reopened workspaceReady];
        assert(reopened.canPaste);
        [reopened preparePaste:^(NSString *text, void (^didPaste)(BOOL)) {
            assert([text isEqual:@"Them: First phrase rewritten."]);
            didPaste(YES);
        }];
        assert(!reopened.canPaste);
        [reopened shutdown];
        if (previous) [defaults setObject:previous forKey:@"JardLastMeetingSession"];
        else [defaults removeObjectForKey:@"JardLastMeetingSession"];
        puts("Calendar, automatic call end, and success-only per-source meeting copy through finalization passed.");
    }
}
