#import <AppKit/AppKit.h>

BOOL JardSupportsBrowserTabAccess(NSString *bundleIdentifier);
// readAddress also reports a successful reply containing a non-web address.
NSURL *JardBrowserTabURL(NSRunningApplication *browser, NSTimeInterval timeout, BOOL *readAddress);
NSURL *JardWebContextURL(id value);
BOOL JardRequestBrowserTabAccess(NSRunningApplication *browser);
