#import "jard_annotation_result_panel.h"
#import "jard_interface_language.h"

static const NSTimeInterval quickUndoDuration = 5;

static BOOL routeCanCancel(NSString *phase) {
    return [@[@"transcribing", @"waiting", @"offer", @"countdown", @"held", @"preparing"] containsObject:phase ?: @""];
}

static BOOL routeNeedsPanel(NSString *phase) {
    return [@[@"offer", @"countdown", @"held", @"preparing"] containsObject:phase ?: @""];
}

static BOOL routeAllowsPaste(NSString *phase) {
    return !phase || [@[@"idle", @"cancelled", @"failed"] containsObject:phase];
}

static BOOL receiptWasTakenBack(NSString *status) {
    return [status isEqual:@"withdrawn"] || [status isEqual:@"reverted"];
}

@interface JardAnnotationResultWindow : NSPanel
@property(nonatomic) BOOL pickingDestination;
@end

@implementation JardAnnotationResultWindow
- (BOOL)canBecomeKeyWindow { return self.pickingDestination; }
- (BOOL)canBecomeMainWindow { return NO; }
@end

@interface JardAnnotationResultButton : NSButton
@end

@implementation JardAnnotationResultButton
- (BOOL)acceptsFirstMouse:(NSEvent *)event { return YES; }
@end

static const CGFloat destinationPickerHeight = 264;

static BOOL rememberModifier(NSEventModifierFlags flags) {
    return (flags & NSEventModifierFlagShift) && !(flags & (NSEventModifierFlagCommand | NSEventModifierFlagControl | NSEventModifierFlagOption));
}

@interface JardAnnotationDestinationTable : NSTableView
@property(nonatomic, copy) void (^shiftPick)(NSInteger);
@property(nonatomic, copy) void (^commit)(BOOL);
@end

@implementation JardAnnotationDestinationTable
- (void)keyDown:(NSEvent *)event {
    NSString *key = event.charactersIgnoringModifiers;
    if ([key isEqual:@"\r"] || [key isEqual:@"\n"]) self.commit(rememberModifier(event.modifierFlags));
    else if ([key isEqual:@"\x1b"]) [self.nextResponder tryToPerform:@selector(cancelOperation:) with:self];
    else [super keyDown:event];
}
- (void)mouseDown:(NSEvent *)event {
    NSInteger row = [self rowAtPoint:[self convertPoint:event.locationInWindow fromView:nil]];
    if (rememberModifier(event.modifierFlags) && row >= 0) self.shiftPick(row);
    else [super mouseDown:event];
}
@end

@interface JardAnnotationDestinationPicker : NSView <NSTableViewDataSource, NSTableViewDelegate, NSSearchFieldDelegate>
@property(nonatomic, strong) NSSearchField *search;
@property(nonatomic, strong) NSTextField *status;
@property(nonatomic, strong) JardAnnotationDestinationTable *table;
@property(nonatomic, strong) NSButton *sendButton, *rememberButton;
@property(nonatomic, copy) NSArray<NSDictionary *> *rows;
@property(nonatomic, copy) NSString *service, *suggestedID, *reason;
@property(nonatomic, copy) NSDictionary *scope;
@property(nonatomic, copy) void (^request)(NSString *, JardDictationReply);
@property(nonatomic, copy) void (^choose)(NSString *, NSString *, NSString *);
@property(nonatomic, copy) dispatch_block_t close;
@property(nonatomic) NSUInteger searchGeneration;
@property(nonatomic) BOOL sending;
@end

@implementation JardAnnotationDestinationPicker

