#import "jard_updater.h"
#import "appkit_host.h"
#import <CommonCrypto/CommonDigest.h>
#import <Security/Security.h>
#import <errno.h>
#import <fcntl.h>
#import <stdio.h>
#import <sys/file.h>
#import <sys/stat.h>
#import <unistd.h>

#define UTF8_BYTES(text) (text).UTF8String, [(text) lengthOfBytesUsingEncoding:NSUTF8StringEncoding]

extern NSInteger JardVerifyRolloutFeed(const void *bytes, NSInteger length, const char *publicKey,
    const char *channel, uint8_t *legacy, NSInteger capacity, uint8_t *percentage, uint8_t *required);

static NSString *const pendingKey = @"JardPendingUpdate";
static NSString *const cleanupKey = @"JardInstalledUpdateBackup";
static NSString *const rejectedKey = @"JardRejectedUpdate";
static NSString *const channelKey = @"JardUpdateChannel";
static NSString *const rolloutBucketKey = @"JardUpdateRolloutBucket";
static NSString *const stagePrefix = @".jard-update-";
static const char startupReceipt[] = ".startup-ready";
static const int64_t feedLimit = 64 * 1024;
static const int64_t rolloutFeedLimit = 128 * 1024;
static const uint64_t archiveLimit = 4ULL * 1024 * 1024 * 1024;
static const NSTimeInterval restartFeedTimeout = 15;
static const NSTimeInterval feedTimeout = 60;
static const NSTimeInterval developmentCheckInterval = 600;
static const NSTimeInterval stableCheckInterval = 3600;
static const NSTimeInterval focusCheckInterval = 60;
static const NSTimeInterval archiveTimeout = 1200;
static const NSTimeInterval abandonedStageGrace = 300;
static const uint64_t archivePiece = 8ULL * 1024 * 1024;
static const NSUInteger archiveAttempts = 3;

static NSString *configuredChannel(NSDictionary *settings) {
    id channel = settings[@"channel"];
    if (!channel || [channel isEqual:@"stable"]) return @"stable";
    return [channel isEqual:@"dev"] ? @"dev" : nil;
}

static NSDictionary *updateBundleInfo(NSURL *app) {
    return [NSDictionary dictionaryWithContentsOfURL:[app URLByAppendingPathComponent:@"Contents/Info.plist"]];
}

static NSString *updateBundleChannel(NSDictionary *info) {
    id settings = info[@"JardUpdates"];
    return configuredChannel([settings isKindOfClass:NSDictionary.class] ? settings : nil);
}

static NSString *bundleChannel(NSURL *app) { return updateBundleChannel(updateBundleInfo(app)); }

static NSURL *httpsURL(id address) {
    if (![address isKindOfClass:NSString.class]) return nil;
    NSURLComponents *parts = [NSURLComponents componentsWithString:address];
    return [parts.scheme.lowercaseString isEqual:@"https"] && parts.host.length && !parts.user && !parts.password ? parts.URL : nil;
}

static BOOL developmentVersion(id version) {
    if (![version isKindOfClass:NSString.class] || [version length] > 29) return NO;
    return [version rangeOfString:@"\\A(?:0|[1-9][0-9]{0,8})(?:\\.(?:0|[1-9][0-9]{0,8})){2}\\z"
        options:NSRegularExpressionSearch].location != NSNotFound;
}

// Read this resource only after verifying the candidate's signature and channel.
static NSString *developmentReleaseNotes(NSURL *candidate, NSString *offered, NSString *running) {
    if (!developmentVersion(offered) || (running && !developmentVersion(running))) return nil;
    NSURL *file = [candidate URLByAppendingPathComponent:@"Contents/Resources/Jaro-development-notes.json"];
    struct stat info;
    if (lstat(file.fileSystemRepresentation, &info) != 0 || !S_ISREG(info.st_mode) ||
        info.st_size <= 0 || info.st_size > 128 * 1024) return nil;
    NSData *data = [NSData dataWithContentsOfURL:file];
    if (!data.length || data.length > 128 * 1024) return nil;
    id root = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![root isKindOfClass:NSDictionary.class] || ![root[@"schema"] isEqual:@1]) return nil;
    id builds = root[@"builds"];
    if (![builds isKindOfClass:NSArray.class] || ![builds count] || [builds count] > 40) return nil;

    NSMutableOrderedSet<NSString *> *changes = NSMutableOrderedSet.orderedSet;
    NSString *last = nil;
    for (id build in builds) {
        if (![build isKindOfClass:NSDictionary.class]) return nil;
        NSString *version = build[@"version"];
        if (!developmentVersion(version) ||
            (last ? [version compare:last options:NSNumericSearch] != NSOrderedAscending : ![version isEqual:offered])) return nil;
        last = version;
        id items = build[@"changes"];
        if (![items isKindOfClass:NSArray.class] || [items count] > 40) return nil;
        BOOL include = !running || [version compare:running options:NSNumericSearch] == NSOrderedDescending;
        for (id item in items) {
            if (![item isKindOfClass:NSString.class] || ![item length] || [item length] > 320 ||
                [item rangeOfCharacterFromSet:NSCharacterSet.newlineCharacterSet].location != NSNotFound) return nil;
            if (include && changes.count < 40) [changes addObject:item];
        }
    }
    return changes.count ? [changes.array componentsJoinedByString:@"\n"] : nil;
}

