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

static NSString *draftFixtureScript(BOOL verify) {
    NSString *helpers = @"async function until(test) {\n"
        "  for (let count = 0; count < 200; count++) {\n"
        "    const value = test(); if (value) return value;\n"
        "    await new Promise(resolve => setTimeout(resolve, 50));\n"
        "  }\n"
        "  throw new Error('Expected workspace control did not appear: ' + test);\n"
        "}\n"
        "const find = label => document.querySelector('[aria-label=\"' + label + '\"]');\n"
        "const button = title => [...document.querySelectorAll('button')].find(node => node.textContent.trim() === title);\n"
        "async function selectTab(name) {\n"
        "  const node = await until(() => [...document.querySelectorAll('[role=tab]')].find(tab => tab.textContent.trim() === name));\n"
        "  node.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }));\n"
        "}\n"
        "const draft = 'jard.overviewDraft.' + id + '.en';\n"
        "const notes = 'jard.personalNotes.' + id;\n"
        "function type(editor, text) {\n"
        "  editor.focus();\n"
        "  const selection = getSelection(), range = document.createRange();\n"
        "  range.selectNodeContents(editor); selection.removeAllRanges(); selection.addRange(range);\n"
        "  if (!document.execCommand('insertText', false, text)) throw new Error('Could not enter fixture text');\n"
        "}\n"
        "if (!window.jardDesktop || !document.querySelector('.sidebar-settings')) return null;\n";
    NSString *operation = verify ?
        @"const route = new URL(location.href).searchParams;\n"
         "if (route.get('session') !== id || route.get('tab') !== 'notes') throw new Error('The session and notes route did not reopen');\n"
         "await until(() => find('My notes')?.textContent.includes(note));\n"
         "await until(() => !localStorage.getItem(notes));\n"
         "await selectTab('Overview');\n"
         "(await until(() => find('Resume overview draft'))).click();\n"
         "await until(() => find('Edit overview')?.textContent.includes(overview));\n"
         "(await until(() => button('Save changes'))).click();\n"
         "await until(() => !localStorage.getItem(draft) && document.querySelector('.overview-copy')?.textContent.includes(overview));\n"
         "return 'Both workspace drafts recovered';\n" :
        @"history.pushState(null, '', '/?session=' + id + '&tab=overview');\n"
         "window.dispatchEvent(new PopStateEvent('popstate'));\n"
         "(await until(() => find('Edit overview'))).click();\n"
         "type(await until(() => document.querySelector('[contenteditable=true][aria-label=\"Edit overview\"]')), overview);\n"
         "await until(() => localStorage.getItem(draft)?.includes(overview));\n"
         "await selectTab('My Notes');\n"
         "const realFetch = window.fetch.bind(window);\n"
         "window.fetch = (input, init) => init?.method === 'PUT' && input === '/api/sessions/' + id + '/personal-notes'\n"
         "  ? Promise.resolve(new Response('', { status: 503 })) : realFetch(input, init);\n"
         "type(await until(() => document.querySelector('[contenteditable=true][aria-label=\"My notes\"]')), note);\n"
         "await until(() => button('Retry save') && localStorage.getItem(notes)?.includes(note));\n"
         "return 'Both workspace drafts created';\n";
    return [NSString stringWithFormat:@"%@try {\n%@} catch (error) { return String(error); }", helpers, operation];
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
            BOOL rollbackNotice = [operation isEqual:@"rollback-notice"];
            BOOL verifyDrafts = [operation isEqual:@"workspace-drafts-verify"];
            BOOL prepareDrafts = [operation isEqual:@"workspace-drafts-prepare"];
            if (reopenSetup || dismissSetup || sidebarRestart || settingsRestart || guard || rollbackNotice || prepareDrafts || verifyDrafts ||
                [operation isEqual:@"setup-status"]) {
                if (evaluating) return;
                for (NSWindow *window in NSApp.windows) {
                    WKWebView *web = fixtureWebView(window.contentView, port);
                    if (!web) continue;
                    evaluating = YES;
                    NSString *script;
                    NSDictionary *arguments = nil;
                    if (prepareDrafts || verifyDrafts) {
                        NSData *json = [NSData dataWithContentsOfURL:[root URLByAppendingPathComponent:@".update-menu-drafts.json"]];
                        arguments = json ? [NSJSONSerialization JSONObjectWithData:json options:0 error:nil] : nil;
                        if (![arguments isKindOfClass:NSDictionary.class]) { evaluating = NO; return; }
                        script = draftFixtureScript(verifyDrafts);
                    } else if (reopenSetup) {
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
                                  "const expected = /^Close other Jar[do] windows or browser tabs before restarting[.]$/;\n"
                                  "for (let attempt = 0; attempt < 200; attempt++) {\n"
                                  "  const feedback = document.querySelector('.sidebar-update small')?.textContent.trim();\n"
                                  "  if (expected.test(feedback)) return feedback.replace('Jard windows', 'Jaro windows');\n"
                                  "  await new Promise(resolve => setTimeout(resolve, 50));\n"
                                  "}\n"
                                  "return 'The sidebar did not display the connected-browser guard.';";
                    } else if (rollbackNotice) {
                        script = @"const feedback = document.querySelector('.sidebar-update small')?.textContent.trim();\n"
                                  "return feedback?.includes('restored the previous version') ? feedback : null;";
                    } else {
                        script = @"const desktop = window.jardDesktop; if (!desktop) return null;\n"
                                  "const status = await desktop.request('systemDictation', {action:'status'});\n"
                                  "return status.onboarding;";
                    }
                    [web callAsyncJavaScript:script arguments:arguments inFrame:nil inContentWorld:WKContentWorld.pageWorld
                            completionHandler:^(id value, NSError *error) {
                        evaluating = NO;
                        if (error && (prepareDrafts || verifyDrafts)) value = error.localizedDescription;
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
