#import "jard_annotation_ink.h"
#import <AVFoundation/AVFoundation.h>
#import <CoreImage/CoreImage.h>
#import <CoreText/CoreText.h>
#import <ImageIO/ImageIO.h>

const NSTimeInterval JardAnnotationInkHold = .65;
const NSTimeInterval JardAnnotationInkLifetime = 1.30;

CGFloat JardAnnotationInkAlpha(NSTimeInterval age) {
    CGFloat left = fmin(1, fmax(0, (JardAnnotationInkLifetime - age) / (JardAnnotationInkLifetime - JardAnnotationInkHold)));
    return left * left;
}

NSRect JardAnnotationAppKitRect(CGRect quartz, CGFloat primaryHeight) {
    return NSMakeRect(CGRectGetMinX(quartz), primaryHeight - CGRectGetMaxY(quartz),
                      CGRectGetWidth(quartz), CGRectGetHeight(quartz));
}


NSDictionary *JardAnnotationRoutingOrigin(NSDictionary *snapshot, NSDictionary *selectedApp, pid_t selectedPID,
    NSString *selectedTitle, CGRect captured) {
    NSValue *bounds = snapshot[@"bounds"];
    CGRect focused = bounds ? bounds.rectValue : CGRectZero;
    NSDictionary *previousApp = snapshot[@"app"];
    if (!selectedApp) {
        NSPoint center = NSMakePoint(CGRectGetMidX(focused), CGRectGetMidY(focused));
        return previousApp && !CGRectIsEmpty(focused) && CGRectContainsPoint(captured, center) ? previousApp : nil;
    }
    BOOL sameWindow = [snapshot[@"pid"] intValue] == selectedPID && selectedTitle.length &&
        [snapshot[@"title"] isEqual:selectedTitle] && !CGRectIsEmpty(focused) &&
        CGRectContainsRect(CGRectInset(captured, -4, -4), focused) &&
        CGRectContainsRect(CGRectInset(focused, -4, -4), captured);
    if (sameWindow && (previousApp[@"site"] || previousApp[@"terminal"])) {
        NSMutableDictionary *origin = selectedApp.mutableCopy;
        if (previousApp[@"site"]) origin[@"site"] = previousApp[@"site"];
        if (previousApp[@"terminal"]) origin[@"terminal"] = previousApp[@"terminal"];
        return origin;
    }
    return selectedApp;
}

NSPoint JardAnnotationUnitPoint(NSPoint point, NSSize bounds) {
    return NSMakePoint(fmin(1, fmax(0, point.x / fmax(1, bounds.width))),
                       fmin(1, fmax(0, point.y / fmax(1, bounds.height))));
}

NSDictionary *JardAnnotationEdge(NSPoint from, NSPoint to, NSTimeInterval time) {
    return @{@"from": [NSValue valueWithPoint:from], @"to": [NSValue valueWithPoint:to], @"time": @(time)};
}

NSArray<NSNumber *> *JardAnnotationFrameTimes(NSArray<NSDictionary *> *events, NSTimeInterval duration) {
    if (!isfinite(duration) || duration <= 0) return @[];
    double inset = MIN(.12, duration / 8), end = duration - inset;
    if (duration < .4) return @[@(duration / 2)];
    NSMutableArray<NSNumber *> *marks = NSMutableArray.array;
    double last = -1;
    for (NSDictionary *event in events) {
        if ([event[@"clear"] boolValue]) {
            if (last >= 0) [marks addObject:@(last)];
            last = -1;
            continue;
        }
        double time = MIN(end, [event[@"time"] doubleValue]);
        if (last >= 0 && time - last > .65) [marks addObject:@(last)];
        last = time;
    }
    if (last >= 0) [marks addObject:@(last)];
    NSMutableArray<NSNumber *> *times = [@[@(inset), @(end)] mutableCopy];
    for (NSUInteger i = 0, picked = MIN(4, marks.count); i < picked; i++) {
        NSUInteger row = (NSUInteger)floor((i + .5) * marks.count / picked);
        double time = MIN(end, marks[row].doubleValue + .08);
        if (time > inset + .15 && time < end - .15) [times addObject:@(time)];
    }
    NSUInteger count = MIN(6, MAX(2, (NSInteger)ceil(duration / 2) + 1));
    for (NSUInteger i = 1; i + 1 < count && times.count < count; i++) {
        double time = inset + (end - inset) * i / (count - 1);
        BOOL nearby = NO;
        for (NSNumber *selected in times) if (fabs(selected.doubleValue - time) < .18) { nearby = YES; break; }
        if (!nearby) [times addObject:@(time)];
    }
    [times sortUsingSelector:@selector(compare:)];
    NSMutableArray<NSNumber *> *distinct = NSMutableArray.array;
    for (NSNumber *time in times) {
        if (!distinct.count || time.doubleValue - distinct.lastObject.doubleValue > .12) [distinct addObject:time];
    }
    return distinct;
}