- (instancetype)initWithFrame:(NSRect)frame {
    if (!(self = [super initWithFrame:frame])) return nil;
    CGFloat width = NSWidth(frame);
    self.search = [[NSSearchField alloc] initWithFrame:NSMakeRect(14, 226, width - 28, 26)];
    self.search.placeholderString = JardLocalized(@"Search OpenCode sessions");
    self.search.accessibilityLabel = self.search.placeholderString;
    self.search.delegate = self;
    [self addSubview:self.search];

    NSScrollView *scroll = [[NSScrollView alloc] initWithFrame:NSMakeRect(13, 67, width - 26, 152)];
    scroll.drawsBackground = NO;
    scroll.hasVerticalScroller = YES;
    self.table = [[JardAnnotationDestinationTable alloc] initWithFrame:scroll.bounds];
    self.table.headerView = nil;
    self.table.backgroundColor = NSColor.clearColor;
    self.table.rowHeight = 34;
    self.table.intercellSpacing = NSMakeSize(0, 2);
    self.table.delegate = self;
    self.table.dataSource = self;
    self.table.target = self;
    self.table.doubleAction = @selector(send:);
    __weak typeof(self) weak = self;
    self.table.shiftPick = ^(NSInteger row) { [weak chooseRow:row remembering:YES]; };
    self.table.commit = ^(BOOL shift) { [weak chooseRow:weak.table.selectedRow remembering:shift]; };
    self.table.accessibilityLabel = JardLocalized(@"OpenCode sessions");
    NSTableColumn *column = [[NSTableColumn alloc] initWithIdentifier:@"destination"];
    column.width = NSWidth(scroll.bounds) - 12;
    [self.table addTableColumn:column];
    scroll.documentView = self.table;
    [self addSubview:scroll];

    self.status = [NSTextField labelWithString:@""];
    self.status.textColor = NSColor.secondaryLabelColor;
    self.status.font = [NSFont systemFontOfSize:11];
    self.status.lineBreakMode = NSLineBreakByTruncatingTail;
    self.sendButton = [NSButton buttonWithTitle:JardLocalized(@"Send") target:self action:@selector(send:)];
    self.sendButton.frame = NSMakeRect(width - 130, 7, 117, 28);
    self.status.frame = NSMakeRect(15, 8, width - 146, 28);
    self.sendButton.enabled = NO;
    self.rememberButton = [NSButton checkboxWithTitle:@"" target:self action:@selector(rememberChanged:)];
    self.rememberButton.frame = NSMakeRect(15, 40, width - 28, 22);
    self.rememberButton.hidden = YES;
    self.rememberButton.font = [NSFont systemFontOfSize:12];
    [self addSubview:self.rememberButton];
    [self addSubview:self.status];
    [self addSubview:self.sendButton];
    return self;
}

- (void)showStatus:(NSString *)text {
    self.status.stringValue = text ?: @"";
    self.status.toolTip = text;
}

- (void)reloadAfter:(NSTimeInterval)delay {
    NSUInteger generation = ++self.searchGeneration;
    NSString *query = self.search.stringValue;
    if (query.length > 300) query = [query substringToIndex:[query rangeOfComposedCharacterSequenceAtIndex:300].location];
    self.rows = nil;
    self.service = nil;
    self.scope = nil;
    self.rememberButton.hidden = YES;
    [self.table reloadData];
    [self.table deselectAll:nil];
    [self updateSend];
    [self showStatus:JardLocalized(@"Looking for OpenCode sessions…")];
    __weak typeof(self) weak = self;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(delay * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        JardAnnotationDestinationPicker *picker = weak;
        if (!picker || picker.searchGeneration != generation) return;
        picker.request(query, ^(id result, NSString *error) {
            JardAnnotationDestinationPicker *owner = weak;
            if (!owner || owner.searchGeneration != generation) return;
            NSDictionary *detail = [result isKindOfClass:NSDictionary.class] ? result : @{};
            owner.service = [detail[@"service"] isKindOfClass:NSString.class] ? detail[@"service"] : nil;
            NSArray *rows = [detail[@"sessions"] isKindOfClass:NSArray.class] ? detail[@"sessions"] : @[];
            NSMutableArray *validRows = NSMutableArray.array;
            for (id row in rows) {
                if ([row isKindOfClass:NSDictionary.class] && [row[@"id"] isKindOfClass:NSString.class] &&
                    [row[@"title"] isKindOfClass:NSString.class]) [validRows addObject:row];
                if (validRows.count == 25) break;
            }
            NSDictionary *suggestion = [detail[@"suggestion"] isKindOfClass:NSDictionary.class] ? detail[@"suggestion"] : nil;
            owner.suggestedID = [suggestion[@"sessionId"] isKindOfClass:NSString.class] ? suggestion[@"sessionId"] : nil;
            owner.reason = [suggestion[@"reason"] isKindOfClass:NSString.class] ? suggestion[@"reason"] : nil;
            NSArray *scopes = [detail[@"scopes"] isKindOfClass:NSArray.class] ? detail[@"scopes"] : nil;
            id scope = scopes.firstObject;
            if ([scope isKindOfClass:NSDictionary.class] && [scope[@"kind"] isKindOfClass:NSString.class] &&
                [scope[@"label"] isKindOfClass:NSString.class]) owner.scope = scope;
            owner.rememberButton.hidden = !owner.scope;
            if (owner.scope) {
                NSString *title = [NSString stringWithFormat:JardLocalized(@"Always send from %@"), owner.scope[@"label"]];
                owner.rememberButton.title = title;
                owner.rememberButton.toolTip = title;
            } else owner.rememberButton.state = NSControlStateValueOff;
            owner.rows = validRows;
            [owner.table reloadData];
            NSString *warning = [detail[@"warning"] isKindOfClass:NSString.class] ? detail[@"warning"] : nil;
            [owner showStatus:error ?: warning ?: (validRows.count ? @"" : JardLocalized(query.length ? @"No matching sessions." : @"No open sessions."))];
            [owner updateSend];
        });
    });
}

