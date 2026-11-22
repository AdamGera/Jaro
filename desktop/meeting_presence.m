#import "meeting_presence.h"
#import "jard_dictation_browser.h"
#import <AppKit/AppKit.h>
#import <ApplicationServices/ApplicationServices.h>
#import <CoreAudio/CoreAudio.h>
#import <libproc.h>

static NSString *const googleMeet = @"site:meet.google.com";
static const NSTimeInterval switchedCallLifetime = 120;
static const NSTimeInterval switchedCallAbsence = 3;
static const NSTimeInterval observationGrace = 2.5;

@implementation JardCall
- (BOOL)matchesObservation:(JardCall *)call {
    return [self.identity isEqual:call.identity] || ([self.provider isEqual:googleMeet] &&
        [call.provider isEqual:googleMeet] && [self.application isEqual:call.application] &&
        (!self.googleMeetCode || !call.googleMeetCode));
}
- (BOOL)matchesOfferObservation:(JardCall *)call {
    return [self.identity isEqual:call.identity] || ([self.application isEqual:call.application] &&
        (!self.googleMeetCode || !call.googleMeetCode || [self.googleMeetCode isEqual:call.googleMeetCode]));
}
@end

NSArray<NSDictionary<NSString *, NSString *> *> *JardMeetingProviderCatalog(void) {
    static NSArray *catalog;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        catalog = @[
            @{@"provider": @"app:slack", @"label": @"Slack"},
            @{@"provider": @"app:zoom", @"label": @"Zoom"},
            @{@"provider": googleMeet, @"label": @"Google Meet"},
            @{@"provider": @"app:microsoft teams", @"label": @"Microsoft Teams"},
            @{@"provider": @"app:facetime", @"label": @"FaceTime"},
            @{@"provider": @"app:whatsapp", @"label": @"WhatsApp"},
            @{@"provider": @"app:gather", @"label": @"Gather"},
            @{@"provider": @"app:discord", @"label": @"Discord"},
            @{@"provider": @"app:signal", @"label": @"Signal"},
            @{@"provider": @"app:telegram", @"label": @"Telegram"},
            @{@"provider": @"app:wechat", @"label": @"WeChat"},
            @{@"provider": @"app:line", @"label": @"LINE"},
            @{@"provider": @"app:webex", @"label": @"Webex"},
            @{@"provider": @"app:element", @"label": @"Element"},
            @{@"provider": @"app:phone", @"label": @"Phone"},
            @{@"provider": @"site:voice.google.com", @"label": @"Google Voice"},
            @{@"provider": @"site:messenger.com", @"label": @"Messenger"},
            @{@"provider": @"site:web.groupme.com", @"label": @"GroupMe"},
        ];
    });
    return catalog;
}

static NSString *callIdentity(NSString *application, NSString *provider, NSString *extra) {
    return extra.length ? [NSString stringWithFormat:@"%@|%@|%@", application, provider, extra] :
        [NSString stringWithFormat:@"%@|%@", application, provider];
}

static NSString *unnamedBrowserProvider(NSString *application) {
    return [@"browser:" stringByAppendingString:application];
}

static JardCall *makeCall(NSString *provider, NSString *application, NSString *label, NSString *extra, BOOL slow) {
    JardCall *call = JardCall.new;
    call.provider = provider;
    call.application = application;
    call.captureApplication = application;
    call.label = label;
    call.identity = callIdentity(application, provider, extra);
    call.slow = slow;
    call.inputActive = YES;
    return call;
}

