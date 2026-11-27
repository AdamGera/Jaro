#import <AppKit/AppKit.h>
#import "system_dictation.h"

typedef void (^JardMeetingStart)(NSDictionary *options, BOOL requestPermissions, JardDictationReply reply);

@interface JardMeetings : NSObject
@property(nonatomic, readonly, getter=isManaged) BOOL managed;
@property(nonatomic, copy) void (^showSystemAudioSettings)(void);
@property(nonatomic, copy) void (^openWorkspace)(void);
- (instancetype)initWithManaged:(BOOL)managed start:(JardMeetingStart)start send:(JardDictationSend)send
                        changed:(void (^)(NSDictionary *))changed;
- (void)workspaceReady;
- (BOOL)busy;
- (BOOL)keepsRunning;
- (BOOL)recording;
- (BOOL)sharesMicrophone;
- (BOOL)canPaste;
- (NSDictionary *)status;
- (void)preparePaste:(JardPasteExcerpt)ready;
- (void)startWithEstablishedPermissionsTitle:(NSString *)title reply:(JardDictationReply)reply;
- (void)copyLatestTranscript:(JardDictationReply)reply;
- (void)copySavedTranscript:(NSString *)identifier reply:(JardDictationReply)reply;
- (void)request:(NSDictionary *)request reply:(JardDictationReply)reply;
- (void)handleCapture:(NSDictionary *)capture;
- (void)bridgeDisconnected;
- (void)suspendOffers:(BOOL)suspended;
- (void)shutdown;
@end