- (NSInteger)numberOfRowsInTableView:(NSTableView *)tableView { return self.rows.count; }

- (NSView *)tableView:(NSTableView *)tableView viewForTableColumn:(NSTableColumn *)column row:(NSInteger)index {
    NSDictionary *row = self.rows[(NSUInteger)index];
    NSView *view = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, column.width, 34)];
    NSString *note = [row[@"id"] isEqual:self.suggestedID] ? self.reason : nil;
    if (!note.length && [row[@"directory"] isKindOfClass:NSString.class]) note = row[@"directory"];
    NSTextField *title = [NSTextField labelWithString:row[@"title"]];
    title.font = [NSFont systemFontOfSize:12 weight:NSFontWeightMedium];
    title.lineBreakMode = NSLineBreakByTruncatingTail;
    title.toolTip = row[@"title"];
    title.frame = NSMakeRect(6, note.length ? 16 : 8, column.width - 12, 16);
    [view addSubview:title];
    if (note.length) {
        NSTextField *description = [NSTextField labelWithString:note];
        description.font = [NSFont systemFontOfSize:11];
        description.textColor = [row[@"id"] isEqual:self.suggestedID] ? NSColor.controlAccentColor : NSColor.secondaryLabelColor;
        description.lineBreakMode = NSLineBreakByTruncatingTail;
        description.toolTip = note;
        description.frame = NSMakeRect(6, 2, column.width - 12, 14);
        [view addSubview:description];
    }
    return view;
}

- (void)updateSend {
    self.sendButton.enabled = !self.sending && self.service.length && self.table.selectedRow >= 0;
    self.rememberButton.enabled = !self.sending && self.service.length > 0;
    BOOL remembering = self.scope && self.rememberButton.state == NSControlStateValueOn;
    self.sendButton.title = JardLocalized(remembering ? @"Send & Remember" : @"Send");
}
- (void)rememberChanged:(id)sender { [self updateSend]; }
- (void)tableViewSelectionDidChange:(NSNotification *)notification { [self updateSend]; }
- (BOOL)tableView:(NSTableView *)tableView shouldSelectRow:(NSInteger)row { return !self.sending; }
- (void)controlTextDidChange:(NSNotification *)notification { if (!self.sending) [self reloadAfter:.25]; }

- (BOOL)control:(NSControl *)control textView:(NSTextView *)textView doCommandBySelector:(SEL)command {
    if (command == @selector(cancelOperation:)) { [self cancelOperation:nil]; return YES; }
    BOOL remember = rememberModifier(NSApp.currentEvent.modifierFlags);
    if (command == @selector(insertNewline:) ||
        (remember && (command == @selector(insertNewlineIgnoringFieldEditor:) || command == @selector(insertLineBreak:)))) {
        [self chooseRow:self.table.selectedRow remembering:remember];
        return YES;
    }
    if (command != @selector(moveDown:) && command != @selector(moveUp:)) return NO;
    if (self.sending || !self.rows.count) return YES;
    NSInteger index = self.table.selectedRow;
    index = index < 0 ? 0 : MAX(0, MIN((NSInteger)self.rows.count - 1, index + (command == @selector(moveDown:) ? 1 : -1)));
    [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)index] byExtendingSelection:NO];
    [self.table scrollRowToVisible:index];
    return YES;
}

- (void)cancelOperation:(id)sender { if (!self.sending) self.close(); }

- (void)send:(id)sender {
    [self chooseRow:self.table.selectedRow remembering:NO];
}

- (void)chooseRow:(NSInteger)row remembering:(BOOL)remember {
    if (self.sending || !self.service.length || row < 0 || (NSUInteger)row >= self.rows.count) return;
    if (remember && !self.scope) {
        [self showStatus:JardLocalized(@"This capture has no verified destination scope.")];
        return;
    }
    [self.table selectRowIndexes:[NSIndexSet indexSetWithIndex:(NSUInteger)row] byExtendingSelection:NO];
    NSString *kind = (remember || self.rememberButton.state == NSControlStateValueOn) ? self.scope[@"kind"] : nil;
    self.choose(self.service, self.rows[(NSUInteger)row][@"id"], kind);
}

- (void)setSending:(BOOL)sending {
    _sending = sending;
    self.search.enabled = !sending;
    if (sending) [self showStatus:JardLocalized(@"Preparing annotation…")];
    [self updateSend];
}

@end

