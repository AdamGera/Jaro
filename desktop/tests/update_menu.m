#import <AppKit/AppKit.h>
#import <WebKit/WebKit.h>

static WKWebView *fixtureWebView(NSView *view, NSNumber *port) {
    if ([view isKindOfClass:WKWebView.class]) {
        WKWebView *web = (WKWebView *)view;
        return [web.URL.host isEqual:@"127.0.0.1"] && [web.URL.port isEqual:port] ? web : nil;
    }
    for (NSView *child in view.subviews) {
        WKWebView *web = fixtureWebView(child, port);
        if (web) return web;
    }
    return nil;
}

@interface JardUpdateMenuFixture : NSObject
@end

@implementation JardUpdateMenuFixture

+ (void)load {
    NSString *library = NSProcessInfo.processInfo.environment[@"RUMBLE_DATA_DIR"];
    if (!library.length || ![NSBundle.mainBundle.bundleIdentifier hasPrefix:@"local.jardaudio.cli-fixture."]) return;
    NSURL *root = [NSURL fileURLWithPath:library isDirectory:YES];
    NSURL *request = [root URLByAppendingPathComponent:@".update-menu-fixture"];
    NSURL *acknowledged = [root URLByAppendingPathComponent:@".update-menu-invoked"];
    NSNumber *port = @([NSProcessInfo.processInfo.environment[@"RUMBLE_PORT"] integerValue]);
    dispatch_async(dispatch_get_main_queue(), ^{
        __block BOOL evaluating = NO;
        NSTimer *poll = [NSTimer timerWithTimeInterval:.05 repeats:YES block:^(NSTimer *timer) {
            NSString *operation = [NSString stringWithContentsOfURL:request encoding:NSUTF8StringEncoding error:nil];
            BOOL reopenSetup = [operation isEqual:@"setup-reopen"];
            BOOL dismissSetup = [operation isEqual:@"setup-dismiss"];
            BOOL sidebarRestart = [operation isEqual:@"sidebar-restart"];
            BOOL settingsRestart = [operation isEqual:@"settings-restart"];
            BOOL guard = [operation isEqual:@"restart-guard"];
            if (reopenSetup || dismissSetup || sidebarRestart || settingsRestart || guard || [operation isEqual:@"setup-status"]) {
                if (evaluating) return;
                for (NSWindow *window in NSApp.windows) {
                    WKWebView *web = fixtureWebView(window.contentView, port);
                    if (!web) continue;
                    evaluating = YES;
                    NSString *script;
                    NSDictionary *arguments = nil;
                    if (reopenSetup) {
                        script = @"const desktop = window.jardDesktop; if (!desktop) return null;\n"
                                  "desktop.request('systemDictation', {action:'set', key:'onboarding', value:'access'})\n"
                                  "  .then(() => desktop.request('systemDictation', {action:'reopen'}));\n"
                                  "return 'Reopen Jard';";
                    } else if (dismissSetup) {
                        script = @"if (!window.jardDesktop) return null;\n"
                                  "const status = await jardDesktop.request('systemDictation', {action:'set', key:'onboarding', value:'later'});\n"
                                  "return status.onboarding;";
                    } else if (sidebarRestart || settingsRestart || guard) {
                        arguments = @{@"selector": settingsRestart ? @"#desktop-app-updates button" : @".sidebar-update button.update-ready",
                                      @"guard": @(guard), @"settings": @(settingsRestart)};
                        script = @"if (document.querySelector('[aria-modal=true]')) return null;\n"
                                  "if (settings && !document.querySelector('.workspace-settings[data-pane=settings]')) {\n"
                                  "  document.querySelector('.sidebar-settings')?.click(); return null;\n"
                                  "}\n"
                                  "const button = document.querySelector(selector);\n"
                                  "if (!button || button.disabled || !button.getClientRects().length) return null;\n"
                                  "const label = button.textContent.trim();\n"
                                  "if (label !== 'Restart to Update') return null;\n"
                                  "button.click();\n"
                                  "if (!guard) return label;\n"
                                  "const expected = 'Close other Jard windows or browser tabs before restarting.';\n"
                                  "for (let attempt = 0; attempt < 200; attempt++) {\n"
                                  "  const feedback = document.querySelector('.sidebar-update small')?.textContent.trim();\n"
                                  "  if (feedback === expected) return feedback;\n"
                                  "  await new Promise(resolve => setTimeout(resolve, 50));\n"
                                  "}\n"
                                  "return 'The sidebar did not display the connected-browser guard.';";
                    } else {
                        script = @"const desktop = window.jardDesktop; if (!desktop) return null;\n"
                                  "const status = await desktop.request('systemDictation', {action:'status'});\n"
                                  "return status.onboarding;";
                    }
                    [web callAsyncJavaScript:script arguments:arguments inFrame:nil inContentWorld:WKContentWorld.pageWorld
                            completionHandler:^(id value, NSError *error) {
                        evaluating = NO;
                        if (![value isKindOfClass:NSString.class]) return;
                        [NSFileManager.defaultManager removeItemAtURL:request error:nil];
                        [value writeToURL:acknowledged atomically:YES encoding:NSUTF8StringEncoding error:nil];
                    }];
                    return;
                }
                return;
            }
            BOOL quit = [operation isEqual:@"quit"];
            BOOL restartThenQuit = [operation isEqual:@"restart-then-quit"];
            if (!quit && !restartThenQuit && ![operation isEqual:@"restart"]) return;
            NSMenu *menu = NSApp.mainMenu.itemArray.firstObject.submenu;
            if (!menu) return;
            [menu update];
            SEL action = NSSelectorFromString(quit ? @"quitApp:" : @"updateFromMenu:");
            NSUInteger index = [menu.itemArray indexOfObjectPassingTest:^BOOL(NSMenuItem *item, NSUInteger i, BOOL *stop) {
                return item.action == action;
            }];
            if (index == NSNotFound) return;
            NSMenuItem *item = [menu itemAtIndex:index];
            if (!item.enabled || (!quit && ![item.title isEqual:@"Restart to Update"])) return;
            NSUInteger quitIndex = NSNotFound;
            if (restartThenQuit) {
                quitIndex = [menu.itemArray indexOfObjectPassingTest:^BOOL(NSMenuItem *candidate, NSUInteger i, BOOL *stop) {
                    return candidate.action == NSSelectorFromString(@"quitApp:");
                }];
                if (quitIndex == NSNotFound) return;
            }
            if (![NSFileManager.defaultManager removeItemAtURL:request error:nil]) return;
            [item.title writeToURL:acknowledged atomically:YES encoding:NSUTF8StringEncoding error:nil];
            [menu performActionForItemAtIndex:index];
            // The main queue cannot finish the final network check between these actions.
            if (restartThenQuit) [menu performActionForItemAtIndex:quitIndex];
        }];
        [NSRunLoop.mainRunLoop addTimer:poll forMode:NSRunLoopCommonModes];
    });
}

@end
