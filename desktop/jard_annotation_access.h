#import "system_dictation.h"

@interface JardAnnotationAccess : NSObject
@property(nonatomic, readonly) BOOL resetting;
@property(nonatomic, copy) BOOL (^mayProbe)(void);
@property(nonatomic, copy) void (^settingsVisitChanged)(BOOL granted);
- (BOOL)pollSettingsVisit;
- (void)request:(NSString *)action refresh:(BOOL)refresh reply:(JardDictationReply)reply;
- (BOOL)needsRestart;
@end
