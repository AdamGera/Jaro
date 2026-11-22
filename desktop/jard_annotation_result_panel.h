#import <AppKit/AppKit.h>
#import "system_dictation.h"

@interface JardAnnotationResultPanel : NSObject
@property(nonatomic) BOOL showOverlaysInRecordings;
@property(nonatomic, copy) JardDictationSend destinationRequest;
@property(nonatomic, copy) void (^playCue)(NSString *name);
- (instancetype)initWithAvailability:(BOOL (^)(void))available
    action:(void (^)(NSString *session, BOOL paste, JardDictationReply reply))action
    routeAction:(void (^)(NSString *session, NSString *action, BOOL held, JardDictationReply reply))routeAction;
- (void)showSession:(NSString *)session preview:(NSImage *)preview routing:(NSDictionary *)routing;
- (void)showRoute:(NSDictionary *)routing;
- (BOOL)updateRouting:(NSDictionary *)detail;
- (NSString *)confirmationStateForCapture:(NSString *)capture;
- (void)endForNextCapture;
- (void)dismiss;
@end