JardCall *JardMeetingDirectCall(NSString *bundle) {
    NSDictionary *apps = @{
        @"us.zoom.xos": @"Zoom", @"us.zoom.cpthost": @"Zoom", @"us.zoom.caphost": @"Zoom",
        @"us.zoom.aomhost": @"Zoom", @"us.zoom.zoomcefhelper": @"Zoom", @"us.zoom.zccimeetinghost": @"Zoom",
        @"us.zoom.zoomphone": @"Zoom", @"us.zoom.zoomhybridconf": @"Zoom",
        @"com.microsoft.teams2": @"Microsoft Teams", @"com.microsoft.teams": @"Microsoft Teams",
        @"com.tinyspeck.slackmacgap": @"Slack", @"com.apple.facetime": @"FaceTime",
        @"com.apple.mobilephone": @"Phone", @"com.apple.avconferenced": @"FaceTime",
        @"com.apple.telephonyutilities.callservicesd": @"FaceTime",
        @"net.whatsapp.whatsapp": @"WhatsApp", @"desktop.whatsapp": @"WhatsApp",
        @"com.hnc.discord": @"Discord", @"com.hnc.discordcanary": @"Discord", @"com.hnc.discordptb": @"Discord",
        @"com.gather.gatherv2": @"Gather", @"com.gather.gather": @"Gather",
        @"org.whispersystems.signal-desktop": @"Signal", @"ru.keepcoder.telegram": @"Telegram",
        @"com.tdesktop.telegram": @"Telegram", @"org.telegram.desktop": @"Telegram",
        @"com.tencent.xinwechat": @"WeChat", @"jp.naver.line.mac": @"LINE",
        @"com.webex.meetingmanager": @"Webex", @"com.cisco.webexmeetingsapp": @"Webex",
        @"cisco-systems.spark": @"Webex", @"im.riot.app": @"Element"
    };
    NSString *canonical = bundle.lowercaseString;
    while (canonical.length) {
        NSString *label = apps[canonical];
        if (label) {
            BOOL slow = [@[@"WhatsApp", @"Gather", @"Signal", @"Telegram", @"WeChat", @"LINE", @"Element"] containsObject:label];
            NSString *provider = [@"app:" stringByAppendingString:label.lowercaseString];
            JardCall *call = makeCall(provider, provider, label, @"", slow);
            call.captureApplication = canonical;
            return call;
        }
        NSRange last = [canonical rangeOfString:@"." options:NSBackwardsSearch];
        if (last.location == NSNotFound) break;
        canonical = [canonical substringToIndex:last.location];
    }
    return nil;
}

static BOOL siteMatches(NSString *host, NSString *domain) {
    return [host isEqual:domain] || [host hasSuffix:[@"." stringByAppendingString:domain]];
}

static NSString *googleMeetCode(NSString *value) {
    if (value.length != 12) return nil;
    NSString *code = value.lowercaseString;
    return [code rangeOfString:@"^[a-z]{3}-[a-z]{4}-[a-z]{3}$" options:NSRegularExpressionSearch].location != NSNotFound ? code : nil;
}

static JardCall *googleMeetCall(NSString *application, NSString *code) {
    JardCall *call = makeCall(googleMeet, application, @"Google Meet", code, NO);
    call.googleMeetCode = code;
    return call;
}

