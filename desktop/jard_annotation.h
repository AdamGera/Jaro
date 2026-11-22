#import <AppKit/AppKit.h>
#import "system_dictation.h"

BOOL JardCopyAnnotationHandoff(NSPasteboard *board, NSInteger expectedChangeCount, NSString *markdown, NSData *png, BOOL *includedImage);

@interface JardAnnotation : NSObject
@property(nonatomic) BOOL showOverlaysInRecordings;
@property(nonatomic, copy) void (^saved)(NSString *session, NSImage *preview, NSDictionary *routing);
- (instancetype)initWithSend:(JardDictationSend)send stateChanged:(void (^)(NSDictionary *))changed;
- (NSDictionary *)status;
- (BOOL)busy;
- (BOOL)sharesMicrophone;
- (void)start:(NSArray<NSString *> *)languages source:(NSString *)source microphone:(NSDictionary *)microphone
    origin:(NSDictionary *)origin quartzPointer:(NSValue *)pointer reply:(JardDictationReply)reply;
- (BOOL)handleEvent:(CGEventType)type event:(CGEventRef)event shortcut:(JardAnnotationShortcut)shortcut
           allowStart:(BOOL)allowStart start:(void (^)(CGPoint quartzPointer))start;
- (BOOL)stop;
- (BOOL)cancel;
- (void)reveal;
- (void)shutdown;
@end
