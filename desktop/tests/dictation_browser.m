#import "../jard_dictation_browser.h"
#import "../meeting_presence.h"
#import <ApplicationServices/ApplicationServices.h>
#import <CoreAudio/CoreAudio.h>
#import <libproc.h>
#import <objc/runtime.h>
#import <assert.h>

@interface FixtureBrowser : NSObject
@property(nonatomic, copy) NSString *bundleIdentifier;
@property(nonatomic) pid_t processIdentifier;
@property(nonatomic, getter=isTerminated) BOOL terminated;
@property(nonatomic) NSApplicationActivationPolicy activationPolicy;
@property(nonatomic, copy) NSString *localizedName;
@end

@implementation FixtureBrowser
@end

static OSType expectedTab, expectedURL;
static BOOL denied;
static NSUInteger requests;
static NSUInteger permissionRequests;
static NSString *fixtureAddress;
static NSDictionary<NSNumber *, FixtureBrowser *> *registeredApps;
static NSArray<FixtureBrowser *> *runningApps;
static NSString *audioBundle;
static pid_t inputPID, parentPID;
static pid_t expectedBrowserPID = 12345;
static BOOL microphoneActive;
static NSUInteger axReads;
static NSString *axAddress;

static NSRunningApplication *runningAudioBrowser(id receiver, SEL action, pid_t pid) {
    return (NSRunningApplication *)registeredApps[@(pid)];
}

static NSArray *workspaceRunningApps(id receiver, SEL action) { return runningApps; }

int proc_pidinfo(int pid, int flavor, uint64_t argument, void *buffer, int size) {
    assert(flavor == PROC_PIDTBSDINFO && size == sizeof(struct proc_bsdinfo));
    if (pid != inputPID || !parentPID) return 0;
    struct proc_bsdinfo *info = buffer;
    memset(info, 0, sizeof *info);
    info->pbi_ppid = (uint32_t)parentPID;
    return size;
}

OSStatus AudioObjectGetPropertyDataSize(AudioObjectID object, const AudioObjectPropertyAddress *address, UInt32 qualifierSize,
                                        const void *qualifier, UInt32 *bytes) {
    assert(object == kAudioObjectSystemObject && address->mSelector == kAudioHardwarePropertyProcessObjectList);
    *bytes = sizeof(AudioObjectID);
    return noErr;
}

OSStatus AudioObjectGetPropertyData(AudioObjectID object, const AudioObjectPropertyAddress *address, UInt32 qualifierSize,
                                    const void *qualifier, UInt32 *bytes, void *result) {
    const AudioObjectID process = 42;
    if (address->mSelector == kAudioHardwarePropertyProcessObjectList) {
        assert(object == kAudioObjectSystemObject && *bytes == sizeof process);
        *(AudioObjectID *)result = process;
        return noErr;
    }
    assert(object == process);
    switch (address->mSelector) {
        case kAudioProcessPropertyIsRunningInput: *(UInt32 *)result = microphoneActive; return noErr;
        case kAudioProcessPropertyIsRunningOutput: *(UInt32 *)result = 1; return noErr;
        case kAudioProcessPropertyPID: *(pid_t *)result = inputPID; return noErr;
        case kAudioProcessPropertyBundleID: *(CFTypeRef *)result = CFBridgingRetain(audioBundle); return noErr;
        default: return kAudioHardwareUnknownPropertyError;
    }
}