JardCall *JardMeetingBrowserCall(NSURL *url, NSString *application) {
    if (![url.scheme.lowercaseString isEqual:@"https"] || !url.host.length) return nil;
    NSString *host = url.host.lowercaseString;
    NSArray *services = @[
        @[@"meet.google.com", @"Google Meet"], @[@"zoom.us", @"Zoom"], @[@"zoom.com", @"Zoom"], @[@"zoomgov.com", @"Zoom"],
        @[@"teams.microsoft.com", @"Microsoft Teams"], @[@"teams.cloud.microsoft", @"Microsoft Teams"], @[@"teams.live.com", @"Microsoft Teams"],
        @[@"meet.jit.si", @"Jitsi Meet"], @[@"webex.com", @"Webex"], @[@"app.slack.com", @"Slack"], @[@"banshee.app", @"Banshee"],
        @[@"facetime.apple.com", @"FaceTime"], @[@"voice.google.com", @"Google Voice"], @[@"discord.com", @"Discord"],
        @[@"web.whatsapp.com", @"WhatsApp"], @[@"app.gather.town", @"Gather"], @[@"app.v2.gather.town", @"Gather"],
        @[@"messenger.com", @"Messenger"], @[@"web.groupme.com", @"GroupMe"]
    ];
    for (NSArray<NSString *> *service in services) {
        if (!siteMatches(host, service[0])) continue;
        NSString *label = service[1];
        if ([label isEqual:@"Slack"] && ![url.path.lowercaseString containsString:@"/huddle"]) return nil;
        if ([label isEqual:@"Google Meet"]) {
            for (NSString *component in url.pathComponents) {
                NSString *code = googleMeetCode(component);
                if (code) return googleMeetCall(application, code);
            }
            return nil;
        }
        NSString *key = [label isEqual:@"Microsoft Teams"] ? @"app:microsoft teams" : [@"site:" stringByAppendingString:service[0]];
        BOOL slow = [@[@"WhatsApp", @"Gather", @"Messenger", @"GroupMe"] containsObject:label];
        return makeCall(key, application, label, @"", slow);
    }
    return nil;
}

JardCall *JardMeetingUnnamedBrowser(NSString *application, NSString *name) {
    JardCall *call = makeCall(unnamedBrowserProvider(application), application, name, @"", YES);
    call.unnamedBrowser = YES;
    return call;
}

static BOOL browser(NSString *bundle) {
    if (JardSupportsBrowserTabAccess(bundle)) return YES;
    NSArray *browsers = @[@"com.apple.safari", @"com.google.chrome", @"org.mozilla.firefox", @"com.microsoft.edgemac",
        @"com.brave.browser", @"company.thebrowser.browser", @"com.vivaldi.vivaldi", @"com.operasoftware.opera",
        @"net.imput.helium", @"com.ardia.browser", @"app.zen-browser.zen"];
    for (NSString *known in browsers) {
        if ([bundle isEqual:known] || [bundle hasPrefix:[known stringByAppendingString:@"."]]) return YES;
    }
    return NO;
}

static BOOL browserHelper(NSString *bundle) {
    return [bundle hasSuffix:@".helper"] || [bundle containsString:@".helper."];
}

JardCall *JardMeetingBrowserTitleCall(NSString *title, NSString *application) {
    if (!browser(application) || ![title isKindOfClass:NSString.class] || title.length > 512) return nil;
    NSCharacterSet *whitespace = NSCharacterSet.whitespaceAndNewlineCharacterSet;
    NSString *page = [title stringByTrimmingCharactersInSet:whitespace];
    for (NSString *suffix in @[@" - Audio playing - Brave", @" - Brave", @" - Brave Browser", @" - Google Chrome",
        @" — Mozilla Firefox", @" - Microsoft Edge", @" - Safari", @" - Arc", @" - Vivaldi", @" - Helium",
        @" - Ardia", @" — Zen Browser", @" — Zen Twilight"]) {
        if (![page hasSuffix:suffix]) continue;
        page = [page substringToIndex:page.length - suffix.length];
        break;
    }
    for (NSString *prefix in @[@"Meet - ", @"Meet – ", @"Meet — "]) {
        if (![page hasPrefix:prefix]) continue;
        NSString *name = [[page substringFromIndex:prefix.length] stringByTrimmingCharactersInSet:whitespace];
        // Keep only a normalized call code. A named tab supplies provider evidence without a call identifier.
        if (name.length) return googleMeetCall(application, googleMeetCode(name));
    }
    return nil;
}

static id ax(AXUIElementRef element, CFStringRef name) {
    CFTypeRef value = NULL;
    if (AXUIElementCopyAttributeValue(element, name, &value) != kAXErrorSuccess) return nil;
    return CFBridgingRelease(value);
}

