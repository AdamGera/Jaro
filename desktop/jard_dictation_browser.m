#import "jard_dictation_browser.h"
#import <ApplicationServices/ApplicationServices.h>

static OSType currentTabProperty(NSString *identifier) {
    static NSSet<NSString *> *safari, *chromium;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        safari = [NSSet setWithArray:@[@"com.apple.safari", @"com.apple.safaritechnologypreview", @"com.kagi.kagimacos"]];
        chromium = [NSSet setWithArray:@[@"com.brave.browser", @"com.brave.browser.beta", @"com.brave.browser.nightly",
            @"com.google.chrome", @"com.google.chrome.beta", @"com.google.chrome.canary", @"com.google.chrome.dev",
            @"com.microsoft.edgemac", @"com.operasoftware.opera", @"com.vivaldi.vivaldi",
            @"company.thebrowser.browser", @"net.imput.helium", @"org.chromium.chromium"]];
    });
    NSString *bundle = identifier.lowercaseString ?: @"";
    if ([safari containsObject:bundle]) return 'cTab';
    if ([chromium containsObject:bundle]) return 'acTa';
    return 0;
}

BOOL JardSupportsBrowserTabAccess(NSString *bundleIdentifier) {
    return currentTabProperty(bundleIdentifier) != 0;
}

static NSAppleEventDescriptor *specifier(OSType kind, NSAppleEventDescriptor *container, OSType form, NSAppleEventDescriptor *key) {
    if (!container || !key) return nil;
    AEDesc object = {typeNull, NULL};
    AEDesc root = *container.aeDesc, selector = *key.aeDesc;
    if (CreateObjSpecifier(kind, &root, form, &selector, false, &object) != noErr) return nil;
    return [[NSAppleEventDescriptor alloc] initWithAEDescNoCopy:&object];
}

NSURL *JardWebContextURL(id value) {
    NSString *address = [value isKindOfClass:NSURL.class] ? [value absoluteString] :
        [value isKindOfClass:NSString.class] ? value : nil;
    NSURLComponents *url = address.length ? [NSURLComponents componentsWithString:address] : nil;
    NSString *scheme = url.scheme.lowercaseString;
    if (![scheme isEqual:@"https"] && ![scheme isEqual:@"http"]) return nil;
    if (!url.host.length) return nil;
    url.user = nil;
    url.password = nil;
    return url.URL;
}

NSURL *JardBrowserTabURL(NSRunningApplication *browser, NSTimeInterval timeout, BOOL *readAddress) {
    if (readAddress) *readAddress = NO;
    OSType tabKey = currentTabProperty(browser.bundleIdentifier);
    if (!tabKey || browser.terminated || browser.processIdentifier <= 0 || timeout <= 0) return nil;
    NSAppleEventDescriptor *window = specifier(cWindow, NSAppleEventDescriptor.nullDescriptor,
        formAbsolutePosition, [NSAppleEventDescriptor descriptorWithInt32:1]);
    NSAppleEventDescriptor *tab = specifier(typeProperty, window, formPropertyID, [NSAppleEventDescriptor descriptorWithTypeCode:tabKey]);
    OSType urlKey = tabKey == 'cTab' ? 'pURL' : 'URL ';
    NSAppleEventDescriptor *address = specifier(typeProperty, tab, formPropertyID, [NSAppleEventDescriptor descriptorWithTypeCode:urlKey]);
    NSAppleEventDescriptor *target = [NSAppleEventDescriptor descriptorWithProcessIdentifier:browser.processIdentifier];
    if (!address || !target) return nil;
    NSAppleEventDescriptor *event = [NSAppleEventDescriptor appleEventWithEventClass:kAECoreSuite eventID:kAEGetData
        targetDescriptor:target returnID:kAutoGenerateReturnID transactionID:kAnyTransactionID];
    [event setParamDescriptor:address forKeyword:keyDirectObject];
    NSAppleEventDescriptor *reply = [event sendEventWithOptions:NSAppleEventSendWaitForReply |
        NSAppleEventSendNeverInteract | kAEDoNotPromptForUserConsent timeout:timeout error:nil];
    if (!reply || [reply paramDescriptorForKeyword:keyErrorNumber].int32Value) return nil;
    NSString *value = [reply paramDescriptorForKeyword:keyDirectObject].stringValue;
    if (readAddress) *readAddress = value.length > 0;
    return JardWebContextURL(value);
}

BOOL JardRequestBrowserTabAccess(NSRunningApplication *browser) {
    if (!JardSupportsBrowserTabAccess(browser.bundleIdentifier) || browser.terminated || browser.processIdentifier <= 0) return NO;
    NSAppleEventDescriptor *target = [NSAppleEventDescriptor descriptorWithProcessIdentifier:browser.processIdentifier];
    return target && AEDeterminePermissionToAutomateTarget(target.aeDesc, kAECoreSuite, kAEGetData, true) == noErr;
}
