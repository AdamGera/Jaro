#import "system_dictation.h"

typedef void (^JardScreenAudioRequest)(void (^reply)(BOOL granted));

@interface JardMeetingAudioAccess : NSObject
@property(nonatomic, copy) void (^settingsVisitChanged)(BOOL granted);
- (BOOL)showSettings;
- (BOOL)pollSettingsVisit;
- (void)request:(NSDictionary *)parameters reply:(JardDictationReply)reply;
- (void)checkBackendWithPrompt:(BOOL)prompt screenRequest:(JardScreenAudioRequest)screenRequest
                        reply:(void (^)(NSString *backend))reply;
@end
