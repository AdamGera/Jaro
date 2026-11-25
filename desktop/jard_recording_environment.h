#import <Foundation/Foundation.h>

@protocol JardRecordingOutput <NSObject>
- (NSArray<NSNumber *> *)readVolumes;
- (BOOL)writeVolumes:(NSArray<NSNumber *> *)volumes;
@end

@interface JardRecordingEnvironment : NSObject
- (instancetype)initWithOutput:(id<JardRecordingOutput> (^)(void))openOutput;
- (void)beginWithMode:(NSString *)mode percent:(double)percent;
- (void)setDictationMode:(NSString *)mode percent:(double)percent;
- (void)finish:(dispatch_block_t)completion;
- (void)setAnnotationRecording:(BOOL)recording mode:(NSString *)mode percent:(double)percent
    completion:(dispatch_block_t)completion;
- (void)shutdown;
@end