static NSInteger frameTenths(NSTimeInterval time) {
    return (NSInteger)floor(time * 10 + .0001);
}

static void drawFrameTime(CGContextRef context, NSTimeInterval time, CGPoint location) {
    long tenths = frameTenths(time), seconds = tenths / 10, minutes = seconds / 60;
    NSString *label = minutes < 60
        ? [NSString stringWithFormat:@"%ld:%02ld.%ld", minutes, seconds % 60, tenths % 10]
        : [NSString stringWithFormat:@"%ld:%02ld:%02ld.%ld", minutes / 60, minutes % 60, seconds % 60, tenths % 10];
    CTFontRef font = CTFontCreateWithName(CFSTR("Menlo"), 14, NULL);
    NSAttributedString *value = [[NSAttributedString alloc] initWithString:label attributes:@{
        (__bridge NSString *)kCTFontAttributeName: (__bridge id)font,
        (__bridge NSString *)kCTForegroundColorFromContextAttributeName: @YES}];
    CTLineRef line = CTLineCreateWithAttributedString((__bridge CFAttributedStringRef)value);
    CGContextSetRGBFillColor(context, .84, .87, .91, 1);
    CGContextSetTextPosition(context, location.x, location.y);
    CTLineDraw(line, context);
    CFRelease(line);
    CFRelease(font);
}

static NSArray<NSNumber *> *writeContactSheet(AVURLAsset *asset, NSURL *path, NSArray<NSNumber *> *times, id *firstFrame) {
    if (!times.count) return @[];
    AVAssetImageGenerator *generator = [AVAssetImageGenerator assetImageGeneratorWithAsset:asset];
    generator.appliesPreferredTrackTransform = YES;
    generator.maximumSize = CGSizeMake(420, 248);
    generator.requestedTimeToleranceBefore = generator.requestedTimeToleranceAfter = CMTimeMake(1, 5);
    NSMutableArray *images = NSMutableArray.array;
    NSMutableArray<NSNumber *> *sampled = NSMutableArray.array;
    for (NSNumber *seconds in times) {
        CMTime actual = kCMTimeInvalid;
        CGImageRef frame = [generator copyCGImageAtTime:CMTimeMakeWithSeconds(seconds.doubleValue, 600) actualTime:&actual error:nil];
        if (!frame) continue;
        double time = CMTimeGetSeconds(actual);
        if (!isfinite(time) || time < 0 || (sampled.count && frameTenths(time) <= frameTenths(sampled.lastObject.doubleValue))) {
            CGImageRelease(frame);
            continue;
        }
        [images addObject:CFBridgingRelease(frame)];
        [sampled addObject:@(time)];
    }
    if (!images.count) return @[];
    const size_t cellWidth = 436, cellHeight = 282;
    size_t columns = MIN(2, images.count), rows = (images.count + columns - 1) / columns;
    size_t width = columns * cellWidth, height = rows * cellHeight;
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(NULL, width, height, 8, 0, space, (CGBitmapInfo)kCGImageAlphaPremultipliedLast);
    CGColorSpaceRelease(space);
    if (!context) return @[];
    CGContextSetRGBFillColor(context, .11, .13, .16, 1);
    CGContextFillRect(context, CGRectMake(0, 0, width, height));
    CGContextSetInterpolationQuality(context, kCGInterpolationHigh);
    for (NSUInteger i = 0; i < images.count; i++) {
        CGFloat x = (i % columns) * cellWidth, y = height - (1 + i / columns) * cellHeight;
        CGContextSetRGBFillColor(context, .05, .07, .09, 1);
        CGContextFillRect(context, CGRectMake(x + 8, y + 26, 420, 248));
        CGImageRef frame = (__bridge CGImageRef)images[i];
        CGFloat w = CGImageGetWidth(frame), h = CGImageGetHeight(frame);
        CGFloat scale = MIN(420.0 / w, 248.0 / h);
        w *= scale;
        h *= scale;
        CGContextDrawImage(context, CGRectMake(x + (cellWidth - w) / 2, y + 26 + (248 - h) / 2, w, h), frame);
        drawFrameTime(context, sampled[i].doubleValue, CGPointMake(x + 8, y + 8));
    }
    CGImageRef sheet = CGBitmapContextCreateImage(context);
    CGContextRelease(context);
    NSMutableData *png = NSMutableData.data;
    CGImageDestinationRef output = CGImageDestinationCreateWithData((__bridge CFMutableDataRef)png, CFSTR("public.png"), 1, NULL);
    BOOL saved = NO;
    if (sheet && output) {
        CGImageDestinationAddImage(output, sheet, NULL);
        if (CGImageDestinationFinalize(output)) saved = [png writeToURL:path options:NSDataWritingAtomic error:nil];
    }
    if (output) CFRelease(output);
    if (sheet) CGImageRelease(sheet);
    if (saved) *firstFrame = images.firstObject;
    return saved ? sampled : @[];
}