static NSString *feedETag(NSHTTPURLResponse *response) {
    NSString *tag = [[response valueForHTTPHeaderField:@"ETag"] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    if (tag.length < 2 || tag.length > 512) return nil;
    NSString *quoted = [tag hasPrefix:@"W/"] ? [tag substringFromIndex:2] : tag;
    if (quoted.length < 2 || ![quoted hasPrefix:@"\""] || ![quoted hasSuffix:@"\""]) return nil;
    for (NSUInteger i = 1; i + 1 < quoted.length; i++) {
        unichar character = [quoted characterAtIndex:i];
        if (character <= 0x20 || character > 0x7e || character == '"') return nil;
    }
    return tag;
}

static NSString *feedTagValue(NSString *tag) {
    return [tag hasPrefix:@"W/"] ? [tag substringFromIndex:2] : tag;
}

static BOOL archiveDigest(NSString *digest) {
    return [digest isKindOfClass:NSString.class] && digest.length == 64 &&
        [digest rangeOfCharacterFromSet:[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdef"].invertedSet].location == NSNotFound;
}

static NSURL *archiveCache(NSString *identifier) {
    if (!identifier.length || [identifier rangeOfCharacterFromSet:
        [NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"].invertedSet].location != NSNotFound)
        return nil;
    NSURL *base = [NSFileManager.defaultManager URLForDirectory:NSCachesDirectory inDomain:NSUserDomainMask
        appropriateForURL:nil create:YES error:nil];
    NSURL *folder = [base URLByAppendingPathComponent:[identifier stringByAppendingString:@".update-archives"] isDirectory:YES];
    if (!folder || (mkdir(folder.fileSystemRepresentation, 0700) != 0 && errno != EEXIST)) return nil;
    struct stat info;
    return lstat(folder.fileSystemRepresentation, &info) == 0 && S_ISDIR(info.st_mode) &&
        info.st_uid == geteuid() && (info.st_mode & 077) == 0 ? folder : nil;
}

static BOOL privateArchiveFile(int fd, struct stat *info) {
    return fd >= 0 && fstat(fd, info) == 0 && S_ISREG(info->st_mode) && info->st_uid == geteuid() &&
        info->st_nlink == 1 && (info->st_mode & 077) == 0;
}

static NSOperationQueue *archiveQueue(void) {
    static NSOperationQueue *queue;
    static dispatch_queue_t io;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        queue = NSOperationQueue.new;
        queue.name = @"Jaro update archives";
        queue.maxConcurrentOperationCount = 1;
        io = dispatch_queue_create("local.jardaudio.update-archive", DISPATCH_QUEUE_SERIAL);
        queue.underlyingQueue = io;
        queue.qualityOfService = NSQualityOfServiceUtility;
    });
    return queue;
}

typedef void (^JardArchiveCompletion)(NSURL *file, NSString *failure);

// File I/O and all URLSession delegates run on archiveQueue. The updater only
// receives the completed private file on the main queue.
@interface JardArchiveTransfer : NSObject <NSURLSessionDataDelegate>
@property(nonatomic, strong) NSURL *url, *location, *partial;
@property(nonatomic, copy) NSString *identifier, *channel, *digest;
@property(nonatomic, weak) id<NSURLSessionDelegate> authenticationDelegate;
@property(nonatomic, copy) JardArchiveCompletion completion;
@property(nonatomic, strong) NSURLSession *session;
@property(nonatomic, strong) NSURLSessionDataTask *task;
@property(nonatomic) int descriptor;
@property(nonatomic) uint64_t size, offset, requestedEnd, responseStart, responseEnd;
@property(nonatomic) NSUInteger failures;
@property(nonatomic) NSTimeInterval deadline;
@property(nonatomic) BOOL finished;
@end

@implementation JardArchiveTransfer

- (instancetype)init {
    if ((self = [super init])) _descriptor = -1;
    return self;
}

- (void)finish:(NSURL *)file failure:(NSString *)failure {
    if (self.finished) return;
    self.finished = YES;
    [self.session invalidateAndCancel];
    self.session = nil;
    self.task = nil;
    if (self.descriptor >= 0) close(self.descriptor);
    self.descriptor = -1;
    JardArchiveCompletion completion = self.completion;
    self.completion = nil;
    if (completion) dispatch_async(dispatch_get_main_queue(), ^{ completion(file, failure); });
}

- (void)cancel {
    dispatch_async(archiveQueue().underlyingQueue, ^{ [self finish:nil failure:nil]; });
}

- (void)pruneCache:(NSURL *)folder {
    NSString *prefix = [self.channel stringByAppendingString:@"-"];
    NSFileManager *manager = NSFileManager.defaultManager;
    for (NSURL *file in [manager contentsOfDirectoryAtURL:folder includingPropertiesForKeys:nil options:0 error:nil]) {
        NSString *name = file.lastPathComponent;
        if ([name hasSuffix:@".complete"] && name.length == 36 + @".complete".length &&
            [[NSUUID alloc] initWithUUIDString:[name substringToIndex:36]]) {
            struct stat info;
            if (lstat(file.fileSystemRepresentation, &info) == 0 && S_ISREG(info.st_mode) &&
                info.st_uid == geteuid() && info.st_nlink == 1 && (info.st_mode & 077) == 0 &&
                NSDate.date.timeIntervalSince1970 - info.st_mtimespec.tv_sec > 86400)
                unlink(file.fileSystemRepresentation);
            continue;
        }
        if ([file isEqual:self.partial] || ![name hasPrefix:prefix] || ![name hasSuffix:@".partial"] ||
            name.length != prefix.length + 64 + @".partial".length ||
            !archiveDigest([name substringWithRange:NSMakeRange(prefix.length, 64)])) continue;
        int fd = open(file.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) continue;
        struct stat info;
        if (privateArchiveFile(fd, &info) && flock(fd, LOCK_EX | LOCK_NB) == 0)
            unlink(file.fileSystemRepresentation);
        close(fd);
    }
}

- (void)start {
    dispatch_async(archiveQueue().underlyingQueue, ^{
        if (self.finished) return;
        NSURL *folder = archiveCache(self.identifier);
        self.partial = [folder URLByAppendingPathComponent:
            [NSString stringWithFormat:@"%@-%@.partial", self.channel, self.digest]];
        if (self.partial)
            self.descriptor = open(self.partial.fileSystemRepresentation, O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600);
        struct stat info;
        if (!privateArchiveFile(self.descriptor, &info) || flock(self.descriptor, LOCK_EX | LOCK_NB) != 0 ||
            fstat(self.descriptor, &info) != 0) {
            [self finish:nil failure:@"Could not open the local update download."];
            return;
        }
        if ((uint64_t)info.st_size > self.size && ftruncate(self.descriptor, 0) != 0) {
            [self finish:nil failure:@"Could not reset the local update download."];
            return;
        }
        self.offset = (uint64_t)info.st_size <= self.size ? (uint64_t)info.st_size : 0;
        [self pruneCache:folder];
        self.deadline = NSProcessInfo.processInfo.systemUptime + archiveTimeout;
        [self requestPiece];
    });
}

- (void)requestPiece {
    if (self.finished) return;
    if (self.offset == self.size) {
        NSURL *file = [self.partial.URLByDeletingLastPathComponent
            URLByAppendingPathComponent:[NSUUID.UUID.UUIDString stringByAppendingString:@".complete"]];
        if (rename(self.partial.fileSystemRepresentation, file.fileSystemRepresentation) != 0)
            [self finish:nil failure:@"Could not prepare the downloaded update for verification."];
        else [self finish:file failure:nil];
        return;
    }
    NSTimeInterval remaining = self.deadline - NSProcessInfo.processInfo.systemUptime;
    if (remaining <= 0) {
        [self finish:nil failure:@"The update download timed out. Check again to resume."];
        return;
    }
    NSURLSessionConfiguration *configuration = NSURLSessionConfiguration.ephemeralSessionConfiguration;
    configuration.timeoutIntervalForRequest = MIN(30, remaining);
    configuration.timeoutIntervalForResource = MIN(300, remaining);
    configuration.requestCachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
    self.session = [NSURLSession sessionWithConfiguration:configuration delegate:self delegateQueue:archiveQueue()];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:self.location ?: self.url];
    self.requestedEnd = MIN(self.offset + archivePiece, self.size) - 1;
    self.responseEnd = 0;
    [request setValue:[NSString stringWithFormat:@"bytes=%llu-%llu", self.offset, self.requestedEnd] forHTTPHeaderField:@"Range"];
    [request setValue:@"identity" forHTTPHeaderField:@"Accept-Encoding"];
    self.task = [self.session dataTaskWithRequest:request];
    [self.task resume];
}

- (void)URLSession:(NSURLSession *)session didReceiveChallenge:(NSURLAuthenticationChallenge *)challenge
    completionHandler:(void (^)(NSURLSessionAuthChallengeDisposition, NSURLCredential *))completion {
    id<NSURLSessionDelegate> delegate = self.authenticationDelegate;
    if ([delegate respondsToSelector:_cmd]) [delegate URLSession:session didReceiveChallenge:challenge completionHandler:completion];
    else completion(NSURLSessionAuthChallengePerformDefaultHandling, nil);
}

- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task willPerformHTTPRedirection:(NSHTTPURLResponse *)response
    newRequest:(NSURLRequest *)request completionHandler:(void (^)(NSURLRequest *))completion {
    if (!httpsURL(request.URL.absoluteString)) { completion(nil); return; }
    NSMutableURLRequest *redirect = [request mutableCopy];
    [redirect setValue:[task.originalRequest valueForHTTPHeaderField:@"Range"] forHTTPHeaderField:@"Range"];
    [redirect setValue:@"identity" forHTTPHeaderField:@"Accept-Encoding"];
    completion(redirect);
}

- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task didReceiveResponse:(NSURLResponse *)response
    completionHandler:(void (^)(NSURLSessionResponseDisposition))completion {
    if (self.finished || task != self.task || ![response isKindOfClass:NSHTTPURLResponse.class] ||
        !httpsURL(response.URL.absoluteString)) {
        completion(NSURLSessionResponseCancel);
        return;
    }
    NSHTTPURLResponse *http = (NSHTTPURLResponse *)response;
    NSString *encoding = [http valueForHTTPHeaderField:@"Content-Encoding"];
    uint64_t end = 0;
    BOOL full = http.statusCode == 200;
    if (full) {
        if (![http valueForHTTPHeaderField:@"Content-Range"].length) end = self.size;
    } else if (http.statusCode == 206) {
        NSString *range = [http valueForHTTPHeaderField:@"Content-Range"];
        unsigned long long start, last, total;
        int used = 0;
        // All three numbers fit in ten digits under the signed archive limit.
        if (range.length && sscanf(range.UTF8String, "bytes %10llu-%10llu/%10llu%n", &start, &last, &total, &used) == 3 &&
            (NSUInteger)used == [range lengthOfBytesUsingEncoding:NSUTF8StringEncoding] &&
            start == self.offset && last >= start && last <= self.requestedEnd && total == self.size)
            end = last + 1;
    }
    int64_t promised = response.expectedContentLength;
    if (!end || (encoding.length && [encoding caseInsensitiveCompare:@"identity"] != NSOrderedSame) ||
        (promised >= 0 && (uint64_t)promised != end - (full ? 0 : self.offset))) {
        completion(NSURLSessionResponseCancel);
        return;
    }
    if (full && self.offset && ftruncate(self.descriptor, 0) != 0) {
        completion(NSURLSessionResponseCancel);
        [self finish:nil failure:@"Could not reset the local update download."];
        return;
    }
    if (full) self.offset = 0;
    self.responseStart = self.offset;
    self.responseEnd = end;
    completion(NSURLSessionResponseAllow);
}

- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task didReceiveData:(NSData *)data {
    if (self.finished || task != self.task) return;
    if (!self.responseEnd || data.length > self.responseEnd - self.offset) {
        if (self.responseEnd) ftruncate(self.descriptor, self.responseStart);
        [self finish:nil failure:@"The server sent too much data for this update."];
        return;
    }
    const uint8_t *bytes = data.bytes;
    NSUInteger left = data.length;
    while (left) {
        ssize_t written = pwrite(self.descriptor, bytes, left, (off_t)self.offset);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) {
            [self finish:nil failure:@"Could not write the local update download."];
            return;
        }
        self.offset += (uint64_t)written;
        bytes += written;
        left -= (NSUInteger)written;
    }
}

- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error {
    if (self.finished || task != self.task) return;
    self.session = nil;
    self.task = nil;
    [session finishTasksAndInvalidate];
    if (!error && self.responseEnd && self.offset == self.responseEnd) {
        self.location = task.response.URL;
        self.failures = 0;
        [self requestPiece];
    } else if (++self.failures < archiveAttempts) {
        self.location = nil;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC), archiveQueue().underlyingQueue, ^{
            [self requestPiece];
        });
    } else {
        [self finish:nil failure:@"The update download was interrupted. Check again to resume."];
    }
}

@end

static BOOL realDirectory(NSURL *url) {
    NSNumber *directory = nil, *symlink = nil;
    return [url getResourceValue:&directory forKey:NSURLIsDirectoryKey error:nil] &&
        [url getResourceValue:&symlink forKey:NSURLIsSymbolicLinkKey error:nil] && directory.boolValue && !symlink.boolValue;
}

static BOOL isSiblingStage(NSURL *current, NSURL *stage) {
    if (!realDirectory(stage) || ![stage.lastPathComponent hasPrefix:stagePrefix] ||
        ![stage.URLByDeletingLastPathComponent.URLByResolvingSymlinksInPath.path
            isEqual:current.URLByDeletingLastPathComponent.URLByResolvingSymlinksInPath.path]) return NO;
    struct stat info;
    return lstat(stage.fileSystemRepresentation, &info) == 0 && info.st_uid == getuid() && (info.st_mode & 077) == 0;
}

