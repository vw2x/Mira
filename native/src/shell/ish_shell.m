#import <Foundation/Foundation.h>

#include "shell/ish_shell.h"
#include "shell/ish_hostfs.h"
#include "shell/ish_rootfs.h"
#include "mira_pty_ios_shim.h"

#include <TargetConditionals.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#if defined(__APPLE__)
#include <sys/resource.h>
#endif

#include "debug.h"
#include "fs/dev.h"
#include "fs/devices.h"
#include "fs/path.h"
#include "fs/sock.h"
#include "fs/tty.h"
#include "kernel/calls.h"
#include "kernel/fs.h"
#include "kernel/init.h"
#include "kernel/task.h"

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define MIRA_ISH_OUTPUT_INITIAL_CAPACITY 16384U
#define MIRA_ISH_OUTPUT_MAX_BUFFER (1024U * 1024U)

typedef struct mira_ish_shell mira_ish_shell_t;

struct mira_ish_shell {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    struct tty *tty;
    pid_t_ pid;
    int rows;
    int columns;
    int closed;
    int exited;
    int exit_status;
    char *output;
    size_t output_length;
    size_t output_capacity;
};

static pthread_mutex_t g_ish_boot_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_ish_create_mutex = PTHREAD_MUTEX_INITIALIZER;
static int g_ish_booted = 0;
static int g_ish_boot_errno = 0;
static char g_ish_root_path[PATH_MAX] = {0};
static char g_ish_last_error[512] = {0};
static mira_ish_shell_t *g_creating_shell = NULL;

static void mira_ish_set_error(const char *format, ...) {
    va_list args;
    va_start(args, format);
    vsnprintf(g_ish_last_error, sizeof(g_ish_last_error), format, args);
    va_end(args);
}

const char *mira_ish_shell_last_error(void) {
    return g_ish_last_error[0] != '\0' ? g_ish_last_error : NULL;
}

int mira_ish_shell_available(void) {
#if defined(__APPLE__) && TARGET_OS_IPHONE
    return 1;
#else
    return 0;
#endif
}

static int mira_ish_append_output_locked(mira_ish_shell_t *shell, const void *data, size_t length) {
    if (shell == NULL || data == NULL || length == 0 || shell->closed) {
        return 0;
    }

    if (shell->output_length > MIRA_ISH_OUTPUT_MAX_BUFFER) {
        while (!shell->closed && shell->output_length > MIRA_ISH_OUTPUT_MAX_BUFFER / 2U) {
            pthread_cond_wait(&shell->cond, &shell->mutex);
        }
        if (shell->closed) {
            return 0;
        }
    }

    if (shell->output_length == SIZE_MAX || length > SIZE_MAX - shell->output_length - 1U || length > (size_t) INT_MAX) {
        return -1;
    }
    size_t needed = shell->output_length + length + 1U;

    if (needed > shell->output_capacity) {
        size_t next = shell->output_capacity == 0 ? MIRA_ISH_OUTPUT_INITIAL_CAPACITY : shell->output_capacity;
        while (next < needed) {
            if (next > SIZE_MAX / 2U) {
                next = needed;
                break;
            }
            next *= 2U;
        }
        char *resized = (char *) realloc(shell->output, next);
        if (resized == NULL) {
            return -1;
        }
        shell->output = resized;
        shell->output_capacity = next;
    }

    memcpy(shell->output + shell->output_length, data, length);
    shell->output_length += length;
    shell->output[shell->output_length] = '\0';
    pthread_cond_broadcast(&shell->cond);
    return (int) length;
}

static int mira_ish_tty_init(struct tty *tty) {
    if (g_creating_shell == NULL) {
        return _EIO;
    }
    tty->data = g_creating_shell;
    g_creating_shell->tty = tty;
    return 0;
}

static int mira_ish_tty_write(struct tty *tty, const void *buf, size_t len, bool blocking) {
    (void) blocking;
    mira_ish_shell_t *shell = (mira_ish_shell_t *) tty->data;
    if (shell == NULL) {
        return _EIO;
    }
    pthread_mutex_lock(&shell->mutex);
    int written = mira_ish_append_output_locked(shell, buf, len);
    pthread_mutex_unlock(&shell->mutex);
    if (written < 0) {
        return _ENOMEM;
    }
    return written;
}

