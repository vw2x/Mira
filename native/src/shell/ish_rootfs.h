#ifndef MIRA_ISH_ROOTFS_H
#define MIRA_ISH_ROOTFS_H

#import <Foundation/Foundation.h>

typedef NS_ENUM(NSInteger, MiraISHRootfsResult) {
    MiraISHRootfsFailed = -1,
    MiraISHRootfsReused = 0,
    MiraISHRootfsInstalled,
    MiraISHRootfsUpdatePending,
};

// Existing fakefs data and metadata remain a pair; runtime changes need an explicit migration.
MiraISHRootfsResult mira_ish_rootfs_prepare(NSString *rootPath, NSURL *bundleRoot,
                                          NSFileManager *manager, NSError **error);

#endif /* MIRA_ISH_ROOTFS_H */
