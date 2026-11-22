#import <AppKit/AppKit.h>

NSRect JardAnnotationAppKitRect(CGRect quartz, CGFloat primaryHeight);
NSDictionary *JardAnnotationRoutingOrigin(NSDictionary *snapshot, NSDictionary *selectedApp, pid_t selectedPID,
    NSString *selectedTitle, CGRect captured);
NSPoint JardAnnotationUnitPoint(NSPoint point, NSSize bounds);
NSDictionary *JardAnnotationEdge(NSPoint from, NSPoint to, NSTimeInterval time);
extern const NSTimeInterval JardAnnotationInkHold;
extern const NSTimeInterval JardAnnotationInkLifetime;
CGFloat JardAnnotationInkAlpha(NSTimeInterval age);
void JardRenderAnnotation(NSURL *input, NSURL *output, NSArray<NSDictionary *> *events, void (^completion)(NSError *));
void JardRenderAnnotationInRanges(NSURL *input, NSURL *output, NSArray<NSDictionary *> *events,
    NSArray<NSValue *> *ranges, void (^completion)(NSError *));
NSArray<NSNumber *> *JardAnnotationFrameTimes(NSArray<NSDictionary *> *events, NSTimeInterval duration);
void JardAnnotationMovieHasFrame(NSURL *movie, void (^completion)(BOOL));
void JardWriteAnnotationAssets(NSURL *movie, NSString *source, NSDictionary *origin, NSArray<NSDictionary *> *events,
    NSString *missingVideo, void (^completion)(NSImage *preview, NSError *error));
