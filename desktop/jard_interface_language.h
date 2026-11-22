#import <Foundation/Foundation.h>

extern NSString *const JardInterfaceLanguagePreferenceKey;
NSString *JardInterfaceLanguageChoice(id choice);
NSString *JardResolveInterfaceLanguage(NSString *tag, NSDictionary *choices);
NSDictionary *JardInterfaceLanguageStatus(void);
BOOL JardSetInterfaceLanguage(id choice);
NSString *JardLocalized(NSString *source);
