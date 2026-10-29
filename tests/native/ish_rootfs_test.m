#import <Foundation/Foundation.h>
#import "ish_rootfs.h"

static void require(BOOL condition, NSString *message) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message.UTF8String);
        exit(1);
    }
}

static void write_file(NSString *path, NSString *contents) {
    NSFileManager *manager = NSFileManager.defaultManager;
    require([manager createDirectoryAtPath:path.stringByDeletingLastPathComponent
              withIntermediateDirectories:YES attributes:nil error:NULL], @"create fixture directory");
    require([contents writeToFile:path atomically:YES encoding:NSUTF8StringEncoding error:NULL], @"write fixture");
}

static NSString *read_file(NSString *path) {
    return [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:NULL];
}

static void make_root(NSString *root, NSString *stamp) {
    write_file([root stringByAppendingPathComponent:@"data/root/notes.txt"], @"fixture user data");
    write_file([root stringByAppendingPathComponent:@"meta.db"], @"fixture metadata paired with data");
    if (stamp != nil) {
        write_file([root stringByAppendingPathComponent:@".mira-ish-rootfs.stamp"], stamp);
    }
}

static void require_preserved(NSString *root, NSString *stamp) {
    require([read_file([root stringByAppendingPathComponent:@"data/root/notes.txt"]) isEqualToString:@"fixture user data"], @"user data preserved");
    require([read_file([root stringByAppendingPathComponent:@"meta.db"]) isEqualToString:@"fixture metadata paired with data"], @"paired metadata preserved");
    NSString *actualStamp = read_file([root stringByAppendingPathComponent:@".mira-ish-rootfs.stamp"]);
    require(stamp == nil ? actualStamp == nil : [actualStamp isEqualToString:stamp], @"installed stamp preserved");
}

static void require_no_staging(NSString *parent) {
    NSArray<NSString *> *children = [NSFileManager.defaultManager contentsOfDirectoryAtPath:parent error:NULL];
    for (NSString *name in children) {
        require(![name hasPrefix:@".mira-ish-install."], @"owned staging cleaned");
    }
}

@interface FailingFileManager : NSFileManager
@property(nonatomic) NSInteger failureMode;
@property(nonatomic, copy) NSString *installRoot;
@end