@interface JardAnnotationResultPanel () <NSWindowDelegate>
@property(nonatomic, copy) BOOL (^available)(void);
@property(nonatomic, copy) void (^action)(NSString *, BOOL, JardDictationReply);
@property(nonatomic, copy) void (^routeAction)(NSString *, NSString *, BOOL, JardDictationReply);
@property(nonatomic, copy) NSString *session, *routePhase, *receiptStatus, *manualDelivery;
@property(nonatomic, strong) JardAnnotationResultWindow *window;
@property(nonatomic, strong) NSButton *thumbnail, *inspectButton, *pasteButton, *sendToButton;
@property(nonatomic, strong) JardAnnotationDestinationPicker *picker;
@property(nonatomic, strong) NSView *header;
@property(nonatomic, strong) NSTextField *heading, *status;
@property(nonatomic, strong) NSProgressIndicator *undoProgress;
@property(nonatomic, strong) NSTimer *timer;
@property(nonatomic) BOOL pending, routeActionPending, hovered, hasPreview, canInspect, undoOffered, undoHovered;
@property(nonatomic) NSTimeInterval deadline, routeDeadline;
@property(nonatomic) NSTimeInterval undoRemaining, undoUntil, undoLastTick;
@end

@implementation JardAnnotationResultPanel

- (void)setShowOverlaysInRecordings:(BOOL)show {
    _showOverlaysInRecordings = show;
    self.window.sharingType = show ? NSWindowSharingReadOnly : NSWindowSharingNone;
}

- (instancetype)initWithAvailability:(BOOL (^)(void))available
    action:(void (^)(NSString *, BOOL, JardDictationReply))action
    routeAction:(void (^)(NSString *, NSString *, BOOL, JardDictationReply))routeAction {
    if (!(self = [super init])) return nil;
    _available = [available copy];
    _action = [action copy];
    _routeAction = [routeAction copy];
    return self;
}

- (NSButton *)button:(NSString *)title action:(SEL)selector frame:(NSRect)frame {
    NSButton *button = [[JardAnnotationResultButton alloc] initWithFrame:frame];
    button.bezelStyle = NSBezelStyleRounded;
    button.font = [NSFont systemFontOfSize:12 weight:NSFontWeightMedium];
    button.title = title;
    button.target = self;
    button.action = selector;
    return button;
}

