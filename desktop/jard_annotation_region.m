#import "jard_annotation_region.h"
#import "jard_annotation_ink.h"
#import "jard_interface_language.h"
#import <Carbon/Carbon.h>

static const CGFloat JardMinimumRegion = 36;
static const CGFloat JardClickTolerance = 3;

NSRect JardAnnotationDragRect(NSPoint start, NSPoint end, NSRect bounds) {
    NSRect drag = NSMakeRect(MIN(start.x, end.x), MIN(start.y, end.y), fabs(end.x - start.x), fabs(end.y - start.y));
    return NSIntersectionRect(NSIntegralRect(drag), bounds);
}

CGRect JardAnnotationDisplaySourceRect(NSRect area, NSRect display) {
    NSRect clipped = NSIntersectionRect(area, display);
    if (NSIsEmptyRect(clipped)) return CGRectZero;
    return CGRectMake(NSMinX(clipped) - NSMinX(display), NSMaxY(display) - NSMaxY(clipped),
                      NSWidth(clipped), NSHeight(clipped));
}

NSRect JardAnnotationControlsRect(NSRect available, NSRect area) {
    const CGFloat width = 332, height = 46;
    const CGFloat horizontalInset = MAX(0, MIN(24, (NSWidth(available) - width) / 2));
    const CGFloat verticalInset = MAX(0, MIN(24, (NSHeight(available) - height) / 2));
    const CGFloat x[] = {NSMidX(available) - width / 2, NSMinX(available) + horizontalInset,
                         NSMaxX(available) - width - horizontalInset};
    const CGFloat y[] = {NSMinY(available) + verticalInset, NSMaxY(available) - height - verticalInset};
    NSRect best = NSMakeRect(x[0], y[0], width, height);
    CGFloat leastOverlap = width * height;
    for (NSUInteger row = 0; row < 2; row++) {
        for (NSUInteger column = 0; column < 3; column++) {
            NSRect candidate = NSMakeRect(x[column], y[row], width, height);
            NSRect clipped = NSIntersectionRect(candidate, area);
            CGFloat overlap = NSWidth(clipped) * NSHeight(clipped);
            if (overlap == 0) return candidate;
            if (overlap < leastOverlap) { best = candidate; leastOverlap = overlap; }
        }
    }
    return best;
}

@interface JardRegionView : NSView
@property(nonatomic, copy) void (^complete)(JardRegionChoice, NSRect);
@property(nonatomic) NSPoint anchor;
@property(nonatomic) NSRect selection;
@property(nonatomic) BOOL moved, dragging, screenshot;
@property(nonatomic, strong) NSTextField *instruction;
- (void)confirmSelection;
- (NSString *)selectionTitle;
@end

@implementation JardRegionView
- (BOOL)acceptsFirstResponder { return YES; }
- (BOOL)acceptsFirstMouse:(NSEvent *)event { return YES; }
- (void)resetCursorRects { [self addCursorRect:self.bounds cursor:NSCursor.crosshairCursor]; }

- (NSView *)hitTest:(NSPoint)point {
    NSView *hit = [super hitTest:point];
    return hit && hit == self.instruction ? self : hit;
}

- (void)finish:(JardRegionChoice)choice area:(NSRect)area {
    void (^complete)(JardRegionChoice, NSRect) = self.complete;
    self.complete = nil;
    self.dragging = NO;
    if (complete) complete(choice, area);
}

- (void)confirmSelection {
    if (!self.dragging || !self.moved) {
        [self finish:JardRegionDisplay area:self.window.frame];
        return;
    }
    if (NSWidth(self.selection) < JardMinimumRegion || NSHeight(self.selection) < JardMinimumRegion) {
        self.dragging = NO;
        self.selection = NSZeroRect;
        self.instruction.stringValue = JardLocalized(@"Drag an area at least 36 × 36 points");
        self.needsDisplay = YES;
        return;
    }
    NSRect windowRect = [self convertRect:self.selection toView:nil];
    [self finish:JardRegionArea area:[self.window convertRectToScreen:windowRect]];
}