static void mira_ish_tty_cleanup(struct tty *tty) {
    mira_ish_shell_t *shell = (mira_ish_shell_t *) tty->data;
    if (shell != NULL) {
        pthread_mutex_lock(&shell->mutex);
        if (shell->tty == tty) {
            shell->tty = NULL;
        }
        pthread_cond_broadcast(&shell->cond);
        pthread_mutex_unlock(&shell->mutex);
    }
    tty->data = NULL;
}

static const struct tty_driver_ops mira_ish_tty_ops = {
    .init = mira_ish_tty_init,
    .write = mira_ish_tty_write,
    .cleanup = mira_ish_tty_cleanup,
};

static struct tty_driver mira_ish_pty_driver = {.ops = &mira_ish_tty_ops};
DEFINE_TTY_DRIVER(mira_ish_console_driver, &mira_ish_tty_ops, TTY_CONSOLE_MAJOR, 8);

static void mira_ish_exit_hook(struct task *task, int code) {
    if (task == NULL || task->group == NULL || !task_is_leader(task)) {
        return;
    }
    pid_t_ pid = task->pid;

    for (unsigned i = 0; i < mira_ish_pty_driver.limit; ++i) {
        struct tty *tty = mira_ish_pty_driver.ttys[i];
        if (tty == NULL || tty == (void *) 1) {
            continue;
        }
        mira_ish_shell_t *shell = (mira_ish_shell_t *) tty->data;
        if (shell == NULL || shell->pid != pid) {
            continue;
        }
        pthread_mutex_lock(&shell->mutex);
        shell->exited = 1;
        shell->exit_status = code;
        pthread_cond_broadcast(&shell->cond);
        pthread_mutex_unlock(&shell->mutex);
        break;
    }
}

static void mira_ish_die_handler(const char *message) {
    mira_ish_set_error("iSH fatal error: %s", message == NULL ? "unknown" : message);
}

static int mira_ish_copy_bundle_root_if_needed(NSString *rootPath) {
    NSURL *bundleRoot = [NSBundle.mainBundle URLForResource:@"MiraISHRoot" withExtension:@"fakefs"];
    NSError *error = nil;
    MiraISHRootfsResult result = mira_ish_rootfs_prepare(rootPath, bundleRoot, NSFileManager.defaultManager, &error);
    if (result == MiraISHRootfsFailed) {
        mira_ish_set_error("iSH rootfs preparation failed; existing files preserved: %s", error.localizedDescription.UTF8String ?: "unknown");
        errno = EIO;
        return -1;
    }
    if (result == MiraISHRootfsUpdatePending) {
        fprintf(stderr, "Mira iSH: installed rootfs preserved; bundled runtime update pending explicit migration (stamp changed or unavailable).\n");
    }
    return 0;
}

static int mira_ish_prepare_root_path(char *out, size_t out_size) {
    @autoreleasepool {
        NSArray<NSURL *> *urls = [NSFileManager.defaultManager URLsForDirectory:NSApplicationSupportDirectory inDomains:NSUserDomainMask];
        NSURL *applicationSupport = urls.firstObject;
        if (applicationSupport == nil) {
            mira_ish_set_error("iSH rootfs failed: Application Support directory unavailable");
            errno = ENOENT;
            return -1;
        }
        NSURL *rootURL = [[[applicationSupport URLByAppendingPathComponent:@"Mira" isDirectory:YES]
                           URLByAppendingPathComponent:@"iSH" isDirectory:YES]
                          URLByAppendingPathComponent:@"default" isDirectory:YES];
        if (mira_ish_copy_bundle_root_if_needed(rootURL.path) != 0) {
            return -1;
        }
        snprintf(out, out_size, "%s", rootURL.fileSystemRepresentation);
        return 0;
    }
}

