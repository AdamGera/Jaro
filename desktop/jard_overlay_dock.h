#import <AppKit/AppKit.h>

typedef NS_ENUM(NSInteger, JardOverlaySurface) {
    JardOverlaySurfaceMeeting = 0,
    JardOverlaySurfaceAnnotate = 1,
    JardOverlaySurfaceDictationHUD = 2,
    JardOverlaySurfaceCount = 3,
};

typedef NS_ENUM(NSInteger, JardOverlayEdge) {
    JardOverlayEdgeTop = 0,
    JardOverlayEdgeBottom = 1,
};

CGDirectDisplayID JardDisplayIDForScreen(NSScreen *screen);

@interface JardOverlayDock : NSObject
+ (instancetype)shared;
- (void)setObserver:(void (^)(void))observer forSurface:(JardOverlaySurface)surface;
- (void)claimSurface:(JardOverlaySurface)surface frame:(NSRect)frame edge:(JardOverlayEdge)edge
           displayID:(CGDirectDisplayID)displayID;
- (void)releaseSurface:(JardOverlaySurface)surface;
- (NSRect)resolvedFrameForSurface:(JardOverlaySurface)surface desired:(NSRect)desired
                             edge:(JardOverlayEdge)edge displayID:(CGDirectDisplayID)displayID
                          visible:(NSRect)visible;
@end