- (void)mouseDown:(NSEvent *)event {
    self.anchor = [self convertPoint:event.locationInWindow fromView:nil];
    self.dragging = YES;
    self.moved = NO;
    self.selection = NSZeroRect;
    self.instruction.stringValue = JardLocalized(self.screenshot ? @"Drag an area or click to capture the display"
        : @"Drag an area or click to record the display");
    self.needsDisplay = YES;
}

- (void)mouseDragged:(NSEvent *)event {
    if (!self.dragging) return;
    NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
    CGFloat distance = hypot(point.x - self.anchor.x, point.y - self.anchor.y);
    self.moved = self.moved || distance >= JardClickTolerance;
    if (self.moved) self.selection = JardAnnotationDragRect(self.anchor, point, self.bounds);
    self.needsDisplay = YES;
}

- (void)mouseUp:(NSEvent *)event {
    if (!self.dragging) return;
    [self mouseDragged:event];
    [self confirmSelection];
}

- (void)cancelOperation:(id)sender {
    [self finish:JardRegionCancelled area:NSZeroRect];
}

- (void)keyDown:(NSEvent *)event {
    if (event.keyCode == kVK_Escape) [self cancelOperation:nil];
    else if (event.keyCode == kVK_Return || event.keyCode == kVK_ANSI_KeypadEnter) {
        if (!event.ARepeat) [self confirmSelection];
    } else [super keyDown:event];
}

- (NSString *)selectionTitle {
    return [NSString localizedStringWithFormat:@"%.0f × %.0f", NSWidth(self.selection), NSHeight(self.selection)];
}

- (void)drawRect:(NSRect)dirtyRect {
    [[NSColor colorWithWhite:0 alpha:.48] setFill];
    NSRectFill(dirtyRect);
    NSRect area = self.selection;
    BOOL cropped = !NSIsEmptyRect(area);
    if (cropped) NSRectFillUsingOperation(area, NSCompositingOperationClear);
    else area = NSInsetRect(self.bounds, 2, 2);
    NSBezierPath *outline = [NSBezierPath bezierPathWithRect:NSInsetRect(area, .5, .5)];
    [[NSColor colorWithWhite:0 alpha:.85] setStroke];
    outline.lineWidth = 3;
    [outline stroke];
    [NSColor.whiteColor setStroke];
    outline.lineWidth = 1;
    [outline stroke];
    if (!cropped) return;

    NSString *title = [self selectionTitle];
    NSMutableParagraphStyle *paragraph = NSMutableParagraphStyle.new;
    paragraph.lineBreakMode = NSLineBreakByTruncatingTail;
    NSDictionary *style = @{NSForegroundColorAttributeName: NSColor.whiteColor, NSParagraphStyleAttributeName: paragraph,
        NSFontAttributeName: [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightMedium]};
    NSSize size = [title sizeWithAttributes:style];
    CGFloat width = MIN(480, MIN(NSWidth(self.bounds) - 12, ceil(size.width) + 14)), height = ceil(size.height) + 8;
    CGFloat x = MIN(MAX(6, NSMidX(area) - width / 2), NSWidth(self.bounds) - width - 6);
    CGFloat y = NSMaxY(area) + 6;
    if (y + height > NSHeight(self.bounds) - 6) y = MAX(6, NSMinY(area) - height - 6);
    [[NSColor colorWithWhite:0 alpha:.83] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(x, y, width, height) xRadius:5 yRadius:5] fill];
    [title drawInRect:NSMakeRect(x + 7, y + 4, width - 14, height - 8) withAttributes:style];
}
@end

@interface JardSelectionPanel ()
@property(nonatomic, strong) NSTimer *tracking;
@end

@implementation JardSelectionPanel
- (BOOL)canBecomeKeyWindow { return YES; }
- (BOOL)canBecomeMainWindow { return NO; }
- (void)confirmSelection { [(JardRegionView *)self.contentView confirmSelection]; }
- (void)orderOut:(id)sender {
    [self.tracking invalidate];
    self.tracking = nil;
    [super orderOut:sender];
}
@end

