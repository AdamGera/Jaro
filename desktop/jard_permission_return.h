#pragma once
#import <Foundation/Foundation.h>

static const NSTimeInterval JardPermissionReturnGrace = 3;
static NSString *const JardSystemSettingsBundle = @"com.apple.systempreferences";

static inline BOOL JardTakePermissionReturn(NSTimeInterval *deadline, NSTimeInterval now,
                                             BOOL settingsOpen, BOOL settingsFront, BOOL appActive) {
    if (!*deadline) return NO;
    if (!settingsOpen || appActive || now > *deadline) {
        *deadline = 0;
        return NO;
    }
    if (!settingsFront) return NO;
    *deadline = 0;
    return YES;
}
