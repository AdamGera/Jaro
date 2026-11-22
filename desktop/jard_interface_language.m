#import "jard_interface_language.h"

NSString *const JardInterfaceLanguagePreferenceKey = @"JardInterfaceLanguage";

static NSDictionary *catalog(void) {
    static NSDictionary *value;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        NSURL *file = [NSBundle.mainBundle URLForResource:@"interface-languages" withExtension:@"json"];
        NSData *data = file ? [NSData dataWithContentsOfURL:file] : nil;
        id decoded = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        value = [decoded isKindOfClass:NSDictionary.class] ? decoded : @{@"choices": @{@"en": @"English"}, @"messages": @{}};
    });
    return value;
}

NSString *JardResolveInterfaceLanguage(NSString *tag, NSDictionary *choices) {
    NSString *locale = [[tag.lowercaseString componentsSeparatedByCharactersInSet:
        [NSCharacterSet characterSetWithCharactersInString:@".@"]] firstObject];
    NSArray<NSString *> *parts = [[locale stringByReplacingOccurrencesOfString:@"_" withString:@"-"] componentsSeparatedByString:@"-"];
    NSString *language = parts.firstObject ?: @"en";
    if ([language isEqual:@"zh"]) {
        BOOL explicitSimplified = [parts containsObject:@"hans"];
        BOOL traditional = [parts containsObject:@"hant"] || [parts containsObject:@"tw"] ||
            [parts containsObject:@"hk"] || [parts containsObject:@"mo"];
        if (!explicitSimplified && traditional && choices[@"zh-Hant"]) return @"zh-Hant";
    }
    return choices[language] ? language : @"en";
}

NSString *JardInterfaceLanguageChoice(id choice) {
    if (choice == NSNull.null) return @"system";
    if (![choice isKindOfClass:NSString.class]) return nil;
    NSString *code = [[choice stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet] lowercaseString];
    if ([@[@"system", @"none"] containsObject:code]) return @"system";
    for (NSString *supported in catalog()[@"choices"]) {
        if ([supported.lowercaseString isEqual:code]) return supported;
    }
    return nil;
}

static NSString *selectedLanguage(void) {
    id stored = [NSUserDefaults.standardUserDefaults objectForKey:JardInterfaceLanguagePreferenceKey];
    return [stored isKindOfClass:NSString.class] && catalog()[@"choices"][stored] ? stored : @"system";
}

static NSString *resolvedLanguage(NSString *selected, NSDictionary *choices) {
    return [selected isEqual:@"system"] ? JardResolveInterfaceLanguage(NSLocale.preferredLanguages.firstObject, choices) : selected;
}

NSDictionary *JardInterfaceLanguageStatus(void) {
    NSDictionary *data = catalog();
    NSDictionary *choices = data[@"choices"];
    NSString *selected = selectedLanguage(), *resolved = resolvedLanguage(selected, choices);
    return @{@"selected": selected, @"resolved": resolved, @"choices": choices, @"messages": data[@"messages"][resolved] ?: @{}};
}

BOOL JardSetInterfaceLanguage(id choice) {
    NSString *selected = JardInterfaceLanguageChoice(choice);
    if (!selected) return NO;
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    if ([selected isEqual:@"system"]) [defaults removeObjectForKey:JardInterfaceLanguagePreferenceKey];
    else [defaults setObject:selected forKey:JardInterfaceLanguagePreferenceKey];
    return YES;
}

NSString *JardLocalized(NSString *source) {
    if (!source.length) return source;
    NSDictionary *data = catalog();
    NSDictionary *choices = data[@"choices"];
    return data[@"messages"][resolvedLanguage(selectedLanguage(), choices)][source] ?: source;
}
