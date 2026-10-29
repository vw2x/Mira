#import "ish_rootfs.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static NSString *const MiraISHRootfsStamp = @".mira-ish-rootfs.stamp";

static BOOL rootfs_error(NSError **error, NSString *message) {
    if (error != NULL) {
        *error = [NSError errorWithDomain:@"MiraISHRootfs" code:1
                                userInfo:@{NSLocalizedDescriptionKey: message}];
    }
    return NO;
}

static BOOL validate_rootfs(NSString *path, NSFileManager *manager, NSError **error) {
    NSDictionary *root = [manager attributesOfItemAtPath:path error:error];
    if (root == nil) {
        return NO;
    }
    if (![root.fileType isEqualToString:NSFileTypeDirectory]) {
        return rootfs_error(error, @"iSH rootfs must be a directory, not a file or symbolic link; existing files were preserved.");
    }
    NSDictionary *data = [manager attributesOfItemAtPath:[path stringByAppendingPathComponent:@"data"] error:error];
    if (data == nil) {
        return NO;
    }
    NSDictionary *meta = [manager attributesOfItemAtPath:[path stringByAppendingPathComponent:@"meta.db"] error:error];
    if (meta == nil) {
        return NO;
    }
    if (![data.fileType isEqualToString:NSFileTypeDirectory] ||
        ![meta.fileType isEqualToString:NSFileTypeRegular] || meta.fileSize == 0) {
        return rootfs_error(error, @"iSH rootfs is incomplete or has invalid file types; existing files were preserved. Repair requires an explicit recovery operation.");
    }
    return YES;
}

static NSString *read_stamp(NSString *root, NSFileManager *manager) {
    NSString *path = [root stringByAppendingPathComponent:MiraISHRootfsStamp];
    NSDictionary *attributes = [manager attributesOfItemAtPath:path error:nil];
    if (![attributes.fileType isEqualToString:NSFileTypeRegular]) {
        return nil;
    }
    return [NSString stringWithContentsOfFile:path encoding:NSUTF8StringEncoding error:nil];
}

MiraISHRootfsResult mira_ish_rootfs_prepare(NSString *rootPath, NSURL *bundleRoot,
                                          NSFileManager *manager, NSError **error) {
    NSError *lookupError = nil;
    NSDictionary *existing = [manager attributesOfItemAtPath:rootPath error:&lookupError];
    if (existing != nil) {
        if (!validate_rootfs(rootPath, manager, error)) {
            return MiraISHRootfsFailed;
        }
        NSString *installedStamp = read_stamp(rootPath, manager);
        NSString *bundleStamp = bundleRoot == nil ? nil : read_stamp(bundleRoot.path, manager);
        if (installedStamp != nil && bundleStamp != nil && [installedStamp isEqualToString:bundleStamp]) {
            return MiraISHRootfsReused;
        }
        return MiraISHRootfsUpdatePending;
    }
    if (![lookupError.domain isEqualToString:NSCocoaErrorDomain] ||
        (lookupError.code != NSFileNoSuchFileError && lookupError.code != NSFileReadNoSuchFileError)) {
        if (error != NULL) {
            *error = lookupError;
        }
        return MiraISHRootfsFailed;
    }
    if (bundleRoot == nil || !validate_rootfs(bundleRoot.path, manager, error)) {
        if (bundleRoot == nil) {
            rootfs_error(error, @"Bundled iSH rootfs is missing. Run tools/ios/prepare-ish-rootfs.sh during the iOS build.");
        }
        return MiraISHRootfsFailed;
    }

    NSString *parent = [rootPath stringByDeletingLastPathComponent];
    if (![manager createDirectoryAtPath:parent withIntermediateDirectories:YES attributes:nil error:error]) {
        return MiraISHRootfsFailed;
    }
    NSString *template = [parent stringByAppendingPathComponent:@".mira-ish-install.XXXXXX"];
    char *temporary = strdup(template.fileSystemRepresentation);
    if (temporary == NULL || mkdtemp(temporary) == NULL) {
        int savedError = temporary == NULL ? ENOMEM : errno;
        free(temporary);
        if (error != NULL) {
            *error = [NSError errorWithDomain:NSPOSIXErrorDomain code:savedError userInfo:nil];
        }
        return MiraISHRootfsFailed;
    }
    NSString *staging = [NSString stringWithUTF8String:temporary];
    free(temporary);
    NSURL *stagedRoot = [NSURL fileURLWithPath:[staging stringByAppendingPathComponent:@"root"] isDirectory:YES];
    MiraISHRootfsResult result = MiraISHRootfsFailed;
    if ([manager copyItemAtURL:bundleRoot toURL:stagedRoot error:error] &&
        validate_rootfs(stagedRoot.path, manager, error)) {
        // Same-filesystem rename publishes the pair together; never replace a concurrent installation.
        if (renamex_np(stagedRoot.fileSystemRepresentation, rootPath.fileSystemRepresentation, RENAME_EXCL) == 0) {
            result = MiraISHRootfsInstalled;
        } else if (error != NULL) {
            *error = [NSError errorWithDomain:NSPOSIXErrorDomain code:errno userInfo:nil];
        }
    }
    [manager removeItemAtPath:staging error:nil];
    return result;
}
