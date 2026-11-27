#import "jard_overlay_dock.h"

static const CGFloat jardOverlayGap = 8.0;

typedef struct {
    BOOL active;
    NSRect frame;
    JardOverlayEdge edge;
    CGDirectDisplayID displayID;
} JardOverlayClaim;

CGDirectDisplayID JardDisplayIDForScreen(NSScreen *screen) {
    NSNumber *number = screen.deviceDescription[@"NSScreenNumber"];
    return number ? (CGDirectDisplayID)number.unsignedIntValue : CGMainDisplayID();
}

@interface JardOverlayDock () {
    JardOverlayClaim _claims[JardOverlaySurfaceCount];
}
@property(nonatomic, strong) NSMutableDictionary<NSNumber *, void (^)(void)> *observers;
@end

@implementation JardOverlayDock

+ (instancetype)shared {
    static JardOverlayDock *instance;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ instance = JardOverlayDock.new; });
    return instance;
}

- (instancetype)init {
    self = [super init];
    if (self) _observers = NSMutableDictionary.dictionary;
    return self;
}

- (void)setObserver:(void (^)(void))observer forSurface:(JardOverlaySurface)surface {
    if (surface < 0 || surface >= JardOverlaySurfaceCount) return;
    if (observer) self.observers[@(surface)] = [observer copy];
    else [self.observers removeObjectForKey:@(surface)];
}

- (void)notifyBelow:(JardOverlaySurface)surface {
    for (NSInteger rank = surface + 1; rank < JardOverlaySurfaceCount; rank++) {
        void (^observer)(void) = self.observers[@(rank)];
        if (observer) observer();
    }
}

- (void)claimSurface:(JardOverlaySurface)surface frame:(NSRect)frame edge:(JardOverlayEdge)edge
           displayID:(CGDirectDisplayID)displayID {
    if (surface < 0 || surface >= JardOverlaySurfaceCount) return;
    JardOverlayClaim previous = _claims[surface];
    if (previous.active && NSEqualRects(previous.frame, frame) && previous.edge == edge &&
        previous.displayID == displayID) return;
    _claims[surface] = (JardOverlayClaim){YES, frame, edge, displayID};
    [self notifyBelow:surface];
}

- (void)releaseSurface:(JardOverlaySurface)surface {
    if (surface < 0 || surface >= JardOverlaySurfaceCount || !_claims[surface].active) return;
    _claims[surface].active = NO;
    [self notifyBelow:surface];
}

- (NSRect)resolvedFrameForSurface:(JardOverlaySurface)surface desired:(NSRect)desired
                             edge:(JardOverlayEdge)edge displayID:(CGDirectDisplayID)displayID
                          visible:(NSRect)visible {
    NSRect result = desired;
    for (NSInteger rank = 0; rank < surface && rank < JardOverlaySurfaceCount; rank++) {
        JardOverlayClaim other = _claims[rank];
        if (!other.active || other.displayID != displayID || other.edge != edge) continue;
        BOOL horizontalOverlap = NSMinX(result) < NSMaxX(other.frame) + jardOverlayGap &&
            NSMaxX(result) > NSMinX(other.frame) - jardOverlayGap;
        if (!horizontalOverlap) continue;
        if (edge == JardOverlayEdgeTop) {
            CGFloat clearTop = NSMinY(other.frame) - jardOverlayGap;
            if (NSMaxY(result) > clearTop && NSMinY(result) < NSMaxY(other.frame) + jardOverlayGap) {
                result.origin.y = fmax(NSMinY(visible) + jardOverlayGap, clearTop - NSHeight(result));
            }
        } else {
            CGFloat clearBottom = NSMaxY(other.frame) + jardOverlayGap;
            if (NSMinY(result) < clearBottom && NSMaxY(result) > NSMinY(other.frame) - jardOverlayGap) {
                result.origin.y = fmin(NSMaxY(visible) - NSHeight(result) - jardOverlayGap, clearBottom);
            }
        }
    }
    return result;
}

@end
