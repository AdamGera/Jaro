#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@interface JardCall : NSObject
@property(nonatomic, copy) NSString *provider;
@property(nonatomic, copy) NSString *application;
@property(nonatomic, copy, nullable) NSString *captureApplication;
@property(nonatomic, copy) NSString *label;
@property(nonatomic, copy) NSString *identity;
@property(nonatomic, copy, nullable) NSString *googleMeetCode;
@property(nonatomic) BOOL slow;
@property(nonatomic) BOOL unnamedBrowser;
@property(nonatomic) BOOL inputActive;
- (BOOL)matchesObservation:(JardCall *)call;
- (BOOL)matchesOfferObservation:(JardCall *)call;
@end

// nil represents an unavailable observation; an empty array is a successful read.
NSArray<JardCall *> * _Nullable JardActiveCalls(NSString * _Nullable outputApplication);
JardCall * _Nullable JardMeetingBrowserCall(NSURL *url, NSString *application);
JardCall * _Nullable JardMeetingBrowserTitleCall(NSString * _Nullable title, NSString *application);
JardCall *JardMeetingUnnamedBrowser(NSString *application, NSString *name);
JardCall * _Nullable JardMeetingDirectCall(NSString *bundle);
NSArray<NSDictionary<NSString *, NSString *> *> *JardMeetingProviderCatalog(void);

@interface JardMeetingPresence : NSObject
- (nullable JardCall *)offerFromCalls:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now;
// An unanswered offer uses zero to rearm on the first observed microphone absence.
- (void)snooze:(JardCall *)call absence:(NSTimeInterval)seconds now:(NSTimeInterval)now;
- (void)deferOfferForCall:(JardCall *)call now:(NSTimeInterval)now;
- (void)rememberSwitchFrom:(JardCall *)previous to:(JardCall *)next confirmedAt:(NSTimeInterval)confirmed lastSeen:(NSTimeInterval)lastSeen;
- (NSArray<JardCall *> *)callsResolvingSwitch:(NSArray<JardCall *> *)calls now:(NSTimeInterval)now;
- (nullable NSString *)takePreviousIdentityForCall:(nullable JardCall *)call;
- (void)clearRememberedSwitch;
- (void)resetObservations;
@end

NS_ASSUME_NONNULL_END
