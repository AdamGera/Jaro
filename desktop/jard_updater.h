#import <AppKit/AppKit.h>

BOOL JardVerifyUpdateBundle(NSURL *current, NSURL *candidate, NSString *version, NSString **reason);
BOOL JardSwapUpdateBundles(NSURL *current, NSURL *candidate, NSString **reason);
NSTask *JardScheduleRelaunch(NSURL *bundle, NSString **reason);

typedef void (^JardUpdateInstallReply)(BOOL applied, NSString *reason);

@interface JardUpdater : NSObject
- (instancetype)initWithConfiguration:(NSDictionary *)configuration bundle:(NSBundle *)bundle
                         stateChanged:(void (^)(NSDictionary *))changed;
- (NSDictionary *)status;
- (void)check;
- (void)checkAutomaticallyOnActivation:(BOOL)activation;
- (BOOL)selectChannel:(NSString *)channel reason:(NSString **)reason;
- (BOOL)confirmChannelSwitch:(NSString *)digest reason:(NSString **)reason;
- (void)openRelease;
- (BOOL)openStagedReleaseNotesForVersion:(NSString *)version channel:(NSString *)channel reason:(NSString **)reason;
- (void)checkBeforeRestart:(void (^)(void))completion;
- (void)restoreStagedUpdateWithCompletion:(void (^)(BOOL restored))completion;
- (BOOL)canInstallStagedUpdate;
- (BOOL)prepareToInstallOnQuit;
- (void)resumeAfterCancelledQuit;
- (void)restartWithCompletion:(JardUpdateInstallReply)completion;
- (void)installOnQuitWithCompletion:(JardUpdateInstallReply)completion;
- (void)cancel;
@end