static void mira_ish_create_device_nodes(void) {
    for (int i = 1; i <= 7; ++i) {
        char path[32];
        snprintf(path, sizeof(path), "/dev/tty%d", i);
        generic_mknodat(AT_PWD, path, S_IFCHR | 0666, dev_make(TTY_CONSOLE_MAJOR, i));
    }
    generic_mknodat(AT_PWD, "/dev/tty", S_IFCHR | 0666, dev_make(TTY_ALTERNATE_MAJOR, DEV_TTY_MINOR));
    generic_mknodat(AT_PWD, "/dev/console", S_IFCHR | 0666, dev_make(TTY_ALTERNATE_MAJOR, DEV_CONSOLE_MINOR));
    generic_mknodat(AT_PWD, "/dev/ptmx", S_IFCHR | 0666, dev_make(TTY_ALTERNATE_MAJOR, DEV_PTMX_MINOR));
    generic_mknodat(AT_PWD, "/dev/null", S_IFCHR | 0666, dev_make(MEM_MAJOR, DEV_NULL_MINOR));
    generic_mknodat(AT_PWD, "/dev/zero", S_IFCHR | 0666, dev_make(MEM_MAJOR, DEV_ZERO_MINOR));
    generic_mknodat(AT_PWD, "/dev/full", S_IFCHR | 0666, dev_make(MEM_MAJOR, DEV_FULL_MINOR));
    generic_mknodat(AT_PWD, "/dev/random", S_IFCHR | 0666, dev_make(MEM_MAJOR, DEV_RANDOM_MINOR));
    generic_mknodat(AT_PWD, "/dev/urandom", S_IFCHR | 0666, dev_make(MEM_MAJOR, DEV_URANDOM_MINOR));
    generic_mkdirat(AT_PWD, "/dev/pts", 0755);
    generic_mkdirat(AT_PWD, "/tmp", 01777);
    generic_setattrat(AT_PWD, "/", (struct attr) {.type = attr_mode, .mode = 0755}, false);
}

static int mira_ish_boot_locked(void) {
    if (g_ish_booted) {
        return 0;
    }
    if (g_ish_boot_errno != 0) {
        errno = g_ish_boot_errno;
        return -1;
    }

    char root_path[PATH_MAX];
    if (mira_ish_prepare_root_path(root_path, sizeof(root_path)) != 0) {
        g_ish_boot_errno = errno == 0 ? EIO : errno;
        return -1;
    }

#if defined(__APPLE__)
    int iopol_err = setiopolicy_np(1, IOPOL_SCOPE_PROCESS, 1);
    if (iopol_err != 0 && errno != EPERM) {
        fprintf(stderr, "Mira iSH: could not enable case sensitivity: %s\n", strerror(errno));
    }
    setgid(getgid());
    setuid(getuid());
#endif

    char data_path[PATH_MAX];
    snprintf(data_path, sizeof(data_path), "%s/data", root_path);
    int err = mount_root(&fakefs, data_path);
    if (err < 0) {
        mira_ish_set_error("iSH mount_root failed for %s: %s", data_path, strerror(-err));
        g_ish_boot_errno = EIO;
        errno = EIO;
        return -1;
    }

    err = become_first_process();
    if (err < 0) {
        mira_ish_set_error("iSH become_first_process failed: %s", strerror(-err));
        g_ish_boot_errno = EIO;
        errno = EIO;
        return -1;
    }

    mira_ish_create_device_nodes();
    (void) do_mount(&procfs, "proc", "/proc", "", 0);
    (void) do_mount(&devptsfs, "devpts", "/dev/pts", "", 0);
    err = mira_ish_hostfs_mount();
    if (err < 0) {
        mira_ish_set_error("iSH Mira filesystem mount failed: %s", strerror(-err));
        g_ish_boot_errno = EIO;
        errno = EIO;
        return -1;
    }
    (void) mira_ios_frida_loader_ensure_loaded();

    tty_drivers[TTY_CONSOLE_MAJOR] = &mira_ish_console_driver;
    set_console_device(TTY_CONSOLE_MAJOR, 1);
    exit_hook = mira_ish_exit_hook;
    die_handler = mira_ish_die_handler;

#if !TARGET_OS_SIMULATOR
    NSString *sockTmp = [NSTemporaryDirectory() stringByAppendingString:@"mira-ishsock"];
    sock_tmp_prefix = strdup(sockTmp.UTF8String);
#endif

    snprintf(g_ish_root_path, sizeof(g_ish_root_path), "%s", root_path);
    g_ish_booted = 1;
    return 0;
}

static int mira_ish_boot(void) {
    pthread_mutex_lock(&g_ish_boot_mutex);
    int rc = mira_ish_boot_locked();
    pthread_mutex_unlock(&g_ish_boot_mutex);
    return rc;
}