- (BOOL)presentSession:(NSString *)session preview:(NSImage *)preview backgroundRoute:(BOOL)backgroundRoute {
    [self endForNextCapture];
    if (![session isKindOfClass:NSString.class] || ![[NSUUID alloc] initWithUUIDString:session] || !self.available()) return NO;
    NSPoint mouse = NSEvent.mouseLocation;
    NSScreen *screen = NSScreen.mainScreen;
    for (NSScreen *candidate in NSScreen.screens) {
        if (NSPointInRect(mouse, candidate.frame)) { screen = candidate; break; }
    }
    if (!screen || NSWidth(screen.visibleFrame) < 358) return NO;
    NSRect visible = screen.visibleFrame;
    const CGFloat width = 332, height = 82;
    CGFloat x = backgroundRoute ? NSMinX(visible) + 16 : NSMaxX(visible) - width - 16;
    CGFloat y = NSMaxY(visible) - height - 14;
    if (backgroundRoute && NSWidth(visible) < 2 * (width + 16)) y -= height + 8;
    NSRect frame = NSMakeRect(x, y, width, height);
    JardAnnotationResultWindow *window = [[JardAnnotationResultWindow alloc] initWithContentRect:frame
        styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel backing:NSBackingStoreBuffered defer:NO];
    window.releasedWhenClosed = NO;
    window.hidesOnDeactivate = NO;
    window.opaque = NO;
    window.backgroundColor = NSColor.clearColor;
    window.level = NSStatusWindowLevel;
    window.sharingType = self.showOverlaysInRecordings ? NSWindowSharingReadOnly : NSWindowSharingNone;
    window.delegate = self;
    window.becomesKeyOnlyIfNeeded = YES;
    window.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary;

    NSVisualEffectView *surface = [[NSVisualEffectView alloc] initWithFrame:NSMakeRect(0, 0, width, height)];
    surface.material = NSVisualEffectMaterialHUDWindow;
    surface.blendingMode = NSVisualEffectBlendingModeBehindWindow;
    surface.state = NSVisualEffectStateActive;
    surface.wantsLayer = YES;
    surface.layer.cornerRadius = 14;
    surface.layer.masksToBounds = YES;
    window.contentView = surface;
    self.header = [[NSView alloc] initWithFrame:surface.bounds];
    [surface addSubview:self.header];

    self.heading = [NSTextField labelWithString:JardLocalized(@"Annotation saved")];
    self.heading.font = [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold];
    self.heading.lineBreakMode = NSLineBreakByTruncatingTail;
    self.heading.frame = NSMakeRect(80, 56, 216, 17);
    [self.header addSubview:self.heading];
    self.status = [NSTextField labelWithString:@""];
    self.status.lineBreakMode = NSLineBreakByTruncatingTail;
    self.status.font = [NSFont systemFontOfSize:12];
    self.status.textColor = NSColor.secondaryLabelColor;
    self.status.frame = NSMakeRect(80, 37, 236, 17);
    [self.header addSubview:self.status];

    self.thumbnail = [self button:@"" action:@selector(inspect:) frame:NSMakeRect(12, 18, 56, 46)];
    self.thumbnail.imagePosition = NSImageOnly;
    self.thumbnail.imageScaling = NSImageScaleProportionallyUpOrDown;
    self.thumbnail.image = preview ?: [NSImage imageWithSystemSymbolName:@"checkmark" accessibilityDescription:nil];
    self.thumbnail.bordered = NO;
    self.thumbnail.wantsLayer = YES;
    self.thumbnail.layer.cornerRadius = 7;
    self.thumbnail.layer.masksToBounds = YES;
    self.thumbnail.accessibilityLabel = JardLocalized(preview ? @"Inspect contact sheet" : @"Annotation saved");
    [self.header addSubview:self.thumbnail];

    self.inspectButton = [self button:JardLocalized(@"Inspect") action:@selector(inspect:) frame:NSMakeRect(78, 6, 96, 29)];
    self.pasteButton = [self button:JardLocalized(@"Paste") action:@selector(deliver:) frame:NSMakeRect(179, 6, 130, 29)];
    self.sendToButton = [self button:JardLocalized(@"Send to…") action:@selector(openDestinations:) frame:NSMakeRect(225, 6, 96, 29)];
    NSButton *close = [self button:@"" action:@selector(close:) frame:NSMakeRect(296, 53, 27, 24)];
    close.image = [NSImage imageWithSystemSymbolName:@"xmark" accessibilityDescription:nil];
    close.imagePosition = NSImageOnly;
    close.bordered = NO;
    close.accessibilityLabel = JardLocalized(@"Dismiss");
    [self.header addSubview:self.inspectButton];
    [self.header addSubview:self.pasteButton];
    [self.header addSubview:self.sendToButton];
    [self.header addSubview:close];
    self.undoProgress = [[NSProgressIndicator alloc] initWithFrame:NSMakeRect(187, 4, 114, 3)];
    self.undoProgress.indeterminate = NO;
    self.undoProgress.controlSize = NSControlSizeSmall;
    self.undoProgress.maxValue = quickUndoDuration;
    self.undoProgress.accessibilityHidden = YES;
    [self.header addSubview:self.undoProgress];

    self.window = window;
    self.session = session;
    self.hasPreview = preview != nil;
    self.canInspect = self.hasPreview || backgroundRoute;
    self.deadline = NSProcessInfo.processInfo.systemUptime + 12;
    [self showStatus:nil];
    [self refreshControls];
    [window orderFrontRegardless];
    if (!window.visible) { [self dismiss]; return NO; }
    __weak typeof(self) weak = self;
    self.timer = [NSTimer timerWithTimeInterval:0.10 repeats:YES block:^(NSTimer *timer) { [weak tick]; }];
    [NSRunLoop.mainRunLoop addTimer:self.timer forMode:NSRunLoopCommonModes];
    return YES;
}

- (void)showSession:(NSString *)session preview:(NSImage *)preview routing:(NSDictionary *)routing {
    if ([self presentSession:session preview:preview backgroundRoute:NO] && routing) [self updateRouting:routing];
}

- (void)showRoute:(NSDictionary *)routing {
    NSString *capture = routing[@"capture"], *phase = routing[@"phase"];
    if (![phase isEqual:@"offer"] || ![capture isKindOfClass:NSString.class] || ![[NSUUID alloc] initWithUUIDString:capture]) return;
    if ([self presentSession:capture preview:nil backgroundRoute:YES]) [self updateRouting:routing];
    else self.routeAction(capture, @"cancel", NO, nil);
}

- (void)showStatus:(NSString *)text {
    self.status.stringValue = text ?: @"";
    self.status.toolTip = text;
    NSRect frame = self.heading.frame;
    frame.origin.y = text.length ? 56 : 48;
    self.heading.frame = frame;
}

- (BOOL)canUndo {
    return self.undoRemaining > 0 && NSDate.date.timeIntervalSince1970 < self.undoUntil;
}

- (BOOL)canChooseDestination {
    return self.destinationRequest && (routeAllowsPaste(self.routePhase) || receiptWasTakenBack(self.receiptStatus));
}