static BOOL matchesInstalledPath(id path, NSURL *installed) {
    if (![path isKindOfClass:NSString.class] || ![path hasPrefix:@"/"]) return NO;
    return [[NSURL fileURLWithPath:path].URLByStandardizingPath.path isEqual:installed.URLByStandardizingPath.path];
}

static BOOL receiptMatchesStage(NSDictionary *receipt, NSURL *installed, NSURL *stage) {
    return matchesInstalledPath(receipt[@"installed"], installed) && matchesInstalledPath(receipt[@"stage"], stage);
}

static BOOL rejectedArchive(NSURL *installed, NSString *digest) {
    if (!archiveDigest(digest)) return NO;
    NSDictionary *receipt = [NSUserDefaults.standardUserDefaults dictionaryForKey:rejectedKey];
    return matchesInstalledPath(receipt[@"installed"], installed) && [digest isEqual:receipt[@"sha256"]];
}

static NSURL *receiptCandidate(NSDictionary *receipt, NSURL *stage) {
    id name = receipt[@"candidate"];
    if (![name isKindOfClass:NSString.class] || ![[name lastPathComponent] isEqual:name] ||
        ![[[name pathExtension] lowercaseString] isEqual:@"app"]) return nil;
    return [stage URLByAppendingPathComponent:name isDirectory:YES];
}

static NSString *installationStagePrefix(NSURL *installed, NSString *identifier) {
    NSString *identity = [NSString stringWithFormat:@"%@\n%@", identifier, installed.URLByResolvingSymlinksInPath.path];
    NSData *bytes = [identity dataUsingEncoding:NSUTF8StringEncoding];
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(bytes.bytes, (CC_LONG)bytes.length, digest);
    NSMutableString *prefix = [stagePrefix mutableCopy];
    for (NSUInteger i = 0; i < 16; i++) [prefix appendFormat:@"%02x", digest[i]];
    [prefix appendString:@"-"];
    return prefix;
}

static int lockStage(NSURL *stage) {
    int descriptor = open(stage.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    struct stat opened, current;
    if (fstat(descriptor, &opened) == 0 && opened.st_uid == getuid() && (opened.st_mode & 077) == 0 &&
        flock(descriptor, LOCK_EX | LOCK_NB) == 0 && lstat(stage.fileSystemRepresentation, &current) == 0 &&
        opened.st_dev == current.st_dev && opened.st_ino == current.st_ino) return descriptor;
    close(descriptor);
    return -1;
}

static BOOL directoryMatchesIdentity(NSURL *url, const struct stat *identity) {
    struct stat current;
    return lstat(url.fileSystemRepresentation, &current) == 0 && S_ISDIR(current.st_mode) &&
        current.st_dev == identity->st_dev && current.st_ino == identity->st_ino;
}

static void clearUpdateReceipts(NSURL *installed, NSURL *stage) {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    for (NSString *key in @[cleanupKey, pendingKey]) {
        if (receiptMatchesStage([defaults dictionaryForKey:key], installed, stage))
            [defaults removeObjectForKey:key];
    }
}

static dispatch_queue_t stageMaintenanceQueue(void) {
    static dispatch_queue_t queue;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        queue = dispatch_queue_create("local.jardaudio.update-maintenance",
            dispatch_queue_attr_make_with_qos_class(DISPATCH_QUEUE_SERIAL, QOS_CLASS_UTILITY, 0));
    });
    return queue;
}

BOOL JardVerifyUpdateBundle(NSURL *current, NSURL *candidate, NSString *version, NSString **reason) {
    if (!realDirectory(current) || !realDirectory(candidate)) {
        if (reason) *reason = @"The update must contain a real application bundle.";
        return NO;
    }
    NSDictionary *existing = [NSDictionary dictionaryWithContentsOfURL:[current URLByAppendingPathComponent:@"Contents/Info.plist"]];
    NSDictionary *incoming = [NSDictionary dictionaryWithContentsOfURL:[candidate URLByAppendingPathComponent:@"Contents/Info.plist"]];
    NSString *identifier = existing[@"CFBundleIdentifier"];
    if (!identifier.length || ![incoming[@"CFBundleIdentifier"] isEqual:identifier] ||
        ![incoming[@"CFBundleVersion"] isEqual:version]) {
        if (reason) *reason = @"The update has the wrong bundle identifier or version.";
        return NO;
    }

    SecStaticCodeRef installed = NULL, replacement = NULL;
    SecRequirementRef requirement = NULL;
    OSStatus result = SecStaticCodeCreateWithPath((__bridge CFURLRef)current, kSecCSDefaultFlags, &installed);
    if (result == errSecSuccess) result = SecCodeCopyDesignatedRequirement(installed, kSecCSDefaultFlags, &requirement);
    if (result == errSecSuccess) result = SecStaticCodeCreateWithPath((__bridge CFURLRef)candidate, kSecCSDefaultFlags, &replacement);
    if (result == errSecSuccess) {
        result = SecStaticCodeCheckValidity(replacement, kSecCSCheckAllArchitectures | kSecCSStrictValidate | kSecCSCheckNestedCode,
            requirement);
    }
    if (requirement) CFRelease(requirement);
    if (replacement) CFRelease(replacement);
    if (installed) CFRelease(installed);
    if (result != errSecSuccess && reason) *reason = @"The update does not match the running app’s code signing requirement.";
    return result == errSecSuccess;
}

BOOL JardSwapUpdateBundles(NSURL *current, NSURL *candidate, NSString **reason) {
    if (renamex_np(candidate.fileSystemRepresentation, current.fileSystemRepresentation, RENAME_SWAP) == 0) return YES;
    if (reason) *reason = [NSString stringWithFormat:@"Could not replace Jard. %s", strerror(errno)];
    return NO;
}

static void appendRelaunchEnvironment(NSMutableArray<NSString *> *arguments) {
    NSDictionary *environment = NSProcessInfo.processInfo.environment;
    for (NSString *key in @[@"RUMBLE_DATA_DIR", @"RUMBLE_PORT", @"RUMBLE_LOG_DIR"]) {
        if (environment[key]) [arguments addObjectsFromArray:@[@"--env", [key stringByAppendingFormat:@"=%@", environment[key]]]];
    }
}

NSTask *JardScheduleRelaunch(NSURL *bundle, NSString **reason) {
    NSTask *task = NSTask.new;
    task.executableURL = [NSURL fileURLWithPath:@"/bin/sh"];
    NSProcessInfo *process = NSProcessInfo.processInfo;
    NSMutableArray *arguments = [@[@"-c",
        @"count=0; while /bin/kill -0 \"$1\" 2>/dev/null; do count=$((count+1)); [ \"$count\" -lt 300 ] || exit 1; /bin/sleep 0.2; done; shift; exec /usr/bin/open \"$@\"",
        @"jard-restart", @(process.processIdentifier).stringValue] mutableCopy];
    appendRelaunchEnvironment(arguments);
    [arguments addObject:bundle.path];
    task.arguments = arguments;
    NSError *error = nil;
    if ([task launchAndReturnError:&error]) return task;
    if (reason) *reason = error.localizedDescription;
    return nil;
}

static NSTask *scheduleUpdateRelaunch(NSURL *installed, NSURL *candidate, NSString *version, NSString *digest,
                                    NSString **reason) {
    struct stat previous, incoming;
    if (lstat(installed.fileSystemRepresentation, &previous) < 0 ||
        lstat(candidate.fileSystemRepresentation, &incoming) < 0 || previous.st_dev != incoming.st_dev) {
        if (reason) *reason = @"The update bundles could not be identified.";
        return nil;
    }
    NSTask *task = NSTask.new;
    task.executableURL = NSBundle.mainBundle.executableURL;
    task.arguments = @[@"--jaro-relaunch-update", @(getpid()).stringValue, installed.path, candidate.path,
        @(previous.st_dev).stringValue, @(previous.st_ino).stringValue, @(incoming.st_ino).stringValue, version,
        archiveDigest(digest) ? digest : @""];
    task.standardInput = NSFileHandle.fileHandleWithNullDevice;
    task.standardOutput = NSFileHandle.fileHandleWithNullDevice;
    NSError *error = nil;
    if ([task launchAndReturnError:&error]) return task;
    if (reason) *reason = error.localizedDescription;
    return nil;
}

typedef NS_ENUM(NSInteger, JardUpdateStartup) {
    JardUpdateStartupFailed, JardUpdateStartupReady, JardUpdateStartupPending,
};

static NSURL *updateStartupStage(void) {
    const char *path = getenv("JARD_UPDATE_STARTUP_STAGE");
    if (!path || path[0] != '/') return nil;
    NSURL *stage = [NSURL fileURLWithPath:@(path) isDirectory:YES].URLByStandardizingPath;
    return isSiblingStage(NSBundle.mainBundle.bundleURL, stage) ? stage : nil;
}

BOOL JardIsUpdateStartupProbe(void) {
    return updateStartupStage() != nil;
}