static JardCall *accessibleBrowserCall(pid_t pid, NSString *application) {
    if (!AXIsProcessTrusted()) return nil;
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    AXUIElementSetMessagingTimeout(app, .025);
    id focused = ax(app, kAXFocusedWindowAttribute);
    id main = ax(app, kAXMainWindowAttribute);
    NSArray *windows = ax(app, kAXWindowsAttribute);
    CFRelease(app);
    NSMutableArray *nodes = NSMutableArray.array;
    if (focused) [nodes addObject:focused];
    if (main && ![nodes containsObject:main]) [nodes addObject:main];
    if ([windows isKindOfClass:NSArray.class]) {
        for (id window in windows) {
            if (nodes.count >= 5) break;
            if (![nodes containsObject:window]) [nodes addObject:window];
        }
    }
    NSUInteger rootCount = nodes.count;
    JardCall *titleCall = nil;
    NSTimeInterval limit = NSProcessInfo.processInfo.systemUptime + .14;
    const NSUInteger nodeLimit = 256;
    for (NSUInteger i = 0; i < nodes.count && NSProcessInfo.processInfo.systemUptime < limit; ++i) {
        id node = nodes[i];
        if (CFGetTypeID((__bridge CFTypeRef)node) != AXUIElementGetTypeID()) continue;
        AXUIElementRef element = (__bridge AXUIElementRef)node;
        id role = ax(element, kAXRoleAttribute);
        BOOL webArea = [role isEqual:@"AXWebArea"];
        if (webArea) {
            id raw = ax(element, kAXURLAttribute);
            NSURL *url = [raw isKindOfClass:NSURL.class] ? raw :
                [raw isKindOfClass:NSString.class] ? [NSURL URLWithString:raw] : nil;
            JardCall *call = url ? JardMeetingBrowserCall(url, application) : nil;
            if (call) return call;
        }
        if (!titleCall && (i < rootCount || webArea)) {
            titleCall = JardMeetingBrowserTitleCall(ax(element, kAXTitleAttribute), application);
        }
        if (!webArea) {
            NSArray *children = ax(element, kAXChildrenAttribute);
            if ([children isKindOfClass:NSArray.class]) {
                for (id child in children) {
                    if (nodes.count >= nodeLimit) break;
                    if (![nodes containsObject:child]) [nodes addObject:child];
                }
            }
        }
    }
    return titleCall;
}

static JardCall *browserCallForApplication(NSRunningApplication *application) {
    NSString *bundle = application.bundleIdentifier.lowercaseString;
    if (!browser(bundle) || application.terminated || application.processIdentifier <= 0) return nil;
    BOOL readAddress;
    NSURL *address = JardBrowserTabURL(application, .25, &readAddress);
    // A readable active tab decides the call, even if another open window is still on a call page.
    if (readAddress) return address ? JardMeetingBrowserCall(address, bundle) : nil;
    return accessibleBrowserCall(application.processIdentifier, bundle);
}

static OSStatus audioProperty(AudioObjectID object, AudioObjectPropertySelector selector, UInt32 *bytes, void *value) {
    AudioObjectPropertyAddress address = {selector, kAudioObjectPropertyScopeGlobal, kAudioObjectPropertyElementMain};
    return AudioObjectGetPropertyData(object, &address, 0, NULL, bytes, value);
}

static pid_t parentOf(pid_t pid) {
    struct proc_bsdinfo info;
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof info) != sizeof info) return 0;
    pid_t parent = (pid_t)info.pbi_ppid;
    return parent == pid ? 0 : parent;
}

static NSString *orphanBrowserBundle(NSString *reported, NSString *name) {
    NSString *bundle = reported.lowercaseString;
    if ([bundle isEqual:@"com.apple.webkit.gpu"]) {
        if ([name.lowercaseString hasPrefix:@"safari technology preview"]) return @"com.apple.safaritechnologypreview";
        return [name.lowercaseString hasPrefix:@"safari"] ? @"com.apple.safari" : nil;
    }
    if ([bundle isEqual:@"org.mozilla.plugincontainer"]) return @"org.mozilla.firefox";
    if ([bundle isEqual:@"app.zen-browser.plugincontainer"]) return @"app.zen-browser.zen";
    return browser(bundle) ? bundle : nil;
}