- (void)refreshControls {
    BOOL canCancel = routeCanCancel(self.routePhase);
    BOOL canUndo = [self canUndo];
    BOOL pickerOpen = self.picker != nil;
    self.thumbnail.enabled = self.hasPreview && !self.pending && !pickerOpen;
    self.inspectButton.enabled = self.canInspect && !self.pending && !pickerOpen;
    self.pasteButton.title = JardLocalized(canCancel ? @"Cancel send" : canUndo ? @"Undo send" : @"Paste");
    self.pasteButton.hidden = !canCancel && !canUndo && !routeAllowsPaste(self.routePhase);
    self.pasteButton.enabled = !self.pending && !self.routeActionPending && !pickerOpen;
    self.sendToButton.hidden = ![self canChooseDestination];
    self.sendToButton.enabled = !self.pending && !self.routeActionPending && !pickerOpen;
    self.inspectButton.frame = NSMakeRect(78, 6, self.sendToButton.hidden ? 96 : 71, 29);
    self.pasteButton.frame = self.sendToButton.hidden ? NSMakeRect(179, 6, 130, 29) : NSMakeRect(151, 6, 71, 29);
    self.undoProgress.hidden = !canUndo;
    if (canUndo) self.undoProgress.doubleValue = self.undoRemaining;
}

- (void)openDestinations:(id)sender {
    if (self.picker || !self.sendToButton.enabled || ![self canChooseDestination] || !self.available()) return;
    NSRect frame = self.window.frame;
    if (NSMinY(frame) - destinationPickerHeight < NSMinY(self.window.screen.visibleFrame)) {
        [self showStatus:JardLocalized(@"Open the annotation in Sessions to choose a destination.")];
        return;
    }
    JardAnnotationDestinationPicker *picker = [[JardAnnotationDestinationPicker alloc] initWithFrame:
        NSMakeRect(0, -destinationPickerHeight, NSWidth(frame), destinationPickerHeight)];
    self.picker = picker;
    __weak typeof(self) weak = self;
    NSString *session = self.session;
    picker.request = ^(NSString *query, JardDictationReply reply) {
        JardAnnotationResultPanel *owner = weak;
        if (!owner || !owner.available()) { reply(nil, @"The annotation panel is unavailable."); return; }
        owner.destinationRequest(@"jard.annotationDestinations", @{@"id": session, @"query": query}, reply);
    };
    picker.choose = ^(NSString *service, NSString *target, NSString *scope) { [weak sendTo:target service:service remember:scope]; };
    picker.close = ^{ [weak closeDestinations]; };
    frame.origin.y -= destinationPickerHeight;
    frame.size.height += destinationPickerHeight;
    [self.window setFrame:frame display:YES];
    [self.header setFrameOrigin:NSMakePoint(0, destinationPickerHeight)];
    picker.frame = NSMakeRect(0, 0, NSWidth(frame), destinationPickerHeight);
    [self.window.contentView addSubview:picker];
    self.window.pickingDestination = YES;
    [self.window makeKeyWindow];
    [self.window makeFirstResponder:picker.search];
    [self refreshControls];
    [picker reloadAfter:0];
}

- (void)closeDestinations {
    if (!self.picker) return;
    self.picker.searchGeneration++;
    [self.picker removeFromSuperview];
    self.picker = nil;
    [self.window makeFirstResponder:nil];
    self.window.pickingDestination = NO;
    if (self.window.isKeyWindow) [self.window resignKeyWindow];
    NSRect frame = self.window.frame;
    frame.origin.y += destinationPickerHeight;
    frame.size.height -= destinationPickerHeight;
    [self.header setFrameOrigin:NSZeroPoint];
    [self.window setFrame:frame display:YES];
    self.deadline = NSProcessInfo.processInfo.systemUptime + 12;
    [self refreshControls];
}

- (void)windowDidResignKey:(NSNotification *)notification {
    if (notification.object == self.window && !self.picker.sending) [self closeDestinations];
}

- (void)sendTo:(NSString *)target service:(NSString *)service remember:(NSString *)scope {
    if (!self.picker || self.picker.sending || !self.available() || ![self canChooseDestination]) return;
    self.picker.sending = YES;
    NSPanel *window = self.window;
    __weak typeof(self) weak = self;
    NSMutableDictionary *request = [@{@"id": self.session, @"target": target, @"service": service} mutableCopy];
    if (scope) request[@"rememberScope"] = scope;
    self.destinationRequest(@"jard.annotationPanelSend", request, ^(id result, NSString *error) {
        JardAnnotationResultPanel *owner = weak;
        if (owner.window != window) return;
        owner.picker.sending = NO;
        if (error) {
            [owner.picker showStatus:error];
            if (owner.playCue && owner.available()) owner.playCue(@"error");
            return;
        }
        [owner closeDestinations];
        if ([result isKindOfClass:NSDictionary.class]) [owner updateRouting:result];
    });
}