void JardConfirmUpdateStartup(void) {
    NSURL *stage = updateStartupStage();
    if (!stage) return;
    int directory = open(stage.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0) return;
    int receipt = openat(directory, startupReceipt, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (receipt >= 0) {
        pid_t process = getpid();
        if (write(receipt, &process, sizeof(process)) != sizeof(process)) unlinkat(directory, startupReceipt, 0);
        close(receipt);
    }
    close(directory);
}

static BOOL updateStartupConfirmed(int directory) {
    int receipt = openat(directory, startupReceipt, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (receipt < 0) return NO;
    pid_t confirmed = 0;
    BOOL ready = read(receipt, &confirmed, sizeof(confirmed)) == sizeof(confirmed) && confirmed > 1;
    close(receipt);
    return ready;
}

static JardUpdateStartup reopenUpdate(NSURL *installed, NSURL *stage, int lease) {
    BOOL rolledBack = stage == nil;
    NSInteger confirmation = rolledBack ? 0 : [updateBundleInfo(installed)[@"JardUpdateStartupConfirmation"] integerValue];
    BOOL canConfirm = confirmation > 0;
    NSMutableArray *arguments = NSMutableArray.array;
    if (!rolledBack) [arguments addObject:@"-W"];
    appendRelaunchEnvironment(arguments);
    if (rolledBack) [arguments addObjectsFromArray:@[@"--env", @"JARD_UPDATE_ROLLED_BACK=1"]];
    if (canConfirm) [arguments addObjectsFromArray:@[@"--env", [@"JARD_UPDATE_STARTUP_STAGE=" stringByAppendingString:stage.path]]];
    [arguments addObject:installed.path];
    NSTask *task = NSTask.new;
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/open"];
    task.arguments = arguments;
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        NSLog(@"Jaro could not open the update bundle: %@", error.localizedDescription);
        return JardUpdateStartupFailed;
    }
    // Allow older launchers their original grace periods. Workspace confirmation
    // can include a cold engine startup, window load and a graceful shutdown.
    NSTimeInterval grace = confirmation >= 2 ? 105 : canConfirm ? 60 : rolledBack ? 15 : 5;
    NSTimeInterval until = NSProcessInfo.processInfo.systemUptime + grace;
    @try {
        do {
            if (canConfirm && updateStartupConfirmed(lease)) return JardUpdateStartupReady;
            if (!task.running)
                return rolledBack && task.terminationStatus == 0 ? JardUpdateStartupReady : JardUpdateStartupFailed;
            usleep(50 * 1000);
        } while (NSProcessInfo.processInfo.systemUptime < until);
        return canConfirm || rolledBack ? JardUpdateStartupPending : JardUpdateStartupReady;
    } @finally {
        // End the wait in Launch Services. The app it opened has its own lifetime.
        if (task.running) [task terminate];
    }
}

int JardRunUpdateRelaunch(int argc, const char *argv[]) {
    if (argc != 10) return 1;
    pid_t parent = (pid_t)strtol(argv[2], NULL, 10);
    if (parent <= 1) return 1;
    NSURL *installed = [NSURL fileURLWithPath:@(argv[3]) isDirectory:YES].URLByStandardizingPath;
    NSURL *candidate = [NSURL fileURLWithPath:@(argv[4]) isDirectory:YES].URLByStandardizingPath;
    NSURL *stage = candidate.URLByDeletingLastPathComponent;
    struct stat previous = {.st_dev = (dev_t)strtol(argv[5], NULL, 10), .st_ino = strtoull(argv[6], NULL, 10)};
    struct stat incoming = {.st_dev = previous.st_dev, .st_ino = strtoull(argv[7], NULL, 10)};
    if (![installed.pathExtension.lowercaseString isEqual:@"app"] ||
        ![candidate.pathExtension.lowercaseString isEqual:@"app"] || !isSiblingStage(installed, stage)) return 1;
    NSTimeInterval until = NSProcessInfo.processInfo.systemUptime + 60;
    while (getppid() == parent && NSProcessInfo.processInfo.systemUptime < until) usleep(50 * 1000);
    if (getppid() == parent) return 1;

    int lease = lockStage(stage);
    if (lease < 0) return 1;
    @try {
        struct stat stageIdentity;
        if (fstat(lease, &stageIdentity) < 0) return 1;
        // The parent may have failed or aborted before the exchange.
        if (!directoryMatchesIdentity(installed, &incoming) || !directoryMatchesIdentity(candidate, &previous)) return 1;
        unlinkat(lease, startupReceipt, 0);
        JardUpdateStartup startup = reopenUpdate(installed, stage, lease);
        if (startup == JardUpdateStartupPending) return 1;
        if (startup == JardUpdateStartupFailed) {
            NSString *reason = nil;
            if (!JardVerifyUpdateBundle(installed, candidate, @(argv[8]), &reason) ||
                !directoryMatchesIdentity(stage, &stageIdentity) ||
                !directoryMatchesIdentity(installed, &incoming) || !directoryMatchesIdentity(candidate, &previous) ||
                !JardSwapUpdateBundles(installed, candidate, &reason)) {
                NSLog(@"Jaro could not restore the previous version: %@", reason ?: @"The installation changed.");
                return 1;
            }
            NSString *digest = @(argv[9]);
            if (archiveDigest(digest)) {
                NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
                [defaults setObject:@{@"installed": installed.path, @"sha256": digest} forKey:rejectedKey];
                [defaults synchronize]; // The restored version is a separate process.
            }
            if (reopenUpdate(installed, nil, -1) != JardUpdateStartupReady)
                NSLog(@"Jaro’s previous version was restored but could not be opened.");
        }
        if (directoryMatchesIdentity(stage, &stageIdentity) && isSiblingStage(installed, stage)) {
            // Retire this stage's receipt before another update can reuse its path.
            clearUpdateReceipts(installed, stage);
            [NSFileManager.defaultManager removeItemAtURL:stage error:nil];
        }
        return 0;
    } @finally {
        close(lease);
    }
}

@interface JardUpdater () <NSURLSessionDownloadDelegate>
@property(nonatomic, strong) NSURL *installed, *stage, *candidate, *releaseDownload;
@property(nonatomic, copy) NSDictionary<NSString *, NSDictionary *> *channels;
@property(nonatomic, copy) NSString *identifier, *version, *phase, *message, *availableVersion, *notes;
@property(nonatomic, copy) NSString *stageVersion, *stageNotes;
@property(nonatomic, copy) NSString *installationStagePrefix;
@property(nonatomic, copy) NSString *buildChannel, *selectedChannel, *stageChannel, *stageDigest, *offerDigest, *confirmedDigest;
@property(nonatomic, copy) NSString *sha256, *transferVersion, *transferNotes, *transferChannel;
@property(nonatomic, strong) NSURLSession *session;
@property(nonatomic, strong) NSURLSessionDownloadTask *task;
@property(nonatomic, strong) JardArchiveTransfer *archive;
@property(nonatomic, strong) NSURL *feedCacheURL, *feedCacheResource;
@property(nonatomic, copy) NSString *feedCacheChannel, *feedCacheTag;
@property(nonatomic, copy) NSData *feedCacheData;
@property(nonatomic, copy) void (^changed)(NSDictionary *);
@property(nonatomic, copy) void (^restartCheck)(void);
@property(nonatomic) NSTimeInterval lastAttempt, nextScheduledCheck, feedDeadline;
@property(nonatomic) NSUInteger generation, checkFailures;
@property(nonatomic) int stageLease;
@property(nonatomic) uint64_t archiveBytes;
@property(nonatomic) BOOL closed, quitting, restoringStage, checkAfterRestore, verifyingInstall;
@end

@implementation JardUpdater

- (instancetype)initWithConfiguration:(NSDictionary *)configuration bundle:(NSBundle *)bundle
                         stateChanged:(void (^)(NSDictionary *))changed {
    if (![configuration isKindOfClass:NSDictionary.class] || !httpsURL(configuration[@"feed_url"]) ||
        ![configuration[@"public_key"] isKindOfClass:NSString.class] || ![configuration[@"public_key"] length] ||
        !bundle.bundleIdentifier.length) return nil;
    id dev = configuration[@"development"];
    NSData *stableKey = [[NSData alloc] initWithBase64EncodedString:configuration[@"public_key"] options:0];
    NSData *devKey = [dev isKindOfClass:NSDictionary.class] && [dev[@"public_key"] isKindOfClass:NSString.class]
        ? [[NSData alloc] initWithBase64EncodedString:dev[@"public_key"] options:0] : nil;
    BOOL development = [dev isKindOfClass:NSDictionary.class] && httpsURL(dev[@"feed_url"]) &&
        stableKey.length == 32 && devKey.length == 32 && ![devKey isEqual:stableKey] &&
        ![httpsURL(dev[@"feed_url"]) isEqual:httpsURL(configuration[@"feed_url"])];
    if (!(self = [super init])) return nil;
    _stageLease = -1;
    _installed = [bundle.bundleURL URLByStandardizingPath];
    _installationStagePrefix = installationStagePrefix(_installed, bundle.bundleIdentifier);
    _buildChannel = configuredChannel(configuration);
    if (!_buildChannel || ([_buildChannel isEqual:@"dev"] && !development)) return nil;
    _channels = development ? @{@"stable": configuration, @"dev": dev} : @{@"stable": configuration};
    _selectedChannel = [self preferredChannel];
    _identifier = [bundle.bundleIdentifier copy];
    _version = [[bundle objectForInfoDictionaryKey:@"CFBundleVersion"] copy];
    _phase = @"idle";
    _message = @"";
    _changed = [changed copy];
    BOOL confirmsWorkspace = [[bundle objectForInfoDictionaryKey:@"JardUpdateStartupConfirmation"] integerValue] >= 2;
    dispatch_async(stageMaintenanceQueue(), ^{
        if (!confirmsWorkspace) [self cleanPreviousUpdate];
        [self pruneAbandonedStages];
    });
    return self;
}

- (void)dealloc {
    if (_stageLease >= 0) close(_stageLease);
}

- (NSURL *)stagedReleaseNotesURL {
    return self.stageChannel ? httpsURL(self.channels[self.stageChannel][@"release_url"]) : nil;
}

- (NSDictionary *)status {
    BOOL staged = [self canInstallStagedUpdate];
    NSMutableDictionary *status = [@{@"phase": self.phase, @"message": self.message ?: @"",
        @"currentVersion": self.version ?: @"", @"version": (staged ? self.stageVersion : self.availableVersion) ?: @"",
        @"notes": (staged ? self.stageNotes : self.notes) ?: @"",
        @"installedChannel": self.buildChannel, @"channel": self.selectedChannel,
        @"channels": self.channels[@"dev"] ? @[@"stable", @"dev"] : @[@"stable"],
        @"updatesDisabled": @([self updatesDisabled]),
        @"staged": @(staged),
        @"channelManaged": @([NSUserDefaults.standardUserDefaults objectIsForcedForKey:channelKey])} mutableCopy];
    if (staged) {
        status[@"stageChannel"] = self.stageChannel;
        status[@"releaseNotesAvailable"] = @([self stagedReleaseNotesURL] != nil);
    }
    if (self.offerDigest && (staged || [self.phase isEqual:@"switch"])) {
        status[@"offer"] = self.offerDigest;
        status[@"offerVersion"] = self.availableVersion;
    }
    return status;
}

- (NSString *)preferredChannel {
    id saved = [NSUserDefaults.standardUserDefaults objectForKey:channelKey];
    return [saved isKindOfClass:NSString.class] && self.channels[saved] ? saved : self.buildChannel;
}

- (BOOL)switching {
    return ![self.selectedChannel isEqual:self.buildChannel];
}

- (NSUInteger)rolloutBucket {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    id saved = [defaults objectForKey:rolloutBucketKey];
    if ([saved isKindOfClass:NSNumber.class]) {
        NSInteger bucket = [saved integerValue];
        if (bucket >= 0 && bucket < 100 && [saved isEqualToNumber:@(bucket)]) return (NSUInteger)bucket;
    }
    NSUInteger bucket = arc4random_uniform(100);
    [defaults setInteger:bucket forKey:rolloutBucketKey];
    return bucket;
}

- (BOOL)updatesDisabled {
    return [NSUserDefaults.standardUserDefaults boolForKey:@"DisableUpdates"];
}

- (void)setState:(NSString *)phase message:(NSString *)message {
    self.phase = phase;
    self.message = message;
    if (self.changed) self.changed([self status]);
    if (self.restartCheck && [@[@"ready", @"current", @"error", @"download", @"switch", @"unavailable"] containsObject:phase]) {
        void (^completion)(void) = self.restartCheck;
        self.restartCheck = nil;
        completion();
    }
}

- (BOOL)canInstallStagedUpdate {
    NSString *preferred = [self preferredChannel];
    // A feed selection alone does not revoke a verified update to this installation's channel.
    BOOL homeStage = [self.stageChannel isEqual:self.buildChannel];
    BOOL channelManaged = [NSUserDefaults.standardUserDefaults objectIsForcedForKey:channelKey];
    return !self.closed && ![self.phase isEqual:@"applying"] && ![self.phase isEqual:@"download"] && ![self updatesDisabled] &&
        self.stage && self.candidate && [self.selectedChannel isEqual:preferred] &&
        (!channelManaged || [self.stageChannel isEqual:preferred]) &&
        (homeStage || ([self.stageChannel isEqual:preferred] && [self.confirmedDigest isEqual:self.stageDigest])) &&
        [self replaceable];
}

- (NSTimeInterval)updateUptime {
    return NSProcessInfo.processInfo.systemUptime;
}

- (void)completeCheck:(NSString *)phase message:(NSString *)message failed:(BOOL)failed {
    self.checkFailures = failed ? MIN(self.checkFailures + 1, 3) : 0;
    NSTimeInterval interval = [self.selectedChannel isEqual:@"dev"] ? developmentCheckInterval : stableCheckInterval;
    self.nextScheduledCheck = [self updateUptime] + MIN(interval * (1u << self.checkFailures), stableCheckInterval);
    [self setState:phase message:message];
}

- (void)error:(NSString *)message {
    [self completeCheck:[self canInstallStagedUpdate] ? @"ready" : @"error" message:message failed:YES];
}

- (void)discardStage {
    if (self.stage && isSiblingStage(self.installed, self.stage)) [NSFileManager.defaultManager removeItemAtURL:self.stage error:nil];
    [self releaseStageLease];
    self.stage = nil;
    self.candidate = nil;
    self.stageChannel = nil;
    self.stageDigest = nil;
    self.stageVersion = self.stageNotes = nil;
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    if (matchesInstalledPath([defaults dictionaryForKey:pendingKey][@"installed"], self.installed))
        [defaults removeObjectForKey:pendingKey];
}

- (void)releaseStageLease {
    if (self.stageLease >= 0) close(self.stageLease);
    self.stageLease = -1;
}

- (void)stopTransfer {
    self.generation++;
    self.restoringStage = self.checkAfterRestore = NO;
    [self.archive cancel];
    self.archive = nil;
    [self.session invalidateAndCancel];
    self.session = nil;
    self.task = nil;
}

- (void)resetForChannel:(NSString *)channel {
    [self stopTransfer];
    if (self.stage && ![self.stageChannel isEqual:self.buildChannel]) [self discardStage];
    [self clearFeedCache];
    self.selectedChannel = channel;
    self.lastAttempt = 0;
    self.nextScheduledCheck = 0;
    self.checkFailures = 0;
    self.offerDigest = self.confirmedDigest = nil;
    self.availableVersion = self.notes = nil;
    self.releaseDownload = nil;
    [self setState:@"idle" message:@""];
}

- (BOOL)selectChannel:(NSString *)channel reason:(NSString **)reason {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    if (self.closed || self.quitting || [self updatesDisabled] || self.restartCheck || self.verifyingInstall || [self.phase isEqual:@"applying"] ||
        [defaults objectIsForcedForKey:channelKey] || ![channel isKindOfClass:NSString.class] || !self.channels[channel]) {
        if (reason) *reason = @"This update channel is unavailable.";
        return NO;
    }
    [defaults setObject:channel forKey:channelKey];
    [self check];
    return YES;
}

- (BOOL)confirmChannelSwitch:(NSString *)digest reason:(NSString **)reason {
    if (self.closed || self.quitting || self.restartCheck || self.verifyingInstall || [self updatesDisabled] ||
        ![self switching] || ![self.selectedChannel isEqual:[self preferredChannel]] ||
        ![@[@"switch", @"ready"] containsObject:self.phase] || !self.offerDigest || ![self.offerDigest isEqual:digest]) {
        if (reason) *reason = @"The release offer changed. Check for updates and confirm the current release.";
        return NO;
    }
    self.confirmedDigest = digest;
    [self check];
    return YES;
}

- (BOOL)replaceable {
    if (![self.installed.pathExtension.lowercaseString isEqual:@"app"] || !realDirectory(self.installed) ||
        [self.installed.path.pathComponents containsObject:@"AppTranslocation"]) return NO;
    NSFileManager *manager = NSFileManager.defaultManager;
    return [manager isWritableFileAtPath:self.installed.path] &&
        [manager isWritableFileAtPath:self.installed.URLByDeletingLastPathComponent.path];
}

- (void)cleanPreviousUpdate {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSDictionary *receipt = [defaults dictionaryForKey:cleanupKey];
    NSString *path = receipt[@"stage"];
    if (!matchesInstalledPath(receipt[@"installed"], self.installed) ||
        ![receipt[@"version"] isEqual:self.version] ||
        ![(receipt[@"channel"] ?: @"stable") isEqual:self.buildChannel] ||
        ![path isKindOfClass:NSString.class] || ![path hasPrefix:@"/"]) return;
    NSURL *stage = [NSURL fileURLWithPath:path isDirectory:YES];
    if (isSiblingStage(self.installed, stage)) {
        int lease = lockStage(stage);
        if (lease < 0) return;
        BOOL removed = [NSFileManager.defaultManager removeItemAtURL:stage error:nil];
        close(lease);
        if (!removed) return;
    }
    if ([[defaults dictionaryForKey:cleanupKey] isEqual:receipt]) [defaults removeObjectForKey:cleanupKey];
    if (receiptMatchesStage([defaults dictionaryForKey:pendingKey], self.installed, stage))
        [defaults removeObjectForKey:pendingKey];
}

- (void)confirmStartup {
    JardConfirmUpdateStartup();
    dispatch_async(stageMaintenanceQueue(), ^{ [self cleanPreviousUpdate]; });
}

- (BOOL)hasLiveReceiptForStage:(NSURL *)stage {
    NSDictionary *installed = updateBundleInfo(self.installed);
    id installedVersion = installed[@"CFBundleVersion"];
    NSString *installedChannel = updateBundleChannel(installed);
    if (![installed[@"CFBundleIdentifier"] isEqual:self.identifier] ||
        ![installedVersion isKindOfClass:NSString.class] || ![installedVersion length] || !installedChannel) return YES;
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    NSDictionary *cleanup = [defaults dictionaryForKey:cleanupKey];
    if (receiptMatchesStage(cleanup, self.installed, stage) && [cleanup[@"version"] isEqual:installedVersion] &&
        [(cleanup[@"channel"] ?: @"stable") isEqual:installedChannel]) return YES;
    NSDictionary *pending = [defaults dictionaryForKey:pendingKey];
    if (!receiptMatchesStage(pending, self.installed, stage) || ![pending[@"runningVersion"] isEqual:installedVersion]) return NO;
    // After an interrupted swap the receipt can name this directory,
    // but the candidate path now holds the old bundle.
    NSURL *candidate = receiptCandidate(pending, stage);
    if (!candidate || !realDirectory(candidate)) return NO;
    NSDictionary *staged = updateBundleInfo(candidate);
    return [staged[@"CFBundleIdentifier"] isEqual:self.identifier] &&
        [staged[@"CFBundleVersion"] isEqual:pending[@"version"]] &&
        [updateBundleChannel(staged) isEqual:(pending[@"channel"] ?: @"stable")];
}

- (void)pruneAbandonedStages {
    NSFileManager *manager = NSFileManager.defaultManager;
    NSURL *parent = self.installed.URLByDeletingLastPathComponent;
    NSArray<NSURL *> *siblings = [manager contentsOfDirectoryAtURL:parent includingPropertiesForKeys:nil options:0 error:nil];
    NSString *prefix = self.installationStagePrefix;
    NSTimeInterval now = NSDate.date.timeIntervalSince1970;
    for (NSURL *stage in siblings) {
        NSString *name = stage.lastPathComponent;
        if (![name hasPrefix:prefix] || ![[NSUUID alloc] initWithUUIDString:[name substringFromIndex:prefix.length]] ||
            !isSiblingStage(self.installed, stage)) continue;
        int lease = lockStage(stage);
        if (lease < 0) continue;
        struct stat info;
        if (fstat(lease, &info) == 0 && now - info.st_mtimespec.tv_sec >= abandonedStageGrace &&
            ![self hasLiveReceiptForStage:stage]) {
            NSError *error = nil;
            if (![manager removeItemAtURL:stage error:&error])
                NSLog(@"Jaro could not remove an abandoned update folder: %@", error.localizedDescription);
        }
        close(lease);
    }
}

- (void)scheduleStageCleanup {
    dispatch_async(stageMaintenanceQueue(), ^{ [self pruneAbandonedStages]; });
}

- (BOOL)verifyCandidate:(NSURL *)candidate version:(NSString *)version channel:(NSString *)channel reason:(NSString **)reason {
    if (!JardVerifyUpdateBundle(self.installed, candidate, version, reason)) return NO;
    if ([bundleChannel(candidate) isEqual:channel]) return YES;
    if (reason) *reason = @"The staged application belongs to a different update channel.";
    return NO;
}

- (void)restoreStagedUpdateWithCompletion:(void (^)(BOOL restored))completion {
    if (self.closed || self.quitting || self.stage || self.restoringStage || self.task || self.archive ||
        [self.phase isEqual:@"staging"] || [self updatesDisabled]) {
        if (completion) completion(NO);
        return;
    }
    NSDictionary *saved = [NSUserDefaults.standardUserDefaults dictionaryForKey:pendingKey];
    id stagePath = saved[@"stage"], version = saved[@"version"];
    if (!matchesInstalledPath(saved[@"installed"], self.installed) ||
        ![saved[@"runningVersion"] isEqual:self.version] || ![stagePath isKindOfClass:NSString.class] ||
        ![stagePath hasPrefix:@"/"] || ![version isKindOfClass:NSString.class] || ![version length]) {
        if (completion) completion(NO);
        return;
    }
    NSURL *stage = [NSURL fileURLWithPath:stagePath isDirectory:YES];
    NSURL *candidate = receiptCandidate(saved, stage);
    if (!candidate) { if (completion) completion(NO); return; }
    NSString *channel = saved[@"channel"] ?: @"stable";
    NSString *selected = self.selectedChannel;
    BOOL eligible = [channel isEqual:self.buildChannel] &&
        !rejectedArchive(self.installed, saved[@"sha256"]) &&
        [version compare:self.version options:NSNumericSearch] == NSOrderedDescending;
    self.restoringStage = YES;
    NSUInteger generation = self.generation;
    dispatch_async(stageMaintenanceQueue(), ^{
        int lease = isSiblingStage(self.installed, stage) ? lockStage(stage) : -1;
        BOOL verified = lease >= 0 && eligible && [self verifyCandidate:candidate version:version channel:channel reason:nil];
        dispatch_async(dispatch_get_main_queue(), ^{
            if (generation != self.generation) {
                if (lease >= 0) close(lease);
                if (completion) completion(NO);
                return;
            }
            self.restoringStage = NO;
            BOOL queuedCheck = self.checkAfterRestore;
            self.checkAfterRestore = NO;
            NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
            BOOL current = !self.closed && !self.quitting && !self.stage && ![self updatesDisabled] &&
                [selected isEqual:self.selectedChannel] && [selected isEqual:[self preferredChannel]] &&
                [[defaults dictionaryForKey:pendingKey] isEqual:saved];
            BOOL restored = current && verified;
            if (restored) {
                self.stage = stage;
                self.stageLease = lease;
                self.candidate = candidate;
                self.stageChannel = channel;
                self.stageDigest = saved[@"sha256"];
                self.stageVersion = version;
                id notes = saved[@"notes"];
                self.stageNotes = [notes isKindOfClass:NSString.class] && [notes length] <= 16384 ? notes : @"";
                if (!queuedCheck && [self canInstallStagedUpdate]) [self setState:@"ready" message:@""];
            } else if (lease >= 0 && current) {
                [defaults removeObjectForKey:pendingKey];
                dispatch_async(stageMaintenanceQueue(), ^{
                    [NSFileManager.defaultManager removeItemAtURL:stage error:nil];
                    close(lease);
                });
            } else if (lease >= 0) {
                close(lease);
            }
            if (queuedCheck) [self check];
            if (completion) completion(restored);
        });
    });
}

- (void)persistStage {
    [NSUserDefaults.standardUserDefaults setObject:@{@"installed": self.installed.path, @"runningVersion": self.version,
        @"stage": self.stage.path, @"candidate": self.candidate.lastPathComponent, @"version": self.stageVersion,
        @"notes": self.stageNotes ?: @"", @"channel": self.stageChannel, @"sha256": self.stageDigest} forKey:pendingKey];
}

- (void)clearFeedCache {
    self.feedCacheURL = self.feedCacheResource = nil;
    self.feedCacheChannel = self.feedCacheTag = nil;
    self.feedCacheData = nil;
}

- (NSString *)conditionalFeedTagForURL:(NSURL *)url resource:(NSURL *)resource {
    return !self.restartCheck && [self.feedCacheChannel isEqual:self.selectedChannel] &&
        [self.feedCacheURL isEqual:url] && [self.feedCacheResource isEqual:resource] && self.feedCacheData.length
        ? self.feedCacheTag : nil;
}

- (void)rememberFeed:(NSData *)data response:(NSHTTPURLResponse *)response url:(NSURL *)url {
    if (response.statusCode != 200) return;
    NSString *etag = feedETag(response);
    if (!etag) return;
    self.feedCacheData = data;
    self.feedCacheTag = etag;
    self.feedCacheChannel = self.selectedChannel;
    self.feedCacheURL = url;
    self.feedCacheResource = response.URL;
}

- (void)downloadFeed:(NSURL *)url timeout:(NSTimeInterval)timeout {
    NSURLSessionConfiguration *configuration = NSURLSessionConfiguration.ephemeralSessionConfiguration;
    configuration.timeoutIntervalForRequest = timeout;
    configuration.timeoutIntervalForResource = timeout;
    configuration.requestCachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
    configuration.URLCache = nil;
    self.session = [NSURLSession sessionWithConfiguration:configuration delegate:self delegateQueue:NSOperationQueue.mainQueue];
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url];
    [request setValue:[self feedUserAgent] forHTTPHeaderField:@"User-Agent"];
    [request setValue:[self conditionalFeedTagForURL:url resource:url] forHTTPHeaderField:@"If-None-Match"];
    if (self.restartCheck) [request setValue:@"no-cache" forHTTPHeaderField:@"Cache-Control"];
    self.task = [self.session downloadTaskWithRequest:request];
    [self setState:@"checking" message:@""];
    [self.task resume];
}