static NSRunningApplication *runningBrowserForHelper(NSString *bundle, NSArray<NSRunningApplication *> *applications) {
    NSString *scriptable = bundle;
    while (scriptable.length && !JardSupportsBrowserTabAccess(scriptable)) {
        NSRange separator = [scriptable rangeOfString:@"." options:NSBackwardsSearch];
        scriptable = separator.location == NSNotFound ? nil : [scriptable substringToIndex:separator.location];
    }
    NSRunningApplication *matched = nil;
    NSUInteger length = 0;
    for (NSRunningApplication *app in applications) {
        if (app.terminated || app.activationPolicy != NSApplicationActivationPolicyRegular) continue;
        NSString *candidate = app.bundleIdentifier.lowercaseString;
        if (!browser(candidate) || browserHelper(candidate) || candidate.length <= length ||
            (scriptable && ![candidate isEqual:scriptable])) continue;
        if ([bundle isEqual:candidate] || [bundle hasPrefix:[candidate stringByAppendingString:@"."]]) {
            matched = app;
            length = candidate.length;
        }
    }
    return matched;
}

static JardCall *browserObservation(NSRunningApplication *app, NSMutableDictionary<NSNumber *, JardCall *> *cache) {
    NSNumber *key = @(app.processIdentifier);
    JardCall *call = cache[key];
    if (call) return call;
    call = browserCallForApplication(app) ?: JardMeetingUnnamedBrowser(app.bundleIdentifier.lowercaseString, app.localizedName ?: @"Browser");
    cache[key] = call;
    return call;
}

