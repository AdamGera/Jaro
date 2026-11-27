#import "../jard_overlay_dock.h"
#import <assert.h>
#import <stdio.h>

int main(void) {
    @autoreleasepool {
        JardOverlayDock *dock = JardOverlayDock.new;
        NSRect visible = NSMakeRect(0, 0, 1440, 875);
        CGDirectDisplayID displayA = 1, displayB = 2;

        __block int hudNotified = 0;
        [dock setObserver:^{ hudNotified++; } forSurface:JardOverlaySurfaceDictationHUD];

        // Meeting capsule at top-center-ish edge; HUD wants top-center.
        NSRect meeting = NSMakeRect(600, 821, 240, 40);
        NSRect hudDesired = NSMakeRect(649, 819, 142, 50);
        [dock claimSurface:JardOverlaySurfaceMeeting frame:meeting edge:JardOverlayEdgeTop displayID:displayA];
        assert(hudNotified == 1);

        NSRect resolved = [dock resolvedFrameForSurface:JardOverlaySurfaceDictationHUD
                                                desired:hudDesired
                                                   edge:JardOverlayEdgeTop
                                              displayID:displayA
                                                visible:visible];
        // HUD stays on top edge and drops 8 pt below the meeting capsule.
        assert(NSMaxY(resolved) == NSMinY(meeting) - 8.0);
        assert(NSMinX(resolved) == NSMinX(hudDesired));

        // Different display never offsets.
        NSRect otherDisplay = [dock resolvedFrameForSurface:JardOverlaySurfaceDictationHUD
                                                    desired:hudDesired
                                                       edge:JardOverlayEdgeTop
                                                  displayID:displayB
                                                    visible:visible];
        assert(NSEqualRects(otherDisplay, hudDesired));

        // Annotate clears meeting; HUD clears Annotate.
        NSRect annotateDesired = NSMakeRect(620, 820, 200, 42);
        NSRect annotateResolved = [dock resolvedFrameForSurface:JardOverlaySurfaceAnnotate
                                                        desired:annotateDesired
                                                           edge:JardOverlayEdgeTop
                                                      displayID:displayA
                                                        visible:visible];
        assert(NSMaxY(annotateResolved) == NSMinY(meeting) - 8.0);
        [dock claimSurface:JardOverlaySurfaceAnnotate frame:annotateResolved edge:JardOverlayEdgeTop displayID:displayA];
        NSRect hudBelowBoth = [dock resolvedFrameForSurface:JardOverlaySurfaceDictationHUD
                                                    desired:hudDesired
                                                       edge:JardOverlayEdgeTop
                                                  displayID:displayA
                                                    visible:visible];
        assert(NSMaxY(hudBelowBoth) == NSMinY(annotateResolved) - 8.0);

        // Releasing higher surfaces lets HUD fall back to desired origin.
        [dock releaseSurface:JardOverlaySurfaceAnnotate];
        [dock releaseSurface:JardOverlaySurfaceMeeting];
        NSRect fallenBack = [dock resolvedFrameForSurface:JardOverlaySurfaceDictationHUD
                                                  desired:hudDesired
                                                     edge:JardOverlayEdgeTop
                                                displayID:displayA
                                                  visible:visible];
        assert(NSEqualRects(fallenBack, hudDesired));

        // Bottom edge stacks upward by 8 pt.
        NSRect bottomMeeting = NSMakeRect(600, 14, 240, 40);
        NSRect bottomHud = NSMakeRect(649, 10, 142, 50);
        [dock claimSurface:JardOverlaySurfaceMeeting frame:bottomMeeting edge:JardOverlayEdgeBottom displayID:displayA];
        NSRect bottomResolved = [dock resolvedFrameForSurface:JardOverlaySurfaceDictationHUD
                                                      desired:bottomHud
                                                         edge:JardOverlayEdgeBottom
                                                    displayID:displayA
                                                      visible:visible];
        assert(NSMinY(bottomResolved) == NSMaxY(bottomMeeting) + 8.0);

        puts("JardOverlayDock geometry, multi-display, stacking, and fallback passed.");
    }
    return 0;
}