@implementation FailingFileManager
- (BOOL)copyItemAtURL:(NSURL *)source toURL:(NSURL *)destination error:(NSError **)error {
    require(![destination.path isEqualToString:self.installRoot], @"never copy directly into live rootfs");
    if (self.failureMode == 1) {
        write_file([destination.path stringByAppendingPathComponent:@"partial"], @"partial copy");
        if (error != NULL) {
            *error = [NSError errorWithDomain:NSPOSIXErrorDomain code:ENOSPC userInfo:nil];
        }
        return NO;
    }
    if (![super copyItemAtURL:source toURL:destination error:error]) {
        return NO;
    }
    if (self.failureMode == 2) {
        require([self removeItemAtPath:[destination.path stringByAppendingPathComponent:@"meta.db"] error:NULL], @"inject damaged staged metadata");
    } else if (self.failureMode == 3) {
        make_root(self.installRoot, @"concurrent installation");
    }
    return YES;
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        require(argc == 2, @"fixture path argument");
        NSString *base = [NSString stringWithUTF8String:argv[1]];
        NSString *bundle = [base stringByAppendingPathComponent:@"bundle"];
        make_root(bundle, @"runtime-v1");
        NSURL *bundleURL = [NSURL fileURLWithPath:bundle isDirectory:YES];
        NSFileManager *manager = NSFileManager.defaultManager;
        NSError *error = nil;

        // 1. A fresh install publishes data and metadata together and removes staging.
        NSString *first = [base stringByAppendingPathComponent:@"fresh/default"];
        require(mira_ish_rootfs_prepare(first, bundleURL, manager, &error) == MiraISHRootfsInstalled, @"first install succeeds");
        require_preserved(first, @"runtime-v1");
        require_no_staging(first.stringByDeletingLastPathComponent);

        // 2. Reopening the same runtime does not replace user changes.
        write_file([first stringByAppendingPathComponent:@"data/root/extra-package"], @"installed later");
        require(mira_ish_rootfs_prepare(first, bundleURL, manager, &error) == MiraISHRootfsReused, @"same stamp reuses environment");
        require([read_file([first stringByAppendingPathComponent:@"data/root/extra-package"]) isEqualToString:@"installed later"], @"user installation survives reuse");

        // 3. A changed bundle must not destroy or rewrite the installed pair or stamp.
        write_file([bundle stringByAppendingPathComponent:@".mira-ish-rootfs.stamp"], @"runtime-v2");
        require(mira_ish_rootfs_prepare(first, bundleURL, manager, &error) == MiraISHRootfsUpdatePending, @"changed runtime awaits migration");
        require_preserved(first, @"runtime-v1");
        require([read_file([first stringByAppendingPathComponent:@"data/root/extra-package"]) isEqualToString:@"installed later"], @"user installation survives bundle upgrade");

        // 4. Legacy environments remain usable without a stamp or an available bundle.
        NSString *legacy = [base stringByAppendingPathComponent:@"legacy"];
        make_root(legacy, nil);
        require(mira_ish_rootfs_prepare(legacy, bundleURL, manager, &error) == MiraISHRootfsUpdatePending, @"legacy stamp absence preserves environment");
        require(mira_ish_rootfs_prepare(legacy, nil, manager, &error) == MiraISHRootfsUpdatePending, @"missing bundle preserves existing environment");
        require_preserved(legacy, nil);

        // 5. An incomplete live rootfs is never silently replaced.
        NSString *partial = [base stringByAppendingPathComponent:@"partial"];
        write_file([partial stringByAppendingPathComponent:@"data/root/recover-me"], @"only remaining copy");
        error = nil;
        require(mira_ish_rootfs_prepare(partial, bundleURL, manager, &error) == MiraISHRootfsFailed && error != nil, @"partial live installation reports failure");
        require([read_file([partial stringByAppendingPathComponent:@"data/root/recover-me"]) isEqualToString:@"only remaining copy"], @"partial live data preserved");
        require(![manager fileExistsAtPath:[partial stringByAppendingPathComponent:@"meta.db"]], @"no overlay metadata installed");

        // 6. Invalid live types and empty metadata are rejected without deletion.
        NSString *invalid = [base stringByAppendingPathComponent:@"invalid"];
        make_root(invalid, @"old");
        write_file([invalid stringByAppendingPathComponent:@"meta.db"], @"");
        require(mira_ish_rootfs_prepare(invalid, bundleURL, manager, &error) == MiraISHRootfsFailed, @"empty metadata is not a complete rootfs");
        require([manager fileExistsAtPath:[invalid stringByAppendingPathComponent:@"data/root/notes.txt"]], @"invalid metadata does not delete user files");
        NSString *link = [base stringByAppendingPathComponent:@"root-link"];
        require([manager createSymbolicLinkAtPath:link withDestinationPath:first error:NULL], @"create synthetic root symlink");
        require(mira_ish_rootfs_prepare(link, bundleURL, manager, &error) == MiraISHRootfsFailed, @"root symlink rejected");
        require_preserved(first, @"runtime-v1");

        // 7. Missing or invalid bundled data must leave the install destination absent.
        NSString *absent = [base stringByAppendingPathComponent:@"absent/default"];
        require(mira_ish_rootfs_prepare(absent, nil, manager, &error) == MiraISHRootfsFailed, @"missing bundle fails fresh install");
        require(mira_ish_rootfs_prepare(absent, [NSURL fileURLWithPath:invalid], manager, &error) == MiraISHRootfsFailed, @"invalid bundle fails fresh install");
        require(![manager fileExistsAtPath:absent], @"invalid source never publishes destination");

        // 8. Simulate a copy failure after writing part of staging (e.g. disk full).
        FailingFileManager *failing = [FailingFileManager new];
        failing.installRoot = [base stringByAppendingPathComponent:@"copy-failure/default"];
        failing.failureMode = 1;
        error = nil;
        require(mira_ish_rootfs_prepare(failing.installRoot, bundleURL, failing, &error) == MiraISHRootfsFailed && error.code == ENOSPC, @"partial copy error survives");
        require(![manager fileExistsAtPath:failing.installRoot], @"partial copy never becomes live");
        require_no_staging(failing.installRoot.stringByDeletingLastPathComponent);

        // 9. A successful copy with missing metadata also fails before publication.
        failing.installRoot = [base stringByAppendingPathComponent:@"validation-failure/default"];
        failing.failureMode = 2;
        require(mira_ish_rootfs_prepare(failing.installRoot, bundleURL, failing, &error) == MiraISHRootfsFailed, @"staged validation failure");
        require(![manager fileExistsAtPath:failing.installRoot], @"invalid staged pair never becomes live");
        require_no_staging(failing.installRoot.stringByDeletingLastPathComponent);

        // 10. If another installer creates the target, publication fails without overwriting it.
        failing.installRoot = [base stringByAppendingPathComponent:@"publish-race/default"];
        failing.failureMode = 3;
        error = nil;
        require(mira_ish_rootfs_prepare(failing.installRoot, bundleURL, failing, &error) == MiraISHRootfsFailed && error != nil, @"publication does not overwrite concurrent rootfs");
        require_preserved(failing.installRoot, @"concurrent installation");
        require_no_staging(failing.installRoot.stringByDeletingLastPathComponent);
        printf("PASS: 10 rootfs preservation and installation cases\n");
    }
    return 0;
}
