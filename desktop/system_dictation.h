#import <AppKit/AppKit.h>

typedef void (^JardDictationReply)(id result, NSString *error);
typedef void (^JardDictationSend)(NSString *command, NSDictionary *payload, JardDictationReply reply);
typedef void (^JardPasteExcerpt)(NSString *text, void (^didPaste)(BOOL inserted));
typedef void (^JardPrepareExcerpt)(JardPasteExcerpt ready);
typedef struct {
    CGKeyCode code;
    CGEventFlags required, pressed;
} JardAnnotationShortcut;
typedef BOOL (^JardAnnotationInput)(CGEventType type, CGEventRef event, JardAnnotationShortcut shortcut, BOOL chordFree);

int JardLockDictationPreferences(BOOL exclusive);
int JardApplyOfflineDictationSettings(BOOL annotationManaged);
BOOL JardChangesAnnotationSettings(id changes);
BOOL JardChangesAnnotationInputSettings(id changes);
extern NSString *const JardInterfaceLanguageSettingKey;
extern NSString *const JardShowDockIconSettingKey;
extern NSString *const JardShowDockIconPreferenceKey;
BOOL JardShowDockIconEnabled(void);

@interface JardSystemDictation : NSObject
@property(nonatomic, copy) void (^preferencesChanged)(NSDictionary *status);
@property(nonatomic, copy) void (^microphonePreferenceChanged)(NSString *uid);
@property(nonatomic, copy) void (^phaseChanged)(NSString *phase);
@property(nonatomic, copy) void (^activityChanged)(void);
@property(nonatomic, copy) void (^accessibilityGrantedInSettings)(void);
- (instancetype)initWithSend:(JardDictationSend)send;
- (void)workspaceReady;
- (void)applicationActivationPolicyChanged;
- (void)stopSoundPreview;
- (BOOL)keepsRunning;
- (BOOL)quietAtLogin;
- (BOOL)sessionUnavailable;
- (BOOL)hasActivity;
- (BOOL)sharesMicrophone;
- (BOOL)vocabularyMenuEnabled;
- (NSArray<NSDictionary *> *)menuVocabularyEntries;
- (void)configurePasteLastMenuItem:(NSMenuItem *)item;
- (void)pasteLastFromMenu;
- (NSDictionary *)sdkMicrophoneSettings;
- (void)setSDKCaptureActive:(BOOL)active;
- (void)setSystemAudioRecordingActive:(BOOL)active;
- (void)setMeetingPasteAction:(void (^)(void))action;
- (void)setMeetingPasteAvailable:(BOOL)available;
- (void)setAnnotationInput:(JardAnnotationInput)input;
- (BOOL)annotationEnabled;
- (void)setAnnotationActive:(BOOL)active;
- (void)setAnnotationActive:(BOOL)active microphoneShared:(BOOL)shared;
- (void)setAnnotationPhase:(NSString *)phase;
- (void)playAnnotationCue:(NSString *)name;
- (NSRunningApplication *)annotationPasteDestinationFromPanel:(BOOL)fromPanel;
- (NSDictionary *)annotationOriginSnapshotAllowingPrevious:(BOOL)allowPrevious;
- (NSUInteger)annotationPreparationRevision;
- (BOOL)annotationRoutingDisplayAvailable;
- (void)pasteAnnotation:(NSString *)markdown image:(NSData *)image path:(NSString *)path
    destination:(NSRunningApplication *)destination fromPanel:(BOOL)fromPanel preparationRevision:(NSUInteger)revision
    clipboardRevision:(NSInteger)clipboardRevision reply:(JardDictationReply)reply;
- (void)enqueueMeetingPaste:(JardPrepareExcerpt)prepare;
- (void)pauseForUpdate:(BOOL)paused;
- (void)helperDisconnected;
- (BOOL)handleCapture:(NSDictionary *)event;
- (void)updateWarmInputStatus:(NSDictionary *)event;
- (NSDictionary *)status;
- (void)request:(NSDictionary *)parameters reply:(JardDictationReply)reply;
- (void)controlSettings:(NSDictionary *)parameters reply:(JardDictationReply)reply;
- (void)control:(NSString *)action reply:(JardDictationReply)reply;
- (void)writeHistorySnapshot:(NSString *)path reply:(JardDictationReply)reply;
- (void)shutdown;
@end
