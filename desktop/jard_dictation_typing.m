#import "jard_dictation_typing.h"
#include <string.h>

static const NSUInteger unicodeLimit = 20;

static BOOL affectedChromium(NSURL *bundle) {
    if (!bundle.isFileURL) return NO;
    NSString *path = bundle.path;
    if (!path.length) return NO;
    static NSRegularExpression *version;
    static NSCache<NSString *, NSNumber *> *cache;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        version = [NSRegularExpression regularExpressionWithPattern:
            @"^15[56]\\.[0-9]+\\.[0-9]+\\.[0-9]+$" options:0 error:nil];
        cache = NSCache.new;
        cache.countLimit = 256;
    });
    NSNumber *cached = [cache objectForKey:path];
    if (cached) return cached.boolValue;
    BOOL affected = NO;
    NSFileManager *manager = NSFileManager.defaultManager;
    NSURL *folder = [bundle URLByAppendingPathComponent:@"Contents/Frameworks" isDirectory:YES];
    for (NSURL *framework in [manager contentsOfDirectoryAtURL:folder includingPropertiesForKeys:nil options:0 error:nil]) {
        if (![framework.lastPathComponent hasSuffix:@"Framework.framework"]) continue;
        NSURL *versions = [framework URLByAppendingPathComponent:@"Versions" isDirectory:YES];
        for (NSString *name in [manager contentsOfDirectoryAtPath:versions.path error:nil]) {
            if ([version numberOfMatchesInString:name options:0 range:NSMakeRange(0, name.length)]) {
                affected = YES;
                break;
            }
        }
        if (affected) break;
    }
    [cache setObject:@(affected) forKey:path];
    return affected;
}

JardInsertionStyle JardInsertionForApp(NSString *mode, NSString *bundleIdentifier, NSURL *bundleURL) {
    NSString *app = bundleIdentifier.lowercaseString;
    if ([app isEqual:@"com.microsoft.rdc.macos"] || [app isEqual:@"com.microsoft.windowsapp"]) return JardInsertPaced;
    if ([mode isEqual:@"type"] || affectedChromium(bundleURL)) return JardInsertBurst;
    return JardInsertPaste;
}

static const UCKeyboardLayout *keyLayout(TISInputSourceRef source) {
    CFDataRef data = source ? TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) : NULL;
    return data ? (const UCKeyboardLayout *)CFDataGetBytePtr(data) : NULL;
}

static NSInteger letterIndex(unichar character) {
    if (character >= 'A' && character <= 'Z') return character - 'A';
    if (character >= 'a' && character <= 'z') return character - 'a';
    return -1;
}

static NSInteger letterAtKey(const UCKeyboardLayout *layout, CGKeyCode code, UInt32 modifiers, UInt32 keyboardType) {
    UInt32 dead = 0;
    UniCharCount length = 0;
    UniChar characters[4];
    if (UCKeyTranslate(layout, code, kUCKeyActionDown, modifiers, keyboardType,
        kUCKeyTranslateNoDeadKeysMask, &dead, 4, &length, characters) != noErr || length != 1) return -1;
    return letterIndex(characters[0]);
}

static void fillLetterKeys(int16_t keys[26], const UCKeyboardLayout *layout, UInt32 modifiers, UInt32 keyboardType) {
    for (CGKeyCode code = 0; layout && code < 128; code++) {
        NSInteger index = letterAtKey(layout, code, modifiers, keyboardType);
        if (index >= 0 && keys[index] < 0) keys[index] = code;
    }
}

JardShortcutLayout JardShortcutLayoutForSources(TISInputSourceRef active, TISInputSourceRef asciiCapable) {
    JardShortcutLayout keys;
    memset(&keys, -1, sizeof keys);
    UInt32 keyboardType = LMGetKbdType();
    const UCKeyboardLayout *layout = keyLayout(active);
    fillLetterKeys(keys.command, layout, cmdKey >> 8, keyboardType);
    fillLetterKeys(keys.plain, layout, 0, keyboardType);
    fillLetterKeys(keys.plain, keyLayout(asciiCapable), 0, keyboardType);
    return keys;
}

JardShortcutLayout JardCurrentShortcutLayout(void) {
    TISInputSourceRef active = TISCopyCurrentKeyboardLayoutInputSource();
    TISInputSourceRef asciiCapable = TISCopyCurrentASCIICapableKeyboardLayoutInputSource();
    JardShortcutLayout layout = JardShortcutLayoutForSources(active, asciiCapable);
    if (active) CFRelease(active);
    if (asciiCapable) CFRelease(asciiCapable);
    return layout;
}

NSInteger JardShortcutLetterCode(const JardShortcutLayout *layout, unichar letter, BOOL command, NSInteger recorded) {
    NSInteger index = letterIndex(letter);
    if (!layout || index < 0) return recorded;
    NSInteger code = command && layout->command[index] >= 0 ? layout->command[index] : layout->plain[index];
    return code >= 0 ? code : recorded;
}