void JardAnnotationMovieHasFrame(NSURL *movie, void (^completion)(BOOL)) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        BOOL playable = NO;
        if (movie) {
            AVURLAsset *asset = [AVURLAsset URLAssetWithURL:movie options:nil];
            AVAssetImageGenerator *generator = [AVAssetImageGenerator assetImageGeneratorWithAsset:asset];
            generator.maximumSize = CGSizeMake(32, 32);
            CGImageRef frame = [generator copyCGImageAtTime:kCMTimeZero actualTime:NULL error:nil];
            playable = frame != NULL;
            if (frame) CGImageRelease(frame);
        }
        dispatch_async(dispatch_get_main_queue(), ^{ completion(playable); });
    });
}

void JardWriteAnnotationAssets(NSURL *media, NSString *source, NSDictionary *origin, NSArray<NSDictionary *> *events,
    NSString *missingVideo, void (^completion)(NSImage *preview, NSError *error)) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        @autoreleasepool {
            NSURL *folder = media.URLByDeletingLastPathComponent;
            id firstFrame = nil;
            NSArray *frames = @[];
            if (!missingVideo) {
                AVURLAsset *asset = [AVURLAsset URLAssetWithURL:media options:nil];
                NSArray *times = JardAnnotationFrameTimes(events, CMTimeGetSeconds(asset.duration));
                frames = writeContactSheet(asset, [folder URLByAppendingPathComponent:@"contact-sheet.png"], times, &firstFrame);
            }
            NSMutableArray *serialized = [NSMutableArray arrayWithCapacity:events.count];
            for (NSDictionary *event in events) {
                if ([event[@"clear"] boolValue]) { [serialized addObject:event]; continue; }
                NSPoint from = [event[@"from"] pointValue], to = [event[@"to"] pointValue];
                NSMutableDictionary *edge = [@{@"time": event[@"time"], @"stroke": event[@"stroke"] ?: @0,
                    @"from": @[@(from.x), @(from.y)], @"to": @[@(to.x), @(to.y)]} mutableCopy];
                if (event[@"liftedAt"]) edge[@"liftedAt"] = event[@"liftedAt"];
                [serialized addObject:edge];
            }
            NSMutableDictionary *manifest = [@{@"version": @1, @"source": source, @"coordinateSpace": @"normalized-bottom-left",
                                               @"events": serialized, @"frames": frames} mutableCopy];
            if (origin.count) manifest[@"origin"] = origin;
            if (missingVideo) manifest[@"missingVideo"] = missingVideo;
            NSError *error = nil;
            NSData *json = [NSJSONSerialization dataWithJSONObject:manifest options:0 error:&error];
            if (json) [json writeToURL:[folder URLByAppendingPathComponent:@"annotation.json"] options:NSDataWritingAtomic error:&error];
            dispatch_async(dispatch_get_main_queue(), ^{
                NSImage *preview = firstFrame ? [[NSImage alloc] initWithCGImage:(__bridge CGImageRef)firstFrame size:NSZeroSize] : nil;
                completion(preview, error);
            });
        }
    });
}