- (NSString *)feedUserAgent {
    return [NSString stringWithFormat:@"jaro/%@/%@", self.buildChannel, self.version ?: @"0.0.0"];
}

- (NSURL *)rolloutFeedURL {
    return httpsURL(self.channels[self.selectedChannel][@"rollout_url"]);
}

- (int64_t)feedLimitForURL:(NSURL *)url {
    return [url isEqual:[self rolloutFeedURL]] ? rolloutFeedLimit : feedLimit;
}

- (void)check {
    if (self.closed || self.quitting || self.verifyingInstall || [self.phase isEqual:@"applying"]) return;
    if ([self updatesDisabled]) {
        [self stopTransfer];
        [self clearFeedCache];
        self.offerDigest = self.confirmedDigest = nil;
        if (self.stage && ![self.stageChannel isEqual:self.buildChannel]) [self discardStage];
        [self setState:@"unavailable" message:@""];
        return;
    }
    NSString *preferred = [self preferredChannel];
    if (![preferred isEqual:self.selectedChannel]) [self resetForChannel:preferred];
    if (self.restoringStage) {
        self.checkAfterRestore = YES;
        [self setState:@"checking" message:@""];
        return;
    }
    if (self.task || self.archive || [self.phase isEqual:@"staging"]) return;
    NSURL *feed = [self rolloutFeedURL] ?: httpsURL(self.channels[self.selectedChannel][@"feed_url"]);
    if (!feed) { [self error:@"The update feed URL is invalid."]; return; }
    self.offerDigest = nil;
    [self scheduleStageCleanup];
    self.lastAttempt = [self updateUptime];
    NSTimeInterval timeout = self.restartCheck ? restartFeedTimeout : feedTimeout;
    self.feedDeadline = self.lastAttempt + timeout;
    [self downloadFeed:feed timeout:timeout];
}