CGKeyCode JardPasteKeyCodeForSources(TISInputSourceRef active, TISInputSourceRef asciiCapable) {
    JardShortcutLayout layout = JardShortcutLayoutForSources(active, asciiCapable);
    return (CGKeyCode)JardShortcutLetterCode(&layout, 'v', YES, kVK_ANSI_V);
}

CGKeyCode JardCurrentPasteKeyCode(void) {
    JardShortcutLayout layout = JardCurrentShortcutLayout();
    return (CGKeyCode)JardShortcutLetterCode(&layout, 'v', YES, kVK_ANSI_V);
}

NSDictionary<NSString *, NSDictionary *> *JardTypingLayout(void) {
    TISInputSourceRef source = TISCopyCurrentKeyboardLayoutInputSource();
    if (!source) return @{};
    const UCKeyboardLayout *layout = keyLayout(source);
    NSMutableDictionary *keys = NSMutableDictionary.dictionary;
    // Unshifted keys take precedence if a layout exposes a character twice.
    for (NSUInteger shifted = 0; shifted < 2; shifted++) {
        for (CGKeyCode code = 0; layout && code < 128; code++) {
            UInt32 dead = 0;
            UniCharCount count = 0;
            UniChar chars[4] = {0};
            if (UCKeyTranslate(layout, code, kUCKeyActionDown, shifted ? shiftKey >> 8 : 0, LMGetKbdType(),
                kUCKeyTranslateNoDeadKeysMask, &dead, 4, &count, chars) != noErr) continue;
            if (!((count == 1 && chars[0] >= ' ' && chars[0] != 0x7f &&
                (chars[0] < 0xf700 || chars[0] > 0xf8ff)) ||
                (count == 2 && CFStringIsSurrogateHighCharacter(chars[0]) && CFStringIsSurrogateLowCharacter(chars[1])))) continue;
            NSString *character = [NSString stringWithCharacters:chars length:count];
            if (!keys[character]) keys[character] = @{@"code": @(code), @"shift": @(shifted)};
        }
    }
    CFRelease(source);
    return keys;
}

static NSUInteger scalarLength(NSString *text, NSUInteger offset) {
    return offset + 1 < text.length && CFStringIsSurrogateHighCharacter([text characterAtIndex:offset]) &&
        CFStringIsSurrogateLowCharacter([text characterAtIndex:offset + 1]) ? 2 : 1;
}

static BOOL lineBreak(unichar unit) { return unit == '\r' || unit == '\n'; }

NSArray *JardTypingEvents(NSString *text, NSUInteger *cursor, JardInsertionStyle style, NSDictionary *layout, int64_t marker) {
    NSUInteger start = *cursor, limit = text.length;
    if (start >= limit) return nil;
    unichar first = [text characterAtIndex:start];
    NSString *characters = nil;
    CGKeyCode key = 0;
    BOOL shift = NO;
    NSUInteger end = start + 1;
    if (lineBreak(first)) {
        if (first == '\r' && end < limit && [text characterAtIndex:end] == '\n') end++;
        key = kVK_Return;
        shift = YES;
    } else {
        end = start + scalarLength(text, start);
        if (style == JardInsertBurst) {
            while (end < limit && !lineBreak([text characterAtIndex:end])) {
                NSUInteger step = scalarLength(text, end);
                if (end + step - start > unicodeLimit) break;
                end += step;
            }
        }
        characters = [text substringWithRange:NSMakeRange(start, end - start)];
        NSDictionary *physical = layout[characters];
        key = [physical[@"code"] unsignedShortValue];
        shift = [physical[@"shift"] boolValue];
    }
    NSMutableArray *events = [NSMutableArray arrayWithCapacity:shift ? 4 : 2];
    for (NSUInteger i = 0; i < (shift ? 4 : 2); i++) {
        BOOL modifier = shift && (i == 0 || i == 3);
        BOOL pressed = shift ? i < 2 : i == 0;
        CGEventRef event = CGEventCreateKeyboardEvent(NULL, modifier ? kVK_Shift : key, pressed);
        if (!event) return nil;
        if (modifier) CGEventSetType(event, kCGEventFlagsChanged);
        CGEventSetFlags(event, shift && i != 3 ? kCGEventFlagMaskShift : 0);
        CGEventSetIntegerValueField(event, kCGEventSourceUserData, marker);
        if (!modifier && characters.length) {
            UniChar buffer[20];
            [characters getCharacters:buffer range:NSMakeRange(0, characters.length)];
            CGEventKeyboardSetUnicodeString(event, characters.length, buffer);
        }
        [events addObject:CFBridgingRelease(event)];
    }
    *cursor = end;
    return events;
}