NSArray<JardCall *> *JardActiveCalls(NSString *outputApplication) {
    AudioObjectPropertyAddress address = {kAudioHardwarePropertyProcessObjectList, kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain};
    UInt32 bytes = 0;
    AudioObjectID system = kAudioObjectSystemObject;
    if (AudioObjectGetPropertyDataSize(system, &address, 0, NULL, &bytes) != noErr || bytes > 65536) return nil;
    if (!bytes) return @[];
    NSMutableData *objects = [NSMutableData dataWithLength:bytes];
    if (audioProperty(system, kAudioHardwarePropertyProcessObjectList, &bytes, objects.mutableBytes) != noErr) return nil;
    const AudioObjectID *ids = objects.bytes;
    NSMutableDictionary<NSString *, JardCall *> *found = NSMutableDictionary.dictionary;
    NSMutableDictionary<NSNumber *, JardCall *> *browserCalls = NSMutableDictionary.dictionary;
    NSArray<NSRunningApplication *> *runningApplications = nil;
    for (UInt32 i = 0; i < bytes / sizeof *ids; ++i) {
        UInt32 running = 0, size = sizeof running;
        BOOL input = audioProperty(ids[i], kAudioProcessPropertyIsRunningInput, &size, &running) == noErr && running;
        if (!input) {
            running = 0;
            size = sizeof running;
            if (!outputApplication || audioProperty(ids[i], kAudioProcessPropertyIsRunningOutput, &size, &running) != noErr || !running) continue;
        }
        pid_t pid = 0;
        size = sizeof pid;
        if (audioProperty(ids[i], kAudioProcessPropertyPID, &size, &pid) != noErr || pid <= 1 || pid == getpid()) continue;
        CFStringRef idValue = NULL;
        size = sizeof idValue;
        if (audioProperty(ids[i], kAudioProcessPropertyBundleID, &size, &idValue) != noErr) idValue = NULL;
        NSString *reported = CFBridgingRelease(idValue);
        JardCall *call = nil;
        NSString *processName = nil, *processBundle = nil;
        for (pid_t host = pid, depth = 0; host > 1 && depth < 6; ++depth, host = parentOf(host)) {
            NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:host];
            NSString *bundle = app.bundleIdentifier.lowercaseString;
            if (depth == 0) {
                processName = app.localizedName;
                processBundle = bundle;
            }
            call = JardMeetingDirectCall(bundle);
            if (call) break;
            if (!browser(bundle) || browserHelper(bundle) || app.terminated || app.activationPolicy != NSApplicationActivationPolicyRegular) continue;
            if (!input && ![bundle isEqual:outputApplication]) break;
            call = browserObservation(app, browserCalls);
            break;
        }
        if (!call && reported) call = JardMeetingDirectCall(reported);
        if (!call) {
            NSString *helper = orphanBrowserBundle(reported, processName) ?: orphanBrowserBundle(processBundle, processName);
            if (helper) {
                if (!runningApplications) runningApplications = NSWorkspace.sharedWorkspace.runningApplications;
                NSRunningApplication *app = runningBrowserForHelper(helper, runningApplications);
                if (app && (input || [app.bundleIdentifier.lowercaseString isEqual:outputApplication]))
                    call = browserObservation(app, browserCalls);
            }
        }
        if ([@[@"com.apple.avconferenced", @"com.apple.telephonyutilities.callservicesd"] containsObject:call.captureApplication ?: @""]) {
            if (!runningApplications) runningApplications = NSWorkspace.sharedWorkspace.runningApplications;
            BOOL faceTimeRunning = [runningApplications indexOfObjectPassingTest:^BOOL(NSRunningApplication *app, NSUInteger index, BOOL *stop) {
                return !app.terminated && [app.bundleIdentifier.lowercaseString isEqual:@"com.apple.facetime"];
            }] != NSNotFound;
            if (!faceTimeRunning) continue;
        }
        if (call && (input || [call.application isEqual:outputApplication])) {
            call.inputActive = input || found[call.identity].inputActive;
            found[call.identity] = call;
        }
    }
    return [found.allValues sortedArrayUsingComparator:^NSComparisonResult(JardCall *a, JardCall *b) {
        return [a.identity compare:b.identity];
    }];
}

@interface JardMeetingPresence ()
@property(nonatomic, strong) NSMutableDictionary<NSString *, NSNumber *> *lastSeen, *start, *absence;
@property(nonatomic, strong) NSMutableDictionary<NSString *, JardCall *> *snoozedCalls;
@property(nonatomic, strong) JardCall *switchedFrom, *switchedTo;
@property(nonatomic) NSTimeInterval switchExpires, switchLastSeen;
@property(nonatomic) NSTimeInterval lastPoll;
@end