static NSUInteger eventCount(NSArray<NSDictionary *> *events, NSTimeInterval time) {
    NSUInteger first = 0, end = events.count;
    while (first < end) {
        NSUInteger middle = first + (end - first) / 2;
        if ([events[middle][@"time"] doubleValue] <= time) first = middle + 1;
        else end = middle;
    }
    return first;
}

@interface JardRenderedStroke : NSObject
@property(nonatomic) NSUInteger firstEdge, endEdge;
@property(nonatomic) NSTimeInterval liftedAt, visibleUntil, previousVisibleUntil;
@end

@implementation JardRenderedStroke
@end

static NSArray<JardRenderedStroke *> *indexStrokes(NSArray<NSDictionary *> *events) {
    NSMutableArray<JardRenderedStroke *> *strokes = NSMutableArray.array;
    JardRenderedStroke *current = nil;
    NSNumber *identifier = nil;
    NSUInteger firstSinceClear = 0;
    for (NSUInteger index = 0; index < events.count; index++) {
        NSDictionary *event = events[index];
        if ([event[@"clear"] boolValue]) {
            NSTimeInterval clearedAt = [event[@"time"] doubleValue];
            for (NSUInteger i = firstSinceClear; i < strokes.count; i++)
                strokes[i].visibleUntil = MIN(strokes[i].visibleUntil, clearedAt);
            firstSinceClear = strokes.count;
            current = nil;
            continue;
        }
        NSNumber *next = event[@"stroke"] ?: @0;
        if (!current || ![identifier isEqual:next]) {
            current = JardRenderedStroke.new;
            current.firstEdge = index;
            current.liftedAt = current.visibleUntil = INFINITY;
            [strokes addObject:current];
            identifier = next;
        }
        current.endEdge = index + 1;
        if (event[@"liftedAt"]) {
            current.liftedAt = [event[@"liftedAt"] doubleValue];
            current.visibleUntil = current.liftedAt + JardAnnotationInkLifetime;
        }
    }
    NSTimeInterval visibleUntil = 0;
    for (JardRenderedStroke *stroke in strokes) {
        visibleUntil = MAX(visibleUntil, stroke.visibleUntil);
        stroke.previousVisibleUntil = visibleUntil;
    }
    return strokes;
}

static NSUInteger firstVisibleStroke(NSArray<JardRenderedStroke *> *strokes, NSTimeInterval time) {
    NSUInteger first = 0, end = strokes.count;
    while (first < end) {
        NSUInteger middle = first + (end - first) / 2;
        if (strokes[middle].previousVisibleUntil <= time) first = middle + 1;
        else end = middle;
    }
    return first;
}

static CGContextRef inkContext(size_t width, size_t height) {
    if (!width || !height) return NULL;
    CGColorSpaceRef space = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
    CGContextRef context = CGBitmapContextCreate(NULL, width, height, 8, 0, space, (CGBitmapInfo)kCGImageAlphaPremultipliedLast);
    CGColorSpaceRelease(space);
    if (context) {
        CGContextSetLineCap(context, kCGLineCapRound);
        CGContextSetLineWidth(context, MAX(3, MIN(width, height) * .007));
    }
    return context;
}

static CIImage *inkImage(NSArray<NSDictionary *> *events, NSArray<JardRenderedStroke *> *strokes,
                         NSUInteger count, NSTimeInterval time, CGRect frame, NSTimeInterval *validUntil, BOOL *failed) {
    *validUntil = INFINITY;
    *failed = NO;
    size_t width = CGRectGetWidth(frame), height = CGRectGetHeight(frame);
    CGContextRef context = NULL;
    for (NSUInteger i = firstVisibleStroke(strokes, time); i < strokes.count; i++) {
        JardRenderedStroke *stroke = strokes[i];
        if (stroke.firstEdge >= count) break;
        if (stroke.visibleUntil <= time) continue;
        NSTimeInterval fadeAt = stroke.liftedAt + JardAnnotationInkHold;
        *validUntil = MIN(*validUntil, MIN(fadeAt, stroke.visibleUntil));
        if (!context) {
            context = inkContext(width, height);
            if (!context) { *failed = YES; return nil; }
        }
        CGContextSetRGBStrokeColor(context, 1, .4, .22, JardAnnotationInkAlpha(time - stroke.liftedAt));
        NSUInteger end = MIN(stroke.endEdge, count);
        for (NSUInteger edge = stroke.firstEdge; edge < end; edge++) {
            NSPoint from = [events[edge][@"from"] pointValue], to = [events[edge][@"to"] pointValue];
            CGContextMoveToPoint(context, from.x * width, from.y * height);
            CGContextAddLineToPoint(context, to.x * width, to.y * height);
        }
        CGContextStrokePath(context);
    }
    if (!context) return nil;
    CGImageRef bitmap = CGBitmapContextCreateImage(context);
    CGContextRelease(context);
    if (!bitmap) { *failed = YES; return nil; }
    CIImage *image = [[CIImage alloc] initWithCGImage:bitmap];
    CGImageRelease(bitmap);
    return [image imageByApplyingTransform:CGAffineTransformMakeTranslation(frame.origin.x, frame.origin.y)];
}

