#import <AppKit/AppKit.h>

BOOL JardHasWiredAnnotationDevice(void);
NSRect JardPhonePictureRect(NSSize pixels, NSRect bounds);

@interface JardPhoneAnnotation : NSObject
- (instancetype)initWithMovie:(NSURL *)movie;
@property(nonatomic, copy) void (^started)(NSTimeInterval uptime);
@property(nonatomic, copy) void (^finished)(BOOL usable, NSString *error);
@property(nonatomic, copy) dispatch_block_t stopRequested;
@property(nonatomic, copy) dispatch_block_t pictureChanged;
@property(nonatomic, readonly) NSRect pictureOnScreen;
@property(nonatomic, readonly) NSPanel *panel;
- (void)start;
- (void)stopDiscarding:(BOOL)discard;
@end