@implementation JardMeetingPresence
- (instancetype)init {
    if (!(self = [super init])) return nil;
    _lastSeen = NSMutableDictionary.dictionary;
    _start = NSMutableDictionary.dictionary;
    _absence = NSMutableDictionary.dictionary;
    _snoozedCalls = NSMutableDictionary.dictionary;
    return self;
}
- (void)resetObservations {
    [self.start removeAllObjects];
    self.lastPoll = 0;
}
- (void)rememberSwitchFrom:(JardCall *)previous to:(JardCall *)next confirmedAt:(NSTimeInterval)confirmed lastSeen:(NSTimeInterval)lastSeen {
    self.switchedFrom = previous;
    self.switchedTo = next;
    self.switchExpires = confirmed + switchedCallLifetime;
    self.switchLastSeen = lastSeen;
    [self.start removeObjectForKey:next.identity];
}
- (void)clearRememberedSwitch {
    self.switchedFrom = self.switchedTo = nil;
}
- (NSString *)takePreviousIdentityForCall:(JardCall *)call {
    NSString *previous = [self.switchedTo.identity isEqual:call.identity] ? self.switchedFrom.identity : nil;
    [self clearRememberedSwitch];
    return previous;
}
- (NSArray<JardCall *> *)callsResolvingSwitch:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now {
    if (!self.switchedTo) return calls;
    if (now >= self.switchExpires || now - self.switchLastSeen >= switchedCallAbsence) {
        [self clearRememberedSwitch];
        return calls;
    }
    for (NSUInteger i = 0; i < calls.count; ++i) {
        JardCall *call = calls[i];
        if (!call.inputActive || ![call.application isEqual:self.switchedTo.application]) continue;
        self.switchLastSeen = now;
        if ([call.identity isEqual:self.switchedTo.identity]) return calls;
        if (call.unnamedBrowser || [self.switchedFrom matchesObservation:call]) {
            // Keep the old call snoozed while the browser still reports its page or no call address.
            NSString *previous = self.switchedFrom.identity;
            if (self.absence[previous]) self.lastSeen[previous] = @(now);
            NSMutableArray<JardCall *> *resolved = calls.mutableCopy;
            resolved[i] = googleMeetCall(self.switchedTo.application, self.switchedTo.googleMeetCode);
            return resolved;
        }
        [self clearRememberedSwitch];
        break;
    }
    return calls;
}
- (void)snooze:(JardCall *)call absence:(NSTimeInterval)seconds now:(NSTimeInterval)now {
    self.absence[call.identity] = @(seconds);
    self.lastSeen[call.identity] = @(now);
    self.snoozedCalls[call.identity] = call;
}
- (void)deferOfferForCall:(JardCall *)call now:(NSTimeInterval)now {
    self.absence[call.identity] = @3;
    self.lastSeen[call.identity] = @(now);
    [self.snoozedCalls removeObjectForKey:call.identity];
}
- (JardCall *)offerFromCalls:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now {
    if (self.lastPoll && now - self.lastPoll > 10) [self.start removeAllObjects];
    self.lastPoll = now;
    NSMutableSet<NSString *> *microphoneApps = NSMutableSet.set;
    for (JardCall *call in calls) if (call.inputActive) [microphoneApps addObject:call.application];
    for (NSString *key in self.lastSeen.allKeys) {
        NSNumber *absence = self.absence[key];
        BOOL unanswered = absence && absence.doubleValue == 0;
        BOOL expired = unanswered ? ![microphoneApps containsObject:self.snoozedCalls[key].application] :
            now - self.lastSeen[key].doubleValue >= (absence ? absence.doubleValue : observationGrace);
        if (expired) {
            [self.start removeObjectForKey:key];
            [self.lastSeen removeObjectForKey:key];
            [self.absence removeObjectForKey:key];
            [self.snoozedCalls removeObjectForKey:key];
        }
    }
    JardCall *offer = nil;
    for (JardCall *call in calls) {
        if (!call.inputActive) continue;
        NSString *key = call.identity;
        NSTimeInterval previous = self.lastSeen[key].doubleValue;
        self.lastSeen[key] = @(now);
        BOOL snoozed = self.absence[key] != nil;
        for (NSString *snoozedKey in self.snoozedCalls) {
            if (![self.snoozedCalls[snoozedKey] matchesOfferObservation:call]) continue;
            self.lastSeen[snoozedKey] = @(now);
            snoozed = YES;
        }
        if (snoozed) {
            [self.start removeObjectForKey:key];
            continue;
        }
        if (!self.start[key] || now - previous > observationGrace) {
            NSString *unresolved = callIdentity(call.application, unnamedBrowserProvider(call.application), nil);
            NSNumber *start = !call.unnamedBrowser && now - self.lastSeen[unresolved].doubleValue < observationGrace ?
                self.start[unresolved] : nil;
            self.start[key] = start ?: @(now);
            if (start) [self.start removeObjectForKey:unresolved];
        }
        if (!offer && now - self.start[key].doubleValue >= (call.slow ? 20 : 2)) offer = call;
    }
    return offer;
}
@end