- (BOOL)updateRouting:(NSDictionary *)detail {
    NSString *phase = detail[@"phase"];
    if (![self.session isEqual:detail[@"capture"]] || ![phase isKindOfClass:NSString.class]) return NO;
    BOOL phaseChanged = ![self.routePhase isEqual:phase];
    BOOL newOffer = [phase isEqual:@"offer"] && phaseChanged;
    BOOL wasTakenBack = receiptWasTakenBack(self.receiptStatus);
    NSString *delivery = [detail[@"deliveryId"] isKindOfClass:NSString.class] ? detail[@"deliveryId"] : nil;
    BOOL newDelivery = delivery && ![self.manualDelivery isEqual:delivery];
    self.routePhase = phase;
    if (newDelivery) {
        self.manualDelivery = delivery;
        self.undoOffered = NO;
    }
    self.routeDeadline = NSProcessInfo.processInfo.systemUptime + [detail[@"seconds"] doubleValue];
    self.deadline = NSProcessInfo.processInfo.systemUptime + 12;
    if (!self.available() || !self.window.visible) { [self endForNextCapture]; return YES; }
    NSNumber *undoUntil = [detail[@"undoUntil"] isKindOfClass:NSNumber.class] ? detail[@"undoUntil"] : nil;
    self.undoUntil = [phase isEqual:@"sent"] ? undoUntil.doubleValue / 1000 : 0;
    if (self.undoUntil <= NSDate.date.timeIntervalSince1970) self.undoRemaining = 0;
    else if (!self.undoOffered) {
        self.undoOffered = YES;
        self.undoRemaining = quickUndoDuration;
        self.undoLastTick = NSProcessInfo.processInfo.systemUptime;
        self.undoHovered = NSPointInRect(NSEvent.mouseLocation, self.window.frame);
    }
    NSString *title = [detail[@"title"] isKindOfClass:NSString.class] ? detail[@"title"] : @"OpenCode";
    NSString *message = [detail[@"message"] isKindOfClass:NSString.class] ? detail[@"message"] : @"";
    self.receiptStatus = [detail[@"receiptStatus"] isKindOfClass:NSString.class] ? detail[@"receiptStatus"] : nil;
    BOOL takenBack = receiptWasTakenBack(self.receiptStatus);
    if (self.picker && ![self canChooseDestination]) [self closeDestinations];
    if (takenBack) self.heading.stringValue = JardLocalized(@"Taken back");
    else if ([phase isEqual:@"unconfirmed"]) self.heading.stringValue = JardLocalized(@"Delivery unconfirmed");
    else if ([phase isEqual:@"sent"]) self.heading.stringValue = [NSString stringWithFormat:JardLocalized(@"Sent to %@"), title];
    else if (routeAllowsPaste(phase)) self.heading.stringValue = JardLocalized(@"Annotation saved");
    else self.heading.stringValue = [NSString stringWithFormat:JardLocalized(@"Send to %@"), title];
    self.heading.toolTip = self.heading.stringValue;
    if ([phase isEqual:@"offer"]) message = JardLocalized(@"Preparing automatic send…");
    if ([phase isEqual:@"held"]) message = JardLocalized(@"Paused while the pointer is here");
    [self showStatus:message];
    [self refreshControls];
    if (self.playCue) {
        if (takenBack) {
            if (!wasTakenBack || newDelivery) self.playCue(@"annotationUndo");
        } else if ([phase isEqual:@"sent"]) {
            if (newDelivery || (phaseChanged && !wasTakenBack)) self.playCue(@"annotationSent");
        } else if (phaseChanged && [@[@"failed", @"unconfirmed"] containsObject:phase]) self.playCue(@"error");
    }
    if (newOffer) {
        self.hovered = NSPointInRect(NSEvent.mouseLocation, self.window.frame);
        self.routeAction(self.session, @"shown", self.hovered, nil);
    } else if ([phase isEqual:@"countdown"] || [phase isEqual:@"held"]) {
        self.hovered = [phase isEqual:@"held"];
        [self tick];
    }
    return YES;
}

- (BOOL)routePanelOnScreen {
    return self.window.visible && self.window.isOnActiveSpace && (self.window.occlusionState & NSWindowOcclusionStateVisible);
}

- (NSString *)confirmationStateForCapture:(NSString *)capture {
    if (![self.session isEqual:capture]) return nil;
    if (![self.routePhase isEqual:@"preparing"] || self.routeActionPending || !self.available() || ![self routePanelOnScreen])
        return @"unavailable";
    return NSPointInRect(NSEvent.mouseLocation, self.window.frame) ? @"held" : @"ready";
}