static JardSelectionPanel *selectionPanel(NSScreen *screen, JardRegionView *view, NSString *label, NSString *description) {
    JardSelectionPanel *panel = [[JardSelectionPanel alloc] initWithContentRect:screen.frame
        styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        backing:NSBackingStoreBuffered defer:NO];
    panel.opaque = NO;
    panel.backgroundColor = NSColor.clearColor;
    panel.hasShadow = NO;
    panel.hidesOnDeactivate = NO;
    panel.level = NSStatusWindowLevel;
    panel.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;
    [panel setAccessibilityLabel:JardLocalized(label)];
    view.frame = panel.contentView.bounds;
    view.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    panel.contentView = view;
    NSTextField *instruction = [NSTextField labelWithString:JardLocalized(description)];
    instruction.alignment = NSTextAlignmentCenter;
    instruction.textColor = NSColor.whiteColor;
    instruction.font = [NSFont systemFontOfSize:15 weight:NSFontWeightSemibold];
    instruction.frame = NSMakeRect(100, NSHeight(view.bounds) - 83, MAX(0, NSWidth(view.bounds) - 200), 26);
    instruction.autoresizingMask = NSViewWidthSizable | NSViewMinYMargin;
    [view addSubview:instruction];
    view.instruction = instruction;
    NSButton *cancel = [NSButton buttonWithTitle:JardLocalized(@"Cancel") target:view action:@selector(cancelOperation:)];
    cancel.frame = NSMakeRect(NSWidth(view.bounds) - 106, NSHeight(view.bounds) - 83, 85, 26);
    cancel.autoresizingMask = NSViewMinXMargin | NSViewMinYMargin;
    [view addSubview:cancel];
    [panel makeFirstResponder:view];
    return panel;
}

JardSelectionPanel *JardAnnotationRegionPanel(NSScreen *screen, BOOL screenshot, void (^complete)(JardRegionChoice, NSRect)) {
    JardRegionView *view = [[JardRegionView alloc] initWithFrame:NSZeroRect];
    view.complete = complete;
    view.screenshot = screenshot;
    return selectionPanel(screen, view, screenshot ? @"Select an area to capture" : @"Select an area to annotate",
        screenshot ? @"Drag an area or click to capture the display" : @"Drag an area or click to record the display");
}

static CGRect windowBounds(NSDictionary *window) {
    NSDictionary *position = window[(__bridge NSString *)kCGWindowBounds];
    CGRect bounds;
    return position && CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)position, &bounds)
        ? bounds : CGRectZero;
}

NSDictionary *JardAnnotationWindowAtPoint(CGPoint point, NSArray<NSDictionary *> *stack, NSDictionary<NSNumber *, NSNumber *> *owners) {
    for (NSDictionary *window in stack) {
        NSNumber *number = window[(__bridge NSString *)kCGWindowNumber];
        NSNumber *pid = window[(__bridge NSString *)kCGWindowOwnerPID];
        NSNumber *alpha = window[(__bridge NSString *)kCGWindowAlpha];
        if (!number || !pid || ![owners[number] isEqual:pid] ||
            [window[(__bridge NSString *)kCGWindowLayer] integerValue] != NSNormalWindowLevel ||
            (alpha && alpha.doubleValue <= 0)) continue;
        CGRect bounds = windowBounds(window);
        if (CGRectGetWidth(bounds) >= 80 && CGRectGetHeight(bounds) >= 80 && CGRectContainsPoint(bounds, point)) return window;
    }
    return nil;
}

static NSScreen *screenAtPointer(NSPoint point) {
    for (NSScreen *screen in NSScreen.screens) if (NSPointInRect(point, screen.frame)) return screen;
    return nil;
}

@interface JardWindowSelectionView : JardRegionView
@property(nonatomic, copy) NSDictionary<NSNumber *, NSNumber *> *owners;
@property(nonatomic, copy) void (^choose)(BOOL, CGWindowID, pid_t, CGPoint);
@property(nonatomic, copy) NSDictionary *hovered;
@end