Boolean AXIsProcessTrusted(void) { return true; }
CFTypeID AXUIElementGetTypeID(void) { return CFStringGetTypeID(); }
AXUIElementRef AXUIElementCreateApplication(pid_t pid) {
    assert(pid == expectedBrowserPID);
    axReads++;
    return (AXUIElementRef)CFRetain(CFSTR("browser"));
}
AXError AXUIElementSetMessagingTimeout(AXUIElementRef element, float timeout) {
    assert(CFEqual(element, CFSTR("browser")) && timeout > 0 && timeout <= .025f);
    return kAXErrorSuccess;
}
AXError AXUIElementCopyAttributeValue(AXUIElementRef element, CFStringRef name, CFTypeRef *result) {
    id value = nil;
    if (CFEqual(element, CFSTR("browser"))) {
        if (CFEqual(name, kAXFocusedWindowAttribute) || CFEqual(name, kAXMainWindowAttribute)) value = @"window";
        else if (CFEqual(name, kAXWindowsAttribute)) value = @[@"window"];
    } else if (CFEqual(element, CFSTR("window"))) {
        if (CFEqual(name, kAXRoleAttribute)) value = @"AXWindow";
        else if (CFEqual(name, kAXTitleAttribute)) value = @"Meet - abc-defg-hij - Google Chrome";
        else if (CFEqual(name, kAXChildrenAttribute)) value = @[@"page"];
    } else if (CFEqual(element, CFSTR("page"))) {
        if (CFEqual(name, kAXRoleAttribute)) value = @"AXWebArea";
        else if (CFEqual(name, kAXURLAttribute)) value = axAddress;
    }
    *result = CFBridgingRetain(value);
    return value ? kAXErrorSuccess : kAXErrorNoValue;
}

OSStatus AEDeterminePermissionToAutomateTarget(const AEAddressDesc *target, AEEventClass eventClass, AEEventID eventID,
                                                Boolean askUserIfNeeded) {
    NSAppleEventDescriptor *expected = [NSAppleEventDescriptor descriptorWithProcessIdentifier:12345];
    assert(target->descriptorType == expected.descriptorType);
    assert(eventClass == kAECoreSuite && eventID == kAEGetData && askUserIfNeeded);
    permissionRequests++;
    return denied ? errAEEventNotPermitted : noErr;
}

static NSAppleEventDescriptor *replyToTabRead(NSAppleEventDescriptor *event, SEL action,
        NSAppleEventSendOptions options, NSTimeInterval timeout, NSError **error) {
    requests++;
    assert(timeout == .25);
    assert(options == (NSAppleEventSendWaitForReply | NSAppleEventSendNeverInteract | kAEDoNotPromptForUserConsent));
    assert([event attributeDescriptorForKeyword:keyEventClassAttr].typeCodeValue == kAECoreSuite);
    assert([event attributeDescriptorForKeyword:keyEventIDAttr].typeCodeValue == kAEGetData);
    NSAppleEventDescriptor *target = [event attributeDescriptorForKeyword:keyAddressAttr];
    NSAppleEventDescriptor *expected = [NSAppleEventDescriptor descriptorWithProcessIdentifier:expectedBrowserPID];
    assert(target.descriptorType == expected.descriptorType && [target.data isEqual:expected.data]);

    NSAppleEventDescriptor *url = [[event paramDescriptorForKeyword:keyDirectObject] coerceToDescriptorType:typeAERecord];
    NSAppleEventDescriptor *tab = [[url descriptorForKeyword:keyAEContainer] coerceToDescriptorType:typeAERecord];
    NSAppleEventDescriptor *window = [[tab descriptorForKeyword:keyAEContainer] coerceToDescriptorType:typeAERecord];
    assert([url descriptorForKeyword:keyAEKeyData].typeCodeValue == expectedURL);
    assert([tab descriptorForKeyword:keyAEKeyData].typeCodeValue == expectedTab);
    assert([window descriptorForKeyword:keyAEDesiredClass].typeCodeValue == cWindow);
    assert([window descriptorForKeyword:keyAEKeyForm].enumCodeValue == formAbsolutePosition);
    assert([window descriptorForKeyword:keyAEKeyData].int32Value == 1);

    NSAppleEventDescriptor *reply = NSAppleEventDescriptor.recordDescriptor;
    if (denied) [reply setDescriptor:[NSAppleEventDescriptor descriptorWithInt32:errAEEventNotPermitted] forKeyword:keyErrorNumber];
    else [reply setDescriptor:[NSAppleEventDescriptor descriptorWithString:fixtureAddress] forKeyword:keyDirectObject];
    return reply;
}