static NSError *renderError(void) {
    return [NSError errorWithDomain:@"JardAnnotation" code:1 userInfo:
        @{NSLocalizedDescriptionKey: @"Could not render the annotation’s drawing layer."}];
}

static void renderAnnotation(NSURL *input, NSURL *output, NSArray<NSDictionary *> *events, NSArray<NSValue *> *ranges,
    void (^completion)(NSError *)) {
    AVURLAsset *asset = [AVURLAsset URLAssetWithURL:input options:nil];
    NSLock *cacheLock = NSLock.new;
    NSArray<JardRenderedStroke *> *strokes = indexStrokes(events);
    __block NSUInteger cachedCount = NSUIntegerMax;
    __block CGRect cachedSize = CGRectZero;
    __block NSTimeInterval cachedAt = 0, cachedUntil = 0;
    __block BOOL cachedFailure = NO;
    __block CIImage *cachedInk;
    [AVMutableVideoComposition videoCompositionWithAsset:asset applyingCIFiltersWithHandler:^(AVAsynchronousCIImageFilteringRequest *request) {
        @autoreleasepool {
            CIImage *video = request.sourceImage;
            if (ranges) {
                BOOL draw = NO;
                for (NSValue *value in ranges) {
                    if (CMTimeRangeContainsTime(value.CMTimeRangeValue, request.compositionTime)) { draw = YES; break; }
                }
                if (!draw) { [request finishWithImage:video context:nil]; return; }
            }
            NSTimeInterval time = CMTimeGetSeconds(request.compositionTime);
            NSUInteger count = eventCount(events, time);
            [cacheLock lock];
            if (count != cachedCount || time < cachedAt || time >= cachedUntil || !CGRectEqualToRect(cachedSize, video.extent)) {
                cachedCount = count;
                cachedSize = video.extent;
                cachedAt = time;
                cachedInk = inkImage(events, strokes, count, time, video.extent, &cachedUntil, &cachedFailure);
            }
            CIImage *ink = cachedInk;
            BOOL failed = cachedFailure;
            [cacheLock unlock];
            if (failed) {
                [request finishWithError:renderError()];
                return;
            }
            [request finishWithImage:ink ? [ink imageByCompositingOverImage:video] : video context:nil];
        }
    } completionHandler:^(AVMutableVideoComposition *composition, NSError *error) {
        if (!composition) { completion(error ?: renderError()); return; }
        AVAssetExportSession *exporter = [[AVAssetExportSession alloc] initWithAsset:asset presetName:AVAssetExportPresetHighestQuality];
        if (!exporter) { completion(renderError()); return; }
        exporter.videoComposition = composition;
        exporter.outputURL = output;
        exporter.outputFileType = AVFileTypeMPEG4;
        [exporter exportAsynchronouslyWithCompletionHandler:^{
            completion(exporter.status == AVAssetExportSessionStatusCompleted ? nil : exporter.error ?: renderError());
        }];
    }];
}

void JardRenderAnnotation(NSURL *input, NSURL *output, NSArray<NSDictionary *> *events, void (^completion)(NSError *)) {
    JardRenderAnnotationInRanges(input, output, events, nil, completion);
}

void JardRenderAnnotationInRanges(NSURL *input, NSURL *output, NSArray<NSDictionary *> *events, NSArray<NSValue *> *ranges,
    void (^completion)(NSError *)) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        renderAnnotation(input, output, events, ranges, completion);
    });
}
