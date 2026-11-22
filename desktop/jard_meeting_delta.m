#import "jard_meeting_delta.h"
#import <math.h>

// Absorb a short pause between already delivered turns on the same source.
static const NSTimeInterval JoinCoverage = 1.5;
static const NSTimeInterval JoinText = 3.0;

static BOOL timed(NSDictionary *turn) {
    return turn[@"start"] != NSNull.null && turn[@"end"] != NSNull.null;
}

static NSNumber *speaker(NSDictionary *turn) {
    return turn[@"speaker"] == NSNull.null ? @0 : turn[@"speaker"];
}

static BOOL containsTime(NSArray<NSArray<NSNumber *> *> *spans, NSTimeInterval time) {
    NSUInteger first = 0, end = spans.count;
    while (first < end) {
        NSUInteger center = first + (end - first) / 2;
        if (spans[center][1].doubleValue < time) first = center + 1;
        else end = center;
    }
    return first < spans.count && spans[first][0].doubleValue <= time;
}

NSArray<NSDictionary *> *JardMeetingTurns(id rows) {
    if (![rows isKindOfClass:NSArray.class]) return @[];
    NSMutableArray<NSDictionary *> *turns = NSMutableArray.array;
    for (id row in rows) {
        if (![row isKindOfClass:NSDictionary.class]) continue;
        id text = row[@"text"], source = row[@"speaker"], start = row[@"start"], end = row[@"end"];
        if (![text isKindOfClass:NSString.class]) continue;
        NSString *trimmed = [text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (!trimmed.length) continue;
        BOOL aligned = [start isKindOfClass:NSNumber.class] && [end isKindOfClass:NSNumber.class]
            && isfinite([start doubleValue]) && isfinite([end doubleValue])
            && [start doubleValue] >= 0 && [end doubleValue] >= [start doubleValue];
        [turns addObject:@{@"text": trimmed, @"speaker": [source isKindOfClass:NSNumber.class] ? source : NSNull.null,
            @"start": aligned ? start : NSNull.null, @"end": aligned ? end : NSNull.null}];
    }
    return turns;
}

static NSArray *orderedTurns(NSArray *turns) {
    return [turns sortedArrayWithOptions:NSSortStable usingComparator:^NSComparisonResult(NSDictionary *a, NSDictionary *b) {
        if (timed(a) && timed(b)) return [a[@"start"] compare:b[@"start"]];
        if (timed(a)) return NSOrderedAscending;
        if (timed(b)) return NSOrderedDescending;
        return NSOrderedSame;
    }];
}

NSString *JardMeetingExcerptText(NSArray<NSDictionary *> *turns, NSDictionary *labels) {
    NSMutableArray<NSMutableString *> *paragraphs = NSMutableArray.array;
    NSNumber *priorSource = nil;
    NSTimeInterval priorEnd = 0;
    BOOL priorTimed = NO;
    for (NSDictionary *turn in orderedTurns(turns)) {
        NSNumber *source = speaker(turn);
        BOOL same = [priorSource isEqual:source];
        BOOL hasTime = timed(turn);
        if (same && ((!hasTime && !priorTimed) ||
            (hasTime && priorTimed && [turn[@"start"] doubleValue] - priorEnd <= JoinText))) {
            [paragraphs.lastObject appendFormat:@" %@", turn[@"text"]];
            priorEnd = hasTime ? MAX(priorEnd, [turn[@"end"] doubleValue]) : 0;
        } else {
            NSString *label = labels[source.stringValue];
            if (![label isKindOfClass:NSString.class] || !label.length) {
                label = source.intValue > 0 ? [@"Speaker " stringByAppendingString:source.stringValue] : nil;
            }
            NSString *line = label ? [NSString stringWithFormat:@"%@: %@", label, turn[@"text"]] : turn[@"text"];
            [paragraphs addObject:line.mutableCopy];
            priorEnd = hasTime ? [turn[@"end"] doubleValue] : 0;
        }
        priorSource = source;
        priorTimed = hasTime;
    }
    return [paragraphs componentsJoinedByString:@"\n\n"];
}

@implementation JardMeetingDelta {
    NSString *_identifier;
    NSMutableDictionary<NSNumber *, NSMutableArray<NSArray<NSNumber *> *> *> *_spans;
    NSMutableSet<NSArray *> *_untimed;
}

- (instancetype)init {
    if ((self = [super init])) {
        _spans = NSMutableDictionary.dictionary;
        _untimed = NSMutableSet.set;
    }
    return self;
}

- (NSArray<NSDictionary *> *)unseenTurns:(NSArray<NSDictionary *> *)turns meeting:(NSString *)identifier {
    if (!identifier || ![_identifier isEqual:identifier]) return turns;
    NSMutableArray *unseen = NSMutableArray.array;
    for (NSDictionary *turn in turns) {
        NSNumber *source = speaker(turn);
        BOOL covered = NO;
        if (timed(turn)) {
            NSTimeInterval start = [turn[@"start"] doubleValue], end = [turn[@"end"] doubleValue];
            covered = containsTime(_spans[source], start + (end - start) / 2);
        } else {
            covered = [_untimed containsObject:@[source, turn[@"text"]]];
        }
        if (!covered) [unseen addObject:turn];
    }
    return unseen;
}

- (void)deliveredTurns:(NSArray<NSDictionary *> *)turns meeting:(NSString *)identifier {
    if (!identifier || !turns.count) return;
    if (![_identifier isEqual:identifier]) {
        _identifier = identifier.copy;
        [_spans removeAllObjects];
        [_untimed removeAllObjects];
    }
    NSMutableSet<NSNumber *> *updated = NSMutableSet.set;
    for (NSDictionary *turn in turns) {
        NSNumber *source = speaker(turn);
        if (timed(turn)) {
            if (!_spans[source]) _spans[source] = NSMutableArray.array;
            [_spans[source] addObject:@[turn[@"start"], turn[@"end"]]];
            [updated addObject:source];
        } else {
            [_untimed addObject:@[source, turn[@"text"]]];
        }
    }
    for (NSNumber *source in updated) {
        [_spans[source] sortUsingComparator:^NSComparisonResult(NSArray *a, NSArray *b) { return [a[0] compare:b[0]]; }];
        NSMutableArray<NSArray<NSNumber *> *> *merged = NSMutableArray.array;
        for (NSArray<NSNumber *> *span in _spans[source]) {
            NSArray<NSNumber *> *last = merged.lastObject;
            if (last && span[0].doubleValue <= last[1].doubleValue + JoinCoverage) {
                merged[merged.count - 1] = @[last[0], @(MAX(last[1].doubleValue, span[1].doubleValue))];
            } else {
                [merged addObject:span];
            }
        }
        _spans[source] = merged;
    }
}

- (void)replaceMeeting:(NSString *)before with:(NSString *)after {
    if (before && after && [_identifier isEqual:before]) _identifier = after.copy;
}

@end