- (void)tick {
    if (!self.window.visible || !self.available()) { [self endForNextCapture]; return; }
    NSTimeInterval now = NSProcessInfo.processInfo.systemUptime;
    BOOL hovered = NSPointInRect(NSEvent.mouseLocation, self.window.frame);
    BOOL countdown = [self.routePhase isEqual:@"countdown"], held = [self.routePhase isEqual:@"held"];
    if ((countdown || held) && self.hovered != hovered) {
        self.hovered = hovered;
        self.routeAction(self.session, hovered ? @"hold" : @"resume", NO, nil);
    }
    if (countdown) {
        NSInteger seconds = MAX(1, (NSInteger)ceil(self.routeDeadline - now));
        [self showStatus:[NSString stringWithFormat:JardLocalized(@"Sending in %ld…"), (long)seconds]];
    }
    if (self.undoRemaining > 0) {
        if (!self.undoHovered) self.undoRemaining = MAX(0, self.undoRemaining - (now - self.undoLastTick));
        self.undoLastTick = now;
        self.undoHovered = hovered;
        if (![self canUndo]) {
            self.undoRemaining = 0;
            [self refreshControls];
        } else self.undoProgress.doubleValue = NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion
            ? ceil(self.undoRemaining) : self.undoRemaining;
    }
    if (self.pending || self.picker || self.routeActionPending || routeNeedsPanel(self.routePhase) || [self.routePhase isEqual:@"sending"] || hovered)
        self.deadline = now + 12;
    else if (now >= self.deadline) [self dismiss];
}

- (void)performPaste:(BOOL)paste {
    if (!self.session || self.pending || (paste && !routeAllowsPaste(self.routePhase))) return;
    if (!self.available()) { [self endForNextCapture]; return; }
    NSString *session = self.session;
    NSPanel *window = self.window;
    self.pending = YES;
    [self refreshControls];
    if (paste) [self showStatus:JardLocalized(@"Preparing to paste…")];
    __weak typeof(self) weak = self;
    self.action(session, paste, ^(id result, NSString *error) {
        JardAnnotationResultPanel *owner = weak;
        if (owner.window != window) return;
        owner.pending = NO;
        if (!error && !routeCanCancel(owner.routePhase) && ![owner.routePhase isEqual:@"sending"] && ![owner canUndo]) {
            [owner dismiss];
            return;
        }
        owner.deadline = NSProcessInfo.processInfo.systemUptime + 12;
        [owner refreshControls];
        if (error) [owner showStatus:error];
    });
}

- (void)inspect:(id)sender { if (self.inspectButton.enabled) [self performPaste:NO]; }
- (void)deliver:(id)sender {
    if (!self.pasteButton.enabled || self.pasteButton.hidden) return;
    BOOL undo = [self canUndo];
    if (!undo && !routeCanCancel(self.routePhase)) { [self performPaste:YES]; return; }
    if (!self.available()) { [self endForNextCapture]; return; }
    self.routeActionPending = YES;
    if (undo) [self showStatus:JardLocalized(@"Taking back…")];
    [self refreshControls];
    NSPanel *window = self.window;
    __weak typeof(self) weak = self;
    JardDictationReply reply = ^(id result, NSString *error) {
        JardAnnotationResultPanel *owner = weak;
        if (owner.window != window) return;
        owner.routeActionPending = NO;
        if (error) {
            [owner showStatus:error];
            [owner refreshControls];
            if (undo && owner.playCue && owner.available()) owner.playCue(@"error");
        }
        else if ([result isKindOfClass:NSDictionary.class]) [owner updateRouting:result];
    };
    if (undo && self.manualDelivery) {
        self.destinationRequest(@"jard.annotationPanelUndo", @{@"id": self.session, @"deliveryId": self.manualDelivery}, reply);
    } else self.routeAction(self.session, undo ? @"undo" : @"cancel", NO, reply);
}

- (void)close:(id)sender { [self endForNextCapture]; }

- (void)endForNextCapture {
    if (routeNeedsPanel(self.routePhase)) self.routeAction(self.session, @"cancel", NO, nil);
    [self dismiss];
}

- (void)dismiss {
    [self.timer invalidate];
    self.timer = nil;
    [self closeDestinations];
    [self.window orderOut:nil];
    self.window = nil;
    self.session = self.routePhase = self.receiptStatus = self.manualDelivery = nil;
    self.thumbnail = self.inspectButton = self.pasteButton = self.sendToButton = nil;
    self.heading = self.status = nil;
    self.undoProgress = nil;
    self.header = nil;
    self.pending = self.routeActionPending = self.hovered = self.hasPreview = self.canInspect = NO;
    self.undoOffered = self.undoHovered = NO;
    self.undoUntil = self.undoRemaining = self.undoLastTick = 0;
}

@end