- (void)checkAutomaticallyOnActivation:(BOOL)activation {
    NSString *channel = [self preferredChannel];
    if (activation && ![channel isEqual:@"dev"]) return;
    NSTimeInterval now = [self updateUptime];
    BOOL due = activation ? !self.lastAttempt || now - self.lastAttempt >= focusCheckInterval
                          : now >= self.nextScheduledCheck;
    if (due || ![channel isEqual:self.selectedChannel]) [self check];
}

- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task willPerformHTTPRedirection:(NSHTTPURLResponse *)response
    newRequest:(NSURLRequest *)request completionHandler:(void (^)(NSURLRequest *))completion {
    if (task != self.task || !httpsURL(request.URL.absoluteString)) { completion(nil); return; }
    NSMutableURLRequest *redirect = [request mutableCopy];
    [redirect setValue:[self feedUserAgent] forHTTPHeaderField:@"User-Agent"];
    [redirect setValue:[self conditionalFeedTagForURL:task.originalRequest.URL resource:request.URL]
        forHTTPHeaderField:@"If-None-Match"];
    if (self.restartCheck) [redirect setValue:@"no-cache" forHTTPHeaderField:@"Cache-Control"];
    completion(redirect);
}

- (void)URLSession:(NSURLSession *)session downloadTask:(NSURLSessionDownloadTask *)task
      didWriteData:(int64_t)bytes totalBytesWritten:(int64_t)written totalBytesExpectedToWrite:(int64_t)expected {
    if (task != self.task) return;
    int64_t limit = [self feedLimitForURL:task.originalRequest.URL];
    if (written > limit || expected > limit) {
        [task cancel];
    }
}

