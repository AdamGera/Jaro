#import <AppKit/AppKit.h>

NSRect JardAnnotationDragRect(NSPoint start, NSPoint end, NSRect bounds);
CGRect JardAnnotationDisplaySourceRect(NSRect area, NSRect display);
NSRect JardAnnotationControlsRect(NSRect available, NSRect area);

typedef NS_ENUM(NSUInteger, JardRegionChoice) {
    JardRegionCancelled,
    JardRegionArea,
    JardRegionDisplay,
};

@interface JardSelectionPanel : NSPanel
- (void)confirmSelection;
@end

JardSelectionPanel *JardAnnotationRegionPanel(NSScreen *screen, BOOL screenshot, void (^complete)(JardRegionChoice, NSRect));

NSDictionary *JardAnnotationWindowAtPoint(CGPoint point, NSArray<NSDictionary *> *stack, NSDictionary<NSNumber *, NSNumber *> *owners);
JardSelectionPanel *JardAnnotationWindowPanel(NSDictionary<NSNumber *, NSNumber *> *owners, BOOL screenshot,
    void (^complete)(BOOL cancelled, CGWindowID window, pid_t owner, CGPoint pointer));
