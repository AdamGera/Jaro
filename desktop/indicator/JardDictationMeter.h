#import <AppKit/AppKit.h>

typedef NS_ENUM(NSInteger, JardIndicatorStage) {
    JardIndicatorRecording,
    JardIndicatorProcessing,
    JardIndicatorDone,
    JardIndicatorFailed,
};

@interface JardDictationMeter : NSView
@property(nonatomic) BOOL embedded;
@property(nonatomic, readonly, getter=isSettled) BOOL settled;
- (void)fitEmbeddedWidth:(CGFloat)width anchorRight:(BOOL)anchorRight;
- (void)setStage:(JardIndicatorStage)stage label:(NSString *)label;
- (void)announceStatus;
- (void)advanceAt:(NSTimeInterval)time rms:(double)rms;
- (void)beginDismissal;
- (void)dismiss;
@end