static void exerciseMeetingTab(FixtureBrowser *fixture) {
    fixture.bundleIdentifier = @"com.google.Chrome";
    registeredApps = @{@(fixture.processIdentifier): fixture};
    inputPID = fixture.processIdentifier;
    audioBundle = fixture.bundleIdentifier;
    Method find = class_getClassMethod(NSRunningApplication.class, @selector(runningApplicationWithProcessIdentifier:));
    IMP original = method_setImplementation(find, (IMP)runningAudioBrowser);
    NSString *chrome = @"com.google.chrome";
    axAddress = @"https://meet.google.com/abc-defg-hij";
    fixtureAddress = @"https://meet.google.com/xyz-wxyz-xyz";
    denied = NO;
    NSUInteger before = requests;
    assert(JardActiveCalls(nil).count == 0 && JardActiveCalls(@"app:zoom").count == 0);
    assert(requests == before && axReads == 0);
    JardCall *call = JardActiveCalls(chrome).firstObject;
    assert(!call.inputActive && [call.googleMeetCode isEqual:@"xyz-wxyz-xyz"]);
    assert(requests == before + 1 && axReads == 0);

    microphoneActive = YES;
    fixtureAddress = @"https://docs.example/voice-note";
    call = JardActiveCalls(nil).firstObject;
    assert(call.unnamedBrowser && call.inputActive && axReads == 0);
    fixtureAddress = @"about:blank";
    assert(JardActiveCalls(nil).firstObject.unnamedBrowser && axReads == 0);

    denied = YES;
    call = JardActiveCalls(nil).firstObject;
    assert(call.inputActive && [call.googleMeetCode isEqual:@"abc-defg-hij"] && axReads == 1);
    axAddress = @"https://meet.google.com/qqq-qqqq-qqq";
    assert([JardActiveCalls(nil).firstObject.googleMeetCode isEqual:@"qqq-qqqq-qqq"] && axReads == 2);
    axAddress = nil;
    assert([JardActiveCalls(nil).firstObject.googleMeetCode isEqual:@"abc-defg-hij"] && axReads == 3);
    assert(requests == before + 6 && permissionRequests == 2);
    method_setImplementation(find, original);
    puts("Meeting detection preferred the active tab, fell back without asking for Automation, and only checked audio output for the recorded app.");
}

static void exerciseAudioHelpers(FixtureBrowser *chrome) {
    Method find = class_getClassMethod(NSRunningApplication.class, @selector(runningApplicationWithProcessIdentifier:));
    IMP original = method_setImplementation(find, (IMP)runningAudioBrowser);
    Method workspace = class_getInstanceMethod(NSWorkspace.class, @selector(runningApplications));
    IMP originalWorkspace = method_setImplementation(workspace, (IMP)workspaceRunningApps);
    inputPID = 4100;
    audioBundle = @"com.google.Chrome.helper";
    microphoneActive = YES;
    denied = NO;
    expectedTab = 'acTa';
    expectedURL = 'URL ';
    fixtureAddress = @"https://meet.google.com/abc-defg-hij";
    registeredApps = @{@(chrome.processIdentifier): chrome};
    parentPID = chrome.processIdentifier;
    runningApps = @[];
    JardCall *call = JardActiveCalls(nil).firstObject;
    assert([call.application isEqual:@"com.google.chrome"] && [call.googleMeetCode isEqual:@"abc-defg-hij"]);
    parentPID = 0;
    runningApps = @[chrome];
    assert([JardActiveCalls(nil).firstObject.captureApplication isEqual:@"com.google.chrome"]);
    audioBundle = @"com.google.Chrome.canary.helper";
    NSUInteger before = requests;
    assert(!JardActiveCalls(nil).count && requests == before);
    FixtureBrowser *canary = FixtureBrowser.new;
    canary.bundleIdentifier = @"com.google.Chrome.canary";
    canary.processIdentifier = 4002;
    expectedBrowserPID = canary.processIdentifier;
    runningApps = @[chrome, canary];
    assert([JardActiveCalls(nil).firstObject.captureApplication isEqual:@"com.google.chrome.canary"] && requests == before + 1);

    FixtureBrowser *webkit = FixtureBrowser.new;
    webkit.processIdentifier = inputPID;
    webkit.bundleIdentifier = @"com.apple.WebKit.GPU";
    webkit.localizedName = @"Safari Graphics and Media";
    webkit.activationPolicy = NSApplicationActivationPolicyProhibited;
    FixtureBrowser *safari = FixtureBrowser.new;
    safari.processIdentifier = 7000;
    safari.bundleIdentifier = @"com.apple.Safari";
    expectedBrowserPID = safari.processIdentifier;
    expectedTab = 'cTab';
    expectedURL = 'pURL';
    registeredApps = @{@(inputPID): webkit};
    runningApps = @[safari];
    audioBundle = webkit.bundleIdentifier;
    assert([JardActiveCalls(nil).firstObject.captureApplication isEqual:@"com.apple.safari"]);
    webkit.localizedName = @"Mail Graphics and Media";
    before = requests;
    assert(!JardActiveCalls(nil).count && requests == before);

    registeredApps = @{};
    runningApps = @[];
    audioBundle = @"com.apple.avconferenced";
    assert(!JardActiveCalls(nil).count);
    FixtureBrowser *faceTime = FixtureBrowser.new;
    faceTime.processIdentifier = 8000;
    faceTime.bundleIdentifier = @"com.apple.FaceTime";
    runningApps = @[faceTime];
    call = JardActiveCalls(nil).firstObject;
    assert([call.provider isEqual:@"app:facetime"] && [call.captureApplication isEqual:audioBundle]);
    audioBundle = @"us.zoom.ZoomCefHelper";
    call = JardActiveCalls(nil).firstObject;
    assert([call.provider isEqual:@"app:zoom"] && [call.captureApplication isEqual:@"us.zoom.zoomcefhelper"]);
    method_setImplementation(workspace, originalWorkspace);
    method_setImplementation(find, original);
    puts("Call detection resolved orphaned browser helpers, distinguished variants and WebKit clients, and retained native call bundle IDs.");
}