- (void)URLSession:(NSURLSession *)session downloadTask:(NSURLSessionDownloadTask *)task
      didFinishDownloadingToURL:(NSURL *)location {
    [self finishFeedTask:task session:session location:location error:nil];
}

- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error {
    [self finishFeedTask:task session:session location:nil error:error];
}

- (void)finishFeedTask:(NSURLSessionTask *)task session:(NSURLSession *)session location:(NSURL *)location error:(NSError *)error {
    if (self.closed || task != self.task) return;
    self.task = nil;
    self.session = nil;
    [session finishTasksAndInvalidate];
    if ([self updatesDisabled] || ![self.selectedChannel isEqual:[self preferredChannel]]) {
        [self check];
        return;
    }
    if (error || ![task.response isKindOfClass:NSHTTPURLResponse.class] || !httpsURL(task.response.URL.absoluteString)) {
        [self error:@"Jard could not download or verify the update."];
        return;
    }
    NSHTTPURLResponse *response = (NSHTTPURLResponse *)task.response;
    NSURL *url = task.originalRequest.URL;
    if (response.statusCode == 404 && [url isEqual:[self rolloutFeedURL]]) {
        NSTimeInterval remaining = self.feedDeadline - [self updateUptime];
        NSURL *legacy = httpsURL(self.channels[self.selectedChannel][@"feed_url"]);
        if (remaining > 0 && legacy && ![legacy isEqual:url]) {
            [self downloadFeed:legacy timeout:remaining];
            return;
        }
    }
    if (response.statusCode == 304) {
        NSString *sent = [task.currentRequest valueForHTTPHeaderField:@"If-None-Match"];
        NSString *expected = [self conditionalFeedTagForURL:url resource:response.URL];
        NSString *returned = [response valueForHTTPHeaderField:@"ETag"];
        if (!expected || ![sent isEqual:expected] || (returned && ![feedTagValue(feedETag(response)) isEqual:feedTagValue(expected)])) {
            [self clearFeedCache];
            [self error:@"The update server reported an unchanged feed without a matching signed copy."];
            return;
        }
        [self receiveFeed:self.feedCacheData response:response requestedURL:url];
        return;
    }
    if (response.statusCode != 200) {
        [self error:@"Jard could not download the update."];
        return;
    }
    [self clearFeedCache];
    NSNumber *size = nil;
    [location getResourceValue:&size forKey:NSURLFileSizeKey error:nil];
    NSData *data = size.longLongValue > 0 && size.longLongValue <= [self feedLimitForURL:url]
        ? [NSData dataWithContentsOfURL:location] : nil;
    if (!data.length) {
        [self error:@"The update feed was empty or too large."];
        return;
    }
    [self receiveFeed:data response:response requestedURL:url];
}

- (void)receiveFeed:(NSData *)data response:(NSHTTPURLResponse *)response requestedURL:(NSURL *)feedURL {
    char version[64], address[4096], notes[16384];
    BOOL switching = [self switching];
    NSString *key = self.channels[self.selectedChannel][@"public_key"];
    NSData *release = data;
    uint8_t percentage = 100, required = 0;
    if ([feedURL isEqual:[self rolloutFeedURL]]) {
        NSMutableData *legacy = [NSMutableData dataWithLength:feedLimit];
        NSInteger length = JardVerifyRolloutFeed(data.bytes, data.length, key.UTF8String, self.selectedChannel.UTF8String,
            legacy.mutableBytes, legacy.length, &percentage, &required);
        if (!length) {
            [self clearFeedCache];
            [self error:@"The signed update feed could not be verified."];
            return;
        }
        legacy.length = length;
        release = legacy;
    }
    NSString *baseline = switching ? @"0.0.0" : self.version;
    native_sdk_update_verify_result_t result = native_sdk_update_verify_feed(release.bytes, release.length, UTF8_BYTES(key),
        UTF8_BYTES(self.identifier), UTF8_BYTES(baseline), UTF8_BYTES(@"macos-aarch64"),
        version, sizeof version, address, sizeof address, notes, sizeof notes);
    if (result.ok != 1) {
        if (result.ok == 2 && !switching) {
            [self rememberFeed:data response:response url:feedURL];
            [self completeCheck:[self canInstallStagedUpdate] ? @"ready" : @"current" message:@"" failed:NO];
        } else {
            [self clearFeedCache];
            [self error:@"The signed update feed could not be verified."];
        }
        return;
    }
    NSString *next = [[NSString alloc] initWithBytes:version length:result.version_len encoding:NSUTF8StringEncoding];
    NSString *digest = [[NSString alloc] initWithBytes:result.sha256 length:sizeof result.sha256 encoding:NSASCIIStringEncoding];
    NSURL *url = httpsURL([[NSString alloc] initWithBytes:address length:result.archive_url_len encoding:NSUTF8StringEncoding]);
    if (!next.length || !archiveDigest(digest) || !url || !result.archive_bytes || result.archive_bytes > archiveLimit) {
        [self clearFeedCache];
        [self error:@"The signed update contained invalid download details."];
        return;
    }
    [self rememberFeed:data response:response url:feedURL];
    if (!switching && !required && percentage < 100 && [self rolloutBucket] >= percentage) {
        [self completeCheck:[self canInstallStagedUpdate] ? @"ready" : @"current" message:@"" failed:NO];
        return;
    }
    if (rejectedArchive(self.installed, digest)) {
        if ([self.stageDigest isEqual:digest]) [self discardStage];
        [self error:@"This update failed to start. Jaro will check again for a different build."];
        return;
    }
    self.transferVersion = next;
    self.transferNotes = [[NSString alloc] initWithBytes:notes length:result.release_notes_len encoding:NSUTF8StringEncoding] ?: @"";
    self.transferChannel = self.selectedChannel;
    if (![self replaceable]) {
        self.availableVersion = next;
        self.notes = self.transferNotes;
        self.releaseDownload = httpsURL(self.channels[self.selectedChannel][@"release_url"]) ?: (switching || [self.selectedChannel isEqual:@"dev"] ? url : nil);
        [self completeCheck:@"download" message:@"Install the new version from a writable Applications folder." failed:NO];
        return;
    }
    if (switching && ![digest isEqual:self.confirmedDigest]) {
        if (self.stage && ![self.stageChannel isEqual:self.buildChannel]) [self discardStage];
        self.availableVersion = next;
        self.notes = self.transferNotes;
        self.offerDigest = digest;
        self.confirmedDigest = nil;
        [self completeCheck:[self canInstallStagedUpdate] ? @"ready" : @"switch" message:@"" failed:NO];
        return;
    }
    if (self.stage && [next isEqual:self.stageVersion] &&
        [self.stageChannel isEqual:self.selectedChannel] && [self.stageDigest isEqual:digest]) {
        [self completeCheck:@"ready" message:@"" failed:NO];
        return;
    }
    self.archiveBytes = result.archive_bytes;
    self.sha256 = digest;
    [self downloadArchive:url];
}

- (void)downloadArchive:(NSURL *)url {
    JardArchiveTransfer *archive = JardArchiveTransfer.new;
    archive.url = url;
    archive.identifier = self.identifier;
    archive.channel = self.transferChannel;
    archive.digest = self.sha256;
    archive.size = self.archiveBytes;
    archive.authenticationDelegate = self;
    NSUInteger generation = self.generation;
    __weak typeof(self) weakSelf = self;
    archive.completion = ^(NSURL *file, NSString *failure) {
        JardUpdater *updater = weakSelf;
        if (!updater || updater.generation != generation || updater.closed || updater.quitting) {
            if (file) [NSFileManager.defaultManager removeItemAtURL:file error:nil];
            return;
        }
        updater.archive = nil;
        if ([updater updatesDisabled] || ![updater.selectedChannel isEqual:[updater preferredChannel]]) {
            if (file) [NSFileManager.defaultManager removeItemAtURL:file error:nil];
            [updater check];
        } else if (file) [updater stageArchive:file];
        else [updater error:failure ?: @"Jard could not download the update."];
    };
    self.archive = archive;
    [self setState:@"downloading" message:@""];
    if (self.archive == archive) [archive start];
}