@implementation JardWindowSelectionView

- (CGPoint)refreshAtPointer:(NSPoint)point {
    CGFloat height = CGRectGetHeight(CGDisplayBounds(CGMainDisplayID()));
    CGPoint quartz = CGPointMake(point.x, height - point.y);
    NSScreen *screen = screenAtPointer(point);
    if (screen && !NSEqualRects(screen.frame, self.window.frame)) [self.window setFrame:screen.frame display:YES];
    NSArray *stack = CFBridgingRelease(CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID));
    NSDictionary *hovered = JardAnnotationWindowAtPoint(quartz, stack, self.owners);
    if (hovered != self.hovered && ![hovered isEqual:self.hovered]) {
        self.hovered = hovered;
        NSString *hint = self.screenshot ? (hovered ? @"Click to capture the highlighted window" : @"Point to a window to capture")
            : (hovered ? @"Click to record the highlighted window" : @"Click to record this display");
        self.instruction.stringValue = JardLocalized(hint);
        self.needsDisplay = YES;
    }
    NSRect bounds = JardAnnotationAppKitRect(windowBounds(hovered), height);
    NSRect target = hovered ? NSIntersectionRect(bounds, self.window.frame) : NSZeroRect;
    if (!NSIsEmptyRect(target)) target = NSOffsetRect(target, -NSMinX(self.window.frame), -NSMinY(self.window.frame));
    if (!NSEqualRects(self.selection, target)) {
        self.selection = target;
        self.needsDisplay = YES;
    }
    return quartz;
}

- (NSString *)selectionTitle {
    NSString *title = self.hovered[(__bridge NSString *)kCGWindowName];
    return title.length ? title : self.hovered[(__bridge NSString *)kCGWindowOwnerName] ?: JardLocalized(@"Window");
}

- (void)confirmSelection {
    NSPoint location = NSEvent.mouseLocation;
    if (!screenAtPointer(location)) return;
    CGPoint pointer = [self refreshAtPointer:location];
    if (self.screenshot && !self.hovered) return;
    NSNumber *window = self.hovered[(__bridge NSString *)kCGWindowNumber];
    NSNumber *owner = self.hovered[(__bridge NSString *)kCGWindowOwnerPID];
    void (^choose)(BOOL, CGWindowID, pid_t, CGPoint) = self.choose;
    self.choose = nil;
    if (choose) choose(NO, window.unsignedIntValue, owner.intValue, pointer);
}

- (void)mouseDown:(NSEvent *)event { }
- (void)mouseUp:(NSEvent *)event { [self confirmSelection]; }

- (void)cancelOperation:(id)sender {
    void (^choose)(BOOL, CGWindowID, pid_t, CGPoint) = self.choose;
    self.choose = nil;
    if (choose) choose(YES, 0, 0, CGPointZero);
}
@end

JardSelectionPanel *JardAnnotationWindowPanel(NSDictionary<NSNumber *, NSNumber *> *owners, BOOL screenshot,
    void (^complete)(BOOL, CGWindowID, pid_t, CGPoint)) {
    NSPoint point = NSEvent.mouseLocation;
    NSScreen *screen = screenAtPointer(point) ?: NSScreen.mainScreen;
    if (!screen) return nil;
    JardWindowSelectionView *view = [[JardWindowSelectionView alloc] initWithFrame:NSZeroRect];
    view.owners = owners;
    view.choose = complete;
    view.screenshot = screenshot;
    JardSelectionPanel *panel = selectionPanel(screen, view, screenshot ? @"Select a window to capture" : @"Select a window to annotate",
        screenshot ? @"Point to a window to capture" : @"Click to record this display");
    [view refreshAtPointer:point];
    __weak JardWindowSelectionView *weak = view;
    panel.tracking = [NSTimer timerWithTimeInterval:.04 repeats:YES block:^(NSTimer *timer) {
        [weak refreshAtPointer:NSEvent.mouseLocation];
    }];
    [NSRunLoop.mainRunLoop addTimer:panel.tracking forMode:NSRunLoopCommonModes];
    return panel;
}