int main(void) {
    @autoreleasepool {
        Method send = class_getInstanceMethod(NSAppleEventDescriptor.class, @selector(sendEventWithOptions:timeout:error:));
        IMP original = method_setImplementation(send, (IMP)replyToTabRead);
        FixtureBrowser *fixture = FixtureBrowser.new;
        fixture.processIdentifier = 12345;
        NSRunningApplication *browser = (NSRunningApplication *)fixture;
        fixtureAddress = @"https://person:token@Docs.Example:443/private?q=keep-local#section";
        fixture.bundleIdentifier = @"com.apple.SafariTechnologyPreview";
        expectedTab = 'cTab';
        expectedURL = 'pURL';
        NSURL *address = JardBrowserTabURL(browser, .25, NULL);
        assert([address.host.lowercaseString isEqual:@"docs.example"]);
        assert([address.path isEqual:@"/private"] && [address.query isEqual:@"q=keep-local"]);
        assert([address.fragment isEqual:@"section"] && !address.user && !address.password);
        fixture.bundleIdentifier = @"com.google.Chrome";
        expectedTab = 'acTa';
        expectedURL = 'URL ';
        assert([JardBrowserTabURL(browser, .25, NULL).host.lowercaseString isEqual:@"docs.example"]);
        fixtureAddress = @"file:///tmp/local.html";
        BOOL readAddress = NO;
        assert(JardBrowserTabURL(browser, .25, &readAddress) == nil && readAddress);
        assert(permissionRequests == 0);
        assert(JardRequestBrowserTabAccess(browser));
        denied = YES;
        assert(JardBrowserTabURL(browser, .25, &readAddress) == nil && !readAddress);
        assert(!JardRequestBrowserTabAccess(browser));
        fixture.terminated = YES;
        assert(JardBrowserTabURL(browser, .25, NULL) == nil);
        fixture.terminated = NO;
        assert(JardBrowserTabURL(browser, 0, NULL) == nil);
        fixture.bundleIdentifier = @"org.mozilla.firefox";
        assert(!JardSupportsBrowserTabAccess(fixture.bundleIdentifier));
        assert(JardBrowserTabURL(browser, .25, NULL) == nil);
        assert(!JardRequestBrowserTabAccess(browser));
        assert(requests == 4 && permissionRequests == 2);
        exerciseMeetingTab(fixture);
        exerciseAudioHelpers(fixture);
        method_setImplementation(send, original);
        puts("Safari and Chromium tab requests were PID-addressed, read-only and nonprompting. HTTP and HTTPS paths, queries and fragments were preserved without credentials.");
    }
    return 0;
}