- (void)stageArchive:(NSURL *)location {
    NSFileManager *manager = NSFileManager.defaultManager;
    NSURL *stage = [self.installed.URLByDeletingLastPathComponent URLByAppendingPathComponent:
        [self.installationStagePrefix stringByAppendingString:NSUUID.UUID.UUIDString] isDirectory:YES];
    NSURL *archive = [stage URLByAppendingPathComponent:@"update.zip"];
    NSError *error = nil;
    BOOL created = [manager createDirectoryAtURL:stage withIntermediateDirectories:NO attributes:@{NSFilePosixPermissions: @0700} error:&error];
    int lease = created ? lockStage(stage) : -1;
    if (lease < 0 || ![manager moveItemAtURL:location toURL:archive error:&error]) {
        if (lease >= 0) {
            [manager removeItemAtURL:stage error:nil];
            close(lease);
        }
        [manager removeItemAtURL:location error:nil];
        [self error:error.localizedDescription ?: @"Could not stage the update."];
        return;
    }
    NSURL *installed = self.installed;
    NSString *version = self.transferVersion, *notes = self.transferNotes, *digest = self.sha256, *channel = self.transferChannel;
    NSString *runningDevelopment = [self.buildChannel isEqual:@"dev"] ? self.version : nil;
    NSUInteger generation = self.generation;
    uint64_t size = self.archiveBytes;
    [self setState:@"staging" message:@""];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSString *failure = nil;
        NSURL *candidate = nil;
        if (!native_sdk_update_verify_archive(UTF8_BYTES(archive.path), size, UTF8_BYTES(digest))) {
            failure = @"The downloaded update did not match its signed size and digest.";
        } else {
            NSTask *extract = NSTask.new;
            extract.executableURL = [NSURL fileURLWithPath:@"/usr/bin/ditto"];
            extract.arguments = @[@"-x", @"-k", archive.path, stage.path];
            extract.standardOutput = NSFileHandle.fileHandleWithNullDevice;
            extract.standardError = NSFileHandle.fileHandleWithNullDevice;
            if ([extract launchAndReturnError:nil]) {
                [extract waitUntilExit];
                [manager removeItemAtURL:archive error:nil];
                NSArray<NSURL *> *entries = [manager contentsOfDirectoryAtURL:stage includingPropertiesForKeys:nil options:0 error:nil];
                if (extract.terminationStatus == 0 && entries.count == 1 &&
                    [entries.firstObject.pathExtension.lowercaseString isEqual:@"app"]) {
                    candidate = entries.firstObject;
                }
            }
            if (!candidate) failure = @"The update archive must contain exactly one application.";
            else if (!JardVerifyUpdateBundle(installed, candidate, version, &failure)) candidate = nil;
            else if (![bundleChannel(candidate) isEqual:channel]) {
                candidate = nil;
                failure = @"The signed application belongs to a different update channel.";
            }
        }
        NSString *displayNotes = candidate && [channel isEqual:@"dev"]
            ? (developmentReleaseNotes(candidate, version, runningDevelopment) ?: notes) : notes;
        dispatch_async(dispatch_get_main_queue(), ^{
            BOOL policyChanged = [self updatesDisabled] || ![self.selectedChannel isEqual:[self preferredChannel]];
            if (self.closed || self.quitting || generation != self.generation || !candidate || policyChanged) {
                [manager removeItemAtURL:stage error:nil];
                close(lease);
                if (!self.closed && !self.quitting && generation == self.generation) {
                    if (policyChanged) {
                        self.phase = @"idle";
                        [self check];
                    }
                    else [self error:failure ?: @"The update could not be staged."];
                }
                return;
            }
            NSURL *previous = self.stage;
            int previousLease = self.stageLease;
            self.stage = stage;
            self.stageLease = lease;
            self.candidate = candidate;
            self.stageChannel = channel;
            self.stageDigest = digest;
            self.stageVersion = version;
            self.stageNotes = displayNotes;
            self.availableVersion = self.notes = nil;
            [self persistStage];
            if (previous && isSiblingStage(installed, previous)) [manager removeItemAtURL:previous error:nil];
            if (previousLease >= 0) close(previousLease);
            [self completeCheck:@"ready" message:@"" failed:NO];
        });
    });
}

- (void)checkBeforeRestart:(void (^)(void))completion {
    if (self.quitting || self.restartCheck || self.verifyingInstall || ![self canInstallStagedUpdate]) {
        completion();
        return;
    }
    [self stopTransfer];
    self.phase = @"ready"; // Clear any in-progress verification before the final request.
    self.restartCheck = completion;
    [self check];
}

- (BOOL)prepareToInstallOnQuit {
    if (self.verifyingInstall) return NO;
    self.quitting = YES;
    self.restartCheck = nil;
    [self stopTransfer];
    return [self canInstallStagedUpdate];
}

- (void)resumeAfterCancelledQuit {
    if (!self.quitting || self.closed || self.verifyingInstall) return;
    self.quitting = NO;
    BOOL staged = [self canInstallStagedUpdate];
    [self setState:staged ? @"ready" : @"idle" message:@""];
    if (!staged) [self restoreStagedUpdateWithCompletion:nil];
}

- (void)openRelease {
    NSURL *url = self.releaseDownload ?: [NSURL URLWithString:@"https://github.com/AdamGera/Jaro/releases/latest"];
    [NSWorkspace.sharedWorkspace openURL:url];
}

- (BOOL)openStagedReleaseNotesForVersion:(NSString *)version channel:(NSString *)channel reason:(NSString **)reason {
    NSURL *url = [self stagedReleaseNotesURL];
    if (![self canInstallStagedUpdate] || ![self.stageVersion isEqual:version] || ![self.stageChannel isEqual:channel] || !url) {
        if (reason) *reason = @"Release notes are unavailable for this update.";
        return NO;
    }
    BOOL opened = [NSWorkspace.sharedWorkspace openURL:url];
    if (!opened && reason) *reason = @"Could not open the release notes.";
    return opened;
}

- (void)restartWithCompletion:(JardUpdateInstallReply)completion {
    [self verifyInstallAndRelaunch:YES completion:completion];
}

- (void)installOnQuitWithCompletion:(JardUpdateInstallReply)completion {
    [self verifyInstallAndRelaunch:NO completion:completion];
}

- (void)verifyInstallAndRelaunch:(BOOL)relaunch completion:(JardUpdateInstallReply)completion {
    if (self.verifyingInstall || ![self canInstallStagedUpdate] ||
        (relaunch && (self.quitting || self.restartCheck || ![self.phase isEqual:@"ready"])) ||
        !isSiblingStage(self.installed, self.stage)) {
        completion(NO, @"There is no verified update ready to restart.");
        return;
    }
    int lease = fcntl(self.stageLease, F_DUPFD_CLOEXEC, 0);
    if (lease < 0) {
        completion(NO, @"The staged update is no longer available.");
        return;
    }
    if (!relaunch && !self.quitting) [self prepareToInstallOnQuit];
    NSUInteger generation = self.generation;
    NSURL *installed = self.installed, *stage = self.stage, *candidate = self.candidate;
    NSString *version = self.stageVersion, *channel = self.stageChannel, *selection = self.selectedChannel;
    self.verifyingInstall = YES;
    dispatch_async(stageMaintenanceQueue(), ^{
        struct stat installedIdentity = {0}, stageIdentity = {0}, candidateIdentity = {0};
        BOOL identified = lstat(installed.fileSystemRepresentation, &installedIdentity) == 0 &&
            fstat(lease, &stageIdentity) == 0 && directoryMatchesIdentity(stage, &stageIdentity) &&
            lstat(candidate.fileSystemRepresentation, &candidateIdentity) == 0;
        NSString *failure = nil;
        BOOL verified = identified && [self verifyCandidate:candidate version:version channel:channel reason:&failure];
        dispatch_async(dispatch_get_main_queue(), ^{
            self.verifyingInstall = NO;
            BOOL authorized = generation == self.generation && [self canInstallStagedUpdate] &&
                [self.stage isEqual:stage] && [self.candidate isEqual:candidate] &&
                [self.stageChannel isEqual:channel] && [self.stageVersion isEqual:version] &&
                [selection isEqual:self.selectedChannel] && [selection isEqual:[self preferredChannel]] &&
                (!relaunch || (!self.quitting && [self.phase isEqual:@"ready"]));
            BOOL sameDirectories = verified && directoryMatchesIdentity(installed, &installedIdentity) &&
                directoryMatchesIdentity(stage, &stageIdentity) && directoryMatchesIdentity(candidate, &candidateIdentity);
            NSString *installFailure = failure ?: @"The staged update changed during verification.";
            BOOL applied = authorized && sameDirectories && [self swapVerifiedUpdateAndRelaunch:relaunch reason:&installFailure];
            close(lease);
            completion(applied, applied ? nil : installFailure);
        });
    });
}

- (BOOL)swapVerifiedUpdateAndRelaunch:(BOOL)relaunch reason:(NSString **)reason {
    NSTask *reopen = nil;
    if (relaunch) {
        reopen = scheduleUpdateRelaunch(self.installed, self.candidate, self.version, self.stageDigest, reason);
        if (!reopen) return NO;
    }
    if (!JardSwapUpdateBundles(self.installed, self.candidate, reason)) {
        [reopen terminate];
        return NO;
    }
    [NSUserDefaults.standardUserDefaults setObject:@{@"installed": self.installed.path, @"stage": self.stage.path,
        @"version": self.stageVersion, @"channel": self.stageChannel} forKey:cleanupKey];
    [NSUserDefaults.standardUserDefaults removeObjectForKey:pendingKey];
    [self setState:@"applying" message:@""];
    return YES;
}

- (void)cancel {
    self.closed = YES;
    self.restartCheck = nil;
    [self stopTransfer];
    [self releaseStageLease];
}

@end