static void mira_ish_make_argv(const mira_shell_options_t *options, const char **exe_out, size_t *argc_out, char *argv_blob, size_t argv_blob_size) {
    const char *exe = "/bin/sh";
    if (options != NULL && options->shell_path != NULL && options->shell_path[0] != '\0') {
        exe = options->shell_path;
    }

    char *cursor = argv_blob;
    char *end = argv_blob + argv_blob_size;
    size_t argc = 0;

    if (options != NULL && options->argv != NULL && options->argv[0] != NULL) {
        exe = options->argv[0];
        for (char *const *arg = options->argv; *arg != NULL && cursor < end - 1; ++arg) {
            size_t len = strlen(*arg) + 1U;
            if (cursor + len >= end) {
                break;
            }
            memcpy(cursor, *arg, len);
            cursor += len;
            argc++;
        }
    } else {
        size_t len = strlen(exe) + 1U;
        if (cursor + len < end) {
            memcpy(cursor, exe, len);
            cursor += len;
            argc = 1;
        }
    }
    if (cursor < end) {
        *cursor = '\0';
    }
    *exe_out = exe;
    *argc_out = argc;
}

mira_ish_shell_t *mira_ish_shell_open(const mira_shell_options_t *options) {
    g_ish_last_error[0] = '\0';
    if (!mira_ish_shell_available()) {
        mira_ish_set_error("iSH backend is not available on this platform");
        errno = ENOSYS;
        return NULL;
    }
    if (mira_ish_boot() != 0) {
        return NULL;
    }

    mira_ish_shell_t *shell = (mira_ish_shell_t *) calloc(1, sizeof(*shell));
    if (shell == NULL) {
        return NULL;
    }
    pthread_mutex_init(&shell->mutex, NULL);
    pthread_cond_init(&shell->cond, NULL);
    shell->columns = options != NULL && options->columns > 0 ? options->columns : 80;
    shell->rows = options != NULL && options->rows > 0 ? options->rows : 24;

    pthread_mutex_lock(&g_ish_create_mutex);
    int err = become_new_init_child();
    if (err < 0) {
        pthread_mutex_unlock(&g_ish_create_mutex);
        mira_ish_set_error("iSH become_new_init_child failed: %s", strerror(-err));
        errno = EIO;
        mira_ish_shell_close(shell);
        mira_ish_shell_destroy(shell);
        return NULL;
    }

    g_creating_shell = shell;
    struct tty *tty = pty_open_fake(&mira_ish_pty_driver);
    g_creating_shell = NULL;
    if (IS_ERR(tty)) {
        pthread_mutex_unlock(&g_ish_create_mutex);
        mira_ish_set_error("iSH pty_open_fake failed: %s", strerror((int) -PTR_ERR(tty)));
        errno = EIO;
        mira_ish_shell_close(shell);
        mira_ish_shell_destroy(shell);
        return NULL;
    }

    lock(&tty->lock);
    tty_set_winsize(tty, (struct winsize_) {.col = shell->columns, .row = shell->rows});
    unlock(&tty->lock);

    char stdio_path[64];
    snprintf(stdio_path, sizeof(stdio_path), "/dev/pts/%d", tty->num);
    err = create_stdio(stdio_path, TTY_PSEUDO_SLAVE_MAJOR, tty->num);
    tty_release(tty);
    if (err < 0) {
        pthread_mutex_unlock(&g_ish_create_mutex);
        mira_ish_set_error("iSH create_stdio failed for %s: %s", stdio_path, strerror(-err));
        errno = EIO;
        mira_ish_shell_close(shell);
        mira_ish_shell_destroy(shell);
        return NULL;
    }

    char argv_blob[4096];
    memset(argv_blob, 0, sizeof(argv_blob));
    const char *exe = NULL;
    size_t argc = 0;
    mira_ish_make_argv(options, &exe, &argc, argv_blob, sizeof(argv_blob));
    static const char envp[] =
        "TERM=xterm-256color\0"
        "HOME=/root\0"
        "USER=root\0"
        "LOGNAME=root\0"
        "SHELL=/bin/sh\0"
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\0"
        "\0";

    err = do_execve(exe, argc, argv_blob, envp);
    if (err < 0) {
        pthread_mutex_unlock(&g_ish_create_mutex);
        mira_ish_set_error("iSH exec failed for %s: %s", exe, strerror(-err));
        errno = ENOENT;
        mira_ish_shell_close(shell);
        mira_ish_shell_destroy(shell);
        return NULL;
    }

    shell->pid = current->pid;
    task_start(current);
    current = NULL;
    pthread_mutex_unlock(&g_ish_create_mutex);
    return shell;
}

ssize_t mira_ish_shell_read(mira_ish_shell_t *shell, void *buffer, size_t length) {
    if (shell == NULL || buffer == NULL || length == 0) {
        errno = EINVAL;
        return -1;
    }
    pthread_mutex_lock(&shell->mutex);
    while (!shell->closed && shell->output_length == 0 && !shell->exited) {
        pthread_cond_wait(&shell->cond, &shell->mutex);
    }
    if (shell->output_length == 0 && (shell->closed || shell->exited)) {
        pthread_mutex_unlock(&shell->mutex);
        return 0;
    }
    size_t count = shell->output_length < length ? shell->output_length : length;
    memcpy(buffer, shell->output, count);
    shell->output_length -= count;
    if (shell->output_length > 0) {
        memmove(shell->output, shell->output + count, shell->output_length);
    }
    if (shell->output != NULL) {
        shell->output[shell->output_length] = '\0';
    }
    pthread_cond_broadcast(&shell->cond);
    pthread_mutex_unlock(&shell->mutex);
    return (ssize_t) count;
}

ssize_t mira_ish_shell_write(mira_ish_shell_t *shell, const void *buffer, size_t length) {
    if (shell == NULL || buffer == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (length == 0) {
        return 0;
    }
    pthread_mutex_lock(&shell->mutex);
    struct tty *tty = shell->tty;
    int closed = shell->closed || shell->exited;
    pthread_mutex_unlock(&shell->mutex);
    if (closed || tty == NULL) {
        return 0;
    }
    ssize_t written = tty_input(tty, (const char *) buffer, length, true);
    if (written < 0) {
        errno = EIO;
        return -1;
    }
    return written;
}

int mira_ish_shell_resize(mira_ish_shell_t *shell, int columns, int rows, int cell_width, int cell_height) {
    (void) cell_width;
    (void) cell_height;
    if (shell == NULL) {
        errno = EINVAL;
        return -1;
    }
    pthread_mutex_lock(&shell->mutex);
    if (columns > 0) shell->columns = columns;
    if (rows > 0) shell->rows = rows;
    struct tty *tty = shell->tty;
    int use_columns = shell->columns;
    int use_rows = shell->rows;
    pthread_mutex_unlock(&shell->mutex);
    if (tty != NULL) {
        lock(&tty->lock);
        tty_set_winsize(tty, (struct winsize_) {.col = use_columns, .row = use_rows});
        unlock(&tty->lock);
    }
    return 0;
}

int mira_ish_shell_wait_for(mira_ish_shell_t *shell) {
    if (shell == NULL) {
        errno = EINVAL;
        return -EINVAL;
    }
    pthread_mutex_lock(&shell->mutex);
    while (!shell->closed && !shell->exited) {
        pthread_cond_wait(&shell->cond, &shell->mutex);
    }
    int status = shell->exit_status;
    pthread_mutex_unlock(&shell->mutex);
    return status;
}

int mira_ish_shell_close(mira_ish_shell_t *shell) {
    if (shell == NULL) {
        errno = EINVAL;
        return -1;
    }
    pthread_mutex_lock(&shell->mutex);
    shell->closed = 1;
    struct tty *tty = shell->tty;
    shell->tty = NULL;
    if (tty != NULL) {
        lock(&tty->lock);
        if (tty->data == shell) {
            tty->data = NULL;
        }
        tty_hangup(tty);
        unlock(&tty->lock);
    }
    pthread_cond_broadcast(&shell->cond);
    pthread_mutex_unlock(&shell->mutex);
    return 0;
}

void mira_ish_shell_destroy(mira_ish_shell_t *shell) {
    if (shell == NULL) {
        return;
    }
    pthread_mutex_lock(&shell->mutex);
    shell->closed = 1;
    pthread_cond_broadcast(&shell->cond);
    pthread_mutex_unlock(&shell->mutex);
    pthread_cond_destroy(&shell->cond);
    pthread_mutex_destroy(&shell->mutex);
    free(shell->output);
    free(shell);
}
